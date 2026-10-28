"""Grafika otwierająca README w stylu tapety PiCamera: python hero.py pl|en"""
import sys
from PIL import Image, ImageDraw, ImageFilter
import vhs

I = vhs.REPO + "/docs/images/"
LANG = sys.argv[1] if len(sys.argv) > 1 else "pl"
W, H = 2400, 1200
bg = vhs.tlo(W, H, seed=7, smugi=10, gwiazdki=0)
for x, y in ((150, 140), (2260, 1080), (1000, 1110)):
    vhs.gwiazdka(bg, x, y, 14)


def cien(box, r, blur=36, off=(0, 26), alfa=190):
    sh = Image.new("L", (W, H), 0)
    ImageDraw.Draw(sh).rounded_rectangle([box[0] + off[0], box[1] + off[1], box[2] + off[0], box[3] + off[1]], radius=r, fill=alfa)
    return sh.filter(ImageFilter.GaussianBlur(blur))


# ekran urządzenia w pikselowej ramce
scr = Image.open(I + "ekran_jazda.png").convert("RGB").resize((1100, 660), Image.LANCZOS)
bx, by = 1110, 250
pad = 30
box = (bx - pad, by - pad, bx + 1100 + pad, by + 660 + pad)
bg.paste((0, 0, 0), (0, 0), cien(box, 10))
d = ImageDraw.Draw(bg)
d.rectangle(box, fill=(22, 16, 26))
vhs.ramka(d, box, vhs.TEKST, 4, 14)
vhs.ramka(d, (box[0] + 12, box[1] + 12, box[2] - 12, box[3] - 12), vhs.RAMKA, 2, 8)
bg.paste(scr, (bx, by))

# telefon
ph = Image.open(I + "www_telefon.png").convert("RGB").crop((0, 0, 780, 1560)).resize((340, 680), Image.LANCZOS)
px, py = 1980, 440
pp = 16
pbox = (px - pp, py - pp, px + 340 + pp, py + 680 + pp)
bg.paste((0, 0, 0), (0, 0), cien(pbox, 40, 28, (0, 22), 220))
d = ImageDraw.Draw(bg)
d.rounded_rectangle(pbox, radius=40, fill=(12, 8, 14))
d.rounded_rectangle(pbox, radius=40, outline=vhs.TEKST, width=4)
m = Image.new("L", (340, 680), 0)
ImageDraw.Draw(m).rounded_rectangle([0, 0, 339, 679], radius=28, fill=255)
bg.paste(ph, (px, py), m)

# linie VHS na całości, ekranom delikatniej
ekrany = Image.new("L", (W, H), 0)
ImageDraw.Draw(ekrany).rectangle((bx, by, bx + 1100, by + 660), fill=255)
ImageDraw.Draw(ekrany).rectangle((px, py, px + 340, py + 680), fill=255)
bg = Image.composite(vhs.linie(bg, 4, 0.10), vhs.linie(bg, 4, 0.30), ekrany)

# logo i teksty
lg = vhs.logo_tablica(900)
bg.paste(lg, (90, 300), lg)
d = ImageDraw.Draw(bg)
y = 300 + lg.height + 70
if LANG == "pl":
    haslo = ("WIDEOREJESTRATOR, KTÓRY", "CZYTA TABLICE REJESTRACYJNE")
    spec = ("RASPBERRY PI 5 · HAILO-10H · IMX708 HDR", "RADAR 24 GHZ · WSZYSTKO OFFLINE")
else:
    haslo = ("A DASHCAM THAT READS", "LICENCE PLATES")
    spec = ("RASPBERRY PI 5 · HAILO-10H · IMX708 HDR", "24 GHZ RADAR · FULLY OFFLINE")
for t in haslo:
    d.text((128, y), t, font=vhs.font(76), fill=vhs.TEKST)
    y += 72
y += 46
d.rectangle((130, y, 160, y + 6), fill=vhs.MAGENTA)
d.rectangle((168, y, 186, y + 6), fill=vhs.CYJAN)
y += 34
for t in spec:
    d.text((130, y), t, font=vhs.font(52), fill=vhs.PRZYGASZONY)
    y += 54

bg = bg.resize((1600, 800), Image.LANCZOS)
bg.save(I + ("hero.png" if LANG == "pl" else "hero_en.png"), optimize=True)
print(bg.size)
