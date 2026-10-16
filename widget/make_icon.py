"""Draws ring.ico, the desktop shortcut's icon: the ring, about three-quarters full.
Run once: python make_icon.py
"""

import math
from pathlib import Path

from PIL import Image, ImageDraw

S = 1024
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

m, inset, rw = 24, 150, 130
green = (52, 199, 89, 255)
d.ellipse([m, m, S - m, S - m], fill=(28, 28, 31, 255), outline=(72, 72, 76, 255), width=14)
box = [inset, inset, S - inset, S - inset]
d.arc(box, 0, 360, fill=(55, 55, 58, 255), width=rw)
end = -90 + 360 * 0.72
d.arc(box, -90, end, fill=green, width=rw)
r = (S - 2 * inset) / 2 - rw / 2
for ang in (-90, end):
    x = S / 2 + r * math.cos(math.radians(ang))
    y = S / 2 + r * math.sin(math.radians(ang))
    d.ellipse([x - rw / 2, y - rw / 2, x + rw / 2, y + rw / 2], fill=green)

img.resize((256, 256), Image.LANCZOS).save(
    Path(__file__).with_name("ring.ico"),
    sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
)
print("ring.ico")
