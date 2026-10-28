#!/usr/bin/env python3
"""Rolki pionowe PiLPR (Instagram / TikTok / Shorts): 1080×1920, 30 kl/s,
H.264 yuv420p + AAC, +faststart.

    python3 docs/reklamy/rolki.py A            # „Mijasz znajomych”
    python3 docs/reklamy/rolki.py B            # „Co potrafi”
    python3 docs/reklamy/rolki.py C            # „Radar na osiedlu”
    python3 docs/reklamy/rolki.py all --okladki
    python3 docs/reklamy/rolki.py A --podglad 0.5,6,12   # same klatki PNG

Ekran urządzenia to prawdziwe rysowanie z pi/lpr.py (kit.py), kabina
z kokpit.py, styl VHS z docs/obrazy_demo/vhs.py. Muzyka i dźwięki są
syntezowane tutaj (numpy). Głos: Piper, jak w urządzeniu — opcjonalnie,
gdy ustawione PILPR_PIPER_GLOS (ścieżka .onnx) i PILPR_PIPER_LIB
(katalog z pakietem piper-tts); bez nich rolka powstaje bez głosu.
Wszystkie tablice są fikcyjne.

Zmienne: ROLKI_TMP (klatki/cache, domyślnie /tmp/claude-0/rolki),
ROLKI_OUT (gdzie trafiają mp4/png), ROLKI_PROC (liczba procesów).
"""
import os, sys, argparse, subprocess, pickle, hashlib, wave, time, shutil, math
from datetime import datetime, timedelta
from functools import lru_cache
from multiprocessing import Pool
import numpy as np, cv2
from PIL import Image, ImageDraw, ImageFilter

TU = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TU)
import kit, kokpit, vhs  # noqa: E402

W, H, FPS = 1080, 1920, 30
SR = 44100
GORA, DOL = 250, H - 350          # strefa bezpieczna (UI aplikacji)
TMP = os.environ.get("ROLKI_TMP", "/tmp/claude-0/rolki")
OUT = os.environ.get("ROLKI_OUT", "/mnt/project-files/pilpr/reklamy/rolki")
REPO = os.path.abspath(os.path.join(TU, "..", ".."))
ZIEL = (84, 214, 120)
NIEB = (80, 140, 245)
CZER = (240, 84, 96)


def clamp(x, a=0.0, b=1.0):
    return max(a, min(b, x))


def ease(x):
    x = clamp(x)
    return x * x * (3 - 2 * x)


def lin(T, t0, t1, a, b, f=ease):
    return a + (b - a) * f((T - t0) / max(1e-6, t1 - t0))


# ───────────────────────── sceny (cache na dysku) ─────────────────────────

_sc_mem = {}


def scena(plate, car_w, car_by, car_cx=0.52, body=(52, 70, 98), seed=3):
    key = (plate, int(car_w), round(car_by, 3), round(car_cx, 3), tuple(body), seed)
    if key in _sc_mem:
        return _sc_mem[key]
    d = os.path.join(TMP, "sceny")
    os.makedirs(d, exist_ok=True)
    fn = os.path.join(d, hashlib.md5(repr(key).encode()).hexdigest() + ".pkl")
    lock = fn + ".lock"
    while not os.path.exists(fn):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL)
        except FileExistsError:          # liczy inny proces
            time.sleep(0.2)
            try:
                if time.time() - os.path.getmtime(lock) > 120:
                    os.remove(lock)
            except OSError:
                pass
            continue
        os.close(fd)
        try:
            sc = kit.scena(plate, car_w=int(car_w), car_by=car_by, car_cx=car_cx,
                           body=tuple(body), seed=seed)
            with open(fn + ".tmp", "wb") as f:
                pickle.dump(sc, f, protocol=4)
            os.replace(fn + ".tmp", fn)
        finally:
            try:
                os.remove(lock)
            except OSError:
                pass
    with open(fn, "rb") as f:
        sc = pickle.load(f)
    if len(_sc_mem) > 10:
        _sc_mem.clear()
    _sc_mem[key] = sc
    return sc


def auto(p, plate, body, seed=3, w0=180, w1=740, cx0=0.52, cx1=None, krok=16):
    """Auto przed nami w odległości opisanej postępem p (0 daleko → 1 blisko).
    Szerokość kwantowana co `krok` px – ograniczona liczba klatek kluczowych."""
    w = w0 + (w1 - w0) * clamp(p)
    w = int(round(w / krok) * krok)
    by = 0.60 + 0.30 * (w - 180) / 580
    cx = cx0 if cx1 is None else cx0 + (cx1 - cx0) * clamp(p)
    return scena(plate, w, min(by, 0.97), round(cx, 2), body, seed)


# ───────────────────────────── grafika ─────────────────────────────

def kabina(sc_rgb, dev, T, zoom=1.0, fx=W / 2, fy=H * 0.66, bob=True, **kw):
    """Ujęcie z kabiny; zoom > 1 to najazd kamery na punkt (fx, fy)."""
    s = clamp(zoom, 1.0, 1.6)
    Ws, Hs = int(W * s) // 2 * 2, int(H * s) // 2 * 2
    if bob:
        kw.setdefault("dy", 0.004 * math.sin(T * 2 * math.pi * 1.1))
        kw.setdefault("dx", 0.002 * math.sin(T * 2 * math.pi * 0.37))
    im = kokpit.kokpit(sc_rgb, dev, W=Ws, H=Hs, **kw)
    if zoom <= 1.0001:
        return im if im.size == (W, H) else im.resize((W, H), Image.LANCZOS)
    cw, ch = Ws / zoom * (W / Ws) * s, Hs / zoom * (H / Hs) * s
    cx, cy = fx * s, fy * s
    x0 = clamp(cx - cw / 2, 0, Ws - cw); y0 = clamp(cy - ch / 2, 0, Hs - ch)
    return im.resize((W, H), Image.LANCZOS, box=(x0, y0, x0 + cw, y0 + ch))


def zdjecie_rgba(foto_bgr, szer, kat=0.0, ramka=12):
    """Wydruk zdjęcia z fotoradaru: biała ramka, lekki obrót, cień."""
    im = Image.fromarray(cv2.cvtColor(foto_bgr, cv2.COLOR_BGR2RGB))
    h = int(szer * im.height / im.width)
    im = im.resize((szer, h), Image.LANCZOS)
    r = Image.new("RGBA", (szer + 2 * ramka, h + 2 * ramka), (236, 230, 236, 255))
    r.paste(im, (ramka, ramka))
    pad = 40
    out = Image.new("RGBA", (r.width + 2 * pad, r.height + 2 * pad), (0, 0, 0, 0))
    sh = Image.new("L", out.size, 0)
    ImageDraw.Draw(sh).rectangle([pad + 8, pad + 16, pad + r.width + 8, pad + r.height + 16], fill=170)
    out.paste((0, 0, 0, 255), (0, 0), sh.filter(ImageFilter.GaussianBlur(16)))
    out.paste(r, (pad, pad), r)
    return out.rotate(kat, resample=Image.BICUBIC, expand=True) if kat else out


def blysk(im, s):
    """Błysk lampy fotoradaru (s 0..1)."""
    if s <= 0:
        return im
    return Image.blend(im, Image.new("RGB", im.size, (255, 246, 250)), clamp(s) * 0.85)


def glitch(im, s, seed):
    """Zakłócenie VHS: przesunięte pasy, rozjechane kanały, smugi."""
    if s <= 0.02:
        return im
    rng = np.random.default_rng(seed)
    a = np.asarray(im).copy()
    h, w = a.shape[:2]
    for _ in range(int(5 + 16 * s)):
        y = int(rng.integers(0, h)); hh = int(rng.integers(6, int(14 + 150 * s)))
        a[y:y + hh] = np.roll(a[y:y + hh], int(rng.normal(0, 70 * s)), axis=1)
    k = int(3 + 16 * s)
    o = a.copy()
    o[..., 0] = np.roll(a[..., 0], k, axis=1)
    o[..., 2] = np.roll(a[..., 2], -k, axis=1)
    im = Image.fromarray(o)
    d = ImageDraw.Draw(im, "RGBA")
    for _ in range(int(4 + 14 * s)):
        y = int(rng.uniform(0, h)); x = int(rng.uniform(-0.2, 0.9) * w)
        kol = [vhs.MAGENTA, vhs.CYJAN, (240, 230, 240)][int(rng.integers(0, 3))]
        d.rectangle([x, y, x + int(rng.uniform(0.1, 0.5) * w), y + int(rng.integers(2, 8))],
                    fill=kol + (int(80 + 140 * s),))
    if s > 0.6:
        y = int(rng.uniform(0.1, 0.9) * h)
        szum = rng.integers(0, 255, (int(30 * s), w // 4, 1), dtype=np.uint8).repeat(4, 1).repeat(3, 2)
        b = np.asarray(im).copy()
        b[y:y + szum.shape[0]] = np.maximum(b[y:y + szum.shape[0]], szum[: max(0, min(szum.shape[0], h - y))] // 2)
        im = Image.fromarray(b)
    return im


# ───────────────────────────── napisy ─────────────────────────────

FONT_NAPIS = next((f for f in ("/usr/share/fonts/opentype/inter/Inter-ExtraBold.otf",
                               "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf") if os.path.exists(f)),
                  vhs.VT323)


@lru_cache(maxsize=32)
def fnap(size):
    from PIL import ImageFont
    return ImageFont.truetype(FONT_NAPIS, size)


def zawin(tekst, f, szer):
    linie = []
    for akap in tekst.split("\n"):
        akt = ""
        for sl in akap.split(" "):
            p = (akt + " " + sl).strip()
            if f.getlength(p) > szer and akt:
                linie.append(akt); akt = sl
            else:
                akt = p
        linie.append(akt)
    return linie


class Napis:
    """Napis w stylu TikToka, czytelny bez dźwięku.
    styl "box": biała ramka pod każdą linią, czarny tekst;
    styl "obrys": biały tekst z czarnym obrysem.
    y = górna krawędź bloku (albo dolna, gdy dol=True)."""

    def __init__(self, t0, t1, tekst, y=GORA + 40, size=80, styl="box", dol=False, szer=960,
                 tlo=(255, 255, 255), kol=(12, 10, 14), wejscie=True):
        self.__dict__.update(locals())
        del self.__dict__["self"]

    def rysuj(self, im, T):
        if not (self.t0 <= T < self.t1):
            return im
        f = fnap(self.size)
        linie = zawin(self.tekst, f, self.szer - 2 * int(self.size * 0.35))
        lh = int(self.size * 1.22)
        bh = lh * len(linie)
        y0 = self.y - bh if self.dol else self.y
        # krótkie „wyskoczenie” (bez czekania na tekst – pierwsza klatka już czytelna)
        dt = T - self.t0
        sk = 1.0 if not self.wejscie or self.t0 == 0 else 0.92 + 0.08 * ease(dt / 0.08)
        warstwa = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(warstwa)
        px = int(self.size * 0.32)
        for i, l in enumerate(linie):
            tw = f.getlength(l)
            cy = y0 + i * lh + lh / 2
            if self.styl == "box":
                x0, x1 = W / 2 - tw / 2 - px, W / 2 + tw / 2 + px
                d.rounded_rectangle([x0, cy - lh / 2 - 1, x1, cy + lh / 2 + 1], radius=int(self.size * 0.22),
                                    fill=self.tlo + (255,))
                d.text((W / 2, cy), l, font=f, fill=self.kol, anchor="mm")
            else:
                d.text((W / 2, cy), l, font=f, fill=(255, 255, 255), anchor="mm",
                       stroke_width=max(4, self.size // 11), stroke_fill=(0, 0, 0))
        if sk < 1:
            bb = (0, int(y0 - 10), W, int(y0 + bh + 10))
            kaw = warstwa.crop(bb)
            nw, nh = int(kaw.width * sk), int(kaw.height * sk)
            kaw = kaw.resize((nw, nh), Image.BILINEAR)
            warstwa = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            warstwa.paste(kaw, (int((W - nw) / 2), int(bb[1] + (kaw.height / sk - nh) / 2)))
        im = im.convert("RGBA")
        im.alpha_composite(warstwa)
        return im.convert("RGB")


# ───────────────────────── ekran urządzenia na wprost ─────────────────────────

def _rgb(dev):
    return Image.fromarray(cv2.cvtColor(dev, cv2.COLOR_BGR2RGB))


def tlo_z(dev, ciemniej=0.38):
    """Tło całego kadru: ten sam ekran, powiększony, rozmyty, przyciemniony."""
    a = cv2.resize(dev, (int(H * 800 / 480), H), interpolation=cv2.INTER_LINEAR)
    x0 = (a.shape[1] - W) // 2
    a = a[:, x0:x0 + W]
    a = cv2.GaussianBlur(cv2.resize(a, (W // 8, H // 8), interpolation=cv2.INTER_AREA), (0, 0), 3)
    a = cv2.resize(a, (W, H), interpolation=cv2.INTER_LINEAR)
    a = (a.astype(np.float32) * ciemniej + np.array([18, 6, 10], np.float32)).clip(0, 255).astype(np.uint8)
    return _rgb(a)


def wycinek(dev, box, szer):
    """Wycinek ekranu 800×480 (x0, y0, x1, y1) powiększony do szerokości `szer`."""
    x0, y0, x1, y1 = box
    k = szer / (x1 - x0)
    M = np.float32([[k, 0, -x0 * k], [0, k, -y0 * k]])
    out = cv2.warpAffine(dev, M, (int(szer), int(round((y1 - y0) * k))), flags=cv2.INTER_CUBIC,
                         borderMode=cv2.BORDER_REPLICATE)
    return _rgb(out)


def wklej(im, el, x, y, cien=True):
    if cien:
        sh = Image.new("L", im.size, 0)
        ImageDraw.Draw(sh).rectangle([x + 6, y + 14, x + el.width + 6, y + el.height + 14], fill=160)
        im.paste((0, 0, 0), (0, 0), sh.filter(ImageFilter.GaussianBlur(18)))
    im.paste(el, (int(x), int(y)), el if el.mode == "RGBA" else None)
    return (x, y, x + el.width, y + el.height)


def ekran(dev, kadry, tlo_dev=None):
    """Kadr pionowy: rozmyte tło + ostre wycinki prawdziwego ekranu.
    kadry: lista (box800, y_góra, szerokość)."""
    im = tlo_z(dev if tlo_dev is None else tlo_dev)
    for box, y, sz in kadry:
        el = wycinek(dev, box, sz)
        wklej(im, el, (W - sz) / 2, y)
    return im


def box_lerp(a, b, u):
    u = ease(u)
    return tuple(a[i] + (b[i] - a[i]) * u for i in range(4))


def box_auta(sc, marg=0.35, aspekt=1.0, dol=0.0):
    """Box (800×480) wokół auta ze sceny, o zadanym aspekcie szer/wys."""
    cb = sc[1]
    x0, x1 = cb[0] * 800, cb[2] * 800
    y0, y1 = cb[1] * 450 + 15, cb[3] * 450 + 15
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2 + dol
    w = (x1 - x0) * (1 + 2 * marg)
    h = w / aspekt
    h = max(h, (y1 - y0) * (1 + 2 * marg)); w = h * aspekt
    return (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)


# kadry alertu: zdjęcie auta + dymek z tablicą (prawa część ekranu) i pasek banera
ALERT_GORA = (392, 42, 800, 402)
ALERT_PASEK = (0, 404, 640, 480)


def alert_kadr(dev, y=620, szer=900):
    h1 = szer * (ALERT_GORA[3] - ALERT_GORA[1]) / (ALERT_GORA[2] - ALERT_GORA[0])
    return ekran(dev, [(ALERT_GORA, y, szer), (ALERT_PASEK, y + h1 + 14, szer)])


# ───────────────────────────── plansza końcowa ─────────────────────────────

@lru_cache(maxsize=2)
def _logo(szer):
    return vhs.logo_tablica(szer)


@lru_cache(maxsize=8)
def _tlo(seed):
    return vhs.tlo(W, H, seed=seed, smugi=22, gwiazdki=7)


def tlo(seed=2):
    return _tlo(seed).copy()


def plansza_koncowa(t, linie, cta, seed=4):
    im = tlo(seed)
    lg = _logo(900)
    im.paste(lg, ((W - lg.width) // 2, 500), lg)
    d = ImageDraw.Draw(im)
    y = 790
    for txt, size, kol in linie:
        f = fnap(size)
        d.text((W / 2, y), txt, font=f, fill=kol, anchor="mt")
        y += int(size * 1.35)
    im = Napis(0, 99, cta, y=y + 60, size=72, wejscie=False).rysuj(im, t)
    f = fnap(34)
    ImageDraw.Draw(im).text((W / 2, DOL - 40), "Tablice na filmie są fikcyjne", font=f,
                            fill=vhs.PRZYGASZONY, anchor="mb")
    im = vhs.linie(im, okres=3, sila=0.10)
    if t < 0.2:
        im = glitch(im, 1 - t / 0.2, int(t * 1000) + seed)
    return im


KONIEC = [("Raspberry Pi 5 + Hailo-10H", 64, vhs.TEKST),
          ("czyta tablice w aucie, w pełni offline", 46, vhs.PRZYGASZONY),
          ("i nagrywa jak wideorejestrator", 46, vhs.PRZYGASZONY)]
CTA = "Obserwuj — pokażę, jak go złożyć"


# ───────────────────────────── rolki ─────────────────────────────

class Rolka:
    def __init__(self, nazwa, plik, dl):
        self.nazwa, self.plik, self.dl = nazwa, plik, dl
        self.seg = []          # (t0, t1, fn(t, T), glitch_na_wejsciu)
        self.napisy = []
        self.dzwieki = []      # (T, rodzaj, arg)
        self.glosy = []        # (T, tekst)
        self.okladka = None    # (T, tytuł)

    def s(self, t0, t1, glitch=False):
        def dek(fn):
            self.seg.append((t0, t1, fn, glitch))
            if glitch and t0 > 0:
                self.dzwieki.append((t0 - 0.04, "glitch", None))
            return fn
        return dek

    def n(self, *a, **k):
        self.napisy.append(Napis(*a, **k))

    def klatka(self, T, napisy=True):
        for t0, t1, fn, _ in self.seg:
            if t0 <= T < t1:
                im = fn(T - t0, T)
                break
        else:
            im = Image.new("RGB", (W, H))
        if napisy:
            for n in self.napisy:
                im = n.rysuj(im, T)
        for t0, t1, fn, g in self.seg:
            if g and t0 > 0 and abs(T - t0) < 0.1:
                im = glitch(im, 1 - abs(T - t0) / 0.1, int(T * 1000))
        return im


def zegar(h, m, s):
    return lambda T: datetime(2026, 10, 27, h, m, s) + timedelta(seconds=T)


@lru_cache(maxsize=8)
def www(nazwa, box=None):
    im = Image.open(os.path.join(REPO, "docs/images", nazwa)).convert("RGB")
    return im.crop(box) if box else im


def rolka_A():
    r = Rolka("A", "rolka_A_znajomi", 17.0)
    TABL, BODY, NOTA = "WX 5678K", (66, 92, 128), "Ania i Tomek"
    zg = zegar(22, 41, 3)
    BLISKO = 0.95

    def dev(sc, T, stan="pewne", wiek=1.0, alert=True):
        f = kit.device_frame(sc, stan, TABL if stan != "szukam" else "",
                             notif_wiek=wiek if stan == "pewne" else None,
                             ostatnia=TABL if stan == "pewne" else "", zegar=zg(T))
        return kit.alert(f, sc, TABL, "znajomi", NOTA) if alert else f

    # 0–2.2 HAK: od pierwszej klatki zielony alert na prawdziwym ekranie
    @r.s(0.0, 2.2)
    def _(t, T):
        sc = auto(BLISKO, TABL, BODY)
        return alert_kadr(dev(sc, T, wiek=0.6 + t), szer=int(lin(t, 0, 2.2, 900, 940, f=lambda x: x)))

    # 2.2–4.2 jak to działa: kamera łapie auto daleko przed nami
    @r.s(2.2, 4.2)
    def _(t, T):
        sc = auto(0.15 + 0.25 * t / 2, TABL, BODY)
        f = dev(sc, T, "szukam", alert=False)
        b = box_lerp((170, 40, 630, 440), (260, 150, 540, 394), t / 2.0)
        return ekran(f, [(b, 580, 1000)])

    # 4.2–6.2 zbliżenie na auto: czytanie → pewny odczyt (pik)
    @r.s(4.2, 6.2)
    def _(t, T):
        sc = auto(0.45 + 0.4 * t / 2, TABL, BODY)
        f = dev(sc, T, "czytam" if t < 1.0 else "pewne", wiek=t - 1.0, alert=False)
        return ekran(f, [(box_auta(sc, 0.5, 1.0, dol=6), 520, 1000)])

    # 6.2–8.0 dymek z tablicą + cały ekran
    @r.s(6.2, 8.0)
    def _(t, T):
        sc = auto(0.88, TABL, BODY)
        f = dev(sc, T, wiek=1.4 + t, alert=False)
        return ekran(f, [((508, 44, 800, 152), 560, 1000), ((0, 0, 800, 480), 1010, 900)])

    # 8.0–10.2 alert: baner, zdjęcie, głos
    @r.s(8.0, 10.2)
    def _(t, T):
        sc = auto(BLISKO, TABL, BODY)
        return alert_kadr(dev(sc, T, wiek=1.0 + t, alert=t > 0.1), y=560)

    # 10.2–12.0 krótko: tak to wygląda w aucie
    @r.s(10.2, 12.0)
    def _(t, T):
        sc = auto(BLISKO, TABL, BODY)
        return kabina(sc[0], dev(sc, T, wiek=2.0), T, zoom=lin(t, 0, 1.8, 1.3, 1.45),
                      fy=H * 0.62, poswiata=(60, 150, 80))

    # 12.0–14.4 listę tworzysz sam: prawdziwy panel www „Bazy obserwowanych”
    @r.s(12.0, 14.4)
    def _(t, T):
        im = tlo(6)
        y = 600
        for i, bx in enumerate([(415, 165, 790, 285), (415, 452, 790, 595)]):
            if t < 0.3 * i:
                continue
            p = www("www_bazy.png", bx)
            p = p.resize((1000, int(1000 * p.height / p.width)), Image.LANCZOS)
            wklej(im, p, 40, y)
            y += p.height + 30
        return im

    @r.s(14.4, 17.0, glitch=True)
    def _(t, T):
        return plansza_koncowa(t, KONIEC, CTA)

    r.n(0.0, 2.2, "Mój dashcam wie,\nkiedy mijam znajomych", size=82, szer=1060)
    r.n(2.2, 4.2, "Kamera łapie auto przede mną…", size=76)
    r.n(4.2, 6.2, "…a malinka czyta tablicę na żywo", size=76)
    r.n(6.2, 8.0, "Ta tablica jest na mojej liście „Znajomi”", size=76)
    r.n(8.0, 10.2, "Zielony alert, zdjęcie auta i głos", size=76)
    r.n(8.2, 10.2, "„Mijasz znajomych”", y=DOL - 6, size=66, styl="obrys", dol=True)
    r.n(10.2, 12.0, "Działa w aucie. W pełni offline.", size=76)
    r.n(12.0, 14.4, "Listę znajomych tworzę sam, w telefonie", size=76)
    r.dzwieki += [(0.05, "dzwonek", None), (5.2, "beep", None), (8.1, "dzwonek", None)]
    r.glosy += [(0.45, "Mijasz znajomych."), (8.6, "Mijasz znajomych. " + kit.L._spell_plate(TABL) + ".")]
    r.okladka = (0.6, "Mój dashcam wie,\nkiedy mijam znajomych")
    return r


def rolka_B():
    r = Rolka("B", "rolka_B_co_potrafi", 19.0)
    zg = zegar(21, 7, 12)

    TZ, BZ = "WX 2468E", (70, 110, 80)

    @r.s(0.0, 2.0)
    def _(t, T):
        sc = auto(0.95, TZ, BZ, seed=6)
        f = kit.device_frame(sc, "pewne", TZ, notif_wiek=0.6 + t, ostatnia=TZ, zegar=zg(T))
        return alert_kadr(kit.alert(f, sc, TZ, "znajomi", "Kasia"), y=560)

    TB, BB = "WX 1357C", (120, 60, 70)

    @r.s(2.0, 4.0)
    def _(t, T):
        sc = auto(0.4 + 0.45 * t / 2, TB, BB, seed=4)
        st = "czytam" if t < 0.9 else "pewne"
        f = kit.device_frame(sc, st, TB, notif_wiek=t - 0.9 if st == "pewne" else None,
                             ostatnia=TB if st == "pewne" else "", zegar=zg(T))
        return ekran(f, [(box_auta(sc, 0.5, 1.0, dol=6), 560, 1000)])

    TS, BS = "WX 7710S", (48, 50, 56)

    @r.s(4.0, 6.2)
    def _(t, T):
        sc = auto(0.95, TS, BS, seed=8)
        f = kit.device_frame(sc, "pewne", TS, notif_wiek=0.6 + t, ostatnia=TS, zegar=zg(T))
        if t > 0.08:
            f = kit.alert(f, sc, TS, "sluzbowe", "z mojej listy")
        return alert_kadr(f, y=560)

    TR, BR = "WX 4096R", (150, 150, 158)
    RADAR = (310, 95, 800, 434)

    @r.s(6.2, 8.2)
    def _(t, T):
        sc = auto(0.75, TR, BR, seed=9)
        v = round(63 * (1 - math.exp(-t * 5)))
        f = kit.device_frame(sc, "pewne", TR, ostatnia=TR, radar=True, radar_v=v, radar_prog=50, zegar=zg(T))
        return ekran(f, [(RADAR, 600, 1080)])

    @lru_cache(maxsize=1)
    def foto():
        sc = auto(0.75, TR, BR, seed=9)
        return kit.radar_foto(sc, TR, 63, datetime(2026, 10, 27, 21, 7, 20))

    @r.s(8.2, 10.2)
    def _(t, T):
        f = foto()
        im = tlo_z(cv2.resize(f, (800, 450)))
        el = _rgb(f).resize((1080, 608), Image.LANCZOS)
        wklej(im, el, 0, 620)
        z = (1700, 20, 2290, 150)          # tablica z rogu zdjęcia, powiększona
        wklej(im, _rgb(f).crop(z).resize((720, int(720 * 130 / 590)), Image.LANCZOS), 180, 1260)
        return blysk(im, 1 - t / 0.25 if t < 0.25 else 0)

    MENU = [("main", 0), ("main", 2), ("main", 3), ("main", 11), ("tools", 0), ("tools", 1), ("tools", 4)]
    KROK = 0.34

    def kafel(view, sel):
        k = 4 if view == "main" else 3
        return 10 + (780 / k) * (sel % k + 0.5), 47 + 134 * (sel // k) + 62

    @r.s(10.2, 12.8)
    def _(t, T):
        sc = auto(0.4, "WX 5150A", (52, 70, 98), seed=3)
        base = kit.device_frame(sc, "szukam", zegar=zg(T))
        k = min(len(MENU) - 1, int(t / KROK)); tk = t - k * KROK
        view, sel = MENU[k]
        f = kit.menu(base, view, sel)
        # zbliżenie: ekran szerszy niż kadr, kamera jedzie za wybranym kaflem
        szer = 1700
        def srodek(v, s_):
            x_, _ = kafel(v, s_)
            return clamp(W / 2 - x_ * szer / 800 + szer / 2, W - szer / 2, szer / 2)
        pv = MENU[max(0, k - 1)]
        cx = srodek(*pv) + (srodek(view, sel) - srodek(*pv)) * ease(tk / 0.15)
        x0 = (szer / 2 - cx) * 800 / szer
        box = (x0, 0, x0 + 800 * W / szer, 480)
        y_el = 560
        im = ekran(f, [(box, y_el, W)])
        x, y = kafel(view, sel)
        sk = szer / 800
        px, py = (x - x0) * sk, y_el + y * sk
        if 0.04 <= tk < 0.32:
            u = (tk - 0.04) / 0.28
            rr = 44 + 100 * u
            d = ImageDraw.Draw(im, "RGBA")
            d.ellipse([px - rr, py - rr, px + rr, py + rr], outline=(255, 255, 255, int(230 * (1 - u))), width=7)
            d.ellipse([px - 36, py - 36, px + 36, py + 36], fill=(255, 255, 255, 120))
        return im

    for k in range(len(MENU)):
        r.dzwieki.append((10.2 + 0.05 + k * KROK, "tik", None))

    @r.s(12.8, 14.8)
    def _(t, T):
        im = tlo(12)
        ph = www("www_telefon.png")
        sw = 760
        sk = sw / ph.width
        okno_h = 940
        przes = lin(t, 0.3, 2.0, 0, 520)
        ekr = ph.resize((sw, int(ph.height * sk)), Image.LANCZOS).crop((0, int(przes), sw, int(przes) + okno_h))
        ram = 22
        tel = Image.new("RGBA", (sw + 2 * ram, okno_h + 2 * ram), (0, 0, 0, 0))
        d = ImageDraw.Draw(tel)
        d.rounded_rectangle([0, 0, tel.width - 1, tel.height - 1], radius=56, fill=(14, 11, 16, 255),
                            outline=(90, 78, 100, 255), width=4)
        tel.paste(ekr, (ram, ram))
        wklej(im, tel, (W - tel.width) / 2, 580)
        return im

    @r.s(14.8, 16.4)
    def _(t, T):
        sc = auto(0.95, TZ, BZ, seed=6)
        f = kit.device_frame(sc, "pewne", TZ, notif_wiek=1.5, ostatnia=TZ, zegar=zg(T))
        f = kit.alert(f, sc, TZ, "znajomi", "Kasia")
        return kabina(sc[0], f, T, zoom=lin(t, 0, 1.6, 1.3, 1.42), fy=H * 0.62, poswiata=(60, 150, 80))

    @r.s(16.4, 19.0, glitch=True)
    def _(t, T):
        return plansza_koncowa(t, KONIEC, CTA, seed=13)

    r.n(0.0, 2.0, "Zbudowałem dashcam,\nktóry to potrafi:", size=84, szer=1060)
    r.n(0.0, 2.0, "1. mijam znajomych → alert", y=DOL - 6, size=60, styl="obrys", dol=True, szer=1060)
    r.n(2.0, 4.0, "2. czyta tablice na żywo", size=80)
    r.n(4.0, 6.2, "3. pojazdy służbowe → niebieski alert", size=80)
    r.n(4.0, 6.2, "z listy, którą tworzysz sam", y=DOL - 6, size=60, styl="obrys", dol=True, szer=1060)
    r.n(6.2, 8.2, "4. radar 24 GHz: prędkość na żywo", size=80)
    r.n(8.2, 10.2, "…i zdjęcie jak z fotoradaru", size=80)
    r.n(10.2, 12.8, "5. menu na dotyk w aucie", size=80)
    r.n(12.8, 14.8, "6. podgląd i historia w telefonie", size=80)
    r.n(14.8, 16.4, "Wszystko w aucie, w pełni offline", size=80)
    r.dzwieki += [(0.05, "dzwonek", None), (2.9, "beep", None), (4.1, "dzwonek2", None), (8.2, "migawka", None)]
    r.glosy += [(0.45, "Mijasz znajomych."), (4.55, "Uwaga, pojazd służbowy.")]
    r.okladka = (0.6, "Zbudowałem dashcam,\nktóry to potrafi")
    return r


def rolka_C():
    r = Rolka("C", "rolka_C_radar", 18.0)
    PROG = 50
    zg = zegar(21, 13, 52)
    RADAR = (310, 95, 800, 434)

    def odjazd(p, plate, body, seed, w0=820, w1=300):
        return auto(1 - clamp(p), plate, body, seed=seed, w0=w1, w1=w0)

    def dev(sc, plate, T, v, czyt=True):
        return kit.device_frame(sc, "pewne" if czyt else "czytam", plate, ostatnia=plate if czyt else "",
                                radar=True, radar_v=v, radar_prog=PROG, zegar=zg(T))

    def pomiar(t, v, t0=0.0):
        return 0 if t < t0 else round(v * (1 - math.exp(-(t - t0) * 7)) + 0.4 * math.sin(t * 9))

    A3, B3 = "WX 6031N", (40, 92, 74)

    # 0–2.0 HAK: 63 km/h w czerwonej ramce od pierwszej klatki
    @r.s(0.0, 2.0)
    def _(t, T):
        sc = odjazd(0.45 + 0.05 * t, A3, B3, 13)
        f = dev(sc, A3, T, 63)
        b = box_lerp(RADAR, (330, 110, 796, 432), t / 2)
        return blysk(ekran(f, [(b, 600, 1080)]), 1 - (t - 0.15) / 0.25 if 0.15 <= t < 0.4 else 0)

    A1, B1 = "WX 5190P", (128, 44, 54)

    @r.s(2.0, 3.8)
    def _(t, T):
        sc = odjazd(0.2 + t / 3.0, A1, B1, 11)
        return ekran(dev(sc, A1, T, pomiar(t, 29)), [(RADAR, 600, 1080)])

    A2, B2 = "WX 8842T", (172, 172, 166)

    @r.s(3.8, 5.6)
    def _(t, T):
        sc = odjazd(0.2 + t / 3.0, A2, B2, 12)
        return ekran(dev(sc, A2, T, pomiar(t, 44)), [(RADAR, 600, 1080)])

    @r.s(5.6, 7.8)
    def _(t, T):
        sc = odjazd(0.1 + t / 4.0, A3, B3, 13)
        return ekran(dev(sc, A3, T, pomiar(t, 63, 0.1), czyt=t > 0.5), [(RADAR, 600, 1080)])

    @lru_cache(maxsize=4)
    def foto(plate, body, seed, v, hh, mm, ss):
        sc = odjazd(0.45, plate, body, seed)
        return kit.radar_foto(sc, plate, v, datetime(2026, 10, 27, hh, mm, ss))

    F3 = (A3, B3, 13, 63, 21, 14, 7)

    @r.s(7.8, 9.8)
    def _(t, T):
        f = foto(*F3)
        im = tlo_z(cv2.resize(f, (800, 450)))
        z = lin(t, 0, 2, 1.0, 1.08, f=lambda x: x)
        cw, ch = 2304 / z, 1296 / z
        el = _rgb(f).resize((1080, 608), Image.LANCZOS, box=(1152 - cw / 2, 648 - ch / 2,
                                                              1152 + cw / 2, 648 + ch / 2))
        wklej(im, el, 0, 640)
        return blysk(im, 1 - t / 0.3 if t < 0.3 else 0)

    @r.s(9.8, 11.8)
    def _(t, T):
        f = _rgb(foto(*F3))
        im = tlo_z(cv2.resize(foto(*F3), (800, 450)))
        kawalki = [((1700, 18, 2290, 150), 560), ((30, 1080, 880, 1270), 820), ((1740, 1030, 2280, 1250), 1110)]
        for i, (bx, y) in enumerate(kawalki):
            if t < 0.25 * i:
                continue
            sz = 900 if i < 2 else 700
            el = f.crop(bx)
            el = el.resize((sz, int(sz * el.height / el.width)), Image.LANCZOS)
            wklej(im, el, (W - sz) / 2, y)
        return im

    LISTA = [("21:14", 63, A3, B3, 13, (21, 14, 7)), ("18:02", 58, "WX 2719L", (90, 60, 120), 16, (18, 2, 41)),
             ("07:48", 55, "WX 3380D", (150, 120, 60), 17, (7, 48, 15))]

    @r.s(11.8, 14.6)
    def _(t, T):
        im = tlo(16)
        d = ImageDraw.Draw(im)
        for i, (g, v, pl, bd, sd, hms) in enumerate(LISTA):
            tt = t - 0.15 - 0.3 * i
            if tt < 0:
                continue
            y = 560 + i * 320
            x = lin(tt, 0, 0.2, -900, 0)
            el = _rgb(foto(pl, bd, sd, v, *hms)).resize((480, 270), Image.LANCZOS)
            wklej(im, el, 50 + x, y)
            kol = CZER if i == 0 else (255, 255, 255)
            f = fnap(120)
            d.text((570 + x, y + 20), f"{v}", font=f, fill=kol)
            d.text((570 + x + f.getlength(f"{v}") + 12, y + 80), "km/h", font=fnap(48), fill=(255, 255, 255))
            d.text((570 + x, y + 180), f"#{i + 1} · {g}", font=fnap(48), fill=vhs.PRZYGASZONY)
        return im

    @r.s(14.6, 16.0)
    def _(t, T):
        sc = odjazd(0.45, A3, B3, 13)
        return kabina(sc[0], dev(sc, A3, T, 63), T, bob=False, zoom=lin(t, 0, 1.4, 1.3, 1.42), fy=H * 0.62,
                      poswiata=(150, 60, 80))

    @r.s(16.0, 18.0, glitch=True)
    def _(t, T):
        return plansza_koncowa(t, [("Raspberry Pi 5 + Hailo-10H", 64, vhs.TEKST),
                                   ("radar 24 GHz · w pełni offline", 46, vhs.PRZYGASZONY),
                                   ("zdjęcia zostają w urządzeniu", 46, vhs.PRZYGASZONY)], CTA, seed=17)

    r.n(0.0, 2.0, "Ile jeżdżą pod moim blokiem?", size=88)
    r.n(0.0, 2.0, "zmierzył mój radar z Raspberry Pi", y=DOL - 6, size=60, styl="obrys", dol=True, szer=1060)
    r.n(2.0, 3.8, "Ten: 29 km/h. Spoko.", size=84)
    r.n(3.8, 5.6, "Ten: 44 km/h. Jeszcze OK.", size=84)
    r.n(5.6, 6.6, "A ten…", size=84)
    r.n(6.6, 7.8, f"63 km/h. Próg {PROG} przekroczony", size=84, tlo=(240, 60, 72), kol=(255, 255, 255))
    r.n(7.8, 9.8, "Pstryk: zdjęcie jak z fotoradaru", size=80)
    r.n(9.8, 11.8, "tablica · data i godzina · prędkość", size=76)
    r.n(11.8, 14.6, "Kto dziś jechał najszybciej?", size=84)
    r.n(14.6, 16.0, "Radar 24 GHz w aucie. Offline.", size=80)
    r.dzwieki += [(0.15, "migawka", None), (2.3, "beep", None), (4.1, "beep", None), (6.1, "beep", None),
                  (7.8, "migawka", None)]
    r.dzwieki += [(11.8 + 0.15 + 0.3 * i, "tik", None) for i in range(3)]
    r.glosy.append((8.2, "63 kilometrów na godzinę."))
    r.okladka = (0.6, "Ile jeżdżą pod\nmoim blokiem?")
    return r


ROLKI = {"A": rolka_A, "B": rolka_B, "C": rolka_C}


# ───────────────────────────── dźwięk ─────────────────────────────


def _fir_lp(fc, n=129):
    k = np.arange(n) - (n - 1) / 2
    h = np.sinc(2 * fc / SR * k) * np.hamming(n)
    return h / h.sum()


def lp(x, fc):
    return np.convolve(x, _fir_lp(fc), mode="same")


def nuta(m):
    return 440.0 * 2 ** ((m - 69) / 12)


def muzyka(dl, bpm=104, seed=1):
    """Synthwave: puls basu, arpeggio, pad, stopa/werbel/hi-hat. Stereo."""
    rng = np.random.default_rng(seed)
    N = int(dl * SR)
    beat = 60 / bpm
    bar = 4 * beat
    akordy = [(45, [57, 60, 64]), (41, [53, 57, 60]), (48, [52, 55, 60]), (43, [55, 59, 62])]  # Am F C G
    bas = np.zeros(N); arp = np.zeros(N); pad = np.zeros(N); bebny = np.zeros(N)
    kick_env = np.zeros(N)

    def dodaj(buf, t0, sig):
        i = int(t0 * SR)
        if i >= N:
            return
        j = min(N, i + len(sig))
        buf[i:j] += sig[: j - i]

    # bas: ósemki
    for k in range(int(dl / (beat / 2)) + 1):
        t0 = k * beat / 2
        root, _ = akordy[int(t0 // bar) % 4]
        f = nuta(root + (12 if k % 4 == 3 else 0))
        t = np.arange(int(0.26 * SR)) / SR
        saw = 2 * ((t * f) % 1) - 1 + 0.5 * (2 * ((t * f * 1.005) % 1) - 1)
        dodaj(bas, t0, saw * np.exp(-t * 7) * np.minimum(1, t / 0.004))
    bas = lp(bas, 420)
    # arpeggio: szesnastki
    wzor = [0, 1, 2, 1, 0, 2, 1, 2]
    for k in range(int(dl / (beat / 4)) + 1):
        t0 = k * beat / 4
        _, ak = akordy[int(t0 // bar) % 4]
        m = ak[wzor[k % 8] % 3] + 12 + (12 if (k // 8) % 2 and k % 8 in (2, 5) else 0)
        f = nuta(m)
        t = np.arange(int(0.16 * SR)) / SR
        sq = np.where((t * f) % 1 < 0.3, 1.0, -1.0)
        dodaj(arp, t0, sq * np.exp(-t * 16) * np.minimum(1, t / 0.003))
    arp = lp(arp, 3200)
    # pad
    for b in range(int(dl / bar) + 1):
        _, ak = akordy[b % 4]
        t = np.arange(int(bar * SR) + 2000) / SR
        s = np.zeros_like(t)
        for m in ak:
            for det in (-0.12, 0, 0.11):
                s += 2 * ((t * nuta(m - 12 + det)) % 1) - 1
        env = np.minimum(1, t / 0.4) * np.minimum(1, np.maximum(0, (bar + 0.05 - t)) / 0.2 + 0.0)
        dodaj(pad, b * bar, s * env / 9)
    pad = lp(lp(pad, 900), 900)
    # bębny
    for k in range(int(dl / beat) + 1):
        t0 = k * beat
        t = np.arange(int(0.3 * SR)) / SR
        fr = 45 + 90 * np.exp(-t * 30)
        dodaj(bebny, t0, np.sin(2 * np.pi * np.cumsum(fr) / SR) * np.exp(-t * 9) * 0.9)
        dodaj(kick_env, t0, np.exp(-t * 9))
        if k % 2 == 1:
            n = rng.normal(0, 1, int(0.2 * SR))
            n = n - lp(n, 1500)
            tt = np.arange(len(n)) / SR
            dodaj(bebny, t0, (n * 0.5 + 0.4 * np.sin(2 * np.pi * 190 * tt)) * np.exp(-tt * 16) * 0.55)
        n = rng.normal(0, 1, int(0.05 * SR)); n = n - lp(n, 6000)
        dodaj(bebny, t0 + beat / 2, n * np.exp(-np.arange(len(n)) / SR * 70) * 0.18)
    pompa = 1 - 0.55 * np.clip(kick_env, 0, 1)
    L = bas * 0.42 * pompa + pad * 0.5 * pompa + bebny * 0.5 + arp * 0.10
    R = L.copy()
    d = int(beat * 0.75 * SR)
    echo = np.zeros(N); echo[d:] = arp[:-d] * 0.08
    L += arp * 0.03; R += echo + arp * 0.02
    out = np.stack([L, R], 1)
    fade = np.ones(N); nf = int(1.2 * SR); fade[-nf:] = np.linspace(1, 0, nf)
    nin = int(0.03 * SR); fade[:nin] = np.linspace(0, 1, nin)
    return out * fade[:, None]


def sfx(rodzaj):
    t = lambda s: np.arange(int(s * SR)) / SR  # noqa: E731
    if rodzaj == "beep":          # odczyt tablicy
        a = t(0.07)
        s = np.concatenate([np.sign(np.sin(2 * np.pi * 1318 * a)) * np.exp(-a * 20), np.zeros(int(0.03 * SR)),
                            np.sign(np.sin(2 * np.pi * 1760 * a)) * np.exp(-a * 20)])
        return lp(s, 5000) * 0.30
    if rodzaj in ("dzwonek", "dzwonek2"):   # alert z bazy
        nuty = [76, 81, 88] if rodzaj == "dzwonek" else [79, 74, 79, 74]
        a = t(1.1)
        s = np.zeros(int((0.12 * len(nuty) + 1.1) * SR))
        for i, m in enumerate(nuty):
            f = nuta(m)
            x = (np.sin(2 * np.pi * f * a) + 0.3 * np.sin(2 * np.pi * 2.01 * f * a)) * np.exp(-a * 4.5)
            j = int(i * 0.12 * SR); s[j:j + len(x)] += x
        return s * 0.28
    if rodzaj == "migawka":        # zdjęcie
        n = np.random.default_rng(3).normal(0, 1, int(0.12 * SR))
        a = np.arange(len(n)) / SR
        klik = n * (np.exp(-a * 90) + 0.6 * np.exp(-np.maximum(0, a - 0.06) * 90) * (a > 0.06))
        b = t(0.5)
        swish = np.sin(2 * np.pi * (2400 - 1800 * b) * b) * np.exp(-b * 8) * 0.25
        out = np.zeros(int(0.5 * SR)); out[:len(klik)] += klik * 0.5; out += swish
        return out * 0.6
    if rodzaj == "tik":
        a = t(0.03)
        return np.sin(2 * np.pi * 2200 * a) * np.exp(-a * 160) * 0.25
    if rodzaj == "glitch":
        n = np.random.default_rng(5).normal(0, 1, int(0.18 * SR))
        n = np.repeat(n[::8], 8)[: len(n)]
        a = np.arange(len(n)) / SR
        return n * np.exp(-a * 14) * 0.10
    return np.zeros(1)


def glos_wav(tekst):
    """Synteza Piper (opcjonalna) → mono float @SR, albo None."""
    model = os.environ.get("PILPR_PIPER_GLOS")
    lib = os.environ.get("PILPR_PIPER_LIB", "")
    if not model or not os.path.exists(model):
        return None
    d = os.path.join(TMP, "glos"); os.makedirs(d, exist_ok=True)
    fn = os.path.join(d, hashlib.md5((model + tekst).encode()).hexdigest() + ".wav")
    if not os.path.exists(fn):
        kod = ("import sys,wave\nfrom piper import PiperVoice\nv=PiperVoice.load(sys.argv[1])\n"
               "w=wave.open(sys.argv[2],'wb')\nv.synthesize_wav(sys.argv[3],w)\nw.close()\n")
        env = dict(os.environ, PYTHONPATH=lib)
        r = subprocess.run([sys.executable, "-c", kod, model, fn, tekst], env=env, capture_output=True)
        if r.returncode:
            print("[głos] błąd:", r.stderr.decode()[-300:])
            return None
    with wave.open(fn) as w:
        sr = w.getframerate()
        a = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float64) / 32768
    x = np.interp(np.arange(int(len(a) * SR / sr)) / SR, np.arange(len(a)) / sr, a)
    x = lp(x - lp(x, 160), 6000)          # „głośnik w aucie”
    return np.tanh(x / (np.sqrt(np.mean(x ** 2)) + 1e-9) * 0.22 * 1.6) * 0.75


def dzwiek(r, fn):
    N = int(r.dl * SR)
    mus = muzyka(r.dl, seed=ord(r.nazwa))
    duck = np.ones(N)
    efekty = np.zeros(N)

    def dodaj(buf, T, s):
        i = int(T * SR)
        if i >= N or len(s) == 0:
            return
        j = min(N, i + len(s)); buf[i:j] += s[: j - i]

    for T, rodzaj, _ in r.dzwieki:
        dodaj(efekty, T, sfx(rodzaj))
    glosy = 0
    for T, tekst in r.glosy:
        g = glos_wav(tekst)
        if g is None:
            continue
        glosy += 1
        dodaj(efekty, T, g)
        i, j = int((T - 0.1) * SR), int((T + len(g) / SR + 0.2) * SR)
        duck[max(0, i):min(N, j)] = 0.4
    duck = lp(duck, 8)
    mix = mus * 0.55 * duck[:, None] + efekty[:, None]
    mix = np.tanh(mix / max(1e-9, np.abs(mix).max()) * 1.2) * 0.89
    with wave.open(fn, "wb") as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(SR)
        w.writeframes((mix * 32767).astype(np.int16).tobytes())
    print(f"[{r.nazwa}] dźwięk: {r.dl:.1f} s, głosów {glosy}/{len(r.glosy)}")


# ───────────────────────────── render ─────────────────────────────

_R = None


def _klatka(i):
    return np.asarray(_R.klatka(i / FPS).convert("RGB"), np.uint8).tobytes()


def render(nazwa, okladki=False, proc=None):
    global _R
    r = ROLKI[nazwa]()
    _R = r
    rob = os.path.join(TMP, r.plik); os.makedirs(rob, exist_ok=True)
    wav = os.path.join(rob, "audio.wav")
    dzwiek(r, wav)
    mp4 = os.path.join(rob, r.plik + ".mp4")
    n = int(round(r.dl * FPS))
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}",
           "-r", str(FPS), "-i", "-", "-i", wav, "-c:v", "libx264", "-preset", "slow", "-crf", "21",
           "-maxrate", "6M", "-bufsize", "12M", "-pix_fmt", "yuv420p", "-profile:v", "high",
           "-c:a", "aac", "-b:a", "160k", "-shortest", "-movflags", "+faststart", mp4]
    ff = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    t0 = time.time()
    with Pool(proc or int(os.environ.get("ROLKI_PROC", os.cpu_count() or 4))) as pool:
        for k, b in enumerate(pool.imap(_klatka, range(n), chunksize=4)):
            ff.stdin.write(b)
            if k % 60 == 0:
                print(f"[{nazwa}] klatka {k}/{n}  {time.time() - t0:.0f} s", flush=True)
    ff.stdin.close()
    if ff.wait():
        raise SystemExit("ffmpeg: błąd")
    os.makedirs(OUT, exist_ok=True)
    shutil.copyfile(mp4, os.path.join(OUT, r.plik + ".mp4"))
    print(f"[{nazwa}] gotowe: {os.path.join(OUT, r.plik + '.mp4')} "
          f"({os.path.getsize(mp4) / 1e6:.1f} MB, {time.time() - t0:.0f} s)")
    if okladki:
        okladka(r)


def okladka(r):
    """Okładka 1080×1920: klatka z hakiem (bez napisów), duży tytuł, logo."""
    T, tytul = r.okladka
    im = r.klatka(T, napisy=False)
    im = Napis(0, 9, tytul, y=GORA + 40, size=82, szer=1060, wejscie=False).rysuj(im, 1)
    fn = os.path.join(TMP, r.plik + "_okladka.png")
    im.save(fn)
    os.makedirs(OUT, exist_ok=True)
    shutil.copyfile(fn, os.path.join(OUT, r.plik + "_okladka.png"))
    print(f"[{r.nazwa}] okładka: {os.path.join(OUT, r.plik + '_okladka.png')}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("rolka", choices=["A", "B", "C", "all"])
    ap.add_argument("--okladki", action="store_true", help="zapisz też okładki PNG")
    ap.add_argument("--tylko-okladki", action="store_true")
    ap.add_argument("--podglad", help="czasy w s, po przecinku: zapisz te klatki jako PNG i zakończ")
    ap.add_argument("--proc", type=int)
    a = ap.parse_args()
    for nz in (["A", "B", "C"] if a.rolka == "all" else [a.rolka]):
        if a.podglad:
            r = ROLKI[nz]()
            d = os.path.join(TMP, "podglad"); os.makedirs(d, exist_ok=True)
            for T in a.podglad.split(","):
                fn = os.path.join(d, f"{nz}_{float(T):05.2f}.png")
                r.klatka(float(T)).save(fn); print(fn)
        elif a.tylko_okladki:
            okladka(ROLKI[nz]())
        else:
            render(nz, a.okladki, a.proc)


if __name__ == "__main__":
    main()
