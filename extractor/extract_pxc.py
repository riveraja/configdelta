#!/usr/bin/env python3
"""Extract the Percona XtraDB Cluster (Galera) variables and status counters from a
percona/percona-xtradb-cluster docker image and write
docs/data/pxc/percona-<version>.json (same shape as the other engines).

    extract_pxc.py percona 8.0.40-31.1

The image is bootstrapped as a one-node cluster. Only wsrep_* / pxc_* system variables and
wsrep_* status variables are kept; everything else is covered by the mysql engine.
wsrep_provider_options is one long string of Galera settings, so it is split into one
variable per setting ("wsrep_provider_options.gcache.size") to make changes diffable.
"""
import json
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from extract import HOSTNAME, parse_help, scrub, sh  # noqa: E402

DATA = Path(__file__).resolve().parent.parent / "docs" / "data" / "pxc"
IMAGE = "percona/percona-xtradb-cluster:{v}"
PLATFORM = []
KEEP_RE = re.compile(r"^(wsrep_|pxc_)", re.I)
PROVIDER = "wsrep_provider_options"
# Values that depend on the container's address or the cluster that was just created.
DERIVED = {"wsrep_node_address", "wsrep_cluster_address", "wsrep_node_incoming_address", "wsrep_start_position", "wsrep_cluster_name",
           f"{PROVIDER}.base_host", f"{PROVIDER}.ist.recv_addr"}


def help_values(text):
    """{lowercase name: default} from the values table of `mysqld --help --verbose`."""
    values, started = {}, False
    for line in text.partition("Variables (--variable-name=value)")[2].splitlines():
        if line.startswith("----"):
            started = True
        elif started:
            m = re.match(r"^(\S+)\s+(.*)$", line.rstrip())
            if m and re.fullmatch(r"[a-z0-9_]+", m.group(1).replace("-", "_").lower()):
                values[m.group(1).replace("-", "_").lower()] = m.group(2).strip()
    return values


def provider_settings(value):
    out = {}
    for part in value.split(";"):
        k, sep, v = part.partition("=")
        if sep and k.strip():
            out[k.strip()] = v.strip()
    return out


def live_queries(image):
    name = f"vdiff-{uuid.uuid4().hex[:8]}"
    sh("docker", "run", "-d", *PLATFORM, "--hostname", HOSTNAME, "--name", name,
       "-e", "MYSQL_ALLOW_EMPTY_PASSWORD=1", "-e", "CLUSTER_NAME=vdiff", image)
    try:
        for _ in range(300):
            time.sleep(2)
            r = subprocess.run(["docker", "exec", name, "mysql", "-uroot", "-N", "-B", "-e",
                                "SHOW GLOBAL STATUS LIKE 'wsrep_local_state_comment'"], capture_output=True, text=True)
            if r.returncode == 0 and r.stdout.strip().endswith("Synced"):
                break
        else:
            raise RuntimeError("node did not reach Synced")
        time.sleep(2)

        def q(sql):
            out = sh("docker", "exec", name, "mysql", "-uroot", "-N", "-B", "-e", sql)
            return [l.split("\t") for l in out.splitlines() if l]

        like = "(VARIABLE_NAME LIKE 'wsrep\\_%' OR VARIABLE_NAME LIKE 'pxc\\_%')"
        glob = {n: v for n, v in q(f"SELECT VARIABLE_NAME, VARIABLE_VALUE FROM performance_schema.global_variables WHERE {like}")}
        status = [r[0] for r in q("SELECT VARIABLE_NAME FROM performance_schema.global_status WHERE VARIABLE_NAME LIKE 'wsrep\\_%'")]
        version = q("SELECT VERSION()")[0][0]

        script = "".join(f"SELECT 'VAR:{n}';\nSELECT @@global.`{n}`;\nSHOW WARNINGS;\n" for n in sorted(glob))
        out = sh("docker", "exec", "-i", name, "mysql", "-uroot", "-N", "-B", "--force", check=False, input=script)
        deprecated, cur = {}, None
        for line in out.splitlines():
            m = re.match(r"^VAR:(\S+)$", line)
            if m:
                cur = m.group(1)
                continue
            m = re.match(r"^Warning\t(?:1287|1681)\t(.*)$", line)
            if m and cur and "deprecated" in m.group(1):
                deprecated[cur] = m.group(1)
        return glob, status, version, deprecated
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def main():
    vendor, version = sys.argv[1], sys.argv[2]
    image = IMAGE.format(v=version)
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
    print(f"[{vendor} {version}] --help --verbose", flush=True)
    help_text = sh("docker", "run", "--rm", *PLATFORM, "--hostname", HOSTNAME, "--entrypoint", "mysqld", image,
                   "--no-defaults", "--help", "--verbose", check=False)
    opts = {k.lower(): v for k, v in parse_help(help_text)[0].items()}
    values = help_values(help_text)
    print(f"[{vendor} {version}] live cluster node", flush=True)
    glob, status, full_version, deprecated = live_queries(image)
    glob = {n: v for n, v in glob.items() if KEEP_RE.match(n)}
    if not glob:
        raise RuntimeError("image exposes no wsrep_/pxc_ variables")

    variables = {}
    for n in sorted(glob):
        key, o = n.lower(), opts.get(n.lower(), {})
        if n == PROVIDER:
            for k, v in provider_settings(glob[n]).items():
                variables[f"{PROVIDER}.{k}".lower()] = {
                    "default": scrub(v),
                    "default_source": "derived" if f"{PROVIDER}.{k}" in DERIVED else "help",  # not a start-up setting, so not "runtime"
                    "scope": "provider option",
                    "cmdline_option": False,
                    "desc": "",
                    **({"orig": f"{PROVIDER}.{k}"} if k != k.lower() else {}),
                }
            continue
        entry = {
            "default": scrub(values.get(key, glob[n])),
            "default_source": "derived" if key in DERIVED else "help",  # empty defaults are absent from the --help table
            "scope": "global",
            "cmdline_option": key in opts,
            "desc": o.get("desc", ""),
        }
        if n != key:
            entry["orig"] = n
        if n in deprecated:
            entry["deprecated"] = deprecated[n]
        elif re.search(r"\bdeprecated\b", entry["desc"], re.I):
            entry["deprecated"] = entry["desc"]
        if "deprecated" in entry:
            r = re.search(r"(?:use|using)\s+(\S+?)\s+instead", entry["deprecated"], re.I)
            if r:
                entry["replacement"] = r.group(1).replace("@@", "").strip("'`\".,")
        variables[key] = entry

    out = {
        "vendor": vendor,
        "version": version,
        "server_version": full_version,
        "series": ".".join(version.split(".")[:2]),
        "image": image,
        "variables": variables,
        "status": sorted(status),
    }
    DATA.mkdir(parents=True, exist_ok=True)
    path = DATA / f"{vendor}-{version}.json"
    path.write_text(json.dumps(out, indent=1, sort_keys=True))
    print(f"wrote {path} ({len(variables)} variables, {len(status)} status)")


if __name__ == "__main__":
    main()
