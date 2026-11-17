#!/usr/bin/env python3
"""Extract every version listed in versions/<engine>.json (skipping ones already
on disk), then rebuild the index/history.

    run_all.py <engine> [workers]        e.g.  run_all.py mariadb 3
"""
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

here = Path(__file__).resolve().parent
SCRIPTS = {"mysql": "extract.py", "mariadb": "extract_mariadb.py", "postgres": "extract_postgres.py", "rocksdb": "extract_rocksdb.py", "mongodb": "extract_mongodb.py", "pxc": "extract_pxc.py"}
engine = sys.argv[1]
workers = int(sys.argv[2]) if len(sys.argv) > 2 else 3
data = here.parent / "docs" / "data" / engine
versions = json.loads((here / "versions" / f"{engine}.json").read_text())
jobs = [(v, ver) for v, vs in versions.items() for ver in vs if not (data / f"{v}-{ver}.json").exists()]
print(f"{len(jobs)} to extract", flush=True)


def run(job):
    vendor, ver = job
    r = subprocess.run([sys.executable, str(here / SCRIPTS[engine]), vendor, ver], capture_output=True, text=True)
    ok = r.returncode == 0
    print(("ok   " if ok else "FAIL ") + f"{vendor} {ver}", flush=True)
    if not ok:
        (here / "failures").mkdir(exist_ok=True)
        (here / "failures" / f"{vendor}-{ver}.log").write_text(r.stdout + r.stderr)
    return ok


with ThreadPoolExecutor(workers) as ex:
    results = list(ex.map(run, jobs))
print(f"{sum(results)}/{len(jobs)} succeeded")
subprocess.run([sys.executable, str(here / "build_index.py"), engine])
subprocess.run([sys.executable, str(here / "build_doclinks.py"), engine])
