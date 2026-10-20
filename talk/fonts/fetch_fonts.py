#!/usr/bin/env python3
"""Vendor the (SIL OFL) Google Fonts used by the deck as local woff2 files, latin subset only."""
import re
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
FAMILIES = [
    ("Anton", "Anton", [400]),
    ("Rubik Wet Paint", "Rubik+Wet+Paint", [400]),
    ("Press Start 2P", "Press+Start+2P", [400]),
    ("Teko", "Teko:wght@500;700", [500, 700]),
    ("Barlow Condensed", "Barlow+Condensed:wght@500;600;700", [500, 600, 700]),
    ("JetBrains Mono", "JetBrains+Mono:wght@500;700", [500, 700]),
]
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
css_out = []
for name, query, _ in FAMILIES:
    url = f"https://fonts.googleapis.com/css2?family={query}&display=block"
    css = urllib.request.urlopen(urllib.request.Request(url, headers=UA)).read().decode()
    for block in re.findall(r"/\* (\S+) \*/\s*(@font-face \{.*?\})", css, re.S):
        subset, face = block
        if subset != "latin":
            continue
        weight = re.search(r"font-weight:\s*(\d+)", face).group(1)
        src = re.search(r"url\((https://[^)]+\.woff2)\)", face).group(1)
        fname = f"{name.replace(' ', '')}-{weight}.woff2"
        (HERE / fname).write_bytes(urllib.request.urlopen(urllib.request.Request(src, headers=UA)).read())
        css_out.append(f"@font-face{{font-family:'{name}';font-weight:{weight};font-style:normal;src:url('{fname}') format('woff2');}}")
(HERE / "fonts.css").write_text("\n".join(css_out) + "\n")
print(f"{len(css_out)} font files")
