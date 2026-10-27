"""Wspólny styl grafik README: ten sam co tapeta PiCamera
(pi/local_resources/lpr_splash_pilpr_vhs.png) - ciemny fiolet, pikselowa
czcionka VT323, linie VHS, zakłócenia i gwiazdki."""
import os
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
FONT_DIR = "/home/admin/hailo-apps/local_resources/web/"
VT323 = FONT_DIR + "VT323-Regular.ttf"
TAPETA = REPO + "/pi/local_resources/lpr_splash_pilpr_vhs.png"

TLO = (15, 10, 18)
TLO_JASNE = (30, 21, 36)
TEKST = (228, 214, 224)
PRZYGASZONY = (142, 128, 140)
RAMKA = (92, 78, 96)
NIEBIESKI = (38, 56, 150)      # pole UE na tablicy
ZLOTY = (226, 190, 92)          # gwiazdki UE
MAGENTA = (176, 70, 170)
CYJAN = (110, 160, 214)
CZERWONY = (226, 92, 110)
ZIELONY = (128, 210, 150)


def hexc(c):
    return "#%02x%02x%02x" % c


def font(size):
    return ImageFont.truetype(VT323, size)


def tlo(w, h, seed=1, smugi=14, gwiazdki=4):
    """Tło jak na tapecie: winieta, poziome zakłócenia i gwiazdki."""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:h, 0:w]
    r = np.sqrt(((xx - w / 2) / (w / 1.6)) ** 2 + ((yy - h / 2) / (h / 1.3)) ** 2)
    k = np.clip(1 - r, 0, 1)[..., None]
    a = np.array(TLO, float)
    b = np.array(TLO_JASNE, float)
    img = Image.fromarray((a + (b - a) * k).astype(np.uint8), "RGB")
    nak = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(nak)
    s = max(1, round(w / 800))           # rozmiar „piksela” zakłóceń
    for _ in range(smugi):
        y = int(rng.uniform(0.05, 0.95) * h)
        x = int(rng.uniform(-0.1, 0.8) * w)
        dl = int(rng.uniform(0.08, 0.3) * w)
        while x < min(w, x + dl):
            kol = [MAGENTA, CYJAN, PRZYGASZONY][rng.integers(0, 3)]
            sz = int(rng.choice([1, 1, 2, 3])) * 2 * s
            alfa = int(rng.uniform(40, 150))
            d.rectangle([x, y, x + sz, y + 2 * s], fill=kol + (alfa,))
            x += sz + int(rng.integers(1, 6)) * 2 * s
            if rng.random() < 0.08:
                break
    img.paste(nak, (0, 0), nak)
    for _ in range(gwiazdki):
        gwiazdka(img, int(rng.uniform(0.03, 0.97) * w), int(rng.uniform(0.05, 0.95) * h), 5 * s)
    return img


def gwiazdka(img, x, y, r):
    """Pikselowy krzyżyk z poświatą, jak w rogach tapety."""
    pos = Image.new("L", img.size, 0)
    ImageDraw.Draw(pos).ellipse([x - 2 * r, y - 2 * r, x + 2 * r, y + 2 * r], fill=34)
    pos = pos.filter(ImageFilter.GaussianBlur(1.2 * r))
    img.paste(Image.new("RGB", img.size, TEKST), (0, 0), pos)
    d = ImageDraw.Draw(img)
    p = max(1, r // 3)
    d.rectangle([x - r, y - p // 2, x + r, y + p // 2], fill=(200, 186, 198))
    d.rectangle([x - p // 2, y - r, x + p // 2, y + r], fill=(200, 186, 198))
    d.rectangle([x - p, y - p, x + p, y + p], fill=(250, 240, 248))


def linie(img, okres=4, sila=0.22):
    """Linie VHS: co `okres` pikseli ciemniejszy pasek."""
    a = np.asarray(img.convert("RGB")).astype(np.float32)
    m = np.ones(a.shape[0], np.float32)
    m[okres - 1::okres] = 1 - sila
    m[okres - 2::okres] = 1 - sila / 2
    return Image.fromarray(np.clip(a * m[:, None, None], 0, 255).astype(np.uint8), "RGB")


def logo_tablica(szer):
    """Logo w kształcie tablicy wycięte z tapety, przezroczyste tło."""
    t = Image.open(TAPETA).convert("RGB")
    w, h = t.size
    t = t.crop((int(w * 0.248), int(h * 0.345), int(w * 0.752), int(h * 0.575)))
    a = np.asarray(t).astype(np.float32)
    jas = a.max(axis=2)
    alfa = np.clip((jas - 30) * 3.2, 0, 255).astype(np.uint8)
    rgba = Image.fromarray(np.dstack([a.astype(np.uint8), alfa]), "RGBA")
    return rgba.resize((szer, int(szer * t.height / t.width)), Image.LANCZOS)


def ramka(d, box, kolor=RAMKA, grubosc=2, schodek=6):
    """Prostokąt ze ściętymi pikselowo rogami."""
    x0, y0, x1, y1 = box
    s = schodek
    pts = [(x0 + s, y0), (x1 - s, y0), (x1, y0 + s), (x1, y1 - s), (x1 - s, y1),
           (x0 + s, y1), (x0, y1 - s), (x0, y0 + s), (x0 + s, y0)]
    d.line(pts, fill=kolor, width=grubosc)
