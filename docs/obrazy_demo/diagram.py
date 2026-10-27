"""Diagram architektury: render SVG (diagram.js) na tle w stylu tapety PiCamera."""
import os, subprocess
from PIL import Image
import vhs

tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_diagram.png")
subprocess.run(["node", os.path.join(os.path.dirname(os.path.abspath(__file__)), "diagram.js"), tmp], check=True)
rys = Image.open(tmp).convert("RGBA")
os.remove(tmp)
tlo = vhs.tlo(rys.width, rys.height, seed=21, smugi=5, gwiazdki=0)
for x, y in ((60, 1500), (2340, 60)):
    vhs.gwiazdka(tlo, x, y, 10)
tlo.paste(rys, (0, 0), rys)
vhs.linie(tlo, 4, 0.18).save(vhs.REPO + "/docs/images/architektura.png", optimize=True)
print(tlo.size)
