---
name: configdelta
description: Look up and compare database server configuration across versions — system variables, defaults, status counters, deprecations and removals — for MySQL (Oracle and Percona Server), MariaDB, PostgreSQL, MyRocks, MongoDB (Community and Percona Server for MongoDB) and Percona XtraDB Cluster (wsrep/Galera). Use when asked what changed between two versions, whether a variable or parameter exists, was added, removed, deprecated or had its default changed, or what replaced it. Reads the published Config Delta dataset; no browser or Docker needed.
---

# Config Delta

Config Delta is a dataset of server variables and status counters extracted from the official
images of each release. This skill queries it with a bundled script instead of the web UI
(https://riveraja.github.io/configdelta/).

Run the script from this skill's folder: `python3 scripts/configdelta.py <command>` (Python 3, standard library only).
It downloads from the published site and caches in `~/.cache/configdelta`. To use a local checkout instead, set
`CONFIGDELTA_URL` to its `docs/` directory.

## Commands

| Need | Command |
|---|---|
| Which engines/vendors exist | `engines` |
| Which versions are covered | `versions <engine>` |
| What differs between versions | `compare <engine> <baseline> <version> [<version> ...]` |
| Does a name exist, current default | `find <engine> <text>` |
| When was a name added, removed, deprecated or its default changed | `history <engine> <name>` |
| Full record for one name in one version | `show <engine> <version> <name>` |

Engine ids: `mysql`, `mariadb`, `postgres`, `rocksdb` (MyRocks), `mongodb`, `pxc`.
A version is `vendor:version` (for example `percona:8.4.11-11`, `oracle:8.4.4`) or a bare version when only one
vendor has it; the script says so if it is ambiguous and suggests near matches.

`compare` options: `--kind variables|status` (default variables), `--only added,removed,changed,deprecated`,
`--search TEXT`, `--limit N`, `--format table|json`. The first version is the baseline: "added" means absent in
the baseline, "removed" means present in the baseline but absent in a later version, "changed" is a different default.
Use `--format json` when you need full values, notes and documentation links per row.

## Working method

1. Run `engines` / `versions` if you are unsure of an id or version string; do not guess version strings.
2. Pick the narrowest command: `history` for "when did X change", `find` for "does X exist", `compare` for a version range.
3. For upgrade questions, compare the old version (baseline) to the new one, run `--only removed` and `--only deprecated`
   first, then `--only changed`.
4. Answer from the output. Quote the "between A and B" ranges as given and include the documentation links from the JSON
   output when you cite a variable.

## Caveats to state when they matter

- Defaults come from a fresh, unconfigured instance (or `--help` output). Values that depend on the machine or cluster
  (memory-sized defaults, node addresses, hostnames) are hidden by default; `--include-env` shows them, but do not
  present those as deliberate default changes.
- "Added/removed between A and B" means the change happened after A and no later than B, since only the listed versions
  were extracted. Versions are ordered numerically, so across series (for example PXC 8.0.46 vs 8.4.0, or MongoDB 8.0 vs 8.3)
  a backported item can appear as removed then added again. Prefer comparing within a series, or say that it may be a backport.
- Deprecation data exists for MySQL, MariaDB, MyRocks and PXC only; PostgreSQL and MongoDB have none, so absence there
  proves nothing.
- Min/max ranges and descriptions are not recorded for every engine; MongoDB entries have no descriptions.
- Oracle MySQL entries have no documentation links.
- PXC covers only `wsrep_*` / `pxc_*` names; each Galera setting inside `wsrep_provider_options` is listed as
  `wsrep_provider_options.<setting>`. MyRocks covers only `rocksdb_*`. Everything else is under `mysql`.
- A name missing from the dataset is not evidence it never existed: check `versions` for the range covered.
- Only releases extracted so far are present; newer ones appear after the maintainer updates the data.
