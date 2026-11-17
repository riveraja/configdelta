#!/usr/bin/env python3
"""Query the Config Delta dataset (database server variables, defaults, status counters,
deprecations and removals across versions) without the web UI. Standard library only.

    configdelta.py engines
    configdelta.py versions <engine>
    configdelta.py compare <engine> <baseline> <version> [<version> ...] [--kind variables|status]
                           [--only added,removed,changed,deprecated] [--search TEXT]
                           [--include-env] [--include-options] [--format table|json]
    configdelta.py find    <engine> <text> [--kind variables|status]
    configdelta.py history <engine> <name>
    configdelta.py show    <engine> <version> <name>

A version is "<vendor>:<version>" (e.g. percona:8.4.11-11) or just "<version>" when only one
vendor has it. The first version given to `compare` is the baseline.

Data comes from the published site; override with CONFIGDELTA_URL (a URL or a local `docs/`
directory). Downloads are cached in ~/.cache/configdelta (CONFIGDELTA_CACHE to move it).
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

BASE = os.environ.get("CONFIGDELTA_URL", "https://riveraja.github.io/configdelta/").rstrip("/") + "/"
CACHE = Path(os.environ.get("CONFIGDELTA_CACHE", Path.home() / ".cache" / "configdelta"))
MANIFEST_TTL, VERSION_TTL = 600, 7 * 86400  # index/history/doclinks change on each update; a version file barely ever


class Fail(Exception):
    pass


def fetch(path, ttl):
    if not BASE.startswith(("http://", "https://")):
        try:
            return json.loads((Path(BASE) / path).read_text())
        except OSError as e:
            raise Fail(f"cannot read {Path(BASE) / path}: {e}")
    cached = CACHE / re.sub(r"[^\w.-]", "_", BASE + path)
    if cached.exists() and time.time() - cached.stat().st_mtime < ttl:
        return json.loads(cached.read_text())
    try:
        with urllib.request.urlopen(urllib.request.Request(BASE + path, headers={"User-Agent": "configdelta-skill"}), timeout=60) as r:
            body = r.read()
    except Exception as e:
        if cached.exists():  # offline: a stale copy beats nothing
            return json.loads(cached.read_text())
        raise Fail(f"cannot fetch {BASE + path}: {e}")
    CACHE.mkdir(parents=True, exist_ok=True)
    cached.write_bytes(body)
    return json.loads(body)


def vkey(v):
    return [int(x) if x.isdigit() else 0 for x in re.split(r"[.-]", v)]


def cmp_ver(a, b):
    x, y = vkey(a), vkey(b)
    n = max(len(x), len(y))
    x, y = x + [0] * (n - len(x)), y + [0] * (n - len(y))
    return (x > y) - (x < y)


class Engine:
    def __init__(self, engine_id):
        engines = fetch("data/engines.json", MANIFEST_TTL)
        match = [e for e in engines if e["id"] == engine_id]
        if not match:
            raise Fail(f"unknown engine '{engine_id}'. Available: {', '.join(e['id'] for e in engines)}")
        self.cfg = match[0]
        self.id = engine_id
        self.index = fetch(f"data/{engine_id}/index.json", MANIFEST_TTL)
        self._history = self._docs = None

    @property
    def history(self):
        if self._history is None:
            self._history = fetch(f"data/{self.id}/history.json", MANIFEST_TTL)
        return self._history

    @property
    def docs(self):
        if self._docs is None:
            try:
                self._docs = fetch(f"data/{self.id}/doclinks.json", MANIFEST_TTL)
            except Fail:
                self._docs = {}
        return self._docs

    def short(self, vendor):
        return self.cfg["vendors"].get(vendor, {}).get("short", vendor)

    def resolve(self, spec):
        vendor, _, version = spec.rpartition(":")
        hits = [e for e in self.index if e["version"] == version and (not vendor or e["vendor"] == vendor)]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise Fail(f"'{spec}' exists for several vendors ({', '.join(e['vendor'] for e in hits)}); write vendor:version")
        near = sorted({f"{e['vendor']}:{e['version']}" for e in self.index if e["version"].startswith(version.rstrip("."))})
        raise Fail(f"no version '{spec}' in engine {self.id}." + (f" Did you mean: {', '.join(near[:12])}?" if near else " Run `versions` to list them."))

    def load(self, entry):
        return fetch(f"data/{self.id}/{entry['file']}", VERSION_TTL)

    def records(self, data, kind):
        if kind == "status":
            return {n.lower(): {"default": "", "orig": n} for n in data["status"]}
        return data["variables"]

    def doc_links(self, name, kind, cols):
        links = []
        for vendor in self.cfg["vendors"]:
            for c in reversed([c for c in cols if c["vendor"] == vendor]):
                docs = self.docs.get(vendor, {})
                entry = docs.get(c["series"]) or docs.get("*")
                hit = entry and entry.get(kind, {}).get(name)
                if not hit:
                    continue
                rel = hit[0] if isinstance(hit, list) else hit
                url = rel if rel.startswith("http") else entry["base"] + rel
                if url not in links:
                    links.append(url)
                break
        return links

    def notes(self, name, kind, cols):
        out, message, replacement = [], None, None
        many = len({c["vendor"] for c in cols}) > 1
        for vendor in dict.fromkeys(c["vendor"] for c in cols):
            vers = sorted((c["version"] for c in cols if c["vendor"] == vendor), key=vkey)
            lo, hi = vers[0], vers[-1]
            h = self.history.get(vendor, {}).get(kind, {}).get(name)
            if not h:
                continue
            if h.get("message"):
                message, replacement = h["message"], h.get("replacement") or replacement
            pre = f"{self.short(vendor)}: " if many else ""
            for ev in h["events"]:
                span = f"between {ev['from_version']} and {ev['to_version']}" if ev["from_version"] else f"as of {ev['to_version']} or earlier"
                in_span = cmp_ver(ev["to_version"], lo) > 0 and cmp_ver(ev["to_version"], hi) <= 0
                if ev["type"] == "deprecated" and not ev["from_version"]:
                    if cmp_ver(ev["to_version"], hi) <= 0:
                        out.append(f"{pre}Deprecated {span}")
                elif not in_span:
                    continue
                elif ev["type"] == "default":
                    old = "(empty)" if ev["old"] == "" else ev["old"]
                    new = "(empty)" if ev["new"] == "" else ev["new"]
                    out.append(f"{pre}Default changed {span}: {old} -> {new}")
                else:
                    out.append(f"{pre}{ev['type'].capitalize()} {span}")
        if replacement:
            out.append(f"Replacement: {replacement}")
        elif message:
            out.append(message)
        return out


def value(rec):
    return None if rec is None else str(rec.get("default") if rec.get("default") is not None else "")


def cmd_engines(a):
    for e in fetch("data/engines.json", MANIFEST_TTL):
        print(f"{e['id']}: {e['name']}")
        print(f"  vendors: " + ", ".join(f"{k} ({v['name']})" for k, v in e["vendors"].items()))
        print(f"  kinds:   " + ", ".join(f"{k['id']} ({k['label']})" for k in e["kinds"]))
        if e.get("deprecations") is False:
            print("  note:    no deprecation data for this engine")


def cmd_versions(a):
    eng = Engine(a.engine)
    for vendor in eng.cfg["vendors"]:
        items = [e for e in eng.index if e["vendor"] == vendor]
        if not items:
            continue
        print(f"{vendor}:")
        for s in sorted({e["series"] for e in items}, key=vkey):
            vs = sorted((e["version"] for e in items if e["series"] == s), key=vkey)
            print(f"  {s}: {', '.join(vs)}")


def cmd_compare(a):
    eng = Engine(a.engine)
    if len(a.versions) < 2:
        raise Fail("compare needs a baseline and at least one more version")
    cols = [eng.resolve(s) for s in a.versions]
    datasets = [eng.load(c) for c in cols]
    recs = [eng.records(d, a.kind) for d in datasets]
    only = set(a.only.split(",")) if a.only else {"added", "removed", "changed", "deprecated"}
    q = (a.search or "").lower()
    rows = []
    for name in sorted({n for r in recs for n in r}):
        if q and q not in name:
            continue
        cells = [r.get(name) for r in recs]
        base = cells[0]
        if not a.include_env and any(c and c.get("default_source") in ("runtime", "derived") for c in cells):
            continue
        if a.kind == "variables" and not a.include_options and all(c is None or c.get("option_only") for c in cells):
            continue
        present = [c is not None for c in cells]
        tags = []
        if base is None and any(present):
            tags.append("added")
        elif base is not None and not all(present):
            tags.append("removed")
        elif a.kind == "variables" and any(value(c) != value(base) for c in cells):
            tags.append("changed")
        if a.kind == "variables" and any(c and c.get("deprecated") for c in cells) and not (base and base.get("deprecated")):
            tags.append("deprecated")
        if not set(tags) & only:
            continue
        rows.append({
            "name": next((c["orig"] for c in cells if c and c.get("orig")), name),
            "tags": tags,
            "values": {f"{c['vendor']}:{c['version']}": (None if r is None else ("present" if a.kind == "status" else value(r))) for c, r in zip(cols, cells)},
            "deprecated": next((c["deprecated"] for c in cells if c and c.get("deprecated")), None),
            "notes": eng.notes(name, a.kind, cols),
            "docs": eng.doc_links(name, a.kind, cols),
        })
    labels = [f"{c['vendor']}:{c['version']}" for c in cols]
    counts = {t: sum(t in r["tags"] for r in rows) for t in ("added", "removed", "changed", "deprecated")}
    if a.limit:
        rows = rows[: a.limit]
    if a.format == "json":
        print(json.dumps({"engine": a.engine, "kind": a.kind, "baseline": labels[0], "columns": labels, "counts": counts, "rows": rows}, indent=1))
        return
    print(f"{eng.cfg['name']} {a.kind}: baseline {labels[0]}; " + ", ".join(f"{n} {t}" for t, n in counts.items() if a.kind == "variables" or t in ("added", "removed")))
    if not rows:
        print("No differences match the filters.")
        return
    cut = lambda s: "absent" if s is None else (s if len(s) <= 50 else s[:47] + "...")
    print("| name | tags | " + " | ".join(labels) + " | notes |")
    print("|" + "---|" * (len(labels) + 3))
    for r in rows:
        print(f"| {r['name']} | {','.join(r['tags'])} | " + " | ".join(cut(v).replace("|", "\\|") for v in r["values"].values())
              + " | " + "; ".join(r["notes"]).replace("|", "\\|") + " |")


def cmd_find(a):
    eng = Engine(a.engine)
    q = a.text.lower()
    for vendor in eng.cfg["vendors"]:
        items = [e for e in eng.index if e["vendor"] == vendor]
        if not items:
            continue
        latest = max(items, key=lambda e: vkey(e["version"]))
        recs = eng.records(eng.load(latest), a.kind)
        hits = sorted(n for n in recs if q in n)
        print(f"{vendor} {latest['version']} (latest): {len(hits)} match(es)")
        for n in hits[:a.limit]:
            r = recs[n]
            extra = " [deprecated]" if r.get("deprecated") else ""
            print(f"  {r.get('orig', n)}" + ("" if a.kind == "status" else f" = {value(r)!r}") + extra)
        if len(hits) > a.limit:
            print(f"  ... {len(hits) - a.limit} more (narrow the text or raise --limit)")


def cmd_history(a):
    eng = Engine(a.engine)
    name = a.name.lower()
    found = False
    for vendor in eng.cfg["vendors"]:
        for kind in ("variables", "status"):
            h = eng.history.get(vendor, {}).get(kind, {}).get(name)
            if not h:
                continue
            found = True
            print(f"{vendor} {kind[:-1] if kind == 'variables' else kind} {a.name}:")
            for ev in sorted(h["events"], key=lambda e: vkey(e["to_version"])):
                span = f"between {ev['from_version']} and {ev['to_version']}" if ev["from_version"] else f"as of {ev['to_version']} or earlier"
                extra = f": {ev['old']!r} -> {ev['new']!r}" if ev["type"] == "default" else ""
                print(f"  {ev['type']} {span}{extra}")
            if h.get("replacement"):
                print(f"  replacement: {h['replacement']}")
            elif h.get("message"):
                print(f"  message: {h['message']}")
    if not found:
        print(f"No recorded changes for '{a.name}' (it may be unchanged across all versions, or not exist: try `find`).")


def cmd_show(a):
    eng = Engine(a.engine)
    entry = eng.resolve(a.version)
    data = eng.load(entry)
    name = a.name.lower()
    if name in data["variables"]:
        print(json.dumps({"vendor": entry["vendor"], "version": entry["version"], "name": name, **data["variables"][name]}, indent=1))
    elif name in {s.lower() for s in data["status"]}:
        print(json.dumps({"vendor": entry["vendor"], "version": entry["version"], "name": name, "kind": "status"}, indent=1))
    else:
        print(f"'{a.name}' is not present in {entry['vendor']}:{entry['version']}.")
    links = eng.doc_links(name, "variables" if name in data["variables"] else "status", [entry])
    if links:
        print("docs: " + " ".join(links))


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("engines").set_defaults(fn=cmd_engines)
    s = sub.add_parser("versions"); s.add_argument("engine"); s.set_defaults(fn=cmd_versions)
    s = sub.add_parser("compare"); s.add_argument("engine"); s.add_argument("versions", nargs="+")
    s.add_argument("--kind", choices=["variables", "status"], default="variables")
    s.add_argument("--only", help="comma list: added,removed,changed,deprecated")
    s.add_argument("--search"); s.add_argument("--limit", type=int, default=0)
    s.add_argument("--include-env", action="store_true", help="also show values that depend on the extraction environment")
    s.add_argument("--include-options", action="store_true", help="also show startup-option-only entries (MySQL)")
    s.add_argument("--format", choices=["table", "json"], default="table"); s.set_defaults(fn=cmd_compare)
    s = sub.add_parser("find"); s.add_argument("engine"); s.add_argument("text")
    s.add_argument("--kind", choices=["variables", "status"], default="variables"); s.add_argument("--limit", type=int, default=40); s.set_defaults(fn=cmd_find)
    s = sub.add_parser("history"); s.add_argument("engine"); s.add_argument("name"); s.set_defaults(fn=cmd_history)
    s = sub.add_parser("show"); s.add_argument("engine"); s.add_argument("version"); s.add_argument("name"); s.set_defaults(fn=cmd_show)
    a = p.parse_args()
    try:
        a.fn(a)
    except Fail as e:
        sys.exit(f"error: {e}")


if __name__ == "__main__":
    main()
