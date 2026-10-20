#!/usr/bin/env python3
"""Regenerate talk/qr.svg, the link on the last slide.   python3 talk/make_qr.py [url]   (needs: pip install segno)"""
import re
import sys
from pathlib import Path

import segno

url = sys.argv[1] if len(sys.argv) > 1 else "https://github.com/kloba/rAIden"
svg = segno.make(url, error="m", boost_error=False).svg_inline(dark="#07070a", light="#fff", border=0, scale=1)
svg = re.sub(r'width="(\d+)" height="(\d+)"', r'width="100%" height="100%" viewBox="0 0 \1 \2"', svg, count=1)
(Path(__file__).parent / "qr.svg").write_text(svg)
print("wrote talk/qr.svg for", url)
