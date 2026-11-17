#!/usr/bin/env python3
"""Extract server parameters (getParameter "*") and serverStatus metric names from a
MongoDB / Percona Server for MongoDB docker image and write
docs/data/mongodb/<vendor>-<version>.json (same shape as the other engines).

    extract_mongodb.py mongodb 8.0.34
    extract_mongodb.py percona 8.0.34-15

The server runs as a single-node replica set so the replication sections of
serverStatus exist. "variables" are server parameters with their value on a
fresh instance; "status" are the flattened serverStatus metric names.
"""
import json
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from extract import HOSTNAME, sh  # noqa: E402

DATA = Path(__file__).resolve().parent.parent / "docs" / "data" / "mongodb"
IMAGES = {"mongodb": "mongo:{v}", "percona": "percona/percona-server-mongodb:{v}"}
PLATFORM = []

SKIP = {"ok", "$clusterTime", "operationTime", "$configTime", "$topologyTime"}
# Values that follow the feature compatibility version or the build rather than a deliberate default.
# Names created by whichever client or command happened to run (e.g. the mongosh version that connected).
# locks.<resource>.<counter>.<mode> only exist for the lock modes that were taken since startup.
TRANSIENT_RE = re.compile(r"^metrics\.apiVersions\.|<UNKNOWN>|^locks\.[^.]+\.[^.]+\.[A-Za-z]$")
# logComponentVerbosity is a tree of every log component, tcmallocMaxPerCpuCacheSize follows the machine's memory.
DERIVED_RE = re.compile(r"^featureFlag|^featureCompatibilityVersion$|^logComponentVerbosity$|^tcmallocMaxPerCpuCacheSize$", re.I)

SCRIPT = """
const gp = db.adminCommand({getParameter: '*'});
const ss = db.adminCommand({serverStatus: 1});
const bi = db.adminCommand({buildInfo: 1});
print('@@' + EJSON.stringify({params: gp, status: ss, version: bi.version}, {relaxed: true}));
"""


def mongosh(name, js):
    return sh("docker", "exec", name, "mongosh", "--quiet", "--eval", js)


def start(image):
    name = f"vdiff-{uuid.uuid4().hex[:8]}"
    # mongod 8.0.21-8.3.8 refuses to start on Linux 6.19+ (SERVER-121912) unless glibc's rseq is left to the loader.
    sh("docker", "run", "-d", *PLATFORM, "--hostname", HOSTNAME, "--name", name, "-e", "GLIBC_TUNABLES=glibc.pthread.rseq=1", image,
       "--replSet", "rs0", "--bind_ip_all")
    try:
        for _ in range(60):
            time.sleep(2)
            r = subprocess.run(["docker", "exec", name, "mongosh", "--quiet", "--eval", "db.hello().ok"],
                               capture_output=True, text=True)
            if r.returncode == 0 and r.stdout.strip().endswith("1"):
                break
        else:
            raise RuntimeError("server did not start")
        mongosh(name, "try { rs.initiate() } catch (e) {}")
        for _ in range(60):
            time.sleep(1)
            r = subprocess.run(["docker", "exec", name, "mongosh", "--quiet", "--eval", "db.hello().isWritablePrimary"],
                               capture_output=True, text=True)
            if r.stdout.strip().endswith("true"):
                return name
        raise RuntimeError("no primary elected")
    except Exception:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        raise


def live_query(image):
    name = start(image)
    try:
        out = mongosh(name, SCRIPT)
        line = next(l for l in out.splitlines() if l.startswith("@@"))
        return json.loads(line[2:])
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def unwrap(v):
    """EJSON wrappers such as {"$numberLong": "5"} or {"$date": ...} become plain values."""
    while isinstance(v, dict) and len(v) == 1 and next(iter(v)).startswith("$"):
        v = next(iter(v.values()))
    return v


def flatten(prefix, v, out):
    v = unwrap(v)
    if isinstance(v, dict) and v:
        for k in sorted(v):
            if prefix or k not in SKIP:
                flatten(f"{prefix}.{k}" if prefix else k, v[k], out)
    elif not TRANSIENT_RE.search(prefix):
        out.add(prefix)


def fmt(v):
    v = unwrap(v)
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, dict) and "value" in v:  # feature flags: {value, version, ...}
        return fmt(v["value"])
    if isinstance(v, (dict, list)):
        return json.dumps(v, sort_keys=True, separators=(",", ":"))
    return "" if v is None else str(v)


def main():
    vendor, version = sys.argv[1], sys.argv[2]
    image = IMAGES[vendor].format(v=version)
    preexisting = subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode == 0
    try:
        extract(vendor, version, image)
    finally:
        if not preexisting:
            subprocess.run(["docker", "rmi", "-f", image], capture_output=True)


def extract(vendor, version, image):
    global PLATFORM
    if subprocess.run(["docker", "pull", "-q", image], capture_output=True).returncode != 0:
        sh("docker", "pull", "-q", "--platform", "linux/amd64", image)
        PLATFORM = ["--platform", "linux/amd64"]
    print(f"[{vendor} {version}] live server", flush=True)
    live = live_query(image)

    variables = {}
    for n, v in live["params"].items():
        if n in SKIP:
            continue
        entry = {
            "default": fmt(v),
            "default_source": "derived" if DERIVED_RE.search(n) or fmt(v) == "###" else "help",  # ### = redacted by the server
            "scope": "",
            "cmdline_option": False,
            "desc": "",
        }
        if n != n.lower():
            entry["orig"] = n  # keep the documented camelCase spelling; keys are lowercase for search
        variables[n.lower()] = entry

    names = set()
    for k, v in live["status"].items():
        if k not in SKIP:
            flatten(k, v, names)

    out = {
        "vendor": vendor,
        "version": version,
        "server_version": live["version"],
        "series": ".".join(version.split(".")[:2]),
        "image": image,
        "variables": variables,
        "status": sorted(names),
    }
    DATA.mkdir(parents=True, exist_ok=True)
    path = DATA / f"{vendor}-{version}.json"
    path.write_text(json.dumps(out, indent=1, sort_keys=True))
    print(f"wrote {path} ({len(variables)} parameters, {len(names)} status metrics)")


if __name__ == "__main__":
    main()
