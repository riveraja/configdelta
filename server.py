#!/usr/bin/env python3
"""Serve docs/ and expose an update API.

    python3 server.py [port]        (default 8765)

POST /api/update?engine=mysql   start a check for new releases (no-op if one is running)
GET  /api/status                {state, engine, message, found, done, failed}

New releases are discovered on Docker Hub: for every series already tracked
(e.g. 8.0, 8.4, 9.7) any tag newer than the newest one on disk is extracted
with the engine's extractor, then the index/history are rebuilt. Engines with
track_new_series also pick up series newer than any tracked one.

Ctrl-C / SIGTERM stop the server gracefully: no new requests, the running extraction is
interrupted (so its container and image are removed), releases already extracted are
still recorded and indexed, then the process exits. A second signal exits immediately.
"""
import json
import re
import signal
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
WEB, EXT = ROOT / "docs", ROOT / "extractor"
ENGINES = {
    "mysql": {
        "script": "extract.py",
        "series": lambda t: ".".join(t.split(".")[:2]),
        "repos": {
            "oracle": ("library/mysql", re.compile(r"^\d+\.\d+\.\d+$")),
            "percona": ("percona/percona-server", re.compile(r"^\d+\.\d+\.\d+-\d+$")),
        },
    },
    "mariadb": {
        "script": "extract_mariadb.py",
        "series": lambda t: ".".join(t.split(".")[:2]),
        "track_new_series": True,  # rolling releases: 12.x, 13.0, ... appear regularly
        "repos": {"mariadb": ("library/mariadb", re.compile(r"^\d+\.\d+\.\d+$"))},
    },
    "rocksdb": {  # MyRocks variables from the same Percona Server images as the mysql engine
        "script": "extract_rocksdb.py",
        "series": lambda t: ".".join(t.split(".")[:2]),
        "repos": {"percona": ("percona/percona-server", re.compile(r"^\d+\.\d+\.\d+-\d+$"))},
    },
    "mongodb": {
        "script": "extract_mongodb.py",
        "series": lambda t: ".".join(t.split(".")[:2]),
        "track_new_series": True,
        "repos": {
            "mongodb": ("library/mongo", re.compile(r"^\d+\.\d+\.\d+$")),
            "percona": ("percona/percona-server-mongodb", re.compile(r"^\d+\.\d+\.\d+-\d+$")),
        },
    },
    "pxc": {  # wsrep_*/pxc_* only; the rest of MySQL is the mysql engine
        "script": "extract_pxc.py",
        "series": lambda t: ".".join(t.split(".")[:2]),
        "track_new_series": True,
        "repos": {"percona": ("percona/percona-xtradb-cluster", re.compile(r"^\d+\.\d+\.\d+-\d+\.\d+$"))},
    },
    "postgres": {
        "script": "extract_postgres.py",
        "series": lambda t: t.split(".")[0],  # the major version is the release series
        "track_new_series": True,
        "repos": {"postgresql": ("library/postgres", re.compile(r"^\d+\.\d+$"))},
    },
}

lock = threading.Lock()
status = {"state": "idle", "engine": "", "message": "", "found": [], "done": [], "failed": []}


def vkey(v):
    return [int(x) for x in re.split(r"[.-]", v)]


def tags_for(repo, prefix, pattern):
    """Tags matching pattern whose name starts with prefix (e.g. "8.4." or "" for all)."""
    url = f"https://hub.docker.com/v2/repositories/{repo}/tags?page_size=100&ordering=last_updated" + (f"&name={prefix}" if prefix else "")
    found = set()
    for page_no in range(10 if not prefix else 5):  # Hub refuses ordered pages beyond the 10th
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                page = json.load(r)
        except urllib.error.HTTPError:
            if page_no == 0:
                raise
            break
        found |= {t["name"] for t in page["results"] if pattern.match(t["name"]) and t["name"].startswith(prefix)}
        url = page.get("next")
        if not url:
            break
    return found


def find_new(engine):
    cfg = ENGINES[engine]
    index = json.loads((data_dir(engine) / "index.json").read_text())
    new = []
    for vendor, (repo, pattern) in cfg["repos"].items():
        have = [e["version"] for e in index if e["vendor"] == vendor]
        tracked = sorted({e["series"] for e in index if e["vendor"] == vendor}, key=vkey)
        if cfg.get("track_new_series"):
            top = vkey(tracked[-1]) if tracked else []
            cands = {t for t in tags_for(repo, "", pattern) if vkey(cfg["series"](t)) > top}
            new += [(vendor, t) for t in cands if t not in have]
        for series in tracked:
            # Anything on Hub after the first tracked release of the series that we do not have, so a release
            # that failed earlier, or was skipped between two others, is retried. Older pre-GA tags stay excluded.
            first = min((vkey(v) for v in have if v.startswith(series + ".")), default=[])
            for tag in tags_for(repo, series + ".", pattern):
                if vkey(tag) > first and tag not in have:
                    new.append((vendor, tag))
    return sorted(set(new), key=lambda x: (x[0], vkey(x[1])))


def data_dir(engine):
    return WEB / "data" / engine


def record_versions(engine, new):
    path = EXT / "versions" / f"{engine}.json"
    versions = json.loads(path.read_text())
    for vendor, ver in new:
        if ver not in versions.setdefault(vendor, []):
            versions[vendor].append(ver)
    for v in versions.values():
        v.sort(key=vkey)
    path.write_text(json.dumps(versions, indent=1))


stopping = threading.Event()
proc_lock = threading.Lock()
current = [None]  # the extractor subprocess that is running, if any
worker = [None]


def run_child(cmd):
    """Run an extractor; None if the server began stopping before it could start."""
    with proc_lock:
        if stopping.is_set():
            return None
        # own session: a terminal Ctrl-C must not reach it, we forward exactly one SIGINT ourselves
        current[0] = proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    out, err = proc.communicate()
    with proc_lock:
        current[0] = None
    return proc.returncode, out, err


def job(engine):
    try:
        status.update(state="checking", engine=engine, message="Checking Docker Hub for new releases…", found=[], done=[], failed=[])
        new = find_new(engine)
        if not new or stopping.is_set():
            status.update(state="uptodate", message="No new releases found.")
            return
        status.update(state="extracting", found=[f"{v} {t}" for v, t in new])
        for vendor, ver in new:
            status["message"] = f"Extracting {vendor} {ver} ({len(status['done']) + len(status['failed']) + 1}/{len(new)})…"
            res = run_child([sys.executable, str(EXT / ENGINES[engine]["script"]), vendor, ver])
            if res is None or (stopping.is_set() and res[0] != 0):
                break  # interrupted by shutdown: neither done nor failed
            code, out, err = res
            (status["done"] if code == 0 else status["failed"]).append(f"{vendor} {ver}")
            if code != 0:
                (EXT / "failures").mkdir(exist_ok=True)
                (EXT / "failures" / f"{vendor}-{ver}.log").write_text(out + err)
        ok = [tuple(s.split()) for s in status["done"]]
        if ok:
            status["message"] = "Rebuilding index and documentation links…"
            record_versions(engine, ok)
            subprocess.run([sys.executable, str(EXT / "build_index.py"), engine], check=True, capture_output=True)
            subprocess.run([sys.executable, str(EXT / "build_doclinks.py"), engine], capture_output=True)  # best effort
        note = " Stopped early: server is shutting down." if stopping.is_set() else ""
        status.update(state="done", message=f"Added {len(ok)} release(s)." + (f" {len(status['failed'])} failed." if status["failed"] else "") + note)
    except Exception as e:  # network, docker, etc.
        status.update(state="error", message=f"Update failed: {e}")
    finally:
        lock.release()


class Handler(SimpleHTTPRequestHandler):
    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/status":
            return self.send_json(status)
        super().do_GET()

    def do_POST(self):
        url = urlparse(self.path)
        engine = parse_qs(url.query).get("engine", ["mysql"])[0]
        if url.path != "/api/update" or engine not in ENGINES:
            return self.send_error(404)
        if stopping.is_set():
            return self.send_error(503, "server is shutting down")
        if lock.acquire(blocking=False):
            worker[0] = threading.Thread(target=job, args=(engine,), daemon=True)
            worker[0].start()
        self.send_json(status, 202)

    def end_headers(self):
        if not self.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-cache")  # always revalidate so edits show up
        super().end_headers()


def serve(port):
    httpd = ThreadingHTTPServer(("127.0.0.1", port), partial(Handler, directory=str(WEB)))

    def on_signal(signum, _frame):
        if stopping.is_set():  # second signal: stop waiting
            signal.signal(signum, signal.SIG_DFL)
            raise KeyboardInterrupt
        print(f"\n{signal.Signals(signum).name} received: shutting down (repeat to force)…", flush=True)
        stopping.set()
        with proc_lock:
            if current[0]:
                current[0].send_signal(signal.SIGINT)  # lets the extractor remove its container and image
        threading.Thread(target=httpd.shutdown, daemon=True).start()  # must not run on the serving thread

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)
    print(f"http://localhost:{port}  (Ctrl-C to stop)", flush=True)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
        if worker[0] and worker[0].is_alive():
            print("Waiting for the running update to stop…", flush=True)
            worker[0].join(timeout=120)
    print("Stopped.", flush=True)


if __name__ == "__main__":
    try:
        serve(int(sys.argv[1]) if len(sys.argv) > 1 else 8765)
    except KeyboardInterrupt:
        sys.exit(130)
