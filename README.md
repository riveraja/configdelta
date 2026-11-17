# Config Delta

Compare variables, defaults, deprecations and removals across versions of one database
engine at a time (MySQL, MariaDB, PostgreSQL, MyRocks, MongoDB, Percona XtraDB Cluster).

- `docs/` — the static viewer and its data (`docs/data/<engine>/`). This is what GitHub Pages serves.
- `extractor/` — Docker-based extractors that produce the per-version JSON, the index/history and the doc links.
- `server.py` — local server for `docs/` plus the Update button's API (`python3 server.py [port]`).

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

git add docs && git commit -m "Update data" && git push
```

Pages republishes within a minute or two of the push.
