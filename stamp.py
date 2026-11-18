#!/usr/bin/env python3
"""Add a content hash to the app.js / style.css URLs in docs/index.html, so browsers
fetch a changed file immediately instead of reusing a copy GitHub Pages let them cache
for 10 minutes. Run before committing changes to docs/app.js or docs/style.css:

    python3 stamp.py
"""
import hashlib
import re
from pathlib import Path

docs = Path(__file__).resolve().parent / "docs"
page = docs / "index.html"
html = page.read_text()
for name in ("app.js", "style.css"):
    h = hashlib.sha1((docs / name).read_bytes()).hexdigest()[:8]
    html, n = re.subn(rf'(["\']){re.escape(name)}(?:\?v=\w+)?(["\'])', rf"\g<1>{name}?v={h}\g<2>", html)
    print(f"{name}?v={h} ({n} reference)")
page.write_text(html)
