"""Renders the README images with the widget's own drawing code and demo data.

Run from the repo root after changing how the ring or its card looks:
    python docs/render_docs.py widget/widget.pyw docs
"""
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image, ImageDraw

widget_path, out = Path(sys.argv[1]), Path(sys.argv[2])
out.mkdir(parents=True, exist_ok=True)

# Loaded under its own one-instance name, so a running ring doesn't stop it.
source = widget_path.read_text(encoding="utf-8")
source = source.replace("ClaudeSessionLimitWidget", "ClaudeSessionLimitDocsRender")
w = types.ModuleType("ring_widget")
w.__file__ = str(widget_path)
exec(compile(source, str(widget_path), "exec"), w.__dict__)

NOW = datetime(2026, 10, 16, 14, 20).astimezone().timestamp()
RESETS = NOW + 2 * 3600 + 20 * 60
START = RESETS - w.WINDOW
WEEK_RESETS = NOW + 4 * 86400 + 3 * 3600


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def history(used_now):
    """A morning's use: slow at first, faster over the last hour."""
    pts, t = [], START + 10 * 60
    while t <= NOW:
        f = (t - START) / (NOW - START)
        pts.append([int(t * 1000), round(used_now * (0.55 * f + 0.45 * f ** 2.2))])
        t += 10 * 60
    pts.append([int(NOW * 1000), used_now])
    return pts


def view(used=72, weekly_used=36, context=41, working=True):
    usage = {
        "updatedAt": int(NOW * 1000),
        "limits": [
            {"kind": "five_hour", "percentUsed": used, "resetsAt": iso(RESETS)},
            {"kind": "seven_day", "percentUsed": weekly_used, "resetsAt": iso(WEEK_RESETS)},
        ],
        "history": history(used),
    }
    sessions = [{"working": working, "since": int((NOW - 83) * 1000), "updatedAt": int(NOW * 1000),
                 "doneAt": 0, "context": context}]
    return w.View(usage, sessions, NOW)


def backdrop(width, height, radius=36):
    """A dark, slightly lit desk with rounded corners."""
    bg = Image.new("RGBA", (width, height))
    top, bottom = (38, 40, 52), (18, 18, 24)
    d = ImageDraw.Draw(bg)
    for y in range(height):
        f = y / max(1, height - 1)
        d.line([(0, y), (width, y)], fill=tuple(int(a + (b - a) * f) for a, b in zip(top, bottom)) + (255,))
    mask = Image.new("L", (width, height), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, width - 1, height - 1], radius=radius, fill=255)
    bg.putalpha(mask)
    return bg


def ring_image(ring, v, frame=None, glow_color=None, glow=0.0, line=None):
    line, color = line or v.ring_line()
    base = ring.base(v.left, line, color, v.weekly_left, False)
    return ring.compose(base, glow_color, glow, frame, False)


def hero(lang, name, scale=2.0):
    w.LANG[0] = lang
    v = view()
    ring = w.Ring(int(w.BASE * scale))
    card = w.Card(scale).render(v, muted=False, volume=0.8)
    r = ring_image(ring, v, frame=7)
    pad, gap = int(40 * scale), int(28 * scale)
    W = pad * 2 + r.width + gap + card.width
    H = pad * 2 + max(r.height, card.height)
    img = backdrop(W, H, radius=int(18 * scale))
    rtl = lang == "ar"
    ring_x = pad + card.width + gap if rtl else pad
    card_x = pad if rtl else pad + r.width + gap
    img.alpha_composite(r, (ring_x, pad))
    img.alpha_composite(card, (card_x, pad))
    img.save(out / name, optimize=True)
    print(name, img.size)


def states(name, scale=1.5):
    w.LANG[0] = "en"
    ring = w.Ring(int(w.BASE * scale))
    shots = []
    for used, glow in ((22, 0), (74, 0), (93, 0.9)):
        v = view(used=used)
        color = w.color_for(v.left) if glow else None
        shots.append(ring_image(ring, v, glow_color=color, glow=glow))
    gap = int(18 * scale)
    W = sum(s.width for s in shots) + gap * (len(shots) - 1)
    img = Image.new("RGBA", (W, shots[0].height), (0, 0, 0, 0))
    x = 0
    for s in shots:
        img.alpha_composite(s, (x, 0))
        x += s.width + gap
    img.save(out / name, optimize=True)
    print(name, img.size)


def drain(name, scale=1.5):
    """The ring running down while Claude works: green, amber, then a pulsing red."""
    w.LANG[0] = "en"
    ring = w.Ring(int(w.BASE * scale))
    pad = int(14 * scale)
    S = ring.size + 2 * pad
    frames, count, hold = [], 96, 30
    for i in range(count + hold):
        t = i * 0.05
        f = min(1.0, i / count)
        used = round(38 + (94 - 38) * (f * f * (3 - 2 * f)))
        v = view(used=used)
        left = v.left
        line_color = w.SOFT if left > 30 else w.AMBER if left > 10 else w.RED
        line = (w.T("ring_renew", t=w.clock(RESETS)), line_color)
        glow_color, glow = None, 0.0
        if left <= 10:
            glow_color, glow = w.color_for(left), 0.25 + 0.75 * abs(1 - 2 * ((t * 2.5) % 1.0))
        frame = int((t % 1.2) / 1.2 * w.FRAMES)
        r = ring_image(ring, v, frame=frame, glow_color=glow_color, glow=glow, line=line)
        img = Image.new("RGBA", (S, S), (28, 29, 36, 255))
        img.alpha_composite(r, (pad, pad))
        frames.append(img.convert("RGB").convert("P", palette=Image.ADAPTIVE, colors=255))
    frames[0].save(out / name, save_all=True, append_images=frames[1:], duration=50, loop=0,
                   optimize=True, disposal=1)
    print(name, (S, S), len(frames), "frames")


hero("en", "card.png")
hero("ar", "card-ar.png")
states("states.png")
drain("ring.gif")
