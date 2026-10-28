"""Grafiki reklamowe PiLPR, runda 2: jeden pomysł na grafikę, duży hak,
prawdziwy ekran urządzenia na płasko i formaty „natywne” (notatka, czat,
post, porównanie, napisy jak w rolkach) zamiast jednego szablonu.

Ekrany urządzenia to prawdziwe klatki z pi/lpr.py (przez kit.py / grafiki.py),
sceny syntetyczne, tablice i osoby fikcyjne.

    python3 docs/reklamy/grafiki_v2.py [katalog_wyjściowy] [filtr ...]
"""
import os, re, sys
import numpy as np, cv2
from PIL import Image, ImageDraw, ImageFilter, ImageFont

TU = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TU)
import grafiki as G  # noqa: E402  (sceny i klatki urządzenia)
import kit  # noqa: E402
import vhs  # noqa: E402

OUT = "/mnt/project-files/pilpr/reklamy/grafiki_v2"
IMG = os.path.join(TU, "..", "images")
INTER = "/usr/share/fonts/opentype/inter/"
EMOJI = "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf"
MONO = "/usr/share/fonts/truetype/jetbrains-mono/JetBrainsMono-Bold.ttf"

BIALY, CZARNY = (255, 255, 255), (0, 0, 0)
IOS_NIEB, IOS_SZARY = (10, 132, 255), (233, 233, 235)
IOS_ZOLTY = (227, 170, 0)
SZARY_TXT = (138, 138, 142)

FIKCJA = "Tablice i osoby fikcyjne. Ekran: prawdziwy interfejs PiLPR na scenie poglądowej."
SLUZB = ("PiLPR nie zawiera żadnych list - listę pojazdów służbowych tworzysz sam. "
         "Tablice fikcyjne. Jedź zgodnie z przepisami.")
RADAR = "Pomiar poglądowy, nie urządzenie legalizowane. Jedź zgodnie z przepisami. Tablice fikcyjne."


# ───────────────────────── tekst ─────────────────────────

def F(waga, size):
    nazwy = {"r": "Inter-Regular", "m": "Inter-Medium", "s": "Inter-SemiBold", "b": "Inter-Bold",
             "x": "InterDisplay-ExtraBold", "k": "InterDisplay-Black"}
    return ImageFont.truetype(INTER + nazwy[waga] + ".otf", int(size))


_EMO_RE = re.compile("([\U0001F300-\U0001FAFF☀-➿])")
_emo_cache = {}


def _emoji(ch, size):
    k = (ch, size)
    if k not in _emo_cache:
        f = ImageFont.truetype(EMOJI, 109)
        im = Image.new("RGBA", (140, 140), (0, 0, 0, 0))
        ImageDraw.Draw(im).text((4, 4), ch, font=f, embedded_color=True)
        im = im.crop(im.getbbox())
        s = size / im.height
        _emo_cache[k] = im.resize((max(1, int(im.width * s)), int(size)), Image.LANCZOS)
    return _emo_cache[k]


def dlugosc(t, f):
    w = 0
    for cz in _EMO_RE.split(t):
        if not cz:
            continue
        w += (f.size * 1.08 if _EMO_RE.fullmatch(cz) else f.getlength(cz))
    return w


def tekst(img, xy, t, f, fill, stroke=0, stroke_fill=CZARNY):
    """Tekst z obsługą emoji (kolorowe, wklejane obrazkiem)."""
    d = ImageDraw.Draw(img)
    x, y = xy
    asc = f.getmetrics()[0]
    for cz in _EMO_RE.split(t):
        if not cz:
            continue
        if _EMO_RE.fullmatch(cz):
            e = _emoji(cz, int(f.size * 0.95))
            img.paste(e, (int(x + f.size * 0.06), int(y + asc - f.size * 0.86)), e)
            x += f.size * 1.08
        else:
            d.text((x, y), cz, font=f, fill=fill, stroke_width=stroke, stroke_fill=stroke_fill)
            x += f.getlength(cz)


def zawin(t, f, max_w):
    out = []
    for par in t.split("\n"):
        cur = ""
        for s in par.split(" "):
            n = (cur + " " + s).strip()
            if dlugosc(n, f) <= max_w or not cur:
                cur = n
            else:
                out.append(cur); cur = s
        out.append(cur)
    return out


def akapit(img, x, y, t, f, fill, max_w, lh=1.25, wyrownaj="l", stroke=0):
    for i, l in enumerate(zawin(t, f, max_w)):
        w = dlugosc(l, f)
        xx = x if wyrownaj == "l" else (x - w / 2 if wyrownaj == "c" else x - w)
        tekst(img, (xx, y + i * f.size * lh), l, f, fill, stroke)
    return y + len(zawin(t, f, max_w)) * f.size * lh


def kapsle(img, cx, y, linie, size, tlo=BIALY, kolor=CZARNY, waga="b", pad=0.28, r=0.22):
    """Napisy jak w rolkach: każda linia w osobnej białej „kapsułce”."""
    f = F(waga, size)
    d = ImageDraw.Draw(img)
    lh = int(size * (1 + 2 * pad)) + 4
    for i, l in enumerate(linie):
        w = dlugosc(l, f)
        x0 = cx - w / 2 - size * pad * 1.4
        y0 = y + i * lh
        d.rounded_rectangle([x0, y0, cx + w / 2 + size * pad * 1.4, y0 + size * (1 + 2 * pad)],
                            radius=int(size * r), fill=tlo)
        tekst(img, (cx - w / 2, y0 + size * pad - size * 0.04), l, f, kolor)
    return y + len(linie) * lh


def obrys(img, cx, y, linie, size, kolor=BIALY, waga="k", akcent=None):
    """Biały tekst z czarnym obrysem (styl rolek). {słowo} = kolor akcentu."""
    f = F(waga, size)
    lh = int(size * 1.08)
    for i, l in enumerate(linie):
        czysty = l.replace("{", "").replace("}", "")
        x = cx - dlugosc(czysty, f) / 2
        for j, cz in enumerate(re.split(r"[{}]", l)):
            if not cz:
                continue
            k = akcent if (j % 2 and akcent) else kolor
            tekst(img, (x, y + i * lh), cz, f, k, stroke=max(4, size // 11))
            x += dlugosc(cz, f)
    return y + len(linie) * lh


def dopasuj(linie, waga, size, max_w):
    while size > 20 and max(dlugosc(l.replace("{", "").replace("}", ""), F(waga, size))
                            for l in linie) > max_w:
        size -= 2
    return size


def drobny(img, t, y=None, kolor=SZARY_TXT, size=24, margines=48, wyrownaj="c"):
    W, H = img.size
    f = F("r", size)
    n = len(zawin(t, f, W - 2 * margines))
    if y is None:
        y = H - margines - n * size * 1.3
    x = W // 2 if wyrownaj == "c" else margines
    akapit(img, x, y, t, f, kolor, W - 2 * margines, lh=1.3, wyrownaj=wyrownaj)


def znak(img, x, y, szer=150, wyrownaj="r"):
    """Jedno małe logo PI LPR w rogu."""
    lg = vhs.logo_tablica(szer)
    if wyrownaj == "r":
        x -= lg.width
    img.paste(lg, (int(x), int(y)), lg)


# ───────────────────────── obrazy ─────────────────────────

def rgb(f_bgr):
    return Image.fromarray(cv2.cvtColor(f_bgr, cv2.COLOR_BGR2RGB))


def wycinek(f_bgr, box, szer):
    """Wycinek prawdziwej klatki 800×480 (box w jej pikselach), powiększony."""
    x0, y0, x1, y1 = box
    c = f_bgr[y0:y1, x0:x1]
    s = szer / (x1 - x0)
    c = cv2.resize(c, (int(szer), int((y1 - y0) * s)), interpolation=cv2.INTER_LANCZOS4)
    bl = cv2.GaussianBlur(c, (0, 0), 1.2)
    c = cv2.addWeighted(c, 1.35, bl, -0.35, 0)          # lekkie wyostrzenie
    return rgb(c)


def zaokr(im, r):
    m = Image.new("L", im.size, 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, im.width - 1, im.height - 1], radius=r, fill=255)
    out = Image.new("RGBA", im.size)
    out.paste(im.convert("RGB"), (0, 0), m)
    out.putalpha(m)
    return out


def wklej(img, im, x, y, r=0, cien=0):
    im = zaokr(im, r) if r else im.convert("RGBA")
    if cien:
        sh = Image.new("L", img.size, 0)
        ImageDraw.Draw(sh).rounded_rectangle([x, y + cien // 2, x + im.width, y + im.height + cien // 2],
                                             radius=max(r, 1), fill=110)
        img.paste((0, 0, 0), (0, 0), sh.filter(ImageFilter.GaussianBlur(cien)))
    img.paste(im, (int(x), int(y)), im)
    return (x, y, x + im.width, y + im.height)


def rozmyte_tlo(src, W, H, ciemniej=0.45, blur=40):
    s = max(W / src.width, H / src.height)
    im = src.resize((int(src.width * s) + 1, int(src.height * s) + 1), Image.LANCZOS)
    im = im.crop(((im.width - W) // 2, (im.height - H) // 2, (im.width - W) // 2 + W, (im.height - H) // 2 + H))
    im = im.filter(ImageFilter.GaussianBlur(blur))
    a = np.asarray(im).astype(np.float32) * ciemniej
    return Image.fromarray(a.astype(np.uint8))


def pasek_statusu(img, y=0, kolor=CZARNY, h=110):
    W = img.width
    d = ImageDraw.Draw(img)
    f = F("s", 40)
    d.text((80, y + h // 2), "21:47", font=f, fill=kolor, anchor="lm")
    # zasięg, wifi, bateria - proste kształty
    x = W - 210
    for i in range(4):
        hh = 10 + i * 7
        d.rounded_rectangle([x + i * 13, y + h // 2 + 14 - hh, x + i * 13 + 8, y + h // 2 + 14], radius=2, fill=kolor)
    x = W - 140
    d.rounded_rectangle([x, y + h // 2 - 13, x + 52, y + h // 2 + 13], radius=7, outline=kolor, width=3)
    d.rounded_rectangle([x + 5, y + h // 2 - 8, x + 38, y + h // 2 + 8], radius=4, fill=kolor)
    d.rounded_rectangle([x + 55, y + h // 2 - 5, x + 60, y + h // 2 + 5], radius=2, fill=kolor)


# ───────────────────────── klatki (cache) ─────────────────────────

_k = {}


def K(nazwa):
    if nazwa not in _k:
        _k[nazwa] = {"znajomi": G.kadr_znajomi, "sluzbowe": G.kadr_sluzbowe, "radar": G.kadr_radar,
                     "menu": G.kadr_menu, "jazda": G.kadr_jazda}[nazwa]()
    return _k[nazwa]


# ───────────────────────── grafiki ─────────────────────────

def v01_czat_story():
    """iMessage: „Właśnie was minąłem” + zrzut alertu Znajomi."""
    W, H = 1080, 1920
    im = Image.new("RGB", (W, H), BIALY)
    d = ImageDraw.Draw(im)
    pasek_statusu(im, 40)
    # nagłówek rozmowy
    d.text((60, 250), "‹", font=F("r", 90), fill=IOS_NIEB, anchor="lm")
    cx = W // 2
    d.ellipse([cx - 62, 170, cx + 62, 294], fill=(160, 170, 186))
    d.text((cx, 232), "A", font=F("s", 64), fill=BIALY, anchor="mm")
    d.text((cx, 330), "Ania", font=F("m", 34), fill=CZARNY, anchor="mm")
    d.line([(0, 380), (W, 380)], fill=(222, 222, 226), width=2)
    d.text((cx, 430), "Dziś 21:47", font=F("m", 28), fill=SZARY_TXT, anchor="mm")

    def dymek(y, t, moj, size=66):
        f = F("m", size)
        linie = zawin(t, f, 880)
        w = max(dlugosc(l, f) for l in linie) + 76
        h = len(linie) * size * 1.22 + 56
        x0 = W - 44 - w if moj else 44
        d.rounded_rectangle([x0, y, x0 + w, y + h], radius=46, fill=IOS_NIEB if moj else IOS_SZARY)
        for i, l in enumerate(linie):
            tekst(im, (x0 + 38, y + 24 + i * size * 1.22), l, f, BIALY if moj else CZARNY)
        return y + h

    y = dymek(490, "Właśnie was minąłem 👋", True)
    y = dymek(y + 26, "skąd wiesz?! 😳", False)
    # zdjęcie wysłane: zrzut ekranu z banerem
    zr = wycinek(K("znajomi"), (0, 150, 800, 480), 860)
    wklej(im, zr, W - 44 - zr.width, y + 40, r=40)
    y = y + 40 + zr.height
    y = dymek(y + 26, "Malinka w aucie mi powiedziała", True)
    d.text((W - 60, y + 20), "Dostarczono", font=F("m", 26), fill=SZARY_TXT, anchor="ra")
    kapsle(im, W // 2, y + 110, ["Mój wideorejestrator", "zna moich znajomych"], 64,
           tlo=CZARNY, kolor=BIALY)
    drobny(im, "Wideorejestrator na Raspberry Pi 5, który czyta tablice offline. " + FIKCJA, size=24)
    return im


def v02_notatka_feed():
    """Notatka w stylu iOS: lista „Co robi moja malinka w aucie:”."""
    W, H = 1080, 1350
    im = Image.new("RGB", (W, H), (255, 255, 255))
    d = ImageDraw.Draw(im)
    d.text((40, 70), "‹ Notatki", font=F("m", 40), fill=IOS_ZOLTY, anchor="lm")
    d.text((W - 60, 70), "Gotowe", font=F("s", 40), fill=IOS_ZOLTY, anchor="rm")
    d.text((W // 2, 150), "28 października 2026 o 21:47", font=F("r", 28), fill=SZARY_TXT, anchor="mm")
    y = akapit(im, 60, 196, "Co robi moja malinka\nw aucie:", F("b", 88), CZARNY, 980, lh=1.1)
    pozycje = [
        "nagrywa jak wideorejestrator",
        "czyta tablice na żywo, bez internetu",
        "mówi, gdy mijam znajomych 👋",
        "mierzy prędkość radarem 24 GHz",
        "pojazdy służbowe z mojej listy: alert*",
    ]
    f = F("r", 47)
    y += 26
    for p in pozycje:
        d.ellipse([62, y + 6, 108, y + 52], fill=IOS_ZOLTY)
        d.line([(73, y + 30), (82, y + 40), (98, y + 18)], fill=BIALY, width=6, joint="curve")
        tekst(im, (134, y), p, f, (40, 40, 42))
        y += 80
    zr = wycinek(K("jazda"), (0, 40, 800, 440), 960)
    hmax = H - 110 - (y + 20)
    zr = zr.crop((0, (zr.height - hmax) // 2, zr.width, (zr.height - hmax) // 2 + hmax))
    wklej(im, zr, 60, y + 20, r=26)
    drobny(im, "*Listę tworzysz sam, aplikacja nie zawiera żadnej. Tablice fikcyjne, "
               "ekran: prawdziwy interfejs PiLPR.", size=22, y=H - 76, margines=60)
    return im


def v03_porownanie_feed():
    """Zwykły wideorejestrator vs. mój (prawdziwy ekran z alertem)."""
    W, H = 1080, 1350
    im = Image.new("RGB", (W, H), (14, 14, 16))
    d = ImageDraw.Draw(im)
    pol = (H - 0) // 2
    # góra: „zwykły” - surowy obraz, szary timestamp
    s = G.sc("znajomi")[0]
    sw = Image.fromarray(s).resize((W, int(W * s.shape[0] / s.shape[1])), Image.LANCZOS)
    sw = sw.crop((0, (sw.height - pol) // 2, W, (sw.height - pol) // 2 + pol))
    a = np.asarray(sw.convert("L").convert("RGB")).astype(np.float32) * 0.8
    sw = Image.fromarray(a.astype(np.uint8))
    im.paste(sw, (0, 0))
    fm = ImageFont.truetype(MONO, 30)
    d.text((30, pol - 56), "2026/10/28 21:47:12", font=fm, fill=(235, 235, 235))
    d.ellipse([W - 150, 34, W - 128, 56], fill=(220, 40, 40))
    d.text((W - 118, 28), "REC", font=fm, fill=(235, 235, 235))
    # dół: mój - prawdziwa klatka z banerem
    zr = wycinek(K("znajomi"), (0, 32, 800, 480), W)
    zr = zr.crop((0, zr.height - pol, W, zr.height))
    im.paste(zr, (0, pol))
    d.rectangle([0, pol - 3, W, pol + 3], fill=BIALY)
    kapsle(im, W // 2, 120, ["Zwykły wideorejestrator:", "po prostu nagrywa"], 54)
    kapsle(im, W // 2, pol + 50, ["Mój: wie, że to Ania i Tomek"], 54, tlo=(52, 199, 89), kolor=BIALY)
    drobny(im, FIKCJA, y=pol - 40, size=20, kolor=(200, 200, 200), margines=30, wyrownaj="r") if False else None
    d.text((W - 24, H - 20), "Tablice i osoby fikcyjne", font=F("r", 20), fill=(220, 220, 220), anchor="rd")
    return im


def v04_radar_feed():
    """„Dorobiłem radar” + ogromne 63 km/h z prawdziwego ekranu."""
    W, H = 1080, 1350
    im = Image.new("RGB", (W, H), (8, 8, 10))
    zr = wycinek(K("radar"), (330, 100, 800, 440), W)       # auto + licznik
    im.paste(zr, (0, H - zr.height - 120))
    # przejście od czarnego u góry
    g = Image.new("L", (W, H), 0)
    gd = ImageDraw.Draw(g)
    y0 = H - zr.height - 120
    for i in range(260):
        gd.line([(0, y0 + i), (W, y0 + i)], fill=int(255 * (1 - i / 260)))
    gd.rectangle([0, 0, W, y0], fill=255)
    gd.rectangle([0, H - 120, W, H], fill=255)
    im.paste((8, 8, 10), (0, 0), g)
    size = dopasuj(["Dorobiłem radar", "do wideorejestratora"], "k", 108, W - 100)
    obrys(im, W // 2, 70, ["Dorobiłem {radar}", "do wideorejestratora"], size, akcent=(255, 70, 80))
    akapit(im, W // 2, 70 + size * 2.3, "24 GHz mierzy prędkość aut przed tobą,\nkamera czyta tablicę.",
           F("m", 38), (210, 210, 214), 980, wyrownaj="c")
    drobny(im, RADAR, size=22, margines=40, kolor=(150, 150, 156))
    return im


def v05_sluzbowe_story():
    """Pełny ekran: niebieski baner, napisy jak w rolkach."""
    W, H = 1080, 1920
    f = K("sluzbowe")
    im = rozmyte_tlo(rgb(f), W, H, ciemniej=0.5)
    zr = wycinek(f, (0, 162, 800, 480), W)
    y0 = 640
    im.paste(zr, (0, y0))
    kapsle(im, W // 2, 300, ["Wgrałem własną listę", "pojazdów służbowych"], 68)
    kapsle(im, W // 2, y0 + zr.height + 60, ["Teraz Pi mówi:", "„Uwaga, pojazd służbowy”"], 60,
           tlo=(30, 110, 220), kolor=BIALY)
    drobny(im, SLUZB, size=26, kolor=(225, 225, 230), y=H - 330, margines=70)
    return im


def v06_post_post():
    """Karta posta (bez marki platformy) z twórczym „zbudowałem”."""
    W = H = 1080
    im = Image.new("RGB", (W, H), (236, 238, 242))
    d = ImageDraw.Draw(im)
    x0, y0, x1, y1 = 50, 60, W - 50, H - 60
    d.rounded_rectangle([x0, y0, x1, y1], radius=36, fill=BIALY)
    # „awatar” = mały znak PI LPR
    d.ellipse([x0 + 40, y0 + 40, x0 + 130, y0 + 130], fill=(24, 18, 30))
    lg = vhs.logo_tablica(78)
    im.paste(lg, (x0 + 46, y0 + 85 - lg.height // 2), lg)
    d.text((x0 + 152, y0 + 52), "PiLPR", font=F("b", 38), fill=CZARNY)
    d.text((x0 + 152, y0 + 98), "projekt DIY · 2 godz.", font=F("r", 30), fill=SZARY_TXT)
    y = akapit(im, x0 + 40, y0 + 168, "Zbudowałem wideorejestrator, który czyta tablice "
               "i mówi mi, kiedy mijam znajomych.", F("s", 50), CZARNY, x1 - x0 - 80, lh=1.2)
    y = akapit(im, x0 + 40, y + 14, "Raspberry Pi 5 + Hailo-10H. Bez chmury.", F("r", 42),
               (60, 60, 64), x1 - x0 - 80)
    zr = wycinek(K("znajomi"), (0, 40, 800, 480), x1 - x0 - 80)
    hmax = y1 - 70 - (y + 30)
    if zr.height > hmax:
        zr = zr.crop((0, zr.height - hmax, zr.width, zr.height))
    wklej(im, zr, x0 + 40, y + 30, r=28)
    d.text((W // 2, H - 26), "Tablice i osoby fikcyjne", font=F("r", 20), fill=SZARY_TXT, anchor="md")
    return im


def v07_menu_post():
    """Prawdziwe menu kafelkowe na całość + jedno pytanie."""
    W = H = 1080
    f = K("menu")
    im = Image.new("RGB", (W, H), (12, 9, 15))
    zr = wycinek(f, (40, 40, 760, 450), W)
    im.paste(zr, (0, H - zr.height))
    g = Image.new("L", (W, H), 0)
    gd = ImageDraw.Draw(g)
    y0 = H - zr.height
    for i in range(140):
        gd.line([(0, y0 + i), (W, y0 + i)], fill=int(255 * (1 - i / 140)))
    im.paste((12, 9, 15), (0, 0), g)
    size = dopasuj(["Aplikacja w telefonie?", "Nie trzeba."], "k", 96, W - 100)
    obrys(im, W // 2, 60, ["Aplikacja w telefonie?", "{Nie trzeba.}"], size, akcent=(255, 214, 10))
    akapit(im, W // 2, 60 + size * 2.25, "Wszystko klikasz na ekranie w aucie.", F("m", 38),
           (220, 220, 225), 980, wyrownaj="c")
    return im


def v08_dane_story():
    """„Gdzie są moje dane?” + prawdziwy panel www na telefonie."""
    W, H = 1080, 1920
    scr = Image.open(os.path.join(IMG, "www_telefon.png")).convert("RGB")
    im = rozmyte_tlo(scr, W, H, ciemniej=0.3, blur=36)
    d = ImageDraw.Draw(im)
    obrys(im, W // 2, 230, ["Gdzie są", "moje dane?"], 140)
    kapsle(im, W // 2, 560, ["W moim aucie. Na pendrivie."], 58)
    # telefon płasko, ekran przycięty dołem przez krawędź ujęcia
    sw = 760
    s = sw / scr.width
    ph = scr.resize((sw, int(scr.height * s)), Image.LANCZOS)
    x, y = (W - sw) // 2, 740
    ph = ph.crop((0, 0, sw, 1640 - y))
    d.rounded_rectangle([x - 24, y - 24, x + sw + 24, y + ph.height + 200], radius=80, fill=(10, 10, 12),
                        outline=(120, 120, 128), width=4)
    wklej(im, ph, x, y, r=56)
    d.rectangle([0, 1640, W, H], fill=(14, 12, 18))
    akapit(im, W // 2, 1668, "Panel www prosto z urządzenia. Bez konta.",
           F("s", 34), BIALY, 980, wyrownaj="c")
    drobny(im, "Zrzut prawdziwego panelu PiLPR, tablice fikcyjne.", size=22, kolor=(150, 150, 156), y=1740)
    return im


GRAFIKI = [
    ("01_czat_story.png", v01_czat_story),
    ("02_notatka_feed.png", v02_notatka_feed),
    ("03_porownanie_feed.png", v03_porownanie_feed),
    ("04_radar_feed.png", v04_radar_feed),
    ("05_sluzbowe_story.png", v05_sluzbowe_story),
    ("06_post_post.png", v06_post_post),
    ("07_menu_post.png", v07_menu_post),
    ("08_dane_story.png", v08_dane_story),
]


def przeglad(sciezki, out, hh=640, nazwa="przeglad.png"):
    ims = [Image.open(p).convert("RGB") for p in sciezki]
    ims = [i.resize((int(i.width * hh / i.height), hh), Image.LANCZOS) for i in ims]
    gap, lab = 24, 44
    na_rzad = 4
    rzedy = [ims[i:i + na_rzad] for i in range(0, len(ims), na_rzad)]
    Wt = max(sum(i.width for i in r) + gap * (len(r) + 1) for r in rzedy)
    Ht = len(rzedy) * (hh + lab + gap) + gap
    S = Image.new("RGB", (Wt, Ht), (28, 28, 32))
    y, k = gap, 0
    d = ImageDraw.Draw(S)
    for r in rzedy:
        x = gap
        for i in r:
            S.paste(i, (x, y))
            d.text((x, y + hh + 8), os.path.basename(sciezki[k]), font=F("m", 26), fill=(200, 200, 205))
            x += i.width + gap; k += 1
        y += hh + lab + gap
    p = os.path.join(out, nazwa)
    S.save(p, optimize=True)
    print("zapisano", p)


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else OUT
    filtr = sys.argv[2:]
    os.makedirs(out, exist_ok=True)
    sciezki = []
    for nazwa, fn in GRAFIKI:
        p = os.path.join(out, nazwa)
        if not filtr or any(f in nazwa for f in filtr):
            fn().convert("RGB").save(p, optimize=True)
            print("zapisano", p)
        if os.path.exists(p):
            sciezki.append(p)
    if not filtr or "przeglad" in filtr:
        przeglad(sciezki, out)
