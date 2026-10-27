#!/usr/bin/env python3
"""Ekran ładowania LPR — WSPÓLNY moduł animacji i osobny, lekki proces.

Dlaczego osobny proces (09.10.2026): lpr.py importuje Hailo/GStreamer kilka
sekund, potem buduje potok (kolejne sekundy) — w tym czasie jego okno nie
może się odświeżać, więc ekran ładowania stał w miejscu przez ~10 z 13 s.
`lpr_run.sh` startuje ten plik PRZED lpr.py; animacja (pierścień, mrugające
gwiazdki, pas VHS) chodzi od ~0,7 s po starcie usługi, a lpr.py przejmuje
ekran dopiero, gdy jego pętla rysowania już tyka — i wtedy prosi ten proces
o zakończenie (plik STOP). Obie strony rysują TĄ SAMĄ funkcją
`splash_klatka` z tego samego zegara monotonicznego i tego samego ziarna
gwiazdek, więc przejęcie jest niewidoczne.
"""
import math
import os
import random
import re
import subprocess
import sys
import time

import cv2
import numpy as np
from PIL import Image as _PILImage, ImageDraw as _PILDraw

SPL_AKCENT = (91, 216, 222)
_SPL_MS = 4                     # nadpróbkowanie (gładkie krawędzie)
_SPIN_N = 24
_SPIN_ROZM = 64
_SPIN_KLATKI: dict = {}
STOP_PLIK = "/tmp/lpr_splash.stop"
PETLA_S = 12.0                  # okres pętli animacji (wideo mpv i gwiazdki)
WALLPAPER = "/home/admin/.local/share/lpr_splash.png"


def _spin_klatki(rozm: int) -> list:
    """Klatki pierścienia renderowane RAZ. W pętli jest tylko nałożenie
    małej, gotowej klatki RGBA."""
    if rozm in _SPIN_KLATKI:
        return _SPIN_KLATKI[rozm]
    ms = _SPL_MS
    out = []
    for i in range(_SPIN_N):
        im = _PILImage.new("RGBA", (rozm * ms, rozm * ms), (0, 0, 0, 0))
        d = _PILDraw.Draw(im)
        c = rozm * ms // 2
        r = int(rozm * ms * 0.38)
        gr = max(3, int(ms * 2.2))
        d.arc((c - r, c - r, c + r, c + r), 0, 360,
              fill=(68, 76, 82, 190), width=gr)
        a0 = (i / _SPIN_N) * 360.0
        d.arc((c - r, c - r, c + r, c + r), a0, a0 + 95,
              fill=(*SPL_AKCENT, 255), width=gr)
        rgba = np.array(im.resize((rozm, rozm), _PILImage.LANCZOS), np.uint8)
        out.append((rgba[:, :, :3][:, :, ::-1].copy(), rgba[:, :, 3].copy()))
    _SPIN_KLATKI[rozm] = out
    return out


# ── Gwiazdki (mrugające drobiny tła) ──────────────────────────────────────────
_GWIAZDKI: dict = {}          # (w, h) → lista (x, y, faza, omega, amplituda)
_GWIAZDKI_BIEL = np.array([225, 215, 205], np.float32)   # ciepła biel (BGR)


def _gwiazdki(tlo: np.ndarray, w: int, h: int) -> list:
    """Wybiera do 150 drobin z tła (grafika VHS: śnieżenie i krzyżyki w rogach;
    pikselowa: siatka kropek) poza polem logo i dolnym pasem statusu; każda
    dostaje fazę, tempo (okres 2–6 s) i amplitudę. Ziarno stałe — proces
    splasha i lpr.py wybierają te same drobiny."""
    klucz = (w, h)
    if klucz in _GWIAZDKI:
        return _GWIAZDKI[klucz]
    wynik = []
    try:
        g = cv2.cvtColor(tlo, cv2.COLOR_BGR2GRAY)
        med = int(np.median(g))
        n, _lab, stats, cent = cv2.connectedComponentsWithStats(
            (g > med + 8).astype(np.uint8), 8)
        kropki = [(int(cx), int(cy)) for i, (cx, cy) in enumerate(cent)
                  if i and stats[i, cv2.CC_STAT_AREA] <= 70
                  and cy < h - 45
                  and not (0.22 * w < cx < 0.78 * w and 0.28 * h < cy < 0.68 * h)]
        rng = random.Random(20261009)
        rng.shuffle(kropki)
        for x, y in kropki[:150]:
            # okresy = PETLA_S / k → cała animacja jest okresowa co PETLA_S s
            # (zapętlone wideo mpv łączy się bez skoku)
            okres = PETLA_S / rng.choice((2, 3, 4, 5, 6))
            wynik.append((x, y, rng.random() * 6.283,
                          6.283 / okres, rng.uniform(0.35, 1.0)))
    except Exception as e:
        print(f"[Splash] gwiazdki: {e}", flush=True)
    _GWIAZDKI[klucz] = wynik
    return wynik


def _gwiazdki_rysuj(fr: np.ndarray, tlo: np.ndarray, w: int, h: int,
                    teraz: float) -> None:
    """Mrugnięcia: v = a·sin⁴ — zwykle ciemno, co kilka sekund krótki rozbłysk;
    powyżej połowy mały krzyżyk, przy pełni większy. ~0,6 ms na klatkę."""
    for x, y, faza, omega, amp in _gwiazdki(tlo, w, h):
        v = math.sin(teraz * omega + faza)
        if v <= 0.0:
            continue
        v = amp * v ** 4
        if v < 0.04:
            continue
        bgp = tlo[y, x].astype(np.float32)
        col = tuple(int(c) for c in (bgp + v * (_GWIAZDKI_BIEL - bgp)))
        fr[y, x] = col
        if v > 0.5:
            cv2.line(fr, (x - 2, y), (x + 2, y), col, 1)
            cv2.line(fr, (x, y - 2), (x, y + 2), col, 1)
        if v > 0.85:
            cv2.line(fr, (x - 4, y), (x + 4, y), col, 1)
            cv2.line(fr, (x, y - 4), (x, y + 4), col, 1)


def _vhs_pas(fr: np.ndarray, tlo: np.ndarray, w: int, h: int, teraz: float) -> None:
    """Delikatny jaśniejszy pas „śledzenia" VHS wędrujący z góry na dół
    (35 px/s, 3 wiersze, +10…14 jasności) — tło ma żyć, nie krzyczeć."""
    y = int((teraz * ((h + 60) / PETLA_S)) % (h + 60)) - 30   # jeden przebieg na pętlę
    for dy, jas in ((0, 10), (1, 14), (2, 10)):
        row = y + dy
        if 0 <= row < h:
            fr[row] = np.clip(tlo[row].astype(np.int16) + jas, 0, 255).astype(np.uint8)


def splash_klatka(tlo: np.ndarray, w: int, h: int, teraz: float) -> np.ndarray:
    """Jedna klatka ekranu ładowania: tło + pierścień (18 kroków/s) +
    gwiazdki + pas VHS. Wspólna dla procesu splasha i lpr.py."""
    fr = tlo.copy()
    _vhs_pas(fr, tlo, w, h, teraz)
    kl = _spin_klatki(_SPIN_ROZM)
    i = int(teraz * 18) % _SPIN_N
    x = w // 2 - _SPIN_ROZM // 2
    y = h - 126
    if 0 <= y and y + _SPIN_ROZM <= h and x >= 0:
        fg, alpha = kl[i]
        patch = fr[y:y + _SPIN_ROZM, x:x + _SPIN_ROZM]
        a = alpha[:, :, None].astype(np.uint16)
        patch[:] = ((fg.astype(np.uint16) * a +
                     patch.astype(np.uint16) * (255 - a) + 127) //
                    255).astype(np.uint8)
    _gwiazdki_rysuj(fr, tlo, w, h, teraz)
    return fr


def zatrzymaj() -> None:
    """lpr.py woła, gdy jego okno już rysuje — proces splasha kończy się."""
    try:
        with open(STOP_PLIK, "w") as f:
            f.write(str(time.time()))
    except OSError:
        pass


def renderuj_petle(wyjscie: str, w: int = 800, h: int = 480, fps: int = 25) -> str:
    """Zapętlone wideo animacji (PETLA_S s) do mpv: te same klatki co
    `splash_klatka`, tło = tapeta aplikacji. H.264 (libx264, crf 18)."""
    import tempfile
    tlo = cv2.imread(WALLPAPER)
    if tlo is None:
        tlo = np.full((h, w, 3), (30, 29, 29), np.uint8)
    elif tlo.shape[:2] != (h, w):
        tlo = cv2.resize(tlo, (w, h), interpolation=cv2.INTER_AREA)
    kat = tempfile.mkdtemp(prefix="lpr_petla_")
    n = int(PETLA_S * fps)
    for i in range(n):
        cv2.imwrite(os.path.join(kat, "k_%04d.png" % i), splash_klatka(tlo, w, h, i / fps))
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(fps),
                    "-i", os.path.join(kat, "k_%04d.png"), "-c:v", "libx264",
                    "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
                    "-movflags", "+faststart", wyjscie], check=True)
    for f in os.listdir(kat):
        os.remove(os.path.join(kat, f))
    os.rmdir(kat)
    return wyjscie


def _rozmiar_ekranu() -> tuple:
    try:
        env = os.environ.copy()
        env.setdefault("WAYLAND_DISPLAY", "wayland-0")
        env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        out = subprocess.check_output(["wlr-randr"], env=env, text=True, timeout=3)
        m = re.search(r"(\d+)x(\d+) px.*current", out)
        t = re.search(r"Transform:\s+(\d+)", out)
        if m:
            w, h = int(m.group(1)), int(m.group(2))
            if t and t.group(1) in ("90", "270"):
                w, h = h, w
            return w, h
    except Exception:
        pass
    return 800, 480


def main() -> int:
    if os.environ.get("LPR_HEADLESS"):
        return 0
    try:
        os.remove(STOP_PLIK)
    except OSError:
        pass
    w, h = _rozmiar_ekranu()
    tlo = cv2.imread(WALLPAPER)
    if tlo is None:
        tlo = np.full((h, w, 3), (30, 29, 29), np.uint8)
    elif tlo.shape[:2] != (h, w):
        tlo = cv2.resize(tlo, (w, h), interpolation=cv2.INTER_AREA)
    cv2.namedWindow("LPR-splash", cv2.WINDOW_NORMAL)
    cv2.setWindowProperty("LPR-splash", cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)
    # Zawsze NAD oknem lpr: ono powstaje w run() z jedną klatką i stoi tak ~1–2 s
    # (budowa potoku); my animujemy dalej, aż lpr poprosi o STOP z pętli rysowania
    try:
        cv2.setWindowProperty("LPR-splash", cv2.WND_PROP_TOPMOST, 1)
    except Exception:
        pass
    t0 = time.monotonic()
    while True:
        teraz = time.monotonic()
        if os.path.exists(STOP_PLIK) or os.getppid() == 1 or teraz - t0 > 240.0:
            break
        cv2.imshow("LPR-splash", splash_klatka(tlo, w, h, teraz))
        cv2.waitKey(66)                 # ~15 fps
    cv2.destroyAllWindows()
    return 0


if __name__ == "__main__":
    sys.exit(main())
