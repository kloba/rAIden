#!/usr/bin/env python3
"""Render slides to PNG (previews for QA): python3 talk/render.py [1 2 3 ...]  -> /tmp/s<N>.png"""
import subprocess
import sys
from pathlib import Path

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
HTML = Path(__file__).parent / "slides.html"
for n in [int(a) for a in sys.argv[1:]] or range(1, 11):
    out = f"/tmp/s{n}.png"
    try:
        subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--window-size=1920,1080", "--virtual-time-budget=3000",
                        f"--screenshot={out}", f"file://{HTML}?s={n}"], capture_output=True, timeout=60)
        print(n, "ok")
    except subprocess.TimeoutExpired:
        subprocess.run(["pkill", "-f", "Google Chrome.*headless"])
        print(n, "TIMEOUT")
