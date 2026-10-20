#!/usr/bin/env python3
"""Build the deck: talk/data.json (real battle numbers) + deck.css -> slides.html -> PDF (headless Chrome).

  python3 talk/build.py            # writes talk/slides.html and talk/rAIden-pgconf-2026.pdf
  python3 talk/build.py --html     # HTML only
Preview one slide:  slides.html?s=4
Needs headless Google Chrome; `pip install -r talk/requirements.txt` for the vector headlines (see ann()).
"""
from __future__ import annotations

import html
import json
import math
import random
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

DEFAULT = {
    "rounds": {
        "crash": {"wake": 15.9, "clue": 19.8, "ok": 86.2, "turns": 22, "tools": 30, "denied": 0, "restarts": 1, "tokens": 700000, "won": True, "incidents": 1},
        "toast": {"wake": 11.4, "clue": 14.1, "ok": 90.0, "turns": 27, "tools": 32, "denied": 0, "restarts": 0, "tokens": 650000, "won": True},
        "pitr": {"wake": 15.9, "clue": 20.2, "ok": 74.9, "turns": 23, "tools": 28, "denied": 0, "restarts": 0, "tokens": 330000, "won": True},
        "rematch": {"wake": 14.0, "clue": 17.0, "ok": 60.0, "turns": 14, "tools": 18, "denied": 0, "restarts": 1, "tokens": 300000, "won": True, "prevented": False},
        "bomb": {"wake": 12.0, "clue": 14.0, "ok": None, "defused": 72.0, "fuse": 300, "turns": 14, "tools": 18, "denied": 0, "restarts": 0, "tokens": 300000, "won": True, "prevented": True},
        "boss2": {"wake": 14.0, "clue": 18.0, "ok": 70.0, "turns": 16, "tools": 20, "denied": 0, "restarts": 1, "tokens": 320000, "won": True, "prevented": False},
    },
    "final": {"acked": 19218, "lost": 0, "corrupt": 0, "unreadable": 0, "amcheck": "ok"},
    "rounds_won": 5, "rounds_total": 5, "selftest": 106, "skills": 10, "checks": 8,
    "artifacts": ["checks.d/25-preload-allowlist.sh", "learned-pg-crash-preload", "checks.d/90-dangerous-maintenance.sh", "checks.d/85-ledger-critical-relations.sh"],
    "artifacts_modified": [],
    "denials": 0, "tokens_total": 2_300_000, "duration_min": 38, "storm_lines": 120,
    "log_lines": [
        ["L", "server process (PID 2491) was terminated by signal 9: Killed"],
        ["L", "terminating any other active server processes"],
        ["F", "terminating connection due to administrator command"],
        ["F", "the database system is in recovery mode"],
        ["L", "all server processes terminated; reinitializing"],
    ],
    "timeline": [],
    "chart": {"crash": {"points": [[t, 255 if t < 0 or t > 90 else 120 + (t * 37) % 70] for t in range(-30, 160, 10)],
                         "marks": [["ATTACK", 0, "#ff4a3a"], ["FINISH HIM!", 20, "#ffd36b"], ["OK", 86, "#7dff6a"]]}},
}


REAL = False  # True only when talk/data.json holds a complete, finished battle


def load() -> dict:
    global REAL
    data = json.loads(json.dumps(DEFAULT))
    p = HERE / "data.json"
    if p.exists():
        raw = json.loads(p.read_text())

        def merge(a, b):
            for k, v in b.items():
                if v is None:
                    continue  # partial battle: keep the sample value for layout, but REAL stays False
                if isinstance(v, dict) and isinstance(a.get(k), dict):
                    merge(a[k], v)
                else:
                    a[k] = v
        merge(data, raw)
        REAL = isinstance(raw.get("final", {}).get("acked"), int) and len(raw.get("rounds", {})) >= 5 and raw.get("rounds_total", 0) >= 5
    return data


D = load()
R = D["rounds"]


def loc() -> int:
    """lines of Python in the agent package (guardrails and daemon included), rounded for the slide"""
    try:
        return round(sum(len(f.read_text().splitlines()) for f in (HERE.parent / "raiden").glob("*.py")), -2)
    except OSError:
        return 2900


def sec(x: float | None) -> str:
    return "–" if x is None else f"{x:.0f} s"


def esc(s: str) -> str:
    return html.escape(s, quote=False)


# ------------------------------------------------------------------------------------------------ scenes
def scene(kind: str, seed: int = 3) -> str:
    rng = random.Random(seed)
    pal = {
        "arena": dict(sky=("#05040a", "#1a0507", "#3a0909"), moon=("#ff6a3a", "#b3180f"), mtn=("#150608", "#0b0305"), glow="255,60,30"),
        "pit": dict(sky=("#04060b", "#0a1220", "#1b2b45"), moon=("#d8e6ff", "#5a78a8"), mtn=("#0a111d", "#05080f"), glow="90,140,255"),
        "toast": dict(sky=("#150a04", "#3a1a07", "#7a3a0b"), moon=("#ffd36b", "#e8920a"), mtn=("#2a1306", "#150a03"), glow="255,150,40"),
        "volt": dict(sky=("#02050c", "#06142b", "#0b2a55"), moon=("#b9f1ff", "#1b6fd6"), mtn=("#071428", "#030a15"), glow="89,213,255"),
        "gold": dict(sky=("#050403", "#120d03", "#2a1d05"), moon=("#fff2b0", "#f2a900"), mtn=("#0f0a02", "#060401"), glow="255,200,60"),
    }[kind]
    s0, s1, s2 = pal["sky"]
    m0, m1 = pal["moon"]
    near, far = pal["mtn"]

    def ridge(base: float, amp: float, step: int, fill: str, jag: float = 1.0) -> str:
        pts, x = [], -40
        while x <= 1960:
            pts.append((x, base - rng.random() * amp * jag - (amp * 0.35 if (x // step) % 3 == 0 else 0)))
            x += step
        d = "M-40,1080 " + " ".join(f"L{px:.0f},{py:.0f}" for px, py in pts) + " L1960,1080 Z"
        return f'<path d="{d}" fill="{fill}"/>'

    ember = "".join(
        f'<circle cx="{rng.randint(0, 1920)}" cy="{rng.randint(120, 1060)}" r="{rng.uniform(1.2, 4.2):.1f}" fill="rgba({pal["glow"]},{rng.uniform(.25, .9):.2f})"/>'
        for _ in range(70)
    )
    extra = ""
    if kind == "pit":  # spikes + abyss
        spikes = "".join(
            f'<path d="M{x},1080 L{x + 36},{930 - (x * 7) % 40} L{x + 72},1080 Z" fill="url(#spk)"/>' for x in range(-20, 1960, 72)
        )
        extra = (
            '<defs><linearGradient id="spk" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#dfe6f2"/><stop offset="1" stop-color="#2b3140"/></linearGradient></defs>'
            + spikes
            + '<rect x="0" y="1010" width="1920" height="70" fill="#05070c"/>'
        )
    if kind == "volt":  # circuit lines + lightning
        lines = ""
        for _ in range(16):
            x, y = rng.randint(0, 1920), rng.randint(140, 1000)
            seg = f"M{x},{y}"
            for _ in range(rng.randint(2, 4)):
                x += rng.choice([-1, 1]) * rng.randint(60, 220)
                seg += f" L{x},{y}"
                y += rng.choice([-1, 1]) * rng.randint(40, 160)
                seg += f" L{x},{y}"
            lines += f'<path d="{seg}" stroke="rgba(89,213,255,.16)" stroke-width="3" fill="none"/><circle cx="{x}" cy="{y}" r="7" fill="rgba(89,213,255,.45)"/>'
        bolt = "M1500,0 L1430,260 L1520,250 L1390,560 L1480,545 L1330,900"
        extra = lines + f'<path d="{bolt}" stroke="#b9f1ff" stroke-width="8" fill="none" opacity=".55"/><path d="{bolt}" stroke="#59d5ff" stroke-width="22" fill="none" opacity=".12"/>'
    if kind == "gold":  # radiating rays
        rays = "".join(
            f'<path d="M960,460 L{960 + 2400 * math.cos(a):.0f},{460 + 2400 * math.sin(a):.0f} L{960 + 2400 * math.cos(a + .06):.0f},{460 + 2400 * math.sin(a + .06):.0f} Z" fill="rgba(255,200,60,.07)"/>'
            for a in [i * (math.pi / 14) for i in range(28)]
        )
        extra = rays
    if kind == "toast":  # dojo tiles
        tiles = "".join(f'<path d="M0,{y} L1920,{y}" stroke="rgba(0,0,0,.22)" stroke-width="3"/>' for y in range(620, 1080, 64))
        tiles += "".join(f'<path d="M{x},620 L{x - (x - 960) * 0.35:.0f},1080" stroke="rgba(0,0,0,.22)" stroke-width="3"/>' for x in range(-400, 2400, 160))
        extra = tiles
    moon_cx, moon_cy, moon_r = (1380, 300, 210) if kind in ("arena", "pit") else (960, 380, 260)
    moon = f'<circle cx="{moon_cx}" cy="{moon_cy}" r="{moon_r * 2.4}" fill="url(#halo)"/><circle cx="{moon_cx}" cy="{moon_cy}" r="{moon_r}" fill="url(#moon)"/>'
    if kind in ("volt", "toast"):
        moon = f'<circle cx="{moon_cx}" cy="{moon_cy}" r="{moon_r * 2.6}" fill="url(#halo)"/>'
    return f'''<svg class="layer" viewBox="0 0 1920 1080" width="1920" height="1080">
<defs>
 <linearGradient id="sky{kind}" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{s0}"/><stop offset=".55" stop-color="{s1}"/><stop offset="1" stop-color="{s2}"/></linearGradient>
 <radialGradient id="halo"><stop offset="0" stop-color="rgba({pal['glow']},.38)"/><stop offset="1" stop-color="rgba({pal['glow']},0)"/></radialGradient>
 <radialGradient id="moon" cx=".38" cy=".34"><stop offset="0" stop-color="{m0}"/><stop offset="1" stop-color="{m1}"/></radialGradient>
 <radialGradient id="fogr{kind}"><stop offset="0" stop-color="rgba(255,255,255,.13)"/><stop offset="1" stop-color="rgba(255,255,255,0)"/></radialGradient>
</defs>
<rect width="1920" height="1080" fill="url(#sky{kind})"/>
{moon}
{extra if kind in ("gold", "volt") else ""}
{ridge(760, 170, 90, far, 1.0)}
{ridge(900, 130, 70, near, 1.1)}
{extra if kind in ("pit", "toast") else ""}
<ellipse cx="560" cy="700" rx="900" ry="70" fill="url(#fogr{kind})"/><ellipse cx="1400" cy="860" rx="900" ry="85" fill="url(#fogr{kind})"/>
{ember}
<rect width="1920" height="1080" fill="url(#vig)"/>
<defs><radialGradient id="vig" cx=".5" cy=".5" r=".75"><stop offset=".55" stop-color="rgba(0,0,0,0)"/><stop offset="1" stop-color="rgba(0,0,0,.72)"/></radialGradient></defs>
</svg>'''


# ------------------------------------------------------------------------------------------------ emblems
def emblem_raiden(size: int = 300) -> str:
    return f'''<svg viewBox="0 0 200 200" width="{size}" height="{size}">
<defs><radialGradient id="rg{size}" cx=".5" cy=".5"><stop offset="0" stop-color="rgba(89,213,255,.5)"/><stop offset="1" stop-color="rgba(89,213,255,0)"/></radialGradient>
<linearGradient id="hat{size}" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#f4e7b8"/><stop offset="1" stop-color="#a98a4a"/></linearGradient></defs>
<circle cx="100" cy="100" r="98" fill="url(#rg{size})"/>
<circle cx="100" cy="100" r="88" fill="#0a1626" stroke="#59d5ff" stroke-width="5"/>
<circle cx="100" cy="100" r="78" fill="none" stroke="#1b6fd6" stroke-width="2" stroke-dasharray="4 7"/>
<path d="M100 22 L172 100 Q100 122 28 100 Z" fill="url(#hat{size})" stroke="#1a1308" stroke-width="5" stroke-linejoin="round"/>
<path d="M62 78 L138 78 M48 90 L152 90 M76 62 L124 62" stroke="#7a6130" stroke-width="3" fill="none"/>
<path d="M28 100 Q100 122 172 100" fill="none" stroke="#1a1308" stroke-width="5"/>
<ellipse cx="76" cy="125" rx="15" ry="7" fill="#e9fbff" transform="rotate(8 76 125)"/><ellipse cx="124" cy="125" rx="15" ry="7" fill="#e9fbff" transform="rotate(-8 124 125)"/>
<ellipse cx="76" cy="125" rx="15" ry="7" fill="none" stroke="#59d5ff" stroke-width="3" transform="rotate(8 76 125)"/><ellipse cx="124" cy="125" rx="15" ry="7" fill="none" stroke="#59d5ff" stroke-width="3" transform="rotate(-8 124 125)"/>
<polygon points="108,134 84,176 102,172 90,198 130,152 110,156 122,134" fill="#b9f1ff" stroke="#1b6fd6" stroke-width="3" stroke-linejoin="round"/>
</svg>'''


def emblem_crash(size: int = 300) -> str:
    return f'''<svg viewBox="0 0 200 200" width="{size}" height="{size}">
<defs><radialGradient id="cg{size}" cx=".5" cy=".5"><stop offset="0" stop-color="rgba(255,40,30,.55)"/><stop offset="1" stop-color="rgba(255,40,30,0)"/></radialGradient>
<linearGradient id="cyl{size}" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#3a0d10"/><stop offset=".45" stop-color="#8a1a1f"/><stop offset="1" stop-color="#2a0a0c"/></linearGradient></defs>
<circle cx="100" cy="100" r="98" fill="url(#cg{size})"/>
<circle cx="100" cy="100" r="88" fill="#1a0507" stroke="#e4151b" stroke-width="5"/>
<path d="M50 62 L50 138 Q50 160 100 160 Q150 160 150 138 L150 62 Z" fill="url(#cyl{size})" stroke="#0a0204" stroke-width="5" stroke-linejoin="round"/>
<ellipse cx="100" cy="62" rx="50" ry="18" fill="#b3252b" stroke="#0a0204" stroke-width="5"/>
<path d="M50 92 Q100 112 150 92" fill="none" stroke="#0a0204" stroke-width="4"/><path d="M50 120 Q100 140 150 120" fill="none" stroke="#0a0204" stroke-width="4"/>
<path d="M74 84 L94 96 L74 98 Z" fill="#ffe14a" stroke="#0a0204" stroke-width="3" stroke-linejoin="round"/><path d="M126 84 L106 96 L126 98 Z" fill="#ffe14a" stroke="#0a0204" stroke-width="3" stroke-linejoin="round"/>
<path d="M70 124 L80 134 L90 122 L100 134 L110 122 L120 134 L130 124" fill="none" stroke="#fff2d0" stroke-width="5" stroke-linejoin="round" stroke-linecap="round"/>
<polyline points="104,44 92,66 108,72 94,100 112,108 98,140" fill="none" stroke="#ffe14a" stroke-width="4" stroke-linejoin="round" opacity=".95"/>
</svg>'''


def toast_char(size: int = 360) -> str:
    return f'''<svg viewBox="0 0 200 220" width="{size}" height="{int(size * 1.1)}">
<defs><linearGradient id="crust" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#e8a24a"/><stop offset="1" stop-color="#a8641c"/></linearGradient>
<linearGradient id="crumb" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#ffe7a8"/><stop offset="1" stop-color="#f2c066"/></linearGradient></defs>
<path d="M30 70 Q22 30 62 26 Q80 10 100 22 Q120 10 138 26 Q178 30 170 70 Q184 84 170 108 L170 196 Q170 210 156 210 L44 210 Q30 210 30 196 L30 108 Q16 84 30 70 Z" fill="url(#crust)" stroke="#2b1405" stroke-width="6" stroke-linejoin="round"/>
<path d="M46 76 Q42 46 68 44 Q84 32 100 42 Q116 32 132 44 Q158 46 154 76 Q164 88 154 104 L154 190 L46 190 L46 104 Q36 88 46 76 Z" fill="url(#crumb)" stroke="#8a5a1a" stroke-width="3"/>
<g fill="#d99a3e" opacity=".55"><circle cx="70" cy="62" r="3"/><circle cx="130" cy="58" r="3"/><circle cx="62" cy="150" r="3"/><circle cx="138" cy="160" r="3"/><circle cx="100" cy="176" r="3"/></g>
<rect x="58" y="92" width="38" height="24" rx="6" fill="#0b0b10" stroke="#fff" stroke-width="3"/><rect x="104" y="92" width="38" height="24" rx="6" fill="#0b0b10" stroke="#fff" stroke-width="3"/>
<path d="M96 100 L104 100" stroke="#fff" stroke-width="4"/><path d="M62 98 L72 98 M108 98 L118 98" stroke="#59d5ff" stroke-width="3" opacity=".8"/>
<path d="M64 140 Q100 176 136 140 Q100 158 64 140 Z" fill="#fff" stroke="#2b1405" stroke-width="4" stroke-linejoin="round"/>
</svg>'''



RN = '<span class="rn">r<span class="ai">AI</span>den</span>'

# ------------------------------------------------------------------------------------------------ announcer headlines
# Headlines are drawn as real vector glyph outlines (gradient fill + black outline + drop layer) so every PDF viewer
# (Preview, Acrobat, Chrome, poppler) shows the same thing. CSS gradient text clipped to glyphs is not portable in PDF.
try:
    import io
    import itertools

    from fontTools.pens.svgPathPen import SVGPathPen
    from fontTools.pens.transformPen import TransformPen
    from fontTools.ttLib import TTFont

    try:
        import uharfbuzz as hb
    except ImportError:
        hb = None
    OUTLINES = True
except ImportError:
    OUTLINES = False
    hb = None

ANN = {
    "gold": dict(font="Anton-400", ls=0.015, ai="#59d5ff", stops=[(0, "#fff7cf"), (.30, "#ffd36b"), (.56, "#e8920a"), (1, "#b3180f")]),
    "green": dict(font="Anton-400", ls=0.015, ai="#ffd36b", stops=[(0, "#f3ffe0"), (.40, "#9dff7a"), (1, "#1fa31f")]),
    "ice": dict(font="Anton-400", ls=0.015, ai="#ffd36b", stops=[(0, "#ffffff"), (.35, "#b9f1ff"), (.62, "#59d5ff"), (1, "#1b6fd6")]),
    "blood": dict(font="RubikWetPaint-400", ls=0.0, ai="#59d5ff", stops=[(0, "#ff7a5a"), (.48, "#e4151b"), (1, "#8d070c")]),
}
_ids = itertools.count(1) if OUTLINES else None
_fonts: dict = {}


class OutlineFont:
    def __init__(self, name: str):
        path = HERE / "fonts" / f"{name}.woff2"
        self.tt = TTFont(path)
        self.upm = self.tt["head"].unitsPerEm
        self.asc, self.desc = self.tt["hhea"].ascent, -self.tt["hhea"].descent
        self.cap = getattr(self.tt["OS/2"], "sCapHeight", 0) or int(self.upm * 0.7)
        self.gs, self.cmap, self.order, self.hmtx = self.tt.getGlyphSet(), self.tt.getBestCmap(), self.tt.getGlyphOrder(), self.tt["hmtx"]
        self.hbfont = None
        if hb is not None:  # HarfBuzz needs plain sfnt bytes, not woff2
            raw = TTFont(path)
            raw.flavor = None
            buf = io.BytesIO()
            raw.save(buf)
            self.hbfont = hb.Font(hb.Face(buf.getvalue()))

    def shape(self, s: str) -> list[tuple[str, int, int, int, int]]:
        """-> [(glyph name, char index, advance, x offset, y offset)] in font units, kerning applied"""
        if self.hbfont is not None:
            b = hb.Buffer()
            b.add_str(s)
            b.guess_segment_properties()
            hb.shape(self.hbfont, b, {"kern": True, "liga": False})
            return [(self.order[i.codepoint], i.cluster, p.x_advance, p.x_offset, p.y_offset) for i, p in zip(b.glyph_infos, b.glyph_positions)]
        out = []
        for idx, ch in enumerate(s):
            g = self.cmap.get(ord(ch), ".notdef")
            out.append((g, idx, self.hmtx[g][0], 0, 0))
        return out

    def outline(self, glyph: str, scale: float, x: float, y: float) -> str:
        pen = SVGPathPen(self.gs, ntos=lambda v: f"{v:.1f}".rstrip("0").rstrip("."))
        self.gs[glyph].draw(TransformPen(pen, (scale, 0, 0, -scale, x, y)))
        return pen.getCommands()


def _mix(color: str, other: tuple[int, int, int], t: float) -> str:
    c = color.lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    rgb = [int(c[i:i + 2], 16) for i in (0, 2, 4)]
    return "#%02x%02x%02x" % tuple(round(a + (b - a) * t) for a, b in zip(rgb, other))


def _chars(text: str, ai: str) -> list[tuple[str, str | None]]:
    """headline markup -> [(char, colour override)], upper-cased except the 'rAIden' wordmark"""
    pieces, pos = [], 0
    for m in re.finditer(r'<span style="color:(#[0-9a-fA-F]{3,8})">(.*?)</span>', text, flags=re.S):
        if m.start() > pos:
            pieces.append((text[pos:m.start()], None))
        pieces.append((m.group(2), m.group(1)))
        pos = m.end()
    if pos < len(text):
        pieces.append((text[pos:], None))
    out: list[tuple[str, str | None]] = []
    for raw, color in pieces:
        raw = html.unescape(re.sub(r"<[^>]+>", "", raw))
        for part in re.split(r"(rAIden)", raw):
            if part == "rAIden":
                out += [("r", color), ("A", ai), ("I", ai), ("d", color), ("e", color), ("n", color)]
            else:
                out += [(ch.upper(), color) for ch in part]
    return out


def ann_css(cls: str, text: str, size: int, style: str = "") -> str:
    text = text.replace("rAIden", RN)
    return f'<div class="ann {cls}" style="font-size:{size}px;{style}"><span class="sh">{text}</span><span class="fg">{text}</span></div>'


def ann(cls: str, text: str, size: int, style: str = "") -> str:
    if not OUTLINES:
        return ann_css(cls, text, size, style)
    st = ANN[cls]
    if st["font"] not in _fonts:
        _fonts[st["font"]] = OutlineFont(st["font"])
    font = _fonts[st["font"]]
    chars = _chars(text, st["ai"])
    shaped = font.shape("".join(c for c, _ in chars))
    k = size / font.upm
    box_h = size * 0.95  # the old CSS line box, so layouts keep their rhythm
    base = (box_h - (font.asc + font.desc) * k) / 2 + font.asc * k
    x, parts = 0.0, {}
    for glyph, ci, adv, xo, yo in shaped:
        d = font.outline(glyph, k, x + xo * k, base - yo * k)
        if d:
            parts.setdefault(chars[ci][1], []).append(d)
        x += adv * k + st["ls"] * size
    alld = " ".join(d for ds in parts.values() for d in ds)
    gid = next(_ids)
    stops = "".join(f'<stop offset="{o}" stop-color="{c}"/>' for o, c in st["stops"])
    grads = [f'<linearGradient id="ag{gid}" gradientUnits="userSpaceOnUse" x1="0" y1="{base - font.asc * k:.1f}" x2="0" y2="{base + font.desc * k:.1f}">{stops}</linearGradient>']
    fills = []
    for n, (color, ds) in enumerate(parts.items()):
        if color is None:
            ref = f"url(#ag{gid})"
        else:
            ref = f"url(#ac{gid}_{n})"
            top = _mix(color, (255, 255, 255), .55)
            low = _mix(color, (0, 0, 0), .45)
            grads.append(
                f'<linearGradient id="ac{gid}_{n}" gradientUnits="userSpaceOnUse" x1="0" y1="{base - font.cap * k:.1f}" x2="0" y2="{base:.1f}">'
                f'<stop offset="0" stop-color="{top}"/><stop offset=".45" stop-color="{color}"/><stop offset="1" stop-color="{low}"/></linearGradient>'
            )
        fills.append(f'<path d="{" ".join(ds)}" fill="{ref}"/>')
    w = math.ceil(x)
    drop, edge = size * 0.05, size * 0.06
    plain = html.escape(re.sub(r"<[^>]+>", "", html.unescape(text))).replace("rAIden", "r&#65;Iden")  # keep the wordmark regex out of attributes
    return (
        f'<svg class="annv" role="img" aria-label="{plain}" width="{w}" height="{math.ceil(box_h)}" viewBox="0 0 {w} {box_h:.1f}" '
        f'style="display:block;flex:none;overflow:visible;{style}"><defs>{"".join(grads)}</defs>'
        f'<path d="{alld}" transform="translate(0 {drop:.1f})" fill="#000" stroke="#000" stroke-width="{size * 0.075:.1f}" stroke-linejoin="round"/>'
        f'<path d="{alld}" fill="#000" stroke="#000" stroke-width="{edge:.1f}" stroke-linejoin="round"/>'
        f'{"".join(fills)}</svg>'
    )


def convert_headlines(body: str) -> str:
    def sub(m: re.Match) -> str:
        cls, style, text = m.group(1), m.group(2), m.group(3)
        size = re.search(r"font-size:(\d+)px", style).group(1)
        rest = re.sub(r"font-size:\d+px;?", "", style)
        rest = re.sub(r"background:[^;]*;?|-webkit-background-clip:[^;]*;?|background-clip:[^;]*;?", "", rest)
        return ann(cls, text, int(size), rest)
    return re.sub(r'<div class="(gold|blood|ice|green)" style="([^"]*)">(.*?)</div>', sub, body, flags=re.S)

# ------------------------------------------------------------------------------------------------ pieces
def hud(l_pct: int, r_pct: int, l_wins: int = 0, r_wins: int = 0, timer: str = "99", l_name: str = "r<span class=\"ai\">AI</span>den", r_name: str = "pg_crash", r_hurt: bool = False) -> str:
    wl = "".join(f'<b class="{"on" if i < l_wins else ""}"></b>' for i in range(2))
    wr = "".join(f'<b class="{"on" if i < r_wins else ""}"></b>' for i in range(2))
    return f'''<div class="hud">
<div class="name l">{l_name}</div><div class="bar l volt"><i style="width:{l_pct}%"></i></div><div class="wins l">{wl}</div>
<div class="timer">{timer}</div>
<div class="name r">{r_name}</div><div class="bar r {"hurt" if r_hurt else ""}"><i style="width:{r_pct}%"></i></div><div class="wins r">{wr}</div></div>'''


def caption(text: str) -> str:
    return f'<div class="caption"><span>{text}</span></div>'


def slide(n: int, inner: str) -> str:
    return f'<section class="slide" id="s{n}">{inner}</section>'


def ack_chart(round_key: str, w: int, h: int) -> str:
    """Acknowledged commits per 10 s around the attack (real workload data)."""
    ch = D.get("chart", {}).get(round_key)
    if not ch:
        return f'<svg width="{w}" height="{h}"></svg>'
    pts = ch["points"]  # [[offset_s, commits_per_10s], ...] relative to the attack
    xs = [p[0] for p in pts]
    x0, x1 = min(xs), max(xs) + 10
    ymax = max(max(p[1] for p in pts), 1) * 1.15
    px = lambda t: 54 + (t - x0) / (x1 - x0) * (w - 80)
    py = lambda v: h - 44 - v / ymax * (h - 80)
    bars = "".join(
        f'<rect x="{px(t):.1f}" y="{py(v):.1f}" width="{max((w - 80) / len(pts) - 2, 2):.1f}" height="{h - 44 - py(v):.1f}" fill="{"#ff4a3a" if v < ymax / 1.15 * 0.6 else "#59d5ff"}" opacity=".92"/>'
        for t, v in pts
    )
    marks = ""
    for label, t, color in ch.get("marks", []):
        i = len(marks.split("<path")) - 2
        marks += f'<path d="M{px(t):.1f},34 L{px(t):.1f},{h - 44}" stroke="{color}" stroke-width="4" stroke-dasharray="10 8"/><rect x="{px(t) + 6:.1f}" y="{40 + 40 * i}" width="{len(label) * 20 + 12}" height="30" fill="#000" opacity=".82"/><text x="{px(t) + 12:.1f}" y="{62 + 40 * i}" fill="{color}" font-family="\'Press Start 2P\'" font-size="20">{label}</text>'
    return f'''<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}">
<rect x="50" y="30" width="{w - 70}" height="{h - 74}" fill="#0a0910" stroke="#3a3946" stroke-width="3"/>
{bars}{marks}
<text x="54" y="{h - 14}" fill="#a9a4b3" font-family="Barlow Condensed" font-weight="600" font-size="24">acknowledged commits per 10 s  ·  seconds around the attack</text>
</svg>'''


# ------------------------------------------------------------------------------------------------ slides
def s1() -> str:
    return slide(1, scene("arena", 11) + hud(100, 100) + f'''
<div class="tag" style="left:64px;top:160px">PGCONF.EU 2026 · VALENCIA · LIGHTNING TALK</div>
<div style="position:absolute;left:70px;top:540px">{emblem_raiden(320)}</div>
<div style="position:absolute;right:70px;top:540px">{emblem_crash(320)}</div>
<div class="layer" style="display:flex;flex-direction:column;align-items:center;padding-top:208px">
  <div class="gold" style="font-size:212px">ROUND 1…</div>
  <div class="blood" style="font-size:296px;margin-top:6px">FIGHT!</div>
</div>
<div style="position:absolute;left:0;right:0;bottom:66px;text-align:center">
  <div class="teko hl" style="font-size:96px;color:var(--bone);text-shadow:0 5px 0 #000;letter-spacing:.06em">pg_crash <span style="color:var(--blood2)">vs</span> r<span style="color:var(--volt)">AI</span>den</div>
</div>
{caption('the AI DBA that fights back <em>and gets stronger</em> · <em>@kloba</em>')}''')


def stat_row(label: str, value: str, pct: int, cls: str = "") -> str:
    fill = "linear-gradient(180deg,#e6fbff,#59d5ff 50%,#1b6fd6)" if cls == "v" else "linear-gradient(180deg,#ffb0a8,#e4151b 55%,#7d070c)"
    return f'''<div style="display:flex;align-items:center;gap:18px;height:64px;white-space:nowrap">
<div class="teko" style="width:258px;font-size:35px;color:var(--dim);letter-spacing:.04em">{label}</div>
<div style="position:relative;width:190px;height:24px;border:3px solid #000;background:#0b0a10;transform:skewX(-18deg);box-shadow:0 0 0 2px #4a4858"><i style="position:absolute;left:0;top:0;bottom:0;width:{pct}%;background:{fill}"></i></div>
<div class="teko" style="font-size:38px;color:var(--bone)">{value}</div></div>'''


def s2() -> str:
    left = f'''<div class="panel" style="left:70px;top:290px;width:770px;height:650px">
<div style="position:absolute;left:32px;top:40px">{emblem_raiden(190)}</div>
<div style="position:absolute;left:244px;top:34px"><div class="ice" style="font-size:112px">rAIden</div><div class="teko" style="font-size:40px;color:var(--volt2);letter-spacing:.06em;margin-top:8px;white-space:nowrap">autonomous on-call DBA</div></div>
<div style="position:absolute;left:36px;top:262px;right:16px">
{stat_row("wakes up in", sec(R["crash"]["wake"]), 92, "v")}{stat_row("runbooks", str(D["skills"]), 70, "v")}{stat_row("guardrail tests", str(D["selftest"]), 100, "v")}{stat_row("sleep needed", "0 h", 0, "v")}
<div class="teko" style="font-size:37px;color:var(--gold);margin-top:22px;letter-spacing:.03em;white-space:nowrap">special move: root cause + <span style="color:#fff">1</span> restart</div></div></div>'''
    right = f'''<div class="panel" style="left:1080px;top:290px;width:770px;height:650px">
<div style="position:absolute;left:32px;top:40px">{emblem_crash(190)}</div>
<div style="position:absolute;left:244px;top:46px"><div class="blood" style="font-size:76px">pg_crash</div><div class="teko" style="font-size:40px;color:#ffb3a8;letter-spacing:.06em;margin-top:16px;white-space:nowrap">chaos-monkey extension</div></div>
<div style="position:absolute;left:36px;top:262px;right:16px">
{stat_row("signal every", "4 s", 88)}{stat_row("target", "random PID", 100)}{stat_row("signals", "9 · 15 · 3", 75)}{stat_row("mercy", "none", 0)}
<div class="teko" style="font-size:37px;color:var(--gold);margin-top:22px;letter-spacing:.03em;white-space:nowrap">special move: SIGKILL, random target</div></div></div>'''
    return slide(2, scene("arena", 21) + hud(100, 100) + f'''
<div class="layer" style="display:flex;justify-content:center;padding-top:158px"><div class="gold" style="font-size:112px">choose your fighter</div></div>
{left}{right}
<div class="layer" style="display:flex;align-items:center;justify-content:center;padding-top:170px"><div class="blood" style="font-size:150px">VS</div></div>
{caption('pg_crash is CYBERTEC\'s open-source chaos extension · <em>rAIden is this talk</em>')}''')


def move(n: int, name: str, sub: str, combo: str, color: str) -> str:
    return f'''<div class="panel" style="left:{60 + (n - 1) * 360}px;top:250px;width:340px;height:372px">
<div class="num">{n}</div>
<div class="gold" style="position:absolute;left:26px;top:76px;font-size:66px">{name}</div>
<div style="position:absolute;left:26px;top:162px;right:18px;font-size:29px;line-height:1.1;color:var(--bone);font-weight:600">{sub}</div>
<div style="position:absolute;left:22px;bottom:22px">{combo}</div></div>'''


# real refusals from the first full run (results/RUN1-HUNG-POSTMASTER.md), in the order the AI escalated
RUN1_DENIALS = [
    ("sudo-scope", "sudo pg_ctlcluster 18 main stop -m immediate"),
    ("pg-ctl-prod", "sudo -u postgres pg_ctl … stop -m immediate"),
    ("systemctl-verb", "systemctl kill --signal=KILL postgresql@18-main"),
]


def s3() -> str:
    denials = "".join(f'<span class="F">DENIED</span> <span class="d">[{rule}]</span>\n  {esc(cmd)}\n' for rule, cmd in RUN1_DENIALS)
    k = lambda t, c="": f'<span class="kbd {c}">{t}</span>'
    moves = "".join([
        move(1, "watch", "tails the Postgres <b>jsonlog</b> 24/7; a pg_isready heartbeat catches silent outages", k("↓") + k("→") + k("P", "r"), "var(--gold)"),
        move(2, "triage", f"classifies lines, <b>debounces</b> storms: {D['storm_lines']} crash lines = <b>1 incident</b>", k("↓") + k("↓") + k("K", "y"), "var(--gold)"),
        move(3, "fight", f"tool-calling loop · GPT-5.5 · <b>{D['skills']} skills</b> (runbooks) · bash, files, probes", k("←") + k("→") + k("P", "r"), "var(--gold)"),
        move(4, "verify", "the ledger <b>oracle</b>: every commit the app was told OK about must still be there", k("↑") + k("↑") + k("K", "y"), "var(--gold)"),
        move(5, "evolve", "writes a <b>new check</b>, a <b>learned skill</b> and lessons. Stronger next round", k("↓") + k("↑") + k("P", "b"), "var(--volt)"),
    ])
    return slide(3, scene("volt", 31) + hud(100, 100) + f'''
<div class="layer" style="display:flex;justify-content:center;padding-top:150px"><div class="gold" style="font-size:84px">how rAIden fights</div></div>
<div style="position:absolute;inset:0">{moves}</div>
<div class="panel" style="left:60px;top:644px;width:1800px;height:290px">
<h3 style="position:absolute;left:40px;top:22px;color:var(--toxic)">the referee: guardrails</h3>
<div style="position:absolute;left:40px;top:76px;width:880px;font-size:30px;line-height:1.3;font-weight:600">
<span class="chip r">no pkill</span><span class="chip r">no pg_resetwal</span><span class="chip r">never fsync=off</span><br>
<span class="chip r">no DROP on app data</span><span class="chip r">no network</span><br>
<span class="chip g">restart budget 6/h</span><span class="chip g">every call audited</span><br>
<span class="chip g">writes confined</span><span class="chip g">{D['selftest']}/{D['selftest']} tests pass</span></div>
<div style="position:absolute;left:960px;top:24px;bottom:24px;width:2px;background:#2b2a36"></div>
<h3 style="position:absolute;left:1000px;top:22px;color:#ff9a8a">the AI asked for bigger hammers</h3>
<div class="log" style="left:1000px;top:74px;font-size:22px;line-height:1.3"><span class="d"># real refusals from the first full run</span>
{denials}</div></div>
{caption(f'custom harness: <em>~{loc():,} lines of Python</em>, not an SDK · GPT-5.5 on Azure OpenAI · no API keys')}''')


def trow(y: int, t: str, color: str, text: str) -> str:
    return f'<div class="row" style="top:{y}px"><div class="t">{t}</div><div class="d" style="background:{color};color:{color}"></div><div class="x">{text}</div></div>'


def s4() -> str:
    c = R["crash"]
    tl = (
        f'<div style="position:absolute;left:186px;top:62px;width:6px;height:262px;background:linear-gradient(180deg,#e4151b,#ffd36b 50%,#7dff6a)"></div>'
        + trow(34, "T+0 s", "#ff4a3a", "a “temporary” experiment lands in <b>conf.d</b>")
        + trow(124, f"T+{c['wake']:.0f} s", "#ffd36b", f"rAIden wakes: <b>{c['incidents']} incident{'' if c['incidents'] == 1 else 's'}</b>, not {D['storm_lines']} log lines")
        + trow(214, f"T+{c['clue']:.0f} s", "#ffd36b", "<b>crash worker</b> found in shared_preload_libraries")
        + trow(304, f"T+{c['ok']:.0f} s", "#7dff6a", "oracle green: <b>0 commits lost</b>")
    )
    return slide(4, scene("arena", 41) + hud(100, 9, 0, 0, "74", r_hurt=True) + f'''
<div class="tag" style="left:64px;top:160px">ROUND 1 · PG_CRASH STORM</div>
<div class="panel" style="left:60px;top:200px;width:930px;height:430px"><div style="position:absolute;left:24px;right:24px;top:30px">{tl}</div></div>
<div class="panel" style="left:1020px;top:200px;width:840px;height:430px"><div style="position:absolute;left:18px;top:16px">{ack_chart("crash", 804, 398)}</div></div>
<div class="layer" style="display:flex;justify-content:center;padding-top:650px"><div class="blood" style="font-size:236px">finish him!</div></div>
{caption('a crash every ~4 s. the culprit was <em>in the config all along</em>')}''')


def s5() -> str:
    lines = "".join(
        f'<span class="{k}">{"FATAL:  " if k == "F" else "LOG:    "}</span>{esc(m)}\n' for k, m in D["log_lines"][:6]
    )
    return slide(5, scene("arena", 51) + hud(100, 0, 1, 0, "58", r_hurt=True) + f'''
<div class="layer" style="display:flex;justify-content:center;padding-top:150px"><div class="blood" style="font-size:196px">fatality</div></div>
<div class="panel" style="left:60px;top:372px;width:960px;height:300px"><div class="log" style="left:30px;top:30px;font-size:21px;line-height:1.55"><span class="d"># the real Postgres log from the lab VM</span>
{lines}</div></div>
<div class="panel" style="left:1050px;top:372px;width:810px;height:300px"><div class="log" style="left:26px;top:30px;font-size:21px;line-height:1.55"><span class="d"># conf.d/50-experiment.conf</span>
<span class="E">-</span> shared_preload_libraries = 'pg_stat_statements,<span class="F">pg_crash</span>'
<span class="g">+</span> shared_preload_libraries = 'pg_stat_statements'
<span class="d"># the file was backed up first</span>
<span class="d"># validated with postgres -C, then ONE restart</span></div></div>
<div style="position:absolute;left:60px;top:700px;width:1800px">
<span class="chip g" style="font-size:32px">{R['crash']['restarts']} restart{'' if R['crash']['restarts'] == 1 else 's'} (budget 6/h)</span><span class="chip g" style="font-size:32px">{sec(R['crash']['ok'])} to a green oracle</span><span class="chip g" style="font-size:32px">{D['final']['lost']} commits lost</span></div>
<div style="position:absolute;left:60px;top:790px;width:1800px;font-size:46px;line-height:1.15;color:var(--bone);font-weight:600">The Postgres log already prints <span style="color:#ff4a3a">FATAL:</span>.<br>rAIden is just the first thing that <i>reads</i> it at 3 a.m.</div>
{caption('finishing move: <em>remove the preload library</em>, restart once, verify')}''')


def s6() -> str:
    t = R["toast"]
    te = D.get("toast_error") or "unexpected chunk number 2 (expected 1) for toast value 90674 in pg_toast_90666"
    te_a, _, te_b = te.partition(" for toast value")
    chunks = "".join(
        f'<div style="width:118px;height:90px;border:4px solid {"#ff4a3a" if i == 1 else "#8a5a1a"};background:{"repeating-linear-gradient(45deg,#3a0d10,#3a0d10 8px,#1a0507 8px,#1a0507 16px)" if i == 1 else "linear-gradient(#ffe7a8,#e8b25a)"};display:flex;align-items:center;justify-content:center;font-family:Press Start 2P;font-size:20px;color:{"#ff4a3a" if i == 1 else "#4a2a08"}">{"✕" if i == 1 else i}</div>'
        for i in range(5)
    )
    return slide(6, scene("toast", 61) + hud(100, 60, 1, 0, "81", r_hurt=True) + f'''
<div class="tag" style="left:64px;top:160px">ROUND 2 · TOAST</div>
<div class="layer" style="padding:196px 0 0 64px"><div class="gold" style="font-size:200px">toasty!</div></div>
<div style="position:absolute;left:64px;top:430px;font-size:46px;line-height:1.1;font-weight:600;color:var(--bone)"><b style="color:var(--gold)">T</b>he <b style="color:var(--gold)">O</b>versized-<b style="color:var(--gold)">A</b>ttribute <b style="color:var(--gold)">S</b>torage <b style="color:var(--gold)">T</b>echnique</div>
<div style="position:absolute;left:1000px;top:190px;width:860px;font-size:42px;line-height:1.16;font-weight:600"><span class="chip g" style="font-size:30px">4 rows damaged</span><br>
rAIden: <b style="color:var(--gold)">PITR side-clone</b> &rarr; verified payloads &rarr; keyed UPDATE &rarr; <b style="color:var(--toxic)">amcheck clean</b><br>
<span style="color:var(--gold)">{sec(t['clue'])}</span> to the clue · <span style="color:var(--toxic)">{sec(t['ok'])}</span> to a green oracle</div>
<div class="panel" style="left:60px;top:520px;width:1180px;height:330px">
<div class="teko" style="position:absolute;left:34px;top:24px;font-size:38px;color:var(--gold)">a fat value lives out-of-line, in pg_toast chunks</div>
<div style="position:absolute;left:34px;top:90px;display:flex;gap:14px">{chunks}</div>
<div class="log" style="left:34px;top:204px;font-size:25px"><span class="E">ERROR:</span>  {te_a}
        for toast value{te_b}</div></div>
<div style="position:absolute;right:26px;bottom:70px;transform:rotate(-6deg)">{toast_char(340)}</div>
<div style="position:absolute;right:360px;bottom:400px;transform:rotate(-4deg);background:#fff;border:5px solid #000;border-radius:26px;padding:12px 28px 4px;box-shadow:0 6px 0 #000"><div class="gold" style="font-size:80px">toasty!</div></div>
<svg style="position:absolute;right:318px;bottom:372px" width="90" height="60" viewBox="0 0 90 60"><path d="M0,0 L70,0 L52,56 Z" fill="#000" transform="translate(4,6)"/><path d="M6,0 L66,0 L50,48 Z" fill="#fff"/></svg>
{caption('four chunks eaten. rAIden <em>brought them back</em> from a time machine')}''')


def s7() -> str:
    p = R["pitr"]
    return slide(7, scene("pit", 71) + hud(100, 40, 2, 0, "42", r_hurt=True) + f'''
<div class="tag" style="left:64px;top:160px">ROUND 3 · THE PIT</div>
<div class="layer" style="display:flex;justify-content:center;padding-top:150px"><div class="gold" style="font-size:176px">the pit<span style="color:#ff4a3a">R</span></div></div>
<div class="teko" style="position:absolute;left:0;right:0;top:330px;text-align:center;font-size:50px;color:var(--volt2);letter-spacing:.08em">P<span style="color:var(--gold)">oint</span>-<span style="color:var(--gold)">I</span>n-<span style="color:var(--gold)">T</span>ime <span style="color:var(--gold)">R</span>ecovery</div>
<div class="panel" style="left:60px;top:410px;width:1800px;height:440px">
<svg style="position:absolute;left:0;top:0" width="1800" height="440" viewBox="0 0 1800 440">
 <defs><linearGradient id="wal" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#59d5ff"/><stop offset="1" stop-color="#1b6fd6"/></linearGradient>
 <marker id="arrG" markerUnits="userSpaceOnUse" markerWidth="30" markerHeight="30" refX="22" refY="15" orient="auto"><path d="M0,2 L28,15 L0,28 Z" fill="#ffd36b"/></marker>
 <marker id="arrB" markerUnits="userSpaceOnUse" markerWidth="30" markerHeight="30" refX="22" refY="15" orient="auto"><path d="M0,2 L28,15 L0,28 Z" fill="#59d5ff"/></marker></defs>
 <text x="90" y="86" fill="#a9d8ff" font-family="Teko" font-weight="700" font-size="40" letter-spacing="2">WAL ARCHIVE: every commit, in order</text>
 <rect x="90" y="160" width="980" height="16" rx="8" fill="url(#wal)"/><rect x="1090" y="160" width="640" height="16" rx="8" fill="#5a1414"/>
 <g transform="translate(90,168)"><circle r="28" fill="#7dff6a" stroke="#000" stroke-width="5"/><text y="9" text-anchor="middle" font-family="'Press Start 2P'" font-size="22" fill="#000">B</text></g>
 <text x="64" y="236" fill="#c9ffbf" font-family="Teko" font-weight="700" font-size="38">base backup</text>
 <path d="M1042,112 L1042,206" stroke="#ffd36b" stroke-width="6"/><path d="M1042,112 L1100,112 L1090,128 L1100,144 L1042,144 Z" fill="#ffd36b" stroke="#000" stroke-width="3"/>
 <text x="1030" y="132" text-anchor="end" fill="#ffd36b" font-family="Teko" font-weight="700" font-size="38">stop here: just before the DROP</text>
 <g transform="translate(1118,168)"><circle r="34" fill="#ff4a3a" stroke="#000" stroke-width="5"/><text y="12" text-anchor="middle" font-size="34" fill="#000">☠</text></g>
 <text x="1172" y="226" fill="#ffb3a8" font-family="Teko" font-weight="700" font-size="38">DROP TABLE entries</text>
 <text x="1400" y="140" text-anchor="middle" fill="#ff7a6a" font-family="Teko" font-weight="700" font-size="38">the pit: no table, app erroring</text>
 <path d="M1690,146 C1600,40 1180,24 1060,100" fill="none" stroke="#ffd36b" stroke-width="6" stroke-dasharray="3 12" stroke-linecap="round" marker-end="url(#arrG)"/>
 <text x="1390" y="50" text-anchor="middle" fill="#ffd36b" font-family="'Press Start 2P'" font-size="26">REWIND</text>
 <rect x="300" y="280" width="640" height="130" rx="8" fill="#0b1626" stroke="#59d5ff" stroke-width="4"/>
 <text x="620" y="334" text-anchor="middle" fill="#e6fbff" font-family="Teko" font-weight="700" font-size="44">side clone (port 5433)</text>
 <text x="620" y="378" text-anchor="middle" fill="#a9d8ff" font-family="Teko" font-weight="600" font-size="34">base backup + WAL, stopped at the target</text>
 <path d="M1042,212 C1000,250 960,262 900,282" fill="none" stroke="#ffd36b" stroke-width="5" marker-end="url(#arrG)"/>
 <path d="M946,345 L1096,345" stroke="#59d5ff" stroke-width="6" marker-end="url(#arrB)"/>
 <rect x="1110" y="280" width="620" height="130" rx="8" fill="#0b2610" stroke="#7dff6a" stroke-width="4"/>
 <text x="1420" y="334" text-anchor="middle" fill="#e6ffe0" font-family="Teko" font-weight="700" font-size="44">prod: the table is back</text>
 <text x="1420" y="378" text-anchor="middle" fill="#c9ffbf" font-family="Teko" font-weight="600" font-size="34">dump, restore, owner + indexes</text>
</svg></div>
<div style="position:absolute;left:60px;top:880px;width:1800px"><span class="chip g" style="font-size:32px">xid found with pg_waldump</span><span class="chip g" style="font-size:32px">{sec(p['clue'])} to the clue</span><span class="chip g" style="font-size:32px">{sec(p['ok'])} to a green oracle</span><span class="chip g" style="font-size:32px">0 commits lost</span></div>
{caption('the pit is only a <em>fatality</em> if you have no backups. and a pager')}''')


def bomb_timeline(b: dict) -> str:
    fuse = b.get("fuse") or 300
    wake, defused = b.get("wake"), b.get("defused")
    rows = [
        ("T+0 s", "#ff4a3a", "experiment file back in <b>conf.d</b> + reload; restart later"),
        (f"T+{wake:.0f} s", "#ffd36b", "rAIden's <b>own check</b>: the next restart would load <b>pg_crash</b>"),
        (f"T+{defused:.0f} s", "#7dff6a", "file fixed, config reloaded: <b>no restart needed</b>"),
        (f"T+{fuse} s", "#7dff6a", "the scheduled restart fires: <b>nothing happens</b>"),
    ]
    tl = '<div style="position:absolute;left:186px;top:60px;width:6px;height:236px;background:linear-gradient(180deg,#e4151b,#ffd36b 45%,#7dff6a)"></div>'
    return tl + "".join(trow(30 + i * 80, t, c, x) for i, (t, c, x) in enumerate(rows))


def s8() -> str:
    arts = D["artifacts"]
    chip = lambda t: f'<div class="chip g" style="font-size:27px;display:block;width:max-content;max-width:700px">+ {esc(t)}</div>'
    skills = [x for x in arts if x.startswith("learned-")]
    checks = [x for x in arts if x.startswith("checks.d/")]
    other = [x for x in arts if x not in skills and x not in checks]
    nums = sorted(int(m.group(1)) for x in checks if (m := re.match(r"checks\.d/(\d+)-", x)))
    rows = "".join(chip(x) for x in skills[:4] + other[:2])
    if checks:
        rows += chip(f"{len(checks)} patrol checks (checks.d/{nums[0]}-{nums[-1]})" if nums else f"{len(checks)} patrol checks")
    b = R["bomb"]
    if b.get("prevented") and b.get("defused") is not None and b.get("wake") is not None:
        right = f'''<div class="panel" style="left:850px;top:370px;width:1010px;height:540px"><h3 style="position:absolute;left:34px;top:26px;color:var(--gold)">rematch: the time bomb</h3>
<div style="position:absolute;left:0;right:20px;top:96px">{bomb_timeline(b)}</div>
<div style="position:absolute;left:34px;top:450px;width:950px"><span class="chip g" style="font-size:30px">{b.get("crash_lines", 0)} crashes</span><span class="chip g" style="font-size:30px">{b.get("restarts", 0)} restarts by rAIden</span><span class="chip g" style="font-size:30px">0 commits lost</span></div></div>'''
    else:
        c, rm, b2 = R["crash"], R["bomb"], R["boss2"]
        vmax = max(x for x in (c["ok"], rm.get("ok") or 0, b2.get("ok") or 0)) * 1.05

        def bar(label: str, v: float | None, color: str) -> str:
            w = 6 if v is None else max(6, v / vmax * 380)
            return f'''<div style="display:flex;align-items:center;gap:16px;height:62px;white-space:nowrap"><div class="teko" style="width:250px;font-size:36px;color:var(--bone)">{label}</div>
<div style="width:{w}px;height:30px;background:{color};border:3px solid #000;box-shadow:0 0 0 2px #4a4858;transform:skewX(-18deg);flex:none"></div><div class="teko" style="font-size:40px;color:var(--gold)">{sec(v)}</div></div>'''

        right = f'''<div class="panel" style="left:850px;top:370px;width:1010px;height:540px"><h3 style="position:absolute;left:34px;top:26px;color:var(--gold)">time to a green oracle</h3>
<div style="position:absolute;left:34px;top:100px;right:20px">{bar("round 1", c["ok"], "linear-gradient(#ffb0a8,#e4151b)")}{bar("time bomb", rm.get("ok"), "linear-gradient(#e6fbff,#59d5ff)")}{bar("new move", b2.get("ok"), "linear-gradient(#e6fbff,#59d5ff)")}</div></div>'''
    return slide(8, scene("volt", 81) + hud(100, 0, 2, 0, "00") + f'''
<div class="tag" style="left:64px;top:160px">AFTER THE FIGHT</div>
<div class="layer" style="padding:190px 0 0 64px"><div class="ice" style="font-size:128px">level up</div></div>
<div style="position:absolute;left:1000px;top:230px;width:860px;font-size:40px;line-height:1.12;font-weight:600;color:var(--bone)">rAIden wrote itself <b style="color:var(--gold)">{len(D["artifacts"])} new moves</b> during the fight.<br>one of them defused a <b style="color:#ff9a8a">time bomb</b></div>
<div class="panel" style="left:60px;top:370px;width:760px;height:540px"><h3 style="position:absolute;left:34px;top:26px;color:var(--volt2)">unlocked moves (self-written)</h3>
<div style="position:absolute;left:34px;top:92px;right:20px">{rows}
<div style="margin-top:16px;font-size:31px;line-height:1.15;color:var(--bone);font-weight:600">+ <b>lessons.md</b>: read before the next fight<br>+ every artifact is validated: broken ones are <span style="color:#ff9a8a">repaired or quarantined</span></div></div></div>
{right}
{caption('every fight leaves <em>a new check, a new runbook, a new lesson</em>')}''')


def s9() -> str:
    f = D["final"]
    lab = lambda t: f'<div class="teko" style="font-size:36px;color:var(--dim);letter-spacing:.08em;margin-top:14px">{t}</div>'
    return slide(9, scene("gold", 91) + hud(100, 0, 2, 0, "00") + f'''
<div class="layer" style="display:flex;flex-direction:column;align-items:center;padding-top:170px">
<div class="gold" style="font-size:235px">flawless</div><div class="gold" style="font-size:235px;margin-top:-6px">victory</div></div>
<div style="position:absolute;left:0;right:0;top:700px;display:flex;justify-content:center;gap:100px;align-items:flex-start">
<div style="text-align:center"><div class="gold" style="font-size:118px">{f["acked"]:,}</div>{lab("acknowledged commits")}</div>
<div style="text-align:center"><div class="green" style="font-size:118px">{f["lost"]}</div>{lab(f"lost · {f['corrupt']} corrupt · {f['unreadable']} unreadable")}</div>
<div style="text-align:center"><div class="gold" style="font-size:118px">{D["rounds_won"]}/{D["rounds_total"]}</div>{lab("rounds won")}</div>
<div style="text-align:center"><div class="gold" style="font-size:118px">OK</div>{lab("pg_amcheck")}</div></div>
{caption('the referee: a <em>client-side ledger</em> of every commit the app was told OK for')}''')


def s10() -> str:
    qr = (HERE / "qr.svg").read_text() if (HERE / "qr.svg").exists() else ""
    return slide(10, scene("arena", 101) + hud(100, 100, 2, 0, "09") + f'''
<div class="layer" style="padding:176px 0 0 70px"><div class="gold" style="font-size:208px">continue?</div></div>
<div style="position:absolute;left:78px;top:440px"><div class="hud-font" style="font-size:28px;color:var(--gold);text-shadow:0 4px 0 #000">INSERT COIN ▸</div>
<div class="mono" style="font-size:52px;color:#fff;margin-top:14px;font-weight:700">github.com/kloba/r<span class="ai" style="color:var(--volt)">AI</span>den</div></div>
<div style="position:absolute;left:70px;top:610px;width:1130px;font-size:38px;line-height:1.2;font-weight:600">
<div style="margin-bottom:8px">&#9632; <b>pg_crash</b> by CYBERTEC: open-source chaos, used with love and fear</div>
<div style="margin-bottom:8px">&#9632; the evolve loop: ideas from <b>Hermes Agent</b>, <b>ECC</b>, <b>nanobot</b></div>
<div style="margin-bottom:8px">&#9632; built and fought on Azure, <b>deleted after the fight</b></div>
<div>&#9632; oracle, guardrails and every number here: <b>in the repo</b></div></div>
<div style="position:absolute;right:90px;top:250px;width:520px;height:520px;background:#fff;padding:26px;border:8px solid #000;box-shadow:0 0 0 6px var(--gold)">{qr}</div>
<div class="teko" style="position:absolute;right:90px;top:800px;width:520px;text-align:center;font-size:44px;color:var(--gold);letter-spacing:.08em">scan to rematch</div>
{caption('thank you · <em>no DBAs were harmed</em> (they got some sleep)')}''')


SLIDES = [s1, s2, s3, s4, s5, s6, s7, s8, s9, s10]


def build_html() -> str:
    css = (HERE / "fonts" / "fonts.css").read_text().replace("url('", "url('fonts/") + (HERE / "deck.css").read_text()
    body = convert_headlines("\n".join(fn() for fn in SLIDES))
    body = re.sub(r"(?<![>\w/])rAIden(?![\w<])", RN, body)
    js = "const q=new URLSearchParams(location.search).get('s');if(q){document.body.classList.add('preview');document.getElementById('s'+q).classList.add('on')}"
    return f'<!doctype html><html lang="en"><meta charset="utf-8"><title>pg_crash vs rAIden - PGConf.EU 2026</title><style>{css}</style><body>{body}<script>{js}</script></html>'


def write_script() -> None:
    """talk/script.md = talk/script.tmpl.md with the numbers of the recorded battle (only for real data)."""
    tmpl = (HERE / "script.tmpl.md").read_text()
    c, t, p, b = R["crash"], R["toast"], R["pitr"], R["bomb"]
    f = D["final"]
    boss_ok = f"{R['boss2']['ok']:.0f}" if R["boss2"].get("ok") else "no time at all"
    spoken = tmpl.split("## If you are running late")[0]
    words = len(" ".join(re.findall(r'"([^"]+)"', spoken.split("---", 1)[1])).split())
    vals = dict(
        wake1=f"{c['wake']:.0f}", clue1=f"{c['clue']:.0f}", ok1=f"{c['ok']:.0f}", clue2=f"{t['clue']:.0f}", clue3=f"{p['clue']:.0f}",
        bomb_wake=f"{b['wake']:.0f}", bomb_defused=f"{(b.get('defused') or 0):.0f}", fuse_min=f"{(b.get('fuse') or 300) / 60:.0f}",
        acked=f"{f['acked']:,}", lost=f["lost"], rounds_won=D["rounds_won"], rounds_total=D["rounds_total"],
        skills=D["skills"], selftest=D["selftest"], n_art=len(D["artifacts"]), tokens_m=f"{D['tokens_total'] / 1e6:.1f}", minutes=D["duration_min"],
        storm=D['storm_lines'], boss_ok=boss_ok, words=words, minutes_spoken=f"{words / 135:.1f}",
    )
    out = HERE / ("script.md" if REAL else "script.SAMPLE-DATA.md")
    out.write_text(tmpl.format(**vals))
    print(f"wrote {out.name} ({words} spoken words, about {words / 135:.1f} min)")


def main() -> None:
    if not OUTLINES:
        print("WARNING: fontTools/brotli missing -> CSS gradient headlines; some PDF viewers (Preview) flatten them. pip install -r talk/requirements.txt")
    if not REAL:
        print("WARNING: talk/data.json is missing or the battle is not finished: this deck contains SAMPLE numbers (not for sending)")
        if "--final" in sys.argv:
            sys.exit("refusing --final with sample/partial data")
    if REAL and not D.get("flawless"):
        print("WARNING: the recorded battle was NOT flawless, but slide 9 says FLAWLESS VICTORY: fix the story before sending")
        if "--final" in sys.argv:
            sys.exit("refusing --final: battle not flawless")
    write_script()
    out = HERE / "slides.html"
    out.write_text(build_html())
    print(f"wrote {out} ({out.stat().st_size // 1024} KB)")
    if "--html" in sys.argv:
        return
    pdf = HERE / ("rAIden-pgconf-2026.pdf" if REAL else "rAIden-pgconf-2026.SAMPLE-DATA.pdf")
    subprocess.run(
        [CHROME, "--headless=new", "--disable-gpu", "--no-pdf-header-footer", "--virtual-time-budget=8000", f"--print-to-pdf={pdf}", f"file://{out}"],
        check=True, capture_output=True,
    )
    print(f"wrote {pdf} ({pdf.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
