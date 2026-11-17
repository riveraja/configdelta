#!/usr/bin/env python3
"""Build docs/data/<engine>/doclinks.json: exact documentation anchors for every
variable / status name we can verify, so the viewer never has to fall back to a
site search.

    build_doclinks.py [engine ...]        (default: every engine that has a source below)

Format: {vendor: {series (or "*" for an unversioned manual): {"base": url, "variables": {name: "page#anchor"}, "status": {...}}}}
A value is relative to "base", or an absolute URL, or [url, label] when the page belongs to
another vendor's documentation (Percona Server for MongoDB re-uses the upstream parameter docs).
Names without an anchor simply get no link.

Sources
  postgres   postgresql.org runtime-config-*.html, anchors GUC-<NAME>
  mongodb    mongodb.com reference/parameters and reference/command/serverStatus anchors;
             Percona Server for MongoDB falls back to the upstream page for shared names
  mysql, rocksdb (Percona)   mkdocs search index of docs.percona.com/percona-server/<series>
  pxc        mkdocs search index of docs.percona.com/percona-xtradb-cluster/<series>, wsrep_/pxc_ names and provider options
  mariadb    mariadb.com system/status variable pages
Oracle MySQL is not covered: dev.mysql.com refuses scripted requests, so those links cannot be verified.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "docs" / "data"
_cache = {}


def fetch(url):
    if url not in _cache:
        r = subprocess.run(["curl", "-s", "-L", "--max-time", "60", "-w", "\n%{http_code}", url], capture_output=True, text=True)
        body, _, code = r.stdout.rpartition("\n")
        _cache[url] = body if code == "200" else None
    return _cache[url]


def series_of(engine):
    index = json.loads((WEB / engine / "index.json").read_text())
    out = {}
    for e in index:
        out.setdefault(e["vendor"], set()).add(e["series"])
    return out


def postgres(series):
    base = f"https://www.postgresql.org/docs/{series}/"
    toc = fetch(base + "runtime-config.html") or ""
    variables = {}
    for page in sorted(set(re.findall(r'href="(runtime-config-[a-z\-]+\.html)"', toc))):
        for anchor in re.findall(r'id="(GUC-[A-Z0-9\-]+)"', fetch(base + page) or ""):
            variables.setdefault(anchor[4:].lower().replace("-", "_"), f"{page}#{anchor}")
    return {"base": base, "variables": variables}


def mongodb(series):
    base = f"https://www.mongodb.com/docs/v{series}/reference/"
    out = {"base": base, "variables": {}, "status": {}}
    for kind, page, prefix in (("variables", "parameters/", "mongodb-parameter-param."),
                               ("status", "command/serverStatus/", "mongodb-serverstatus-serverstatus.")):
        for name in re.findall(r'id="' + re.escape(prefix) + r'([^"]+)"', fetch(base + page) or ""):
            out[kind].setdefault(name.lower(), f"{page}#{prefix}{name}")
    return out


def percona_index(product, series, kinds_pages):
    base = f"https://docs.percona.com/{product}/{series}/"
    raw = fetch(base + "search/search_index.json")
    out = {"base": base, "variables": {}, "status": {}}
    if not raw:
        return out
    for d in json.loads(raw)["docs"]:
        title = re.sub(r"<[^>]+>", "", d["title"]).strip().lower()
        loc = d["location"]
        if "#" not in loc or not re.fullmatch(r"[a-z0-9_]+", title):
            continue
        kind = "status" if "status" in loc.split("#")[0] else "variables"
        out[kind].setdefault(title, loc)
    return out


def pxc(series):
    """wsrep_/pxc_ names from the system/status index pages, plus Galera provider options from wsrep-provider-index."""
    entry = percona_index("percona-xtradb-cluster", series, None)
    for kind in ("variables", "status"):
        entry[kind] = {n: v for n, v in entry[kind].items() if n.startswith(("wsrep_", "pxc_"))}
    for d in json.loads(fetch(entry["base"] + "search/search_index.json") or '{"docs": []}')["docs"]:
        title = re.sub(r"<[^>]+>", "", d["title"]).strip().lower()
        if d["location"].startswith("wsrep-provider-index.html#") and re.fullmatch(r"[a-z_]+\.[a-z_.]+", title):
            entry["variables"].setdefault(f"wsrep_provider_options.{title}", d["location"])
    return entry


def mariadb(_series):
    base = "https://mariadb.com"
    root = "/docs/server/server-management/variables-and-modes/server-system-variables"
    pages = {root, root.replace("system", "status")}
    for p in re.findall(r'href="(/docs/server/[^"#]*(?:system|status)-variables)"', fetch(base + root) or ""):
        pages.add(p)
    out = {"base": base, "variables": {}, "status": {}}
    for page in sorted(pages):
        kind = "status" if "status-variables" in page else "variables"
        for name in re.findall(r'id="([A-Za-z0-9_]+)"', fetch(base + page) or ""):
            out[kind].setdefault(name.lower(), f"{page}#{name}")
    return out


def build(engine):
    per_vendor = series_of(engine)
    result = {}
    for vendor, series_set in per_vendor.items():
        for series in sorted(series_set):
            if engine == "postgres":
                entry = postgres(series)
            elif engine == "mongodb" and vendor == "mongodb":
                entry = mongodb(series)
            elif engine == "mongodb" and vendor == "percona":
                entry = {"base": f"https://docs.percona.com/percona-server-for-mongodb/{series}/", "variables": {}, "status": {}}
                upstream = mongodb(series)
                for kind in ("variables", "status"):
                    for n, rel in upstream[kind].items():
                        entry[kind][n] = [upstream["base"] + rel, "MongoDB"]
            elif engine in ("mysql", "rocksdb") and vendor == "percona":
                entry = percona_index("percona-server", series, None)
            elif engine == "mariadb":
                entry = mariadb(series)
            elif engine == "pxc":
                entry = pxc(series)
            else:
                continue
            if engine == "rocksdb":  # only MyRocks names belong here
                for kind in ("variables", "status"):
                    entry[kind] = {n: v for n, v in entry[kind].items() if n.startswith("rocksdb_")}
            if engine == "mariadb":  # one unversioned manual: store once under "*"
                result.setdefault(vendor, {})["*"] = entry
                print(f"{engine} {vendor}: {len(entry['variables'])} variables, {len(entry['status'])} status", flush=True)
                break
            result.setdefault(vendor, {})[series] = entry
            print(f"{engine} {vendor} {series}: {len(entry['variables'])} variables, {len(entry.get('status', {}))} status", flush=True)
    (WEB / engine / "doclinks.json").write_text(json.dumps(result, separators=(",", ":")))


if __name__ == "__main__":
    for e in sys.argv[1:] or ["mysql", "mariadb", "postgres", "rocksdb", "mongodb", "pxc"]:
        build(e)
