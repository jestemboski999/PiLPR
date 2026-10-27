"""Składa rendery uchwytów (stlshot.js: s1.png, s2.png, s3.png) na tle w stylu tapety.
Użycie: python uchwyty.py <katalog z renderami>"""
import sys
from PIL import Image
import vhs

kat = sys.argv[1] if len(sys.argv) > 1 else "."
ims = [Image.open(f"{kat}/s{i}.png").convert("RGBA").crop((150, 50, 1050, 750)) for i in (1, 2, 3)]
W, H = sum(i.width for i in ims), ims[0].height
out = vhs.tlo(W, H, seed=31, smugi=4, gwiazdki=0)
for x, y in ((80, 80), (W - 90, H - 80)):
    vhs.gwiazdka(out, x, y, 12)
x = 0
for i in ims:
    out.paste(i, (x, 0), i)
    x += i.width
out = vhs.linie(out, 4, 0.18).resize((W * 2 // 3, H * 2 // 3), Image.LANCZOS)
out.save(vhs.REPO + "/docs/images/uchwyty_3d.png", optimize=True)
print(out.size)
