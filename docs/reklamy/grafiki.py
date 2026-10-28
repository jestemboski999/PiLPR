"""Grafiki reklamowe PiLPR na social media (post 1080², feed 4:5, story 9:16).

Ekrany urządzenia to prawdziwe klatki z pi/lpr.py (przez kit.py) na
syntetycznej scenie nocnej; wszystkie tablice są fikcyjne. Styl: tapeta
PiCamera (vhs.py) - ciemny fiolet, VT323, linie VHS, zakłócenia, gwiazdki.

    python3 docs/reklamy/grafiki.py [katalog_wyjściowy] [nazwa_filtr ...]
"""
import os, re, sys
import numpy as np, cv2
from PIL import Image, ImageDraw, ImageFilter, ImageFont

TU = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TU)
import kit  # noqa: E402
import kokpit as KP  # noqa: E402
import vhs  # noqa: E402

OUT = "/mnt/project-files/pilpr/reklamy/grafiki"
IMG = os.path.join(TU, "..", "images")
PS2P = vhs.FONT_DIR + "PressStart2P-Regular.ttf"

ZIEL = (110, 214, 120)       # baner Znajomi
NIEB = (92, 160, 235)        # baner Pojazdy służbowe
CZER = (236, 84, 96)         # radar
ZOLTY = (236, 196, 92)

STOPKA_FIKCJA = "Tablice, osoby i sceny na grafice są fikcyjne."
STOPKA_SLUZB = ("PiLPR nie zawiera żadnych list pojazdów - tworzysz je sam. "
                "Tablice fikcyjne. Jedź zgodnie z przepisami.")
STOPKA_RADAR = ("Pomiar poglądowy, to nie urządzenie legalizowane. "
                "Jedź zgodnie z przepisami. Tablice fikcyjne.")


# ───────────────────────── typografia ─────────────────────────

def _f(size, ps=False):
    # BASIC: bez ligatur (VT323 skleja „fi” w dziwny glif)
    return ImageFont.truetype(PS2P if ps else vhs.VT323, int(size),
                              layout_engine=ImageFont.Layout.BASIC)


def _segmenty(linia, akcent):
    """'Mijasz {znajomych}?' -> [(tekst, kolor), ...]"""
    out = []
    for i, cz in enumerate(re.split(r"[{}]", linia)):
        if cz:
            out.append((cz, akcent if i % 2 else vhs.TEKST))
    return out


def _szer(linia, f):
    return f.getlength(re.sub(r"[{}]", "", linia))


def dopasuj(linie, max_w, size, ps=False, min_size=20):
    while size > min_size and max(_szer(l, _f(size, ps)) for l in linie) > max_w:
        size -= 2
    return size


def naglowek(img, x, y, tekst, max_w, size, akcent=vhs.MAGENTA, wyrownaj="l",
             interlinia=0.92, glitch=True):
    """Duży nagłówek VT323 z aberracją chromatyczną VHS. Zwraca dolne y."""
    linie = tekst.split("\n")
    size = dopasuj(linie, max_w, size)
    f = _f(size)
    lh = int(size * interlinia)
    off = max(2, size // 40)
    warstwa = Image.new("RGBA", img.size, (0, 0, 0, 0))
    glow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(warstwa)
    dg = ImageDraw.Draw(glow)
    for i, l in enumerate(linie):
        w = _szer(l, f)
        xx = x if wyrownaj == "l" else (x - w / 2 if wyrownaj == "c" else x - w)
        yy = y + i * lh
        if glitch:
            dg.text((xx - off, yy), re.sub(r"[{}]", "", l), font=f, fill=vhs.MAGENTA + (150,))
            dg.text((xx + off, yy), re.sub(r"[{}]", "", l), font=f, fill=vhs.CYJAN + (130,))
        for cz, kol in _segmenty(l, akcent):
            d.text((xx, yy), cz, font=f, fill=kol + (255,))
            xx += f.getlength(cz)
    blur = warstwa.filter(ImageFilter.GaussianBlur(size * 0.12))
    img.paste(blur, (0, 0), Image.eval(blur.getchannel("A"), lambda a: a // 3))
    img.paste(glow, (0, 0), glow)
    img.paste(warstwa, (0, 0), warstwa)
    return y + len(linie) * lh + int(size * 0.15)


def akapit(img, x, y, tekst, max_w, size, kolor=vhs.TEKST, wyrownaj="l", interlinia=1.0,
           ps=False):
    """Tekst łamany do szerokości. Zwraca dolne y."""
    f = _f(size, ps)
    d = ImageDraw.Draw(img)
    linie = []
    for par in tekst.split("\n"):
        cur = ""
        for s in par.split(" "):
            t = (cur + " " + s).strip()
            if f.getlength(t) <= max_w or not cur:
                cur = t
            else:
                linie.append(cur); cur = s
        linie.append(cur)
    lh = int(size * interlinia * (1.6 if ps else 1.0))
    for i, l in enumerate(linie):
        w = f.getlength(l)
        xx = x if wyrownaj == "l" else (x - w / 2 if wyrownaj == "c" else x - w)
        d.text((xx, y + i * lh), l, font=f, fill=kolor)
    return y + len(linie) * lh


def etykieta(img, x, y, tekst, kolor, size=40, wyrownaj="l"):
    """Mały znacznik (VT323 z rozstrzeleniem) w pikselowej ramce. Zwraca (x1, y1).
    PressStart2P nie ma wielkich Ż/Ó, więc tu VT323."""
    f = _f(size)
    sp = size * 0.12
    w = sum(f.getlength(c) + sp for c in tekst) - sp
    padx, pady = size * 0.5, size * 0.22
    kw = int(size * 0.26)
    W = w + 2 * padx + kw + size * 0.35
    if wyrownaj == "c":
        x = x - W / 2
    d = ImageDraw.Draw(img)
    box = (int(x), int(y), int(x + W), int(y + size + 2 * pady))
    d.rectangle(box, fill=(20, 13, 24))
    vhs.ramka(d, box, kolor=kolor, grubosc=3, schodek=int(size * 0.2))
    cx, cy = int(x + padx + kw / 2), int(y + pady + size * 0.5)
    d.rectangle([cx - kw // 2, cy - kw // 2, cx + kw // 2, cy + kw // 2], fill=kolor)
    xx = x + padx + kw + size * 0.35
    for c in tekst:
        d.text((xx, y + pady), c, font=f, fill=kolor)
        xx += f.getlength(c) + sp
    return box[2], box[3]


def stopka(img, tekst, size=26, margines=56, dol=None, kolor=vhs.PRZYGASZONY):
    W, H = img.size
    f = _f(size)
    # ile linii zajmie?
    test = Image.new("RGB", (1, 1))
    n = (akapit(test, 0, 0, tekst, W - 2 * margines, size) // size)
    y = (dol if dol is not None else H - margines * 0.7) - n * size
    akapit(img, margines, int(y), tekst, W - 2 * margines, size, kolor=kolor)
    return int(y)


def logo(img, x, y, szer, wyrownaj="l"):
    lg = vhs.logo_tablica(int(szer))
    if wyrownaj == "c":
        x -= lg.width // 2
    elif wyrownaj == "r":
        x -= lg.width
    img.paste(lg, (int(x), int(y)), lg)
    return int(y) + lg.height


def kreska(img, x, y, kolory=(vhs.MAGENTA, vhs.CYJAN), s=8):
    d = ImageDraw.Draw(img)
    for i, k in enumerate(kolory):
        d.rectangle([x + i * (5 * s), y, x + i * (5 * s) + 4 * s, y + s], fill=k)


def gradient(img, y0, y1, alfa0, alfa1, kolor=(10, 7, 13)):
    """Pionowy gradient przyciemniający (dla tekstu na zdjęciu kabiny)."""
    W, H = img.size
    y0, y1 = int(max(0, y0)), int(min(H, y1))
    a = np.zeros((H, W), np.uint8)
    t = np.linspace(alfa0, alfa1, y1 - y0)
    a[y0:y1] = t[:, None].astype(np.uint8)
    if alfa1 > alfa0 and y1 < H:
        a[y1:] = alfa1
    if alfa0 > alfa1 and y0 > 0:
        a[:y0] = alfa0
    img.paste(Image.new("RGB", img.size, kolor), (0, 0), Image.fromarray(a, "L"))


# ───────────────────────── elementy ─────────────────────────

def bgr2pil(f):
    return Image.fromarray(cv2.cvtColor(f, cv2.COLOR_BGR2RGB))


def ekran_plaski(img, dev_bgr, x, y, szer, poswiata=(120, 80, 150), cien=True):
    """Ekran urządzenia w ramce, płasko (bez perspektywy). Zwraca box."""
    ek, ram = KP._ekran(dev_bgr, int(szer * 800 / 844))
    x, y = int(x), int(y)
    W, H = img.size
    gl = Image.new("L", img.size, 0)
    ImageDraw.Draw(gl).rounded_rectangle([x - 10, y - 10, x + ek.width + 10, y + ek.height + 10],
                                         radius=40, fill=90)
    img.paste(Image.new("RGB", img.size, poswiata), (0, 0), gl.filter(ImageFilter.GaussianBlur(szer * 0.07)))
    if cien:
        sh = Image.new("L", img.size, 0)
        ImageDraw.Draw(sh).rounded_rectangle([x + 8, y + 20, x + ek.width + 8, y + ek.height + 26],
                                             radius=30, fill=170)
        img.paste((0, 0, 0), (0, 0), sh.filter(ImageFilter.GaussianBlur(16)))
    img.paste(ek, (x, y), ek)
    return (x, y, x + ek.width, y + ek.height)


def telefon(img, x, y, szer, wycinek=None, poswiata=(110, 90, 160)):
    """Panel www (docs/images/www_telefon.png) w obudowie telefonu."""
    scr = Image.open(os.path.join(IMG, "www_telefon.png")).convert("RGB")
    if wycinek:
        scr = scr.crop(wycinek)
    r = szer / 780
    sw = int(scr.width * r)
    sh_ = int(scr.height * r)
    scr = scr.resize((sw, sh_), Image.LANCZOS)
    b = int(16 * r * 1.6)       # ramka
    top = int(44 * r * 1.6)
    w, h = sw + 2 * b, sh_ + b + top + int(30 * r * 1.6)
    x, y = int(x), int(y)
    gl = Image.new("L", img.size, 0)
    ImageDraw.Draw(gl).rounded_rectangle([x, y, x + w, y + h], radius=60, fill=80)
    img.paste(Image.new("RGB", img.size, poswiata), (0, 0), gl.filter(ImageFilter.GaussianBlur(w * 0.12)))
    sh = Image.new("L", img.size, 0)
    ImageDraw.Draw(sh).rounded_rectangle([x + 10, y + 24, x + w + 10, y + h + 24], radius=int(70 * r * 1.6), fill=170)
    img.paste((0, 0, 0), (0, 0), sh.filter(ImageFilter.GaussianBlur(20)))
    d = ImageDraw.Draw(img)
    rad = int(90 * r)
    d.rounded_rectangle([x, y, x + w, y + h], radius=rad, fill=(12, 10, 15), outline=(200, 186, 205), width=max(3, int(5 * r)))
    d.rounded_rectangle([x + b // 2, y + b // 2, x + w - b // 2, y + h - b // 2], radius=rad - b // 2, outline=(60, 52, 70), width=2)
    img.paste(scr, (x + b, y + top))
    # „wyspa” u góry
    cx = x + w // 2
    d.rounded_rectangle([cx - int(w * 0.13), y + int(top * 0.3), cx + int(w * 0.13), y + int(top * 0.72)],
                        radius=int(top * 0.25), fill=(0, 0, 0))
    return (x, y, x + w, y + h)


def zdjecie_radar(img, foto_bgr, x, y, szer, kolor=CZER, napis="ZDJĘCIE Z RADARU"):
    p = bgr2pil(foto_bgr)
    h = int(szer * p.height / p.width)
    p = p.resize((int(szer), h), Image.LANCZOS)
    x, y = int(x), int(y)
    sh = Image.new("L", img.size, 0)
    ImageDraw.Draw(sh).rectangle([x + 10, y + 20, x + szer + 10, y + h + 26], fill=170)
    img.paste((0, 0, 0), (0, 0), sh.filter(ImageFilter.GaussianBlur(16)))
    d = ImageDraw.Draw(img)
    d.rectangle([x - 10, y - 10, x + szer + 10, y + h + 10], fill=(232, 226, 232))
    img.paste(p, (x, y))
    if napis:
        etykieta(img, x + 24, y - 36, napis, kolor, size=38)
    return (x - 10, y - 10, x + int(szer) + 10, y + h + 10)


def tlo(W, H, seed=1):
    return vhs.tlo(W, H, seed=seed, smugi=10 + H // 120, gwiazdki=4)


def wykoncz(img, okres=None):
    W, _ = img.size
    return vhs.linie(img, okres=okres or max(3, W // 360), sila=0.12)


def zapisz(img, nazwa, out):
    p = os.path.join(out, nazwa)
    img.save(p, optimize=True)
    print("zapisano", p)
    return p


# ───────────────────────── sceny ─────────────────────────

_cache = {}


def sc(nazwa):
    if nazwa in _cache:
        return _cache[nazwa]
    if nazwa == "jazda":
        s = kit.scena("WX 5678K", car_w=640, car_cx=0.5, car_by=0.80, seed=3)
    elif nazwa == "znajomi":
        s = kit.scena("WX 2481M", car_w=700, car_cx=0.47, car_by=0.82, body=(150, 60, 70), seed=5)
    elif nazwa == "sluzbowe":
        s = kit.scena("WX 7305P", car_w=660, car_cx=0.53, car_by=0.81, body=(70, 74, 82), seed=8)
    elif nazwa == "radar":
        s = kit.scena("WX 9142R", car_w=600, car_cx=0.5, car_by=0.79, body=(205, 205, 210), seed=11)
    else:
        raise KeyError(nazwa)
    _cache[nazwa] = s
    return s


def kadr_jazda():
    s = sc("jazda")
    return kit.device_frame(s, stan="pewne", tablica="WX5678K", notif_wiek=1.0, ostatnia="WX5678K")


def kadr_znajomi():
    s = sc("znajomi")
    f = kit.device_frame(s, stan="pewne", tablica="WX2481M", notif_wiek=1.0, ostatnia="WX2481M")
    return kit.alert(f, s, "WX2481M", "znajomi", "Ania i Tomek")


def kadr_sluzbowe():
    s = sc("sluzbowe")
    f = kit.device_frame(s, stan="pewne", tablica="WX7305P", notif_wiek=1.0, ostatnia="WX7305P")
    return kit.alert(f, s, "WX7305P", "sluzbowe")


def kadr_radar():
    s = sc("radar")
    return kit.device_frame(s, stan="pewne", tablica="WX9142R", radar=True, radar_v=63)


def kadr_menu():
    return kit.menu(kit.device_frame(sc("jazda"), stan="nic", stats=False), "main", 2)


# ───────────────────────── grafiki ─────────────────────────

def g01_kabina_post():
    """Hero w kabinie, kwadrat, wersja luźna."""
    W = H = 1080
    s = sc("jazda")
    im = KP.kokpit(s[0], kadr_jazda(), W, H, ekran_szer=0.60, ekran_y=0.585, dy=0.06,
                   horyzont=0.40, lusterko=False)
    gradient(im, 0, 330, 235, 0)
    gradient(im, 850, 960, 0, 248)
    y = naglowek(im, 60, 46, "Pi w aucie\nczyta {tablice}.", 960, 132, akcent=ZIEL)
    akapit(im, 64, 950, "Wideorejestrator na Raspberry Pi 5,", 640, 38)
    akapit(im, 64, 990, "który rozpoznaje tablice. Offline.", 640, 38, kolor=vhs.PRZYGASZONY)
    logo(im, W - 56, 948, 300, wyrownaj="r")
    return wykoncz(im)


def g02_kabina_story():
    """Hero w kabinie, story, wersja techniczna."""
    W, H = 1080, 1920
    s = sc("jazda")
    im = KP.kokpit(s[0], kadr_jazda(), W, H, ekran_szer=0.80, ekran_y=0.63, horyzont=0.40,
                   dy=0.0, lusterko=False)
    gradient(im, 0, 560, 240, 0)
    gradient(im, 1440, 1600, 0, 238)
    logo(im, W // 2, 120, 440, wyrownaj="c")
    naglowek(im, W // 2, 300, "Wideorejestrator,\nktóry {czyta tablice}", 960, 112,
             akcent=ZIEL, wyrownaj="c")
    y = 1600
    akapit(im, W // 2, y, "Raspberry Pi 5 · Hailo-10H · IMX708 HDR", 980, 46, wyrownaj="c")
    akapit(im, W // 2, y + 56, "rozpoznawanie na żywo · 30 kl./s · bez chmury", 980, 40,
           kolor=vhs.PRZYGASZONY, wyrownaj="c")
    kreska(im, W // 2 - 36, y + 140)
    akapit(im, W // 2, y + 190, "Na ekranie prawdziwa klatka z programu PiLPR. Scena poglądowa.", 980, 30,
           kolor=vhs.PRZYGASZONY, wyrownaj="c")
    return wykoncz(im)


def g03_znajomi_feed():
    """Znajomi, 4:5, wersja luźna: płaski ekran z banerem."""
    W, H = 1080, 1350
    im = tlo(W, H, seed=3)
    etykieta(im, 60, 64, "LISTA ZNAJOMI", ZIEL)
    y = naglowek(im, 56, 150, "Mijasz {znajomych}?\nPi Ci powie.", 970, 128, akcent=ZIEL)
    ekran_plaski(im, kadr_znajomi(), 46, y + 30, 988, poswiata=(60, 140, 80))
    y2 = y + 30 + int(988 * 534 / 844) + 50
    akapit(im, W // 2, y2, "zielony baner · zdjęcie auta · głos przez głośnik", 980, 44,
           wyrownaj="c")
    akapit(im, W // 2, y2 + 54, "Tablice znajomych dodajesz sam w panelu na telefonie.", 980, 36,
           kolor=vhs.PRZYGASZONY, wyrownaj="c")
    logo(im, W - 56, H - 130, 260, wyrownaj="r")
    stopka(im, STOPKA_FIKCJA, size=26, dol=H - 56)
    return wykoncz(im)


def g04_znajomi_story():
    """Znajomi, story, wersja techniczna: kabina z alertem."""
    W, H = 1080, 1920
    s = sc("znajomi")
    im = KP.kokpit(s[0], kadr_znajomi(), W, H, ekran_szer=0.80, ekran_y=0.62, horyzont=0.40,
                   lusterko=False, poswiata=(70, 150, 90))
    gradient(im, 0, 640, 245, 0)
    gradient(im, 1430, 1590, 0, 240)
    etykieta(im, W // 2, 110, "ALERT · ZNAJOMI", ZIEL, wyrownaj="c")
    naglowek(im, W // 2, 220, "Twoja lista,\n{Twoi ludzie}", 960, 132, akcent=ZIEL, wyrownaj="c")
    y = 1560
    akapit(im, W // 2, y, "Dopisz tablice rodziny i znajomych.", 960, 46, wyrownaj="c")
    akapit(im, W // 2, y + 54, "Gdy je mijasz: baner, zdjęcie auta i głos.", 960, 46,
           kolor=ZIEL, wyrownaj="c")
    logo(im, W // 2, y + 140, 260, wyrownaj="c")
    akapit(im, W // 2, y + 240, STOPKA_FIKCJA, 960, 28, kolor=vhs.PRZYGASZONY, wyrownaj="c")
    return wykoncz(im)


def g05_sluzbowe_post():
    """Pojazdy służbowe, kwadrat, wersja techniczna (płaski ekran)."""
    W = H = 1080
    im = tlo(W, H, seed=5)
    etykieta(im, 56, 52, "POJAZDY SŁUŻBOWE", NIEB)
    y = naglowek(im, 52, 124, "{Twoja} lista.\n{Twój} alert.", 600, 120, akcent=NIEB)
    akapit(im, 664, 150, "Importujesz własną listę pojazdów służbowych z CSV. PiLPR pokaże "
           "niebieski baner i powie o tym głosem.", 370, 33, kolor=vhs.TEKST, interlinia=1.02)
    ekran_plaski(im, kadr_sluzbowe(), 90, y + 34, 900, poswiata=(50, 90, 170))
    stopka(im, STOPKA_SLUZB, size=27)
    return wykoncz(im)


def g06_sluzbowe_feed():
    """Pojazdy służbowe, 4:5, wersja luźna: kabina."""
    W, H = 1080, 1350
    s = sc("sluzbowe")
    im = KP.kokpit(s[0], kadr_sluzbowe(), W, H, ekran_szer=0.70, ekran_y=0.585, horyzont=0.38,
                   dy=0.04, lusterko=False, poswiata=(60, 100, 180))
    gradient(im, 0, 400, 245, 0)
    gradient(im, 1000, 1120, 0, 240)
    etykieta(im, 60, 54, "POJAZDY SŁUŻBOWE", NIEB)
    naglowek(im, 56, 130, "Służbowe auto\n{z Twojej listy}", 970, 118, akcent=NIEB)
    akapit(im, 60, 1112, "Niebieski baner i komunikat głosowy - dla pojazdów z listy, "
           "którą sam przygotujesz.", 640, 38)
    logo(im, W - 56, 1118, 270, wyrownaj="r")
    stopka(im, STOPKA_SLUZB, size=26, dol=H - 44)
    return wykoncz(im)


def g07_radar_feed():
    """Radar, 4:5, wersja techniczna: ekran + zdjęcie z radaru."""
    W, H = 1080, 1350
    im = tlo(W, H, seed=7)
    etykieta(im, 60, 60, "RADAR 24 GHZ", CZER)
    y = naglowek(im, 56, 140, "Prędkość {na ekranie}.\nZdjęcie {jak z fotoradaru}.", 970, 96,
                 akcent=CZER)
    s = sc("radar")
    ekran_plaski(im, kadr_radar(), W - 46 - 690, y + 30, 690, poswiata=(150, 60, 80))
    zdjecie_radar(im, kit.radar_foto(s, "WX9142R", 63), 56, y + 360, 560)
    akapit(im, 60, 1040, "Radar dopplerowski mierzy auta przed Tobą. Powyżej progu "
           "PiLPR zapisuje zdjęcie z tablicą, prędkością, datą i godziną.", 960, 36)
    stopka(im, STOPKA_RADAR, size=26, dol=H - 50)
    return wykoncz(im)


def g08_radar_story():
    """Radar, story, wersja luźna."""
    W, H = 1080, 1920
    im = tlo(W, H, seed=8)
    s = sc("radar")
    etykieta(im, W // 2, 120, "TRYB RADARU", CZER, wyrownaj="c")
    naglowek(im, W // 2, 220, "Kto tu jeździ\n{za szybko}?", 960, 150, akcent=CZER, wyrownaj="c")
    zdjecie_radar(im, kit.radar_foto(s, "WX9142R", 63), 60, 600, 880, napis="ZDJĘCIE · 63 KM/H")
    ekran_plaski(im, kadr_radar(), W - 50 - 760, 1130, 760, poswiata=(150, 60, 80))
    y = 1640
    akapit(im, W // 2, y, "Radar 24 GHz mierzy, kamera czyta tablicę.", 980, 44, wyrownaj="c")
    akapit(im, W // 2, y + 52, "Zdjęcie i klip zapisują się same.", 980, 44, kolor=CZER, wyrownaj="c")
    akapit(im, W // 2, y + 130, STOPKA_RADAR, 960, 26, kolor=vhs.PRZYGASZONY, wyrownaj="c")
    return wykoncz(im)


def g09_menu_post():
    """Menu kafelkowe, kwadrat, kabina."""
    W = H = 1080
    s = sc("jazda")
    im = KP.kokpit(s[0], kadr_menu(), W, H, ekran_szer=0.66, ekran_y=0.60, dy=0.06,
                   horyzont=0.40, lusterko=False)
    gradient(im, 0, 300, 240, 0)
    gradient(im, 880, 1000, 0, 240)
    naglowek(im, 56, 40, "Wszystko\nz {ekranu w aucie}", 970, 112, akcent=vhs.CYJAN)
    akapit(im, 60, 960, "Duże kafle pod palec: radar, dźwięk,", 700, 36)
    akapit(im, 60, 998, "zdjęcie, Bluetooth, nagrywanie.", 700, 36, kolor=vhs.PRZYGASZONY)
    logo(im, W - 56, 956, 280, wyrownaj="r")
    return wykoncz(im)


def g10_offline_story():
    """Offline + panel w telefonie, story, wersja luźna."""
    W, H = 1080, 1920
    im = tlo(W, H, seed=21)
    etykieta(im, W // 2, 110, "100% OFFLINE", vhs.CYJAN, wyrownaj="c")
    naglowek(im, W // 2, 210, "Zero chmury.\n{Zero abonamentu}.", 980, 140, akcent=vhs.CYJAN,
             wyrownaj="c")
    ekran_plaski(im, kadr_jazda(), 34, 700, 600)
    telefon(im, 590, 560, 430, wycinek=(0, 0, 780, 1208))
    akapit(im, W // 2, 1390, "Wszystko liczy malinka w aucie.", 980, 52, wyrownaj="c")
    akapit(im, W // 2, 1456, "Panel www serwuje samo urządzenie: podgląd na żywo, "
           "historia tablic, nagrania. Bez konta.", 860, 38, kolor=vhs.PRZYGASZONY, wyrownaj="c")
    kreska(im, W // 2 - 36, 1580)
    logo(im, W // 2, 1620, 360, wyrownaj="c")
    return wykoncz(im)


SPECY = [
    ("Raspberry Pi 5", "8 GB · cały system", vhs.TEKST),
    ("Hailo-10H", "akcelerator AI · detekcja", vhs.MAGENTA),
    ("IMX708 HDR", "2304×1296 · 30 kl./s", vhs.CYJAN),
    ("Radar 24 GHz", "prędkość ±1 km/h", CZER),
    ("Ekran dotykowy", "menu z kafli", ZIEL),
    ("Offline", "bez chmury i konta", ZOLTY),
]


def _kafel(im, box, tytul, opis, kolor, rt=58, ro=32):
    d = ImageDraw.Draw(im)
    x0, y0, x1, y1 = box
    d.rectangle(box, fill=(22, 15, 27))
    vhs.ramka(d, box, kolor=vhs.RAMKA, grubosc=2, schodek=10)
    d.rectangle([x0 + 22, y0 + 26, x0 + 34, y0 + 38], fill=kolor)
    rt = dopasuj([tytul], x1 - x0 - 44, rt)
    d.text((x0 + 22, y0 + 46), tytul, font=_f(rt), fill=kolor)
    akapit(im, x0 + 22, y0 + 46 + rt + 4, opis, x1 - x0 - 44, ro, kolor=vhs.PRZYGASZONY)


def g11_specy_post():
    """Specyfikacja, kwadrat, wersja techniczna."""
    W = H = 1080
    im = tlo(W, H, seed=11)
    logo(im, 56, 56, 330)
    naglowek(im, W - 56, 64, "Co jest\n{w środku}?", 560, 110, akcent=vhs.MAGENTA, wyrownaj="r")
    gx, gy, gw, gh, gap = 56, 330, (W - 112 - 24) // 2, 196, 24
    for i, (t, o, k) in enumerate(SPECY):
        c, r = i % 2, i // 2
        x0 = gx + c * (gw + gap); y0 = gy + r * (gh + gap)
        _kafel(im, (x0, y0, x0 + gw, y0 + gh), t, o, k)
    akapit(im, W // 2, 1000, "Projekt DIY · uchwyty do druku 3D · kod na GitHubie", 980, 34,
           kolor=vhs.PRZYGASZONY, wyrownaj="c")
    return wykoncz(im)


def g12_specy_feed():
    """Specyfikacja / DIY, 4:5, wersja luźna z uchwytami 3D."""
    W, H = 1080, 1350
    im = tlo(W, H, seed=12)
    etykieta(im, 60, 60, "ZRÓB TO SAM", ZOLTY)
    y = naglowek(im, 56, 140, "Malinka, AI\ni {trochę filamentu}.", 970, 120, akcent=ZOLTY)
    u = Image.open(os.path.join(IMG, "uchwyty_3d.png")).convert("RGBA")
    uw = W - 80
    u = u.resize((uw, int(uw * u.height / u.width)), Image.LANCZOS)
    im.paste(u, (40, y + 10), u)
    vhs.ramka(ImageDraw.Draw(im), (40, y + 10, 40 + uw, y + 10 + u.height), kolor=vhs.RAMKA,
              grubosc=2, schodek=10)
    yk = y + 46 + u.height
    linie = [("Raspberry Pi 5", vhs.TEKST), ("Hailo-10H", vhs.MAGENTA), ("IMX708 HDR", vhs.CYJAN),
             ("Radar 24 GHz", CZER)]
    f = _f(66)
    d = ImageDraw.Draw(im)
    for i, (t, k) in enumerate(linie):
        c, r = i % 2, i // 2
        x = 70 + c * 500; yy = yk + r * 92
        d.rectangle([x, yy + 26, x + 14, yy + 40], fill=k)
        d.text((x + 34, yy), t, font=f, fill=vhs.TEKST)
    akapit(im, 70, yk + 210, "Składasz sam, drukujesz uchwyty, wgrywasz kod. "
           "Czyta polskie tablice na żywo i nagrywa jak wideorejestrator.", 940, 42,
           kolor=vhs.TEKST)
    kreska(im, 70, H - 150)
    logo(im, W - 60, H - 190, 320, wyrownaj="r")
    return wykoncz(im)


GRAFIKI = [
    ("01_kabina_post.png", g01_kabina_post),
    ("02_kabina_story.png", g02_kabina_story),
    ("03_znajomi_feed.png", g03_znajomi_feed),
    ("04_znajomi_story.png", g04_znajomi_story),
    ("05_sluzbowe_post.png", g05_sluzbowe_post),
    ("06_sluzbowe_feed.png", g06_sluzbowe_feed),
    ("07_radar_feed.png", g07_radar_feed),
    ("08_radar_story.png", g08_radar_story),
    ("09_menu_post.png", g09_menu_post),
    ("10_offline_story.png", g10_offline_story),
    ("11_specy_post.png", g11_specy_post),
    ("12_specy_feed.png", g12_specy_feed),
]


def przeglad(sciezki, out):
    """Arkusz kontaktowy wszystkich grafik w jednej wysokości."""
    hh = 640
    ims = [Image.open(p).convert("RGB") for p in sciezki]
    ims = [i.resize((int(i.width * hh / i.height), hh), Image.LANCZOS) for i in ims]
    gap, cols_w = 28, 2400
    rzedy, cur, w = [], [], 0
    for i in ims:
        if cur and w + i.width + gap > cols_w:
            rzedy.append(cur); cur, w = [], 0
        cur.append(i); w += i.width + gap
    rzedy.append(cur)
    lab = 40
    Wt = cols_w + gap
    Ht = 160 + len(rzedy) * (hh + lab + gap)
    S = tlo(Wt, Ht, seed=99)
    logo(S, gap, 30, 300)
    akapit(S, 360, 54, "PiLPR · grafiki reklamowe · podgląd", 1500, 56)
    y = 160
    k = 0
    for rz in rzedy:
        x = gap
        for i in rz:
            S.paste(i, (x, y))
            akapit(S, x, y + hh + 4, os.path.basename(sciezki[k]), i.width, 30, kolor=vhs.PRZYGASZONY)
            x += i.width + gap; k += 1
        y += hh + lab + gap
    return zapisz(S, "przeglad.png", out)


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else OUT
    filtr = sys.argv[2:]
    os.makedirs(out, exist_ok=True)
    sciezki = []
    for nazwa, fn in GRAFIKI:
        p = os.path.join(out, nazwa)
        if not filtr or any(f in nazwa for f in filtr):
            p = zapisz(fn(), nazwa, out)
        if os.path.exists(p):
            sciezki.append(p)
    if not filtr or "przeglad" in filtr:
        przeglad(sciezki, out)
