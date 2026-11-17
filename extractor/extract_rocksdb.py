#!/usr/bin/env python3
"""Extract the MyRocks (RocksDB) system and status variables from a Percona
Server docker image and write docs/data/rocksdb/percona-<version>.json (same
shape as the other engines, so index/history building is shared).

    extract_rocksdb.py percona 8.4.11-11

The plugin ships in the image but is not loaded by default, so it is installed
into a throwaway server. Only rocksdb_* variables and Rocksdb_* status variables
are kept; everything else is covered by the mysql engine.
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

DATA = Path(__file__).resolve().parent.parent / "docs" / "data" / "rocksdb"
IMAGE = "percona/percona-server:{v}"
PLATFORM = []
PLUGIN = "ha_rocksdb.so"


def live_queries(image):
    name = f"vdiff-{uuid.uuid4().hex[:8]}"
    sh("docker", "run", "-d", *PLATFORM, "--hostname", HOSTNAME, "--name", name,
       "-e", "MYSQL_ALLOW_EMPTY_PASSWORD=1", image)
    try:
        for _ in range(300):
            time.sleep(2)
            r = subprocess.run(["docker", "exec", name, "mysqladmin", "-uroot", "ping", "--silent"], capture_output=True)
            logs = subprocess.run(["docker", "logs", name], capture_output=True, text=True)
            if r.returncode == 0 and "MySQL init process done" in logs.stdout + logs.stderr:
                break
        else:
            raise RuntimeError("server did not start")
        time.sleep(3)

        def q(sql):
            out = sh("docker", "exec", name, "mysql", "-uroot", "-N", "-B", "-e", sql)
            return [l.split("\t") for l in out.splitlines() if l]

        q(f"INSTALL PLUGIN ROCKSDB SONAME '{PLUGIN}'")
        glob = {n: v for n, v in q("SELECT VARIABLE_NAME, VARIABLE_VALUE FROM performance_schema.global_variables "
                                   "WHERE VARIABLE_NAME LIKE 'rocksdb\\_%'")}
        info = {r[0]: r[1:] for r in q("SELECT VARIABLE_NAME, VARIABLE_SOURCE, MIN_VALUE, MAX_VALUE "
                                       "FROM performance_schema.variables_info WHERE VARIABLE_NAME LIKE 'rocksdb\\_%'")}
        status = [r[0] for r in q("SELECT VARIABLE_NAME FROM performance_schema.global_status WHERE VARIABLE_NAME LIKE 'Rocksdb\\_%'")]
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

        help_text = sh("docker", "exec", name, "mysqld", "--no-defaults", f"--plugin-load-add=rocksdb={PLUGIN}",
                       "--help", "--verbose", check=False)
        return glob, info, status, version, deprecated, help_text
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
    print(f"[{vendor} {version}] live server + MyRocks", flush=True)
    glob, info, status, full_version, deprecated, help_text = live_queries(image)
    if not glob:
        raise RuntimeError("MyRocks plugin exposes no variables in this image")
    opts, values = parse_help(help_text)

    variables = {}
    for n in sorted(glob):
        o = opts.get(n, {})
        entry = {
            "default": scrub(values.get(n, glob[n])),
            "default_source": "help" if n in values else "runtime",
            "scope": "global",
            "cmdline_option": n in opts,
            "desc": o.get("desc", ""),
        }
        if n in deprecated:
            entry["deprecated"] = deprecated[n]
        elif re.search(r"\bdeprecated\b", entry["desc"], re.I):
            entry["deprecated"] = entry["desc"]
        if "deprecated" in entry:
            r = re.search(r"(?:use|using)\s+(\S+?)\s+instead", entry["deprecated"], re.I)
            if r:
                entry["replacement"] = r.group(1).replace("@@", "").strip("'`\".,")
        if n in info:
            _, mn, mx = info[n]
            entry["min"] = None if mn in ("0", "") and mx in ("0", "") else mn
            entry["max"] = None if mn in ("0", "") and mx in ("0", "") else mx
        variables[n] = entry

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
