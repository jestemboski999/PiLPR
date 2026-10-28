"""Grafika tytułowa w stylu malowanych szyb z szaf grających:
python hero_szafa.py pl|en <katalog z ekranami> <plik wyjściowy>"""
import math, random, sys
from PIL import Image, ImageDraw, ImageFilter, ImageFont
from scene_szafa import fajerwerk, gwiazda

LANG = sys.argv[1] if len(sys.argv) > 1 else "pl"
I = sys.argv[2] if len(sys.argv) > 2 else "../images/"
OUT = sys.argv[3] if len(sys.argv) > 3 else I + ("hero.png" if LANG == "pl" else "hero_en.png")
SERIF = "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf"
INTER = "/usr/share/fonts/opentype/inter/InterDisplay-ExtraBold.otf"
INTER_S = "/usr/share/fonts/opentype/inter/InterDisplay-SemiBold.otf"
W, H = 2400, 1200
random.seed(5)
bg = Image.new("RGB", (W, H), (6, 6, 9))
d = ImageDraw.Draw(bg)
# delikatny połysk szyby
pol = Image.new("L", (W, H), 0)
ImageDraw.Draw(pol).polygon([(0, 0), (900, 0), (300, H), (0, H)], fill=10)
bg.paste((255, 255, 255), (0, 0), pol.filter(ImageFilter.GaussianBlur(80)))
d = ImageDraw.Draw(bg)
for x, y in ((70, 120), (600, 60), (1040, 300), (60, 560), (1000, 980), (420, 1150), (1700, 120), (2330, 90), (2350, 320)):
    gwiazda(d, x, y, random.uniform(7, 12), 2)
for x, y, r, k in ((820, 150, 90, (240, 185, 58)), (960, 70, 54, (226, 85, 63)), (160, 1040, 70, (111, 195, 224)), (980, 1090, 60, (226, 85, 63)), (2300, 1110, 46, (240, 185, 58))):
    fajerwerk(d, x, y, r, k, sw=4)


def cien(box, r, blur=36, off=(0, 26), alfa=200):
    sh = Image.new("L", (W, H), 0)
    ImageDraw.Draw(sh).rounded_rectangle([box[0] + off[0], box[1] + off[1], box[2] + off[0], box[3] + off[1]], radius=r, fill=alfa)
    return sh.filter(ImageFilter.GaussianBlur(blur))


def chrom(box, r, w):
    """Ramka w kolorze chromu, jak listwy szafy grającej."""
    x0, y0, x1, y1 = box
    for i in range(w):
        t = i / max(1, w - 1)
        c = int(120 + 120 * math.sin(t * math.pi))
        d.rounded_rectangle([x0 + i, y0 + i, x1 - i, y1 - i], radius=max(2, r - i), outline=(c, c, min(255, c + 8)))


scr = Image.open(I + "ekran_jazda.png").convert("RGB").resize((1100, 660), Image.LANCZOS)
bx, by = 1110, 250
pad = 26
box = (bx - pad, by - pad, bx + 1100 + pad, by + 660 + pad)
bg.paste((0, 0, 0), (0, 0), cien(box, 18))
d = ImageDraw.Draw(bg)
d.rounded_rectangle(box, radius=18, fill=(14, 14, 16))
chrom(box, 18, 12)
bg.paste(scr, (bx, by))

ph = Image.open(I + "www_telefon.png").convert("RGB").crop((0, 0, 780, 1560)).resize((340, 680), Image.LANCZOS)
px, py = 1980, 440
pp = 16
pbox = (px - pp, py - pp, px + 340 + pp, py + 680 + pp)
bg.paste((0, 0, 0), (0, 0), cien(pbox, 40, 28, (0, 22), 230))
d = ImageDraw.Draw(bg)
d.rounded_rectangle(pbox, radius=40, fill=(12, 12, 14))
chrom(pbox, 40, 8)
m = Image.new("L", (340, 680), 0)
ImageDraw.Draw(m).rounded_rectangle([0, 0, 339, 679], radius=28, fill=255)
bg.paste(ph, (px, py), m)

# złoty napis z konturem, pochylony
f = ImageFont.truetype(SERIF, 250)
tw, th = 1000, 330
maska = Image.new("L", (tw, th), 0)
ImageDraw.Draw(maska).text((30, 20), "PiLPR", font=f, fill=255)
kontur = Image.new("L", (tw, th), 0)
ImageDraw.Draw(kontur).text((30, 20), "PiLPR", font=f, fill=255, stroke_width=14, stroke_fill=255)
skos = (1, 0.16, -20, 0, 1, 0)
maska = maska.transform((tw, th), Image.AFFINE, skos, Image.BICUBIC)
kontur = kontur.transform((tw, th), Image.AFFINE, skos, Image.BICUBIC)
zl = Image.new("RGB", (tw, th))
dz = ImageDraw.Draw(zl)
stops = [(0, (255, 240, 168)), (.42, (240, 185, 58)), (.55, (200, 131, 28)), (.75, (240, 185, 58)), (1, (255, 217, 106))]
for y in range(th):
    t = min(1, max(0, (y - 60) / 200))
    for (a, ca), (b, cb) in zip(stops, stops[1:]):
        if a <= t <= b:
            u = (t - a) / (b - a)
            dz.line([(0, y), (tw, y)], fill=tuple(int(ca[i] + (cb[i] - ca[i]) * u) for i in range(3)))
            break
tx, ty = 90, 230
bg.paste((0, 0, 0), (tx + 12, ty + 14), kontur)
bg.paste((42, 26, 6), (tx, ty), kontur)
bg.paste(zl, (tx, ty), maska)
hl = Image.new("L", (tw, th), 0)
ImageDraw.Draw(hl).text((30, 20), "PiLPR", font=f, fill=0, stroke_width=2, stroke_fill=255)
hl = hl.transform((tw, th), Image.AFFINE, skos, Image.BICUBIC)
bg.paste((255, 242, 191), (tx, ty), Image.eval(hl, lambda v: v // 2))

d = ImageDraw.Draw(bg)
if LANG == "pl":
    haslo = ("WIDEOREJESTRATOR, KTÓRY", "CZYTA TABLICE REJESTRACYJNE")
    spec = ("Raspberry Pi 5 · Hailo-10H · IMX708 HDR", "radar 24 GHz · wszystko offline")
else:
    haslo = ("A DASHCAM THAT READS", "LICENCE PLATES")
    spec = ("Raspberry Pi 5 · Hailo-10H · IMX708 HDR", "24 GHz radar · fully offline")
y = 600
for t in haslo:
    d.text((126, y + 4), t, font=ImageFont.truetype(INTER, 58), fill=(0, 0, 0), stroke_width=6, stroke_fill=(0, 0, 0))
    d.text((122, y), t, font=ImageFont.truetype(INTER, 58), fill=(244, 239, 226))
    y += 72
y += 40
for t in spec:
    d.text((122, y), t, font=ImageFont.truetype(INTER_S, 42), fill=(233, 195, 92))
    y += 56
bg.resize((1600, 800), Image.LANCZOS).save(OUT, optimize=True)
print(OUT)
