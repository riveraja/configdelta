#!/usr/bin/env python3
"""    build_index.py [engine]     (default: mysql)

Write docs/data/<engine>/index.json (version manifest) and history.json
(per-vendor change events derived from the per-version snapshots).

An event {from_version, to_version} means the change happened after
from_version and no later than to_version. from_version is null when the
state was already true in the oldest extracted version.
"""
import json
import re
import sys
from pathlib import Path

engine = sys.argv[1] if len(sys.argv) > 1 else "mysql"
data = Path(__file__).resolve().parent.parent / "docs" / "data" / engine


def vkey(v):
    return [int(x) for x in re.split(r"[.-]", v)]


items, snaps = [], {}
for f in sorted(data.glob("*-*.json")):
    d = json.loads(f.read_text())
    items.append({
        "file": f.name,
        "vendor": d["vendor"],
        "version": d["version"],
        "series": d["series"],
        "server_version": d["server_version"],
    })
    snaps.setdefault(d["vendor"], []).append(d)
(data / "index.json").write_text(json.dumps(items, indent=1))
print(f"{len(items)} versions indexed")


def timeline(snapshots, get):
    """Yield (name, [record-or-None per snapshot]) for every name seen."""
    names = set()
    for s in snapshots:
        names |= set(get(s))
    for n in sorted(names):
        yield n, [get(s).get(n) for s in snapshots]


history = {}
for vendor, snapshots in snaps.items():
    snapshots.sort(key=lambda s: vkey(s["version"]))
    vers = [s["version"] for s in snapshots]
    out = {"variables": {}, "status": {}}

    def presence_events(recs):
        ev = []
        for i in range(1, len(recs)):
            if recs[i] and not recs[i - 1]:
                ev.append({"type": "added", "from_version": vers[i - 1], "to_version": vers[i]})
            elif recs[i - 1] and not recs[i]:
                ev.append({"type": "removed", "from_version": vers[i - 1], "to_version": vers[i]})
        return ev

    for n, recs in timeline(snapshots, lambda s: s["variables"]):
        ev = presence_events(recs)
        for i, r in enumerate(recs):
            if not r:
                continue
            prev = recs[i - 1] if i else None
            if "deprecated" in r and (i == 0 or (prev and "deprecated" not in prev)):
                ev.append({"type": "deprecated", "from_version": vers[i - 1] if i else None,
                           "to_version": vers[i]})
            if prev and r.get("default_source") not in ("runtime", "derived") and str(r["default"]) != str(prev["default"]):
                ev.append({"type": "default", "from_version": vers[i - 1], "to_version": vers[i],
                           "old": prev["default"], "new": r["default"]})
        dep = [r for r in recs if r and "deprecated" in r]
        if dep:
            note = {"message": dep[-1]["deprecated"]}
            if "replacement" in dep[-1]:
                note["replacement"] = dep[-1]["replacement"]
        else:
            note = {}
        if ev or note:
            out["variables"][n] = {**note, "events": ev}

    for n, recs in timeline(snapshots, lambda s: {x.lower(): True for x in s["status"]}):
        ev = presence_events(recs)
        if ev:
            out["status"][n] = {"events": ev}
    history[vendor] = out

(data / "history.json").write_text(json.dumps(history, separators=(",", ":")))
for v, h in history.items():
    print(v, {k: len(x) for k, x in h.items()})
