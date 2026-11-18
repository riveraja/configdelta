# Config Delta

Compare variables, defaults, deprecations and removals across versions of one database
engine at a time (MySQL, MariaDB, PostgreSQL, MyRocks, MongoDB, Percona XtraDB Cluster).

- `docs/` — the static viewer and its data (`docs/data/<engine>/`). This is what GitHub Pages serves.
- `extractor/` — Docker-based extractors that produce the per-version JSON, the index/history and the doc links.
- `server.py` — local server for `docs/` plus the Update button's API (`python3 server.py [port]`).

## Agent skill (Claude Code, Codex, other agents)

`skills/configdelta/` is an agent skill that answers "what changed between versions X and Y" from the published
data, with no browser. It is a `SKILL.md` plus a standard-library Python script.

```sh
# Claude Code
cp -R skills/configdelta ~/.claude/skills/
# Codex (skills directory; or point your AGENTS.md at skills/configdelta/SKILL.md)
cp -R skills/configdelta ~/.codex/skills/

python3 skills/configdelta/scripts/configdelta.py compare pxc 8.0.46-38.1 8.4.11-12.1
```

Set `CONFIGDELTA_URL` to a local `docs/` directory to run against a checkout instead of the published site.

## Hosting on GitHub Pages

Repository **Settings → Pages → Build and deployment → Source: Deploy from a branch**,
branch `main`, folder `/docs`. `docs/.nojekyll` makes Pages serve the files as-is.
All paths in the app are relative, so it works under `https://<user>.github.io/<repo>/`.
The Update button hides itself on Pages because the update API only exists locally.

## Updating data (run locally, then push)

```sh
python3 server.py            # open http://localhost:8765 and press Update
# or, without the UI:
python3 extractor/run_all.py <engine>

python3 stamp.py            # only if you changed docs/app.js or docs/style.css
git add docs && git commit -m "Update data" && git push
```

Pages republishes within a minute or two of the push.
