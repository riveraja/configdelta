#!/usr/bin/env python3
"""Extract system variables, defaults and status variables from a MariaDB
docker image and write docs/data/mariadb/mariadb-<version>.json (same shape as
the MySQL extractor, so index/history building is shared).

    extract_mariadb.py mariadb 11.8.9
"""
import json
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from extract import HOSTNAME, scrub, sh  # noqa: E402

DATA = Path(__file__).resolve().parent.parent / "docs" / "data" / "mariadb"
IMAGE = "mariadb:{v}"
PLATFORM = []

# Defaults that change on every release without being a deliberate change.
DERIVED = {"version", "version_comment", "version_compile_machine", "version_compile_os",
           "version_malloc_library", "version_source_revision", "protocol_version",
           "innodb_buffer_pool_size_max"}
DERIVED_RE = re.compile(r"^performance_schema_max_.*_classes$|^performance_schema_error_size$")


DEPRECATED_RE = re.compile(r"\bdeprecated\b|will be removed", re.I)


def replacement_of(text):
    """Name of the replacing variable from "Use --x-y instead" / "Alias for @@x"."""
    m = re.search(r"\bUse\s+(?:--)?(\S+?)\s+instead", text, re.I) or re.search(r"Alias for\s+@@?(\w+)", text, re.I)
    return m.group(1).replace("-", "_").strip("'`\".,") if m else None


def start(image):
    name = f"vdiff-{uuid.uuid4().hex[:8]}"
    sh("docker", "run", "-d", *PLATFORM, "--hostname", HOSTNAME, "--name", name,
       "-e", "MARIADB_ALLOW_EMPTY_ROOT_PASSWORD=1", image)
    for _ in range(300):
        time.sleep(2)
        r = subprocess.run(["docker", "exec", name, "mariadb-admin", "-uroot", "ping", "--silent"], capture_output=True)
        logs = subprocess.run(["docker", "logs", name], capture_output=True, text=True)
        if r.returncode == 0 and "init process done" in logs.stdout + logs.stderr:
            time.sleep(3)
            return name
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    raise RuntimeError("server did not start")


def live_queries(image):
    name = start(image)
    try:
        def q(sql):
            out = sh("docker", "exec", name, "mariadb", "-uroot", "-N", "-B", "-e", sql)
            return [l.split("\t") for l in out.splitlines() if l]

        rows = q("SELECT VARIABLE_NAME, GLOBAL_VALUE, DEFAULT_VALUE, VARIABLE_SCOPE, NUMERIC_MIN_VALUE, NUMERIC_MAX_VALUE, "
                 "COMMAND_LINE_ARGUMENT, REPLACE(REPLACE(VARIABLE_COMMENT, CHAR(10), ' '), CHAR(9), ' ') "
                 "FROM information_schema.SYSTEM_VARIABLES")
        status = [r[0] for r in q("SHOW GLOBAL STATUS")]
        version = q("SELECT VERSION()")[0][0]

        # Reading a deprecated system variable raises warning 1287 naming the replacement.
        names = sorted(r[0] for r in rows if r[3] in ("GLOBAL", "SESSION", "ONLY_SESSION") and r[3] != "ONLY_SESSION")
        script = "".join(f"SELECT 'VAR:{n}';\nSELECT @@global.`{n}`;\nSHOW WARNINGS;\n" for n in names)
        out = sh("docker", "exec", "-i", name, "mariadb", "-uroot", "-N", "-B", "--force", check=False, input=script)
        deprecated, cur = {}, None
        for line in out.splitlines():
            m = re.match(r"^VAR:(\S+)$", line)
            if m:
                cur = m.group(1)
                continue
            m = re.match(r"^Warning\t(?:1287|1681)\t(.*)$", line)
            if m and cur and "deprecated" in m.group(1):
                deprecated[cur] = m.group(1)
        return rows, status, version, deprecated
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
    print(f"[{vendor} {version}] live server", flush=True)
    rows, status, full_version, deprecated = live_queries(image)

    variables = {}
    for name_u, glob, default, scope, mn, mx, cmd, comment in (r + [""] * (8 - len(r)) for r in rows):
        n = name_u.lower()
        static = default != "NULL"
        entry = {
            "default": scrub(default if static else glob),
            "default_source": "derived" if n in DERIVED or DERIVED_RE.match(n) else ("help" if static else "runtime"),
            "scope": "both" if scope == "SESSION" else ("global" if scope == "GLOBAL" else None),
            "cmdline_option": cmd != "NULL",
            "desc": comment,
        }
        if n in deprecated:
            entry["deprecated"] = deprecated[n]
        elif DEPRECATED_RE.search(comment):
            entry["deprecated"] = comment  # MariaDB raises no warning on read, only the description says so
        if "deprecated" in entry:
            rep = replacement_of(entry["deprecated"])
            if rep:
                entry["replacement"] = rep
        if mn != "NULL" or mx != "NULL":
            entry["min"], entry["max"] = mn, mx
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
