#!/usr/bin/env python3
"""Extract configuration parameters (GUCs) and defaults from a PostgreSQL docker
image and write docs/data/postgres/postgresql-<version>.json (same shape as the
other engines, so index/history building is shared).

    extract_postgres.py postgresql 17.6

Defaults are pg_settings.boot_val (the compiled-in default), so they are not
affected by initdb or the container environment.
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

DATA = Path(__file__).resolve().parent.parent / "docs" / "data" / "postgres"
IMAGE = "postgres:{v}"
PLATFORM = []

DERIVED = {"server_version", "server_version_num"}
# initdb writes these into postgresql.conf, so the effective default of a new cluster differs from the
# compiled-in boot_val (shared_buffers: 8MB boot_val up to 14, but 128MB from initdb, as the docs say).
INITDB_SET = {"shared_buffers"}

QUERY = """SELECT json_agg(t ORDER BY name) FROM (
  SELECT name, setting, source, unit, category, short_desc, extra_desc, context, vartype, min_val, max_val, enumvals, boot_val, reset_val
  FROM pg_settings) t"""


def start(image):
    name = f"vdiff-{uuid.uuid4().hex[:8]}"
    sh("docker", "run", "-d", *PLATFORM, "--hostname", HOSTNAME, "--name", name,
       "-e", "POSTGRES_HOST_AUTH_METHOD=trust", image)
    for _ in range(150):
        time.sleep(2)
        r = subprocess.run(["docker", "exec", name, "pg_isready", "-U", "postgres"], capture_output=True)
        logs = subprocess.run(["docker", "logs", name], capture_output=True, text=True)
        if r.returncode == 0 and "init process complete" in logs.stdout + logs.stderr:
            time.sleep(1)
            return name
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    raise RuntimeError("server did not start")


def live_query(image):
    name = start(image)
    try:
        out = sh("docker", "exec", name, "psql", "-U", "postgres", "-At", "-c", QUERY)
        version = sh("docker", "exec", name, "psql", "-U", "postgres", "-At", "-c", "SHOW server_version").strip()
        return json.loads(out), version
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
    rows, full_version = live_query(image)

    variables = {}
    for r in rows:
        n = r["name"]
        boot, unit = r["boot_val"], r["unit"]
        static = boot is not None
        value = r["reset_val"] if n in INITDB_SET and r["source"] == "configuration file" else (boot if static else r["reset_val"])
        if value is not None and unit:
            value = f"{value} {unit}"
        desc = " ".join(x for x in (r["short_desc"], r["extra_desc"]) if x)
        entry = {
            "default": value if value is not None else "",
            "default_source": "derived" if n in DERIVED else ("help" if static else "runtime"),
            "scope": r["context"],
            "cmdline_option": False,
            "desc": desc,
            "category": r["category"],
            "type": r["vartype"],
        }
        if r["min_val"] is not None or r["max_val"] is not None:
            entry["min"], entry["max"] = r["min_val"], r["max_val"]
        if r["enumvals"]:
            entry["enum"] = r["enumvals"]
        if n != n.lower():
            entry["orig"] = n  # keep the documented spelling, e.g. TimeZone
        variables[n.lower()] = entry

    out = {
        "vendor": vendor,
        "version": version,
        "server_version": full_version,
        "series": version.split(".")[0],
        "image": image,
        "variables": variables,
        "status": [],
    }
    DATA.mkdir(parents=True, exist_ok=True)
    path = DATA / f"{vendor}-{version}.json"
    path.write_text(json.dumps(out, indent=1, sort_keys=True))
    print(f"wrote {path} ({len(variables)} settings)")


if __name__ == "__main__":
    main()
