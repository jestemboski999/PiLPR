#!/usr/bin/env python3
"""
LPR — System rozpoznawania tablic rejestracyjnych.

Śledzenie tablicy przez wiele klatek (ByteTracker) → OCR na Hailo w tle →
zapis pełnej klatki + wyciętej tablicy po opuszczeniu kadru.

Zapis (folder lpr/ na pendrivie lub ~/hailo-apps/logs/lpr/):
  WA12345_20260624_143052.jpg       ← najlepsza pełna klatka
  WA12345_20260624_143052_crop.jpg  ← powiększony wycinek tablicy
  UNKNOWN_...jpg / _crop.jpg        ← gdy OCR < 5 zgodnych odczytów
  plates.db                         ← SQLite (historia przejazdów)

Klasy modelu:
  0: car  1: bus  2: truck  3: motorcycle  4: plate
"""

import math
import multiprocessing
import numpy as np
import os
import queue as _queue
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from collections import Counter, deque, OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

# ── Błędy, które NIE przerywają pracy, ale muszą zostawić ślad ──────────────
# Urządzenie jedzie w aucie i ma działać mimo błędów — dlatego w wielu
# miejscach łapiemy wyjątek i lecimy dalej. Ale `except: pass` znaczy
# „nigdy się o tym nie dowiem": tak właśnie odczyt temperatury Hailo
# blokujący GIL ukrywał się 10 dni (30.07.2026), zostawiając sam objaw.
# Kompromis: pierwsze wystąpienie ląduje w logu od razu, kolejne takie same
# są zliczane i przypominane najwyżej co 5 min — ślad jest, a log nie puchnie.
_bledy_ile: dict = {}
_bledy_kiedy: dict = {}
_bledy_lock = threading.Lock()
_BLEDY_CO_S = 300.0


def _zglos(gdzie: str, e: BaseException) -> None:
    """Zaloguj błąd, który celowo nie przerywa pracy. SAM NIGDY NIE RZUCA."""
    try:
        klucz = f"{gdzie}|{type(e).__name__}"
        teraz = time.monotonic()
        with _bledy_lock:
            n = _bledy_ile.get(klucz, 0) + 1
            _bledy_ile[klucz] = n
            pokaz = (n == 1) or (teraz - _bledy_kiedy.get(klucz, 0.0)
                                 >= _BLEDY_CO_S)
            if pokaz:
                _bledy_kiedy[klucz] = teraz
        if pokaz:
            ile = "" if n == 1 else f" [wystąpień: {n}]"
            print(f"[{datetime.now().strftime('%H:%M:%S')}] [Błąd] {gdzie}: "
                  f"{type(e).__name__}: {e}{ile}", flush=True)
    except Exception:
        pass          # zgłaszanie błędu nie ma prawa wywalić aplikacji


# ── Pomiar opóźnienia obrazu (skąd bierze się „obraz do tyłu") ──────────────
# Użytkownik zgłosił 30.07, że w jeździe podgląd jest kilkanaście sekund za
# rzeczywistością, a RAM rośnie z 0,9 do 1,4 GB. Wszystkie kolejki W APLIKACJI
# są ograniczone (podgląd 2, OCR 8, zapis 30, koder 2), więc opóźnienie musi
# powstawać NIŻEJ — w potoku GStreamera albo w kolejce zapisu.
#
# Sposób pomiaru bez zgadywania: wątek kamery stempluje każdy bufor
# `pts = numer_klatki × 33,3 ms`. W callbacku odczytujemy ten numer i
# odejmujemy od licznika kamery — różnica to DOKŁADNA liczba klatek uwięzionych
# w potoku, a podzielona przez 30 daje opóźnienie w sekundach.
_PTS_KLATKA_NS = 1_000_000_000 // 30      # tyle samo co Gst.util_uint64_scale_int(1, SECOND, 30)
_kamera_klatka = 0                        # ostatnia klatka wypchnięta przez kamerę
_diag_lock = threading.Lock()
_diag = {"locie": 0, "locie_max": 0, "locie_suma": 0, "locie_n": 0}
_diag_ref: dict = {}                      # referencje do obiektów z kolejkami


def _diag_zapisz_locie(w_locie: int) -> None:
    """Zapamiętaj głębokość potoku. Wołane z callbacka — musi być tanie."""
    if w_locie < 0 or w_locie > 100000:   # przepełnienie licznika / restart
        return
    with _diag_lock:
        _diag["locie"] = w_locie
        _diag["locie_suma"] += w_locie
        _diag["locie_n"] += 1
        if w_locie > _diag["locie_max"]:
            _diag["locie_max"] = w_locie


_cb_wolny_ostatnio = 0.0
_cb_wolny_ile = 0
_cb_ostatni_ms = 0.0        # ile trwala POPRZEDNIA klatka (raportowane w [Diag])
_cb_pominiete = 0           # nieuzywane od 05.08 (nakladka juz nie jest pomijana)
# Rozmiar ekranu i zoom podgladu — ustawiane przez watek podgladu, czytane
# przez callback. Dzieki temu callback zmniejsza klatke RAZ, przed rysowaniem
# nakladki, zamiast rysowac na pelnych 2304x1296 i oddawac 9 MB do podgladu.
_podglad_wh = (800, 480)
_podglad_zoom = 1


def _cb_zapisz_czas(sekundy: float, t0: float, t1: float, t2: float,
                    ile_det: int, pominieto: bool) -> None:
    """Zapamietaj czas callbacka i zglos, jesli przekroczyl budzet."""
    global _cb_ostatni_ms, _cb_pominiete
    ms = sekundy * 1000.0
    _cb_ostatni_ms = ms
    if pominieto:
        _cb_pominiete += 1
    if ms > 60.0:
        _cb_wolny(ms, (t1 - t0) * 1000.0, (t2 - t1) * 1000.0,
                  ms - (t2 - t0) * 1000.0, ile_det)


def _cb_wolny(razem: float, pobranie: float, sledzenie: float,
              nakladka: float, ile_det: int) -> None:
    """Callback przekroczyl budzet czasu — zapisz, KTORY etap zawinil.

    Przy 30 fps callback ma 33 ms na klatke. Powyzej tego potok zaczyna sie
    zapychac, a podglad zamiera (uzytkownik: „ekran lapie laga, mniej niz
    1 fps", 03.08 w jezdzie). Logujemy z ograniczeniem czestotliwosci —
    interesuje nas rozklad, nie zalanie pliku."""
    global _cb_wolny_ostatnio, _cb_wolny_ile
    _cb_wolny_ile += 1
    teraz = time.monotonic()
    if teraz - _cb_wolny_ostatnio < 5.0:
        return
    _cb_wolny_ostatnio = teraz
    print(f"[{datetime.now().strftime('%H:%M:%S')}] [CB-WOLNY] "
          f"{razem:.0f} ms  (pobranie {pobranie:.0f} + śledzenie/OCR "
          f"{sledzenie:.0f} + nakładka {nakladka:.0f})  "
          f"detekcji={ile_det}  takich klatek: {_cb_wolny_ile}", flush=True)
    _cb_wolny_ile = 0


def _diag_kolejki_potoku() -> str:
    """Ile buforow siedzi w KAZDEJ kolejce potoku GStreamera.

    Pomiar `_diag["locie"]` mowi ILE klatek utknelo, ale nie GDZIE. Bez tego
    nie da sie naprawic celnie — a w jezdzie 03.08 zaleglosc siegala 1532
    klatek (51 s). Tu przechodzimy po wszystkich elementach `queue` i pytamy
    o `current-level-buffers`."""
    app = _diag_ref.get("app")
    if app is None or getattr(app, "pipeline", None) is None:
        return ""
    try:
        from gi.repository import Gst
        it = app.pipeline.iterate_recurse()
        pelne = []
        while True:
            ok, el = it.next()
            if ok != Gst.IteratorResult.OK:
                break
            fab = el.get_factory()
            if fab is None or fab.get_name() != "queue":
                continue
            try:
                n = el.get_property("current-level-buffers")
                if n:
                    pelne.append((n, el.get_name()))
            except Exception:
                pass
        if not pelne:
            return ""
        pelne.sort(reverse=True)
        return "kolejki potoku: " + " ".join(f"{nm}={n}" for n, nm in pelne[:6])
    except Exception:
        return ""


def _diag_podsumowanie() -> str:
    """Jedna linia do logu: gdzie siedzi opóźnienie i ile pamięci zajmujemy."""
    with _diag_lock:
        biez, maks = _diag["locie"], _diag["locie_max"]
        sr = _diag["locie_suma"] / max(1, _diag["locie_n"])
        _diag["locie_suma"] = _diag["locie_n"] = 0
        _diag["locie_max"] = 0
        kam, kam_max = _diag.get("kam_s", 0.0), _diag.get("kam_max", 0.0)
        _diag["kam_max"] = 0.0
    # kamera = matryca → wątek kamery; potok = wątek kamery → callback.
    # Suma to PEŁNE opóźnienie od rzeczywistości do przetworzenia klatki.
    czesci = [f"kamera {kam:.2f} s (maks {kam_max:.2f})",
              f"potok {biez} kl ({biez / 30.0:.2f} s), "
              f"śr {sr:.1f} ({sr / 30.0:.2f} s), maks {maks} ({maks / 30.0:.2f} s)"]
    stan = _diag_ref.get("stan")
    if stan is not None:
        try:
            czesci.append(f"podgląd {stan.frame_queue.qsize()}/2")
        except Exception:
            pass
    for klucz, etykieta, limit in (("zapis", "zapis", 30), ("ocr", "OCR", 8)):
        obj = _diag_ref.get(klucz)
        if obj is not None:
            try:
                czesci.append(f"{etykieta} {obj._wqueue.qsize()}/{limit}")
            except Exception:
                pass
    try:
        with open(f"/proc/{os.getpid()}/status") as fh:
            for linia in fh:
                if linia.startswith("VmRSS:"):
                    czesci.append(f"RAM {int(linia.split()[1]) / 1048576:.2f} GB")
                    break
    except OSError:
        pass
    global _cb_pominiete, _nakl_pominiete
    czesci.append(f"callback {_cb_ostatni_ms:.0f} ms")
    if _cb_pominiete:
        czesci.append(f"nakładek pominiętych {_cb_pominiete}")
        _cb_pominiete = 0
    # wątek nakładki (24.09): czas rysowania i ile klatek PODGLĄDU porzucił;
    # 02.10: średnie etapów od ostatniego [Diag] zamiast jednej próbki
    e = _nakl_etapy
    if e["n"]:
        n = e["n"]
        czesci.append(
            f"nakładka śr {e['razem'] / n:.0f} ms (cpu {e['cpu'] / n:.0f}, z tego: "
            f"skala {e['skala'] / n:.1f} ramki {e['ramki'] / n:.1f} "
            f"dymki {e['dymki'] / n:.1f} hud {e['hud'] / n:.1f} "
            f"radar {e['radar'] / n:.1f} www {e['www'] / n:.1f} "
            f"ekran {e['ekran'] / n:.1f}; maks {e['maks']:.0f}; {n} kl)")
        for k in e:
            e[k] = 0 if k == "n" else 0.0
    else:
        czesci.append(f"nakładka {_nakl_ms:.0f} ms")
    with _nakl_lock:
        n_pom, _nakl_pominiete = _nakl_pominiete, 0
    if n_pom:
        czesci.append(f"podgląd porzucił {n_pom} kl")
    # pierścień pełnych klatek (24.09): ile razy callback nie zastał swojej
    # klatki main (zaległość > LPR_PIERSCIEN) i pracował na lores
    global _pierscien_brak
    if _pierscien_brak:
        czesci.append(f"pierścień: brak {_pierscien_brak} kl")
        _pierscien_brak = 0
    kol = _diag_kolejki_potoku()
    if kol:
        czesci.append(kol)
    z = globals().get("_stat_zasilanie")
    if z and z["hex"]:
        czesci.append(f"zasilanie 0x{z['hex']:x}"
                      + (" SPADEK TERAZ" if z["spadek"] else "")
                      + (" DŁAWIENIE" if z["dlawienie"] else "")
                      + (f" (spadków {z['n_spadkow']}, ostatni {z['ostatni']})"
                         if z["n_spadkow"] else ""))
    return "  ·  ".join(czesci)


# ── Zrzut logu HailoRT przy zacięciu ────────────────────────────────────────
# Rozstrzygający dowód w śledztwie 30.07.2026 leżał WYŁĄCZNIE w hailort.log:
# log aplikacji pokazywał samo `HAILO_TIMEOUT(4)` bez nazwy operacji, a dopiero
# tamten plik zdradzał, że chodzi o `get_chip_temperature`, nie o inferencję.
# Żeby następny incydent udokumentował się sam, przy zacięciu dopisujemy tu
# ogon tamtego pliku.
_HAILORT_LOGI = (
    os.path.expanduser("~/.hailo/hailort/hailort.log"),
    "/home/admin/hailo-apps/hailort.log",
)
_hailort_ostatni_zrzut = 0.0


def _hailort_ogon_do_logu(powod: str) -> None:
    """Dopisz ostatnie błędy HailoRT do logu — W OSOBNYM WĄTKU.

    Pętli rysowania nie wolno blokować niczym, nawet odczytem pliku."""
    global _hailort_ostatni_zrzut
    teraz = time.monotonic()
    if teraz - _hailort_ostatni_zrzut < 30.0:
        return                     # seria zacięć = jeden zrzut, nie dziesięć
    _hailort_ostatni_zrzut = teraz

    def _robota() -> None:
        try:
            for sciezka in _HAILORT_LOGI:
                try:
                    with open(sciezka, "rb") as fh:
                        fh.seek(0, os.SEEK_END)
                        fh.seek(max(0, fh.tell() - 8192))
                        linie = fh.read().decode("utf-8", "replace").splitlines()
                except OSError:
                    continue
                wazne = [w for w in linie
                         if "error" in w.lower() or "warn" in w.lower()][-3:]
                if wazne:
                    print(f"[HailoRT] przy {powod} — ostatnie z {sciezka}:",
                          flush=True)
                    for w in wazne:
                        print(f"[HailoRT]   {w[:200]}", flush=True)
                    return
            print(f"[HailoRT] przy {powod}: brak świeżych błędów w hailort.log "
                  f"— szukać przyczyny GDZIE INDZIEJ niż Hailo", flush=True)
        except Exception as e:
            _zglos("hailort_ogon", e)

    threading.Thread(target=_robota, daemon=True, name="hailort-ogon").start()


# ── Sygnał życia dla systemd (watchdog) ─────────────────────────────────────
# Bez tego zawieszenie aplikacji (w odróżnieniu od jej padnięcia) nie jest
# przez nic zauważane — jedzie się z zamrożonym urządzeniem do końca trasy.
# Sygnał wysyłamy z PĘTLI RYSOWANIA, bo to ona zamiera przy awarii.
def _sd_notify(komunikat: str) -> None:
    """Wyślij komunikat do systemd. Poza usługą (brak NOTIFY_SOCKET) — nic."""
    adres = os.environ.get("NOTIFY_SOCKET")
    if not adres:
        return
    if adres.startswith("@"):
        adres = "\0" + adres[1:]          # gniazdo abstrakcyjne
    try:
        import socket as _sock
        with _sock.socket(_sock.AF_UNIX, _sock.SOCK_DGRAM) as s:
            s.connect(adres)
            s.sendall(komunikat.encode())
    except Exception as e:
        _zglos("sd_notify", e)


# ── Bootstrap tuningu kamery CSI (sufit migawki w AUTO) ─────────────────────
# MUSI wykonać się PRZED importami hailo — tamte moduły potrafią wystartować
# menedżera libcamery już przy imporcie, a IPA czyta LIBCAMERA_RPI_TUNING_FILE
# tylko raz, przy starcie menedżera (potem zmiana env nic nie daje).
def _csi_tuning_bootstrap() -> None:
    import json as _json
    cfg_path = "/home/admin/hailo-apps/local_resources/lpr_runtime_config.json"
    out_path = "/home/admin/hailo-apps/local_resources/imx708_lpr.json"
    src_path = "/usr/share/libcamera/ipa/rpi/pisp/imx708.json"
    try:
        with open(cfg_path) as f:
            cfg = _json.load(f)
        if cfg.get("cam_source") != "csi":
            return
        cap = int((cfg.get("csi_camera") or {}).get("max_shutter_us", 16666))
        if not cap:
            return
        with open(src_path) as f:
            tuning = _json.load(f)
        for a in tuning.get("algorithms", []):
            agc = a.get("rpi.agc")
            if not agc:
                continue
            for ch in agc.get("channels", [agc]):
                for mode in (ch.get("exposure_modes") or {}).values():
                    if mode.get("shutter"):
                        mode["shutter"] = [min(int(s), cap)
                                           for s in mode["shutter"]]
        with open(out_path, "w") as f:
            _json.dump(tuning, f)
        os.environ["LIBCAMERA_RPI_TUNING_FILE"] = out_path
        print(f"[Kamera/CSI] bootstrap: sufit migawki {cap} µs "
              f"(~1/{round(1_000_000 / cap)} s)")
    except FileNotFoundError:
        pass          # brak configu/tuningu = tryb domyślny (np. dev na PC)
    except Exception as e:
        print(f"[Kamera/CSI] bootstrap tuningu: {e}")


_csi_tuning_bootstrap()

import cv2
import hailo
import psutil as _psutil


# ── Rozmiar ekranu z DRM ─────────────────────────────────────────────────────

def _get_screen_size() -> tuple[int, int]:
    """Zwraca efektywną rozdzielczość ekranu po uwzględnieniu rotacji Wayland."""
    import subprocess as _sp, re as _re
    # wlr-randr zna zarówno natywny tryb jak i Transform (90/270 = obróć W↔H)
    try:
        env = os.environ.copy()
        env.setdefault('WAYLAND_DISPLAY', 'wayland-0')
        env.setdefault('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}')
        out = _sp.check_output(['wlr-randr'], env=env, text=True, timeout=3)
        # Szukaj aktywnego trybu (current)
        mode = _re.search(r'(\d+)x(\d+) px.*current', out)
        transform = _re.search(r'Transform:\s+(\d+)', out)
        if mode:
            w, h = int(mode.group(1)), int(mode.group(2))
            # Rotacja 90° lub 270° — zamień wymiary
            if transform and transform.group(1) in ('90', '270'):
                w, h = h, w
            return w, h
    except Exception as e:
        _zglos("ekran_rozmiar", e)
    return 0, 0


# ── Głośnik przewodowy (09.10: Adafruit STEMMA/PAM8302 na dźwięku PWM Pi 5) ──
# Polityka: przewodowy gra zawsze (domyślne wyjście PipeWire), JBL po
# połączeniu wywłaszcza go (`_audio_bt_sink` robi wpctl set-default), po
# rozłączeniu WirePlumber wraca na przewodowy. Wyciszenie = mute węzła
# ALSA w WirePlumberze (zapamiętywane przez niego między restartami).
_WIRED_NAZWY = ("Built-in Audio", "RP1")
_wired_stan = {"id": None, "muted": None, "t": 0.0, "busy": False}


def _wired_id():
    """Numer węzła przewodowego wyjścia w `wpctl status` (cache)."""
    import subprocess as _sp, re as _re
    if _wired_stan["id"] is not None:
        return _wired_stan["id"]
    try:
        env = os.environ.copy()
        env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        out = _sp.check_output(["wpctl", "status"], env=env, text=True, timeout=4)
        blok = out.split("Sinks:", 1)[1].split("Sources:", 1)[0] if "Sinks:" in out else ""
        for ln in blok.splitlines():
            if any(n in ln for n in _WIRED_NAZWY):
                m = _re.search(r"(\d+)\.", ln)
                if m:
                    _wired_stan["id"] = int(m.group(1))
                    return _wired_stan["id"]
    except Exception as e:
        _zglos("wired_id", e)
    return None


def _wired_odswiez() -> None:
    """Czyta stan wyciszenia w tle (nie z wątku rysowania)."""
    import subprocess as _sp
    if _wired_stan["busy"]:
        return
    _wired_stan["busy"] = True

    def praca():
        try:
            wid = _wired_id()
            if wid is None:
                _wired_stan["muted"] = None
                return
            env = os.environ.copy()
            env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
            out = _sp.check_output(["wpctl", "get-volume", str(wid)], env=env,
                                   text=True, timeout=4)
            _wired_stan["muted"] = "MUTED" in out
        except Exception as e:
            _zglos("wired_odswiez", e)
            _wired_stan["id"] = None
        finally:
            _wired_stan["t"] = time.monotonic()
            _wired_stan["busy"] = False
    threading.Thread(target=praca, daemon=True, name="wired-stan").start()


def _wired_muted():
    """None = nie wiadomo / brak przewodowego wyjścia. Odświeża co 3 s."""
    if time.monotonic() - _wired_stan["t"] > 3.0:
        _wired_odswiez()
    return _wired_stan["muted"]


def _wired_set_mute(wycisz: bool) -> None:
    import subprocess as _sp

    def praca():
        try:
            wid = _wired_id()
            if wid is None:
                _menu_banner("Brak głośnika przewodowego w systemie", "red", 5.0)
                return
            env = os.environ.copy()
            env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
            _sp.run(["wpctl", "set-mute", str(wid), "1" if wycisz else "0"],
                    env=env, timeout=4, capture_output=True)
            _wired_stan["muted"] = wycisz
            _wired_stan["t"] = time.monotonic()
        except Exception as e:
            _zglos("wired_set_mute", e)
    threading.Thread(target=praca, daemon=True, name="wired-mute").start()


# ── Dźwięki dotknięć (09.10, jak w telefonie) ─────────────────────────────────
_DOTYK_WAV = {}
_dotyk_ost = [0.0]


def _dotyk_wav(rodzaj: str) -> str:
    """Krótki klik generowany raz (35 ms, zanikający ton): „tap" 1,8 kHz,
    „back" 900 Hz. Bez plików w zasobach — nic do wgrywania."""
    sciezka = _DOTYK_WAV.get(rodzaj)
    if sciezka and os.path.exists(sciezka):
        return sciezka
    import wave as _wave, struct as _st, math as _m
    sr, dl = 22050, 0.035
    hz = 900 if rodzaj == "back" else 1800
    n = int(sr * dl)
    sciezka = f"/tmp/lpr_dotyk_{rodzaj}.wav"
    with _wave.open(sciezka, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes(b"".join(
            _st.pack("<h", int(9000 * _m.sin(2 * _m.pi * hz * i / sr)
                               * _m.exp(-i / (sr * 0.006))))
            for i in range(n)))
    _DOTYK_WAV[rodzaj] = sciezka
    return sciezka


def _dzwiek_dotyku(rodzaj: str = "tap") -> None:
    """Odtwarza klik przez pw-play (ok. 70–85 ms od dotknięcia, zmierzone).
    Idzie na domyślne wyjście — przewodowy, a gdy JBL połączony, JBL.
    Nie częściej niż co 80 ms; wyłączane w panelu Dźwięk (TOUCH_SOUND)."""
    if not TOUCH_SOUND or os.environ.get("LPR_HEADLESS"):
        return
    teraz = time.monotonic()
    if teraz - _dotyk_ost[0] < 0.08:
        return
    _dotyk_ost[0] = teraz
    try:
        import subprocess as _sp
        env = os.environ.copy()
        env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        _sp.Popen(["pw-play", "--volume=0.45", _dotyk_wav(rodzaj)], env=env,
                  stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
    except Exception as e:
        _zglos("dzwiek_dotyku", e)


def _wayland_env() -> dict:
    env = os.environ.copy()
    env.setdefault('WAYLAND_DISPLAY', 'wayland-0')
    env.setdefault('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}')
    return env


_EKRAN_WYJSCIE: list = []        # cache nazwy wyjścia Wayland (np. DSI-2)


def _get_output_name() -> str:
    """Nazwa włączonego wyjścia Wayland (DSI-2 na Pi 5 z panelem DSI1,
    HDMI-A-1 przy monitorze) — do wlopm."""
    if _EKRAN_WYJSCIE:
        return _EKRAN_WYJSCIE[0]
    import subprocess as _sp, re as _re
    try:
        out = _sp.check_output(['wlr-randr'], env=_wayland_env(), text=True,
                               timeout=3)
        for blok in _re.split(r'\n(?=\S)', out):
            m = _re.match(r'(\S+) "', blok)
            if m and 'Enabled: yes' in blok and not m.group(1).startswith('NOOP'):
                _EKRAN_WYJSCIE.append(m.group(1))
                return m.group(1)
    except Exception as e:
        _zglos("ekran_wyjscie", e)
    return ""


def _ekran_zasilanie(wlacz: bool) -> bool:
    """Gasi / budzi panel przez wlr-output-power-management (`wlopm`).
    05.10: panel Waveshare 3.5" DSI nie ma sterowania podświetleniem
    (/sys/class/backlight pusty), ale wyłączenie zasilania wyjścia zatrzymuje
    strumień DSI i panel gaśnie. Wyjście zostaje SKONFIGUROWANE (obrót,
    mapowanie dotyku), więc dotknięcie nadal dociera do okna — to ono budzi."""
    import subprocess as _sp
    nazwa = _get_output_name()
    if not nazwa:
        return False
    try:
        r = _sp.run(['wlopm', '--on' if wlacz else '--off', nazwa],
                    env=_wayland_env(), capture_output=True, text=True,
                    timeout=4)
        if r.returncode != 0:
            print(f"[Ekran] wlopm {nazwa}: {r.stderr.strip()[:120]}")
        return r.returncode == 0
    except Exception as e:
        _zglos("ekran_zasilanie", e)
        return False


def _wejscia_event_paths() -> list:
    """/dev/input/eventN wszystkich urządzeń, które mogą obudzić ekran:
    panel dotykowy, klawiatura, mysz (bez HDMI-CEC i przycisku zasilania)."""
    wynik, nazwa = [], ""
    try:
        for ln in open("/proc/bus/input/devices", encoding="utf-8", errors="replace"):
            if ln.startswith("N: Name="):
                nazwa = ln
            elif ln.startswith("H: Handlers="):
                n = nazwa.lower()
                if "hdmi" in n or "pwr_button" in n or "jack" in n:
                    continue
                for tok in ln.split():
                    if tok.startswith("event"):
                        wynik.append("/dev/input/" + tok)
    except OSError:
        pass
    return wynik


def _budzik_jedno(app, sciezka: str) -> None:
    """Czyta SUROWE zdarzenia jednego urządzenia (evdev, 24-bajtowe
    input_event „llHHi") i przy zgaszonym ekranie każde naciśnięcie
    (EV_KEY ↓: BTN_TOUCH / klawisz / przycisk myszy) albo ruch (EV_REL,
    EV_ABS) zgłasza do pętli rysowania. Kończy się, gdy urządzenie zniknie."""
    import struct as _st
    fmt = "llHHi"
    rozm = _st.calcsize(fmt)
    try:
        with open(sciezka, "rb", buffering=0) as f:
            while True:
                dane = f.read(rozm)
                if not dane or len(dane) < rozm:
                    break
                _s, _us, typ, kod, wart = _st.unpack(fmt, dane)
                if (typ == 1 and wart == 1) or typ in (2, 3):
                    if app._ekran_wyl:
                        app._ekran_dotyk_t = time.monotonic()
    except Exception as e:
        _zglos("budzik:" + sciezka, e)


def _dotyk_watek(app) -> None:
    """Zapasowe (a przy zgaszonym wyjściu — JEDYNE) budzenie ekranu:
    08.10 sprawdzone, że przy wyłączonym zasilaniu wyjścia (wlopm) zdarzenia
    przez okno cv2 nie docierają, więc nasłuchujemy evdev bezpośrednio.
    Admin jest w grupie `input`, python-evdev niepotrzebny. Urządzenia
    skanowane co 10 s (dotyk po nakładce DSI, klawiatura 2.4G po wpięciu)."""
    watki: dict = {}
    while True:
        try:
            for sc in _wejscia_event_paths():
                t = watki.get(sc)
                if t is not None and t.is_alive():
                    continue
                t = threading.Thread(target=_budzik_jedno, args=(app, sc),
                                     daemon=True, name="budzik-" + sc.rsplit("/", 1)[-1])
                t.start()
                watki[sc] = t
                if not watki.get("_log_" + sc):
                    watki["_log_" + sc] = True
                    print(f"[Ekran] nasłuch wejścia (budzenie): {sc}")
        except Exception as e:
            _zglos("dotyk_watek", e)
        time.sleep(10)


# ── PIL: rysowanie tekstu z polskimi znakami ──────────────────────────────────

from PIL import ImageFont as _PILFont, ImageDraw as _PILDraw, Image as _PILImage

# Motyw pixel-art także NA EKRANIE (nie tylko w panelu www). VT323 = ten sam
# krój co w tabeli na stronie: pikselowy terminal CRT z pełną polszczyzną.
# Press Start 2P odpada do tekstów PL — nie ma ogonków (patrz panel www).
# Gdyby pliku brakło, wracamy do DejaVu — interfejs ma działać zawsze.
_HUD_FONT_PIXEL = "/home/admin/hailo-apps/local_resources/web/VT323-Regular.ttf"
_HUD_FONT_ZAPAS = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
_HUD_FONT_PATH = (_HUD_FONT_PIXEL if os.path.exists(_HUD_FONT_PIXEL)
                  else _HUD_FONT_ZAPAS)
# VT323 rysuje drobniej niż DejaVu Mono — bez tego HUD zrobiłby się mikry.
_HUD_SKALA = 1.35 if _HUD_FONT_PATH == _HUD_FONT_PIXEL else 1.0
_HUD_FONT_CACHE: dict[int, _PILFont.FreeTypeFont] = {}


def _pil_font(size: int) -> _PILFont.FreeTypeFont:
    if size not in _HUD_FONT_CACHE:
        try:
            _HUD_FONT_CACHE[size] = _PILFont.truetype(
                _HUD_FONT_PATH, max(8, int(round(size * _HUD_SKALA))))
        except Exception:
            try:
                _HUD_FONT_CACHE[size] = _PILFont.truetype(_HUD_FONT_ZAPAS, size)
            except Exception:
                _HUD_FONT_CACHE[size] = _PILFont.load_default()
    return _HUD_FONT_CACHE[size]


def _put_text_batch(img: np.ndarray,
                    items: list[tuple[str, tuple[int,int], tuple[int,int,int], int]]) -> None:
    """Rysuje wiele tekstów UTF-8 na klatce — jedna konwersja BGR↔RGB dla wszystkich."""
    pil = _PILImage.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
    draw = _PILDraw.Draw(pil)
    for text, xy, color_bgr, size in items:
        draw.text(xy, text, font=_pil_font(size),
                  fill=(color_bgr[2], color_bgr[1], color_bgr[0]))
    img[:] = cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)


def _put_text(img: np.ndarray, text: str, xy: tuple[int, int],
              color_bgr: tuple[int, int, int], size: int = 18) -> None:
    """Rysuje tekst UTF-8 — wrapper dla kompatybilności (używaj _put_text_batch gdy możliwe)."""
    _put_text_batch(img, [(text, xy, color_bgr, size)])


def _text_size(text: str, size: int) -> tuple[int, int]:
    """Zwraca (szerokość, wysokość) tekstu w px dla PIL."""
    font = _pil_font(size)
    bbox = font.getbbox(text)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


# ── Pełnoekranowy podgląd cv2 + ESC ──────────────────────────────────────────

def _fullscreen_display(user_data) -> None:
    sw, sh = _get_screen_size()
    cv2.namedWindow("LPR", cv2.WINDOW_NORMAL)
    cv2.setWindowProperty("LPR", cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)
    while user_data.running:
        frame = user_data.get_frame()
        if frame is not None:
            if sw and sh:
                frame = cv2.resize(frame, (sw, sh), interpolation=cv2.INTER_LINEAR)
            cv2.imshow("LPR", frame)
        key = cv2.waitKey(33) & 0xFF  # ~30fps max, nie 1000/s
        if key == 27:
            user_data.exit_requested.set()
            os.kill(user_data.main_pid, signal.SIGINT)
            break
    cv2.destroyAllWindows()


# ── Ekran startowy: logo, nazwa, płynna animacja ────────────────────────────
# Sekcja celowo PRZED importem Hailo (trwa kilka sekund) — inaczej przez ten
# czas widać pulpit. Samowystarczalna: własne stałe i czcionki, bez zależności
# od reszty pliku.
SPL_TLO    = (29, 30, 30)
SPL_AKCENT = (91, 216, 222)
SPL_WARM   = (246, 103, 80)
SPL_TEKST  = (242, 236, 225)
SPL_MUTED  = (158, 157, 153)
SPL_WARN   = (251, 191, 36)
SPL_OK     = (74, 222, 128)
_SPL_MS = 4                     # nadpróbkowanie (gładkie krawędzie)
_SPL_FONT: dict = {}
# Ekran startowy/błędu też pikselowo — spójnie z grafiką PiLPR i panelem www.
# VT323 nie ma odmiany pogrubionej, więc „bold" rysujemy większym stopniem.
_SPL_REG = (_HUD_FONT_PIXEL if os.path.exists(_HUD_FONT_PIXEL)
            else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
_SPL_BLD = _SPL_REG
SPLASH_ASSET = os.environ.get(
    "LPR_SPLASH_ASSET",
    str(Path(__file__).resolve().parent / "local_resources" /
        "lpr_splash_pilpr_vhs.png"),
)
_SPLASH_ASSET_ORIG = None
_SPLASH_ASSET_FAILED = False
_SPLASH_ASSET_CACHE: dict = {}


def _spl_font(size: int, bold: bool = False):
    key = (size, bold)
    if key not in _SPL_FONT:
        # VT323 rysuje drobno i nie ma odmiany grubej — stąd skala i dodatek
        # dla „bold" (inaczej tytuły na ekranie błędu byłyby ledwo widoczne).
        s = size
        if _SPL_REG == _HUD_FONT_PIXEL:
            s = int(round(size * (1.55 if bold else 1.35)))
        try:
            _SPL_FONT[key] = _PILFont.truetype(_SPL_REG, s)
        except OSError:
            _SPL_FONT[key] = _PILFont.load_default()
    return _SPL_FONT[key]


def _splash_asset_bg(w: int, h: int):
    """Wczytuje wygenerowaną identyfikację i kadruje ją bez deformacji."""
    global _SPLASH_ASSET_ORIG, _SPLASH_ASSET_FAILED
    key = (w, h)
    if key in _SPLASH_ASSET_CACHE:
        return _SPLASH_ASSET_CACHE[key]
    if _SPLASH_ASSET_FAILED:
        return None
    try:
        if _SPLASH_ASSET_ORIG is None:
            with _PILImage.open(SPLASH_ASSET) as src:
                _SPLASH_ASSET_ORIG = src.convert("RGB")
        src = _SPLASH_ASSET_ORIG
        sw, sh = src.size
        target_ratio = w / max(1, h)
        source_ratio = sw / max(1, sh)
        if source_ratio > target_ratio:
            crop_w = max(1, int(round(sh * target_ratio)))
            left = (sw - crop_w) // 2
            src = src.crop((left, 0, left + crop_w, sh))
        elif source_ratio < target_ratio:
            crop_h = max(1, int(round(sw / target_ratio)))
            top = (sh - crop_h) // 2
            src = src.crop((0, top, sw, top + crop_h))
        bg = src.resize((w, h), _PILImage.LANCZOS)
        if len(_SPLASH_ASSET_CACHE) > 4:
            _SPLASH_ASSET_CACHE.clear()
        _SPLASH_ASSET_CACHE[key] = bg
        return bg
    except Exception as e:
        _SPLASH_ASSET_FAILED = True
        print(f"[Splash] grafika niedostępna, używam fallbacku: {e}")
        return None


def _rysuj_logo(d, cx: int, cy: int, R: int, ms: int = _SPL_MS) -> None:
    """Awaryjny, deterministyczny wordmark PiLPR w siatce pikselowej."""
    glify = {
        "P": ("11110", "10001", "10001", "11110",
              "10000", "10000", "10000"),
        "i": ("010", "000", "110", "010", "010", "010", "111"),
        "L": ("10000", "10000", "10000", "10000",
              "10000", "10000", "11111"),
        "R": ("11110", "10001", "10001", "11110",
              "10100", "10010", "10001"),
    }
    napis = "PiLPR"
    kom = max(ms * 2, int(R * 0.095))
    szer = sum(len(glify[ch][0]) for ch in napis) + len(napis) - 1
    x = cx - (szer * kom) // 2
    y = cy - (7 * kom) // 2
    i_x = x
    for ch in napis:
        wzor = glify[ch]
        if ch == "i":
            i_x = x
        kolor = SPL_WARM if ch == "i" else SPL_TEKST
        for row, wiersz in enumerate(wzor):
            for col, piksel in enumerate(wiersz):
                if piksel == "1":
                    px, py = x + col * kom, y + row * kom
                    d.rectangle((px, py, px + kom - 1, py + kom - 1),
                                fill=kolor)
        x += (len(wzor[0]) + 1) * kom
    pasek_h = max(ms, kom // 7)
    pasek_y = y + int(kom * 1.35)
    d.rectangle((i_x - kom, pasek_y, i_x, pasek_y + pasek_h),
                fill=SPL_AKCENT)
    d.rectangle((i_x + 3 * kom, pasek_y, i_x + 4 * kom,
                 pasek_y + pasek_h), fill=SPL_AKCENT)


# 09.10: pierścień i gwiazdki w module lpr_splash (ten sam kod rysuje osobny
# proces ekranu ładowania — patrz lpr_splash.py); tu tylko import nazw
from lpr_splash import (_spin_klatki, _SPIN_N, _SPIN_ROZM, _gwiazdki,
                        _gwiazdki_rysuj, splash_klatka,
                        zatrzymaj as _splash_proces_stop)

_BOOT_TLO: dict = {}


def _boot_tlo(w: int, h: int, tytul: str, opis: str, blad: bool,
              kroki: tuple, status: str) -> np.ndarray:
    """Statyczne tło ekranu startowego (bez animacji) — rysowane raz
    na komplet treści i zapamiętane."""
    klucz = (w, h, tytul, opis, blad, kroki, status)
    if klucz in _BOOT_TLO:
        return _BOOT_TLO[klucz]
    ms = _SPL_MS
    asset = None if blad else _splash_asset_bg(w, h)
    if asset is not None:
        big = asset.copy()
    else:
        big = _PILImage.new("RGB", (w * ms, h * ms), SPL_TLO)
        d = _PILDraw.Draw(big)
        cx = w * ms // 2
        ly = int(h * (0.30 if blad else 0.43)) * ms
        if blad:
            r = int(min(w, h) * 0.15) * ms
            pts = [(cx, ly - r), (cx - int(r * 1.1), ly + int(r * 0.85)),
                   (cx + int(r * 1.1), ly + int(r * 0.85))]
            d.line(pts + [pts[0]], fill=SPL_WARN, width=int(ms * 5),
                   joint="curve")
            d.line((cx, ly - r // 3, cx, ly + r // 4), fill=SPL_WARN,
                   width=int(ms * 5))
            d.ellipse((cx - ms * 3, ly + int(r * 0.48), cx + ms * 3,
                       ly + int(r * 0.48) + ms * 6), fill=SPL_WARN)
        else:
            _rysuj_logo(d, cx, ly, int(min(w, h) * 0.27) * ms, ms)
        big = big.resize((w, h), _PILImage.LANCZOS)
    d = _PILDraw.Draw(big)
    if blad:
        ty = int(h * 0.30) + int(min(w, h) * 0.15) + 30
        d.text((w // 2, ty), tytul, font=_spl_font(30 if h < 560 else 36, True),
               fill=SPL_WARN, anchor="mm")
        ty += 32
        if opis:
            d.text((w // 2, ty), opis, font=_spl_font(15 if h < 560 else 17),
                   fill=SPL_MUTED, anchor="mm")
            ty += 26
        for i, k in enumerate(kroki, 1):
            d.text((w // 2, ty), f"{i}.  {k}",
                   font=_spl_font(15 if h < 560 else 17), fill=SPL_TEKST,
                   anchor="mm")
            ty += 25
        d.text((w // 2, h - 24), "System czeka — po podłączeniu ruszy sam",
               font=_spl_font(15), fill=SPL_OK, anchor="mm")
    else:
        # Bez podpisu „System rozpoznawania tablic" — grafika splash ma własne
        # hasło, drugi napis je dublował i zasłaniał. Zostaje sam status startu.
        if status:
            d.text((w // 2, h - 28), status,
                   font=_spl_font(16 if h < 560 else 18), fill=SPL_MUTED,
                   anchor="mm")
    img = np.array(big, np.uint8)[:, :, ::-1].copy()
    if len(_BOOT_TLO) > 12:
        _BOOT_TLO.clear()
    _BOOT_TLO[klucz] = img
    return img


def _boot_frame(w: int, h: int, boot: dict, teraz: float) -> np.ndarray:
    """Pełnoekranowy obraz statusu (BGR): gotowe tło + wklejona klatka
    animacji. Koszt ~0,3 ms, więc pierścień kręci się płynnie."""
    blad = bool(boot.get("blad"))
    tlo = _boot_tlo(w, h, str(boot.get("tytul", "")), str(boot.get("opis", "")),
                    blad, tuple(boot.get("kroki") or ()),
                    "" if blad else str(boot.get("tytul", "")))
    if blad:
        return tlo                      # ekran błędu jest statyczny
    return splash_klatka(tlo, w, h, teraz)   # wspólne z procesem splasha


def _splash_minimal() -> None:
    """Pokazuje ekran startowy natychmiast — przed importem Hailo."""
    if os.environ.get("LPR_HEADLESS"):
        return
    try:
        sw, sh = _get_screen_size()
        sw, sh = sw or 800, sh or 480
        cv2.namedWindow("LPR", cv2.WINDOW_NORMAL)
        cv2.setWindowProperty("LPR", cv2.WND_PROP_FULLSCREEN,
                              cv2.WINDOW_FULLSCREEN)
        cv2.imshow("LPR", _boot_frame(sw, sh,
                                      {"tytul": "Uruchamianie…", "opis": "",
                                       "blad": False, "kroki": []},
                                      time.monotonic()))
        cv2.waitKey(1)
    except Exception as e:
        print(f"[Splash] {e}")


WALLPAPER = "/home/admin/.local/share/lpr_splash.png"


def _ensure_wallpaper() -> None:
    """Ustawia ekran startowy jako TAPETĘ pulpitu.

    Nawet splash pokazany najwcześniej jak się da nie zdąży przed startem
    interpretera (~0,5 s), a wtedy widać pulpit z ikonami. Gdy tapeta jest
    tym samym obrazem, nie ma czego migać — urządzenie od włączenia wygląda
    jak jedna aplikacja. Robione w tle, bo nie może opóźniać startu."""
    def work():
        try:
            sw, sh = _get_screen_size()
            sw, sh = sw or 800, sh or 480
            img = _boot_tlo(sw, sh, "", "", False, (), "")
            os.makedirs(os.path.dirname(WALLPAPER), exist_ok=True)
            if not cv2.imwrite(WALLPAPER, img):
                raise OSError(f"nie można zapisać {WALLPAPER}")
            import subprocess as _sp
            # 09.10: tapetę rysuje swaybg (autostart labwc, ten sam plik);
            # pcmanfm tylko, gdy naprawdę działa jako pulpit — inaczej
            # `--set-wallpaper` potrafi otworzyć okno menedżera plików
            if _sp.run(["pgrep", "-x", "pcmanfm"], capture_output=True).returncode == 0:
                _sp.run(["pcmanfm", "--set-wallpaper", WALLPAPER,
                         "--wallpaper-mode=stretch"], timeout=15,
                        capture_output=True)
            print(f"[Pulpit] tapeta = ekran startowy ({sw}×{sh})")
        except Exception as e:
            print(f"[Pulpit] tapeta pominięta: {e}")
    threading.Thread(target=work, daemon=True, name="wallpaper").start()


# 09.10: _splash_minimal() już NIE jest wołane — ekran ładowania przez cały
# import i budowę potoku rysuje osobny proces (lpr_splash.py z lpr_run.sh),
# a okno lpr powstaje dopiero w pętli rysowania (_okno_utworz), bo wcześniej
# nie miałoby kto go odświeżać i zasłaniałoby animację statyczną klatką.

import hailo_apps.python.core.gstreamer.gstreamer_app as _gsa
_gsa.display_user_data_frame = _fullscreen_display

from hailo_apps.python.pipeline_apps.detection.detection_pipeline import GStreamerDetectionApp
from hailo_apps.python.core.common.buffer_utils import get_caps_from_pad, get_numpy_from_buffer
from hailo_apps.python.core.gstreamer.gstreamer_app import app_callback_class
from hailo_apps.python.core.common.hailo_logger import get_logger
from fast_plate_ocr import LicensePlateRecognizer as _LPR
from hailo_apps.python.core.common.core import resolve_hef_path

logger = get_logger(__name__)

# ── Konfiguracja ──────────────────────────────────────────────────────────────

OCR_INTERVAL_FRAMES = 8      # co ile klatek uruchamiać OCR (~46 ms/odczyt na 2 rdzeniach;
                             # przy 30 fps i kilku tablicach 5 zapychało kolejkę → HAILO_TIMEOUT)
OCR_MIN_MATCHING    = 3      # min tyle ZGODNYCH odczytów = pewność
OCR_INTERVAL_BLISKO = 3      # 05.10: tablica w STREFIE CZYTELNOŚCI (wycinek 60–120 px) → co 3 klatki
                             # (10 odczytów/s × 46 ms = 1/2 rdzenia, tylko póki tablica jest blisko;
                             #  z OCR na Hailo można zejść do 1). Pomiar na 254 wycinkach z 02.10:
                             #  60–120 px → 90 % trafień, 45–60 px → 69 %, > 120 px → 69 % (rozmycie z bliska)
OCR_DOGRYWKA_MAX    = 12     # po konsensusie: ile dodatkowych odczytów z bliska, gdy pewny tekst
OCR_DOGRYWKA_MAX_HAILO = 40  # 08.10: na Hailo odczyt nic nie kosztuje procesora → dłuższa dogrywka
                             # ustaliły DALEKIE głosy — z bliska widać lepiej, zapis idzie z poprawionym
OCR_MIN_CONF        = 0.3    # minimalna pewność OCR (fast-plate-ocr zwraca realne wartości 0–1)
PLATE_MIN_CONF      = 0.6    # minimalna pewność detekcji tablicy
TRACK_DEAD_SEC      = 1.5    # po ilu s bez detekcji track uznany za martwy
TRACK_OCR_TIMEOUT   = 8.0    # max czas oczekiwania na ostatnie OCR po śmierci
TRACK_MAX_ALIVE_SEC = 120.0  # po tym czasie zapisz i zresetuj (długi postój)
DEDUP_WINDOW_SEC    = 1800.0 # 30 min — ta sama tablica = nie zapisuj ponownie
SCREEN_OFF_MIN      = 10     # 05.10: po ilu minutach BEZ dotyku/klawisza/tablicy w kadrze
                             # gasić ekran (0 = nigdy); menu „Wygaszanie ekranu", plik runtime
SCREEN_OFF_OPCJE    = (0, 2, 5, 10, 20, 30, 60)
TOUCH_SOUND         = True   # 09.10: krótki klik przy dotknięciu kafla/chipa (jak w telefonie); panel Dźwięk
CROP_MARGIN_W       = 0.20   # margines boczny croppa — ciasny crop ucinał skrajne znaki
CROP_MARGIN_H       = 0.16   # (eksperyment 2026-07-08 na 47 parach z jazd: 0.20/0.16 najlepsze,
                             #  0.12/0.10 gubiło np. "W" z WX4AB19; >0.30 pogarsza — znaki za małe)
JPEG_QUALITY        = 92
OCR_FUZZY_DIST      = 1      # max Levenshtein distance — odczyty różniące się o 1 znak to "to samo"
OCR_MODEL           = "/home/admin/hailo-apps/local_resources/ocr/cct_s_v2_pl.onnx"
                             # model fast-plate-ocr: nazwa z hub ALBO ścieżka do .onnx
                             # (cct_s_v2_pl = douczony na polskich tablicach 2026-07-08,
                             #  val 93.1% surowo vs 90.3% bazowego; powrót do bazowego:
                             #  OCR_MODEL = "cct-s-v2-global-model")
OCR_PLATE_CONFIG    = "/home/admin/hailo-apps/local_resources/ocr/cct_s_v2_pl_plate_config.yaml"
OCR_BACKEND         = os.environ.get("LPR_OCR_BACKEND", "cpu").lower()
OCR_MIN_PLATE_PX    = 26     # min wysokość tablicy w px kadru — mniejsze nie idą do OCR
                             # (nieczytelne nawet dla człowieka: śmieciowe głosy w konsensusie + zmarnowane CPU)
HAILO_MAX_FPS       = 234.6  # zmierzone: hailortcli benchmark lpr_yolov8n_v2 (2026-07-10);
                             # obciążenie Hailo = aktualne fps / ta wartość
RUNTIME_CONFIG_PATH = "/home/admin/hailo-apps/local_resources/lpr_runtime_config.json"

# ── Źródło obrazu ─────────────────────────────────────────────────────────────
# "usb" — kamera UVC (Kurokesu C1 PRO, MJPG przez v4l2src) — tor klasyczny.
# "csi" — kamera na taśmie (Arducam IMX708 HDR): lpr_run.sh przekazuje wtedy
#         --input rpi = w hailo-apps wątek Picamera2 pcha klatki RGB888 do
#         appsrc (FrameRate 30 z ich kodu). Dashcam przenosi się na kamerę USB
#         (C1 PRO zostaje wideorejestratorem), a HDR sensora włącza
#         _apply_csi_settings() ZANIM Picamera2 otworzy kamerę.
# Przełączanie: "cam_source" w lpr_runtime_config.json + restart usługi.
# Powrót do czystej wersji USB: gałąź git "kamera-usb" / fork_kamera_usb/.
CAM_SOURCE = "usb"
CSI_HDR    = True    # sprzętowy HDR IMX708 (kontrolka wide_dynamic_range)

# ── Wideorejestrator (dashcam) ────────────────────────────────────────────────
# Zapis surowego strumienia MJPEG z kamery (bez re-kodowania = ~0 CPU) w
# segmentach .mkv. Segmenty zamknięte są odporne na zanik zasilania — przy
# nagłym wyłączeniu tracimy najwyżej bieżący segment (matroska streamable
# i tak odtwarza się do miejsca ucięcia).
DASHCAM_ENABLED = True
DASHCAM_DIR     = "/media/lpr/dashcam"
DASHCAM_MAX_GB  = 20.0   # rotacja: najstarsze segmenty kasowane po przekroczeniu
# 01.10.2026: bez pendrive'a dashcam NAGRYWA NA KARTĘ SD (mały bufor kołowy),
# żeby tryb RADAR miał z czego wycinać klipy; po starcie z pendrive'em sesje
# z SD są przenoszone na pendrive (`_przeprowadzka_z_sd`).
DASHCAM_SD_DIR    = "/home/admin/hailo-apps/logs/dashcam"
DASHCAM_SD_MAX_GB = 8.0   # ≈ 4,5 h przy 4 Mb/s; karta 64 GB, ~39 GB wolne
DASHCAM_SEG_SEC = 60     # długość jednego segmentu
DASHCAM_FPS     = 15     # tryb CSI: fps zapisu (co 2. klatka z 30 fps kamery)
DASHCAM_KBPS    = 4096   # tryb CSI: bitrate H.264 (x264 ultrafast, ~30 MB/min)
DASHCAM_REC_W   = 1280   # tryb CSI: rozdzielczość zapisu — osobny strumień
DASHCAM_REC_H   = 720    # lores z ISP (skalowanie w sprzęcie, YUV420 wprost
                         # pod koder — 1080p z konwersją RGB dusiło CPU >80%)
DASHCAM_THUMB_W = 320    # szerokość miniaturek segmentów (janitor robi je z góry)
DASHCAM_ZNACZNIK = ".obrot_w_sensorze"   # plik-znacznik w katalogu sesji

# ── Dwa strumienie z ISP (24.09.2026) ────────────────────────────────────────
# Detektor i tak widzi 640×640, a zmniejszanie pełnych 2304×1296 do 640 w
# hailocropperze kosztowało 18,8 ms/klatkę (4 wątki po ~15 % rdzenia) plus
# kopie 9 MB na każdym etapie. Teraz do potoku (detekcja + podgląd) idzie
# `lores` 1280×720 skalowane W SPRZĘCIE (ISP): letterbox 1280→640 to 0,8 ms,
# a kopie są 3× mniejsze. Pełne `main` 2304×1296 dalej istnieje — trafia do
# pierścienia kluczowanego numerem klatki, skąd callback bierze je do
# WYCINKÓW (OCR, zdjęcia, FOTO, ekspozycja). Test A/B na 864 klatkach z
# potwierdzoną tablicą (narzedzia_testowe/ab_lores_detekcja.py): recall
# 0,993 (2304) vs 0,988–0,995 (1280), małe tablice <45 px bez straty.
# Rozmiar lores pochodzi z lpr_run.sh (--width/--height), main jest stały.
LPR_MAIN_W, LPR_MAIN_H = 2304, 1296
LPR_PIERSCIEN = 30       # 1 s @30 fps; 30 × 9 MB = 270 MB RAM
_pierscien_lock = threading.Lock()
_pierscien: OrderedDict = OrderedDict()
_pierscien_brak = 0      # callback nie znalazł swojej klatki main (do [Diag])


def _pierscien_dodaj(fc: int, frame_main) -> None:
    with _pierscien_lock:
        _pierscien[fc] = frame_main
        while len(_pierscien) > LPR_PIERSCIEN:
            _pierscien.popitem(last=False)


def _pierscien_wez(fc: int):
    with _pierscien_lock:
        return _pierscien.get(fc)


def _dashcam_obrot_potrzebny(p) -> bool:
    """Czy przy ODTWARZANIU nagranie trzeba jeszcze obrócić o 180°.

    Do 02.09.2026 kamera oddawała obraz do góry nogami, prostował go dopiero
    potok LPR — wideorejestrator zapisywał surowy, odwrócony strumień, więc
    każde odtworzenie dokładało `hflip,vflip`. Od 02.09.2026 obrót robi
    SENSOR (Transform w wątku kamery) i dotyczy OBU strumieni: `main` do
    rozpoznawania i `lores` do nagrywania. Nowe nagrania są więc już proste,
    stare nadal nie są.

    Rozróżnia je plik-znacznik zakładany w katalogu sesji przy starcie
    nagrywania — nie data, bo daty na tym urządzeniu potrafiły się cofać
    (brak baterii RTC, patrz fake-hwclock). Brak znacznika = jak dotąd."""
    try:
        return not (Path(p).parent / DASHCAM_ZNACZNIK).exists()
    except Exception:
        return True


def _vf_z_obrotem(baza: str, p) -> str:
    """Łańcuch filtrów ffmpeg z obrotem dokładanym tylko gdy potrzebny."""
    return baza + ",hflip,vflip" if _dashcam_obrot_potrzebny(p) else baza
_dashcam_active = False
_dash_pipeline  = None   # tryb CSI: osobny pipeline nagrywania
_dash_appsrc    = None   # tryb CSI: appsrc karmiony z wątku kamery
_dash_caps_ok   = False  # czy BIEŻĄCY appsrc dostał już caps (po restarcie
                         # nagrywania trzeba je ustawić PONOWNIE — inaczej
                         # nowy pipeline dostaje bufory bez formatu i milczy)
_csi_picam2     = None   # tryb CSI: uchwyt Picamera2 (sterowanie na żywo z www)
_csi_meta       = {}     # tryb CSI: ostatnie metadane klatki (migawka/gain/lux)

# Ustawienia kamery CSI (panel KAMERA w www; zapisywane w runtime configu).
# ae_mode: short = automatyka preferuje krótkie czasy (anty-smużenie, domyślne)
# max_shutter_us: TWARDY sufit migawki w trybie AUTO (przycinana drabinka
#   czasów w tuningu — patrz _csi_load_tuning); 0 = bez limitu; zmiana
#   wymaga restartu usługi (tuning wczytywany przy otwarciu kamery)
CSI_CAM_DEFAULTS = {"ae_mode": "short", "ev": 0.0, "metering": "centre",
                    "manual": False, "exposure_us": 4000, "gain": 4.0,
                    "max_shutter_us": 16666}
CSI_CAM_SETTINGS = dict(CSI_CAM_DEFAULTS)

# ── Bazy obserwowanych + powiadomienia głosowe ───────────────────────────────
# WATCH_GROUPS: {gid: {name, alert, enabled, color}} — osobne bazy, każda z
#   własnym komunikatem czytanym na głos ({tablica} = literowany numer).
# WATCHLIST:    {"WA12345": {"group": gid, "note": "..."}}
# Trafienie przy konsensusie = baner na ekranie + komunikat głosowy (BT).
WATCH_GROUPS: dict = {}
WATCHLIST: dict = {}
TTS_ALL   = False
TTS_SPELL_MODE = "letters"   # "letters" (wu, a) | "phonetic" (Wanda, Adam)
TTS_GAIN = 1.0               # zgodność wstecz: stara pojedyncza głośność
# Osobne głośności per rodzaj komunikatu (1.0–4.0). Alert z bazy musi być
# słyszalny mimo drogi i muzyki, a rutynowe czytanie tablic ma nie męczyć.
TTS_GAINS = {"alert": 2.4, "plate": 1.4, "system": 1.6}
TTS_KIND_NAZWA = {"alert": "Alerty z bazy", "plate": "Czytanie tablic",
                  "system": "Komunikaty"}
TTS_BRAND = True             # doczytaj markę pojazdu, jeśli baza ją zna
# Ile sekund NIE powtarzać na głos tej samej tablicy. Przy jeździe za tym
# samym autem track bywa gubiony i tworzony na nowo (zakręt, inne auto
# przesłania, reset po 120 s) — bez tego głos czyta ten sam numer w kółko.
# Baner na ekranie i zapis w bazie działają jak dotąd; wyciszamy TYLKO mowę.
TTS_DEDUP_SEC = 120.0
_tts_ostatnio: dict = {}     # tablica → czas ostatniego przeczytania
_tts_dedup_lock = threading.Lock()


def _mowa_dozwolona(plate: str, okno: float = None) -> bool:
    """Czy wolno teraz przeczytać tę tablicę na głos (i zapamiętać fakt)."""
    okno = TTS_DEDUP_SEC if okno is None else okno
    if okno <= 0:
        return True
    teraz = time.monotonic()
    with _tts_dedup_lock:
        if teraz - _tts_ostatnio.get(plate, -1e9) < okno:
            return False
        _tts_ostatnio[plate] = teraz
        if len(_tts_ostatnio) > 400:      # sprzątanie starych wpisów
            for k, v in list(_tts_ostatnio.items()):
                if teraz - v > okno * 4:
                    _tts_ostatnio.pop(k, None)
    return True
HUD_ON = True                # pasek statystyk u góry ekranu w aucie
BT_DEVICE = ""     # MAC zapamiętanego głośnika (auto-connect przy starcie)
TTS_VOICE_PATH = ("/home/admin/hailo-apps/local_resources/tts/"
                  "pl_PL-gosia-medium.onnx")
ALERT_COLORS = {"red": (30, 30, 200), "orange": (0, 140, 255),
                "blue": (200, 120, 20), "green": (40, 160, 40)}
_alert_text  = ""
_alert_until = 0.0
_alert_color = "red"
_alert_img   = None    # wycięty POJAZD z trafienia — pokazywany na ekranie
VEHICLE_LABELS = {"car", "bus", "truck", "motorcycle", "vehicle"}
VEHICLE_MARGIN = 0.14  # zapas wokół ramki pojazdu (detektor obcina ciasno)


def _vehicle_crop(frame_bgr, dets, plate_bbox, w: int, h: int):
    """Wycina POJAZD, na którym rozpoznano tablicę: szuka ramki pojazdu
    zawierającej środek tablicy i wycina ją Z ZAPASEM (detektor kadruje
    ciasno). Gdy pojazdu nie wykryto — wycinek wokół tablicy (auto jest
    zwykle nad/wokół niej)."""
    if not plate_bbox or len(plate_bbox) != 4:
        return None
    px = (plate_bbox[0] + plate_bbox[2]) / 2.0
    py = (plate_bbox[1] + plate_bbox[3]) / 2.0
    best = None
    for d in dets:
        label, x1, y1, x2, y2 = d[1], d[3], d[4], d[5], d[6]
        if label not in VEHICLE_LABELS:
            continue
        if x1 <= px <= x2 and y1 <= py <= y2:
            area = (x2 - x1) * (y2 - y1)
            if best is None or area < best[0]:      # najciaśniejszy pasujący
                best = (area, x1, y1, x2, y2)
    if best:
        _, x1, y1, x2, y2 = best
    else:                                    # brak pojazdu — okolica tablicy
        pw = plate_bbox[2] - plate_bbox[0]
        ph = plate_bbox[3] - plate_bbox[1]
        x1, x2 = px - pw * 1.8, px + pw * 1.8
        y1, y2 = py - ph * 4.5, py + ph * 2.0
    mw = (x2 - x1) * VEHICLE_MARGIN
    mh = (y2 - y1) * VEHICLE_MARGIN
    ix1 = max(0, int((x1 - mw) * w))
    iy1 = max(0, int((y1 - mh) * h))
    ix2 = min(w, int((x2 + mw) * w))
    iy2 = min(h, int((y2 + mh) * h))
    if ix2 - ix1 < 20 or iy2 - iy1 < 20:
        return None
    # pełna klatka jest RGB — wycinek do BGR (kopia w jednym przejściu)
    return cv2.cvtColor(frame_bgr[iy1:iy2, ix1:ix2], cv2.COLOR_RGB2BGR)


def _watch_default_group() -> dict:
    return {"name": "Obserwowane", "enabled": True, "color": "red",
            "alert": "Uwaga! Tablica {tablica} z listy obserwowanych.",
            "nota": True}      # czy doczytywać notatkę (markę/miasto)


def _parse_bulk_line(line: str) -> tuple:
    """Linia importu → (tablica, notatka). Obsługuje realne zapisy:
       'WA12345' | 'WA 12345' | 'WA12345;auto taty' | 'KR9876X, nota'
       | 'GD 555 TK babcia'  (tablice ze spacjami!).
    Bez jawnego separatora: sklejamy WIODĄCE tokeny wielkimi literami/cyframi
    (max 8 znaków tablicy), reszta = notatka."""
    line = line.strip().lstrip("﻿")      # BOM z Excela
    if not line:
        return "", ""
    if ";" in line or "\t" in line:           # CSV wielokolumnowy
        cols = re.split(r"[;\t]", line)
        note = " · ".join(c.strip() for c in cols[1:] if c.strip())[:60]
        return re.sub(r"[^A-Z0-9]", "", cols[0].upper())[:10], note
    parts = re.split(r",", line, maxsplit=1)
    if len(parts) > 1:
        return (re.sub(r"[^A-Z0-9]", "", parts[0].upper())[:10],
                parts[1].strip()[:60])
    toks = line.split()
    plate, used = "", 0
    for t in toks:
        if not t.isalnum() or (t.lower() == t and not t.isdigit()):
            break                       # małe litery / znaki → to już notatka
        cand = plate + re.sub(r"[^A-Z0-9]", "", t.upper())
        if len(cand) > 8:
            break
        plate, used = cand, used + 1
    return plate[:10], " ".join(toks[used:])[:60]


CSI_TUNING_PATH = "/home/admin/hailo-apps/local_resources/imx708_lpr.json"
_csi_tuning_file = None   # ścieżka gotowego tuningu (None = domyślny)


def _csi_write_tuning() -> None:
    """Tuning IMX708 z twardym sufitem migawki w AUTO: automatyka AE dobiera
    czasy z drabinek w pliku strojenia — przycięcie drabinek do
    max_shutter_us sprawia, że AE nigdy nie zejdzie poniżej np. 1/60 s
    (resztę nadrabia gainem). HDR zostaje (tryb HDR ogranicza sensor do
    30 fps, więc sufitu nie da się zrobić klatkarzem).
    Plik podajemy potem jako Picamera2(tuning=ŚCIEŻKA-string): konstruktor
    sam ustawia LIBCAMERA_RPI_TUNING_FILE tuż przed otwarciem kamery
    (a przy tuning=None AKTYWNIE kasuje tę zmienną — dlatego samodzielne
    ustawianie env wcześniej nie działało)."""
    global _csi_tuning_file
    import json as _json
    _csi_tuning_file = None
    cap = int(CSI_CAM_SETTINGS.get("max_shutter_us") or 0)
    if not cap:
        return
    try:
        with open("/usr/share/libcamera/ipa/rpi/pisp/imx708.json") as f:
            tuning = _json.load(f)
        for a in tuning.get("algorithms", []):
            agc = a.get("rpi.agc")
            if not agc:
                continue
            for ch in agc.get("channels", [agc]):
                for mode in (ch.get("exposure_modes") or {}).values():
                    sh = mode.get("shutter")
                    if sh:
                        mode["shutter"] = [min(int(s), cap) for s in sh]
        with open(CSI_TUNING_PATH, "w") as f:
            _json.dump(tuning, f)
        _csi_tuning_file = CSI_TUNING_PATH
        print(f"[Kamera/CSI] sufit migawki w AUTO: {cap} µs "
              f"(~1/{round(1_000_000 / cap)} s) — tuning: {CSI_TUNING_PATH}")
    except Exception as e:
        print(f"[Kamera/CSI] tuning bez sufitu migawki: {e}")


def _csi_controls_from_settings() -> dict:
    """Mapuje CSI_CAM_SETTINGS na kontrolki libcamera (dict do set_controls)."""
    from libcamera import controls as _lc
    s = CSI_CAM_SETTINGS
    if s.get("manual"):
        return {"AeEnable": False,
                "ExposureTime": int(s["exposure_us"]),
                "AnalogueGain": float(s["gain"])}
    ae_map = {"normal": _lc.AeExposureModeEnum.Normal,
              "short":  _lc.AeExposureModeEnum.Short,
              "long":   _lc.AeExposureModeEnum.Long}
    met_map = {"centre": _lc.AeMeteringModeEnum.CentreWeighted,
               "spot":   _lc.AeMeteringModeEnum.Spot,
               "matrix": _lc.AeMeteringModeEnum.Matrix}
    return {"AeEnable": True,
            "AeExposureMode": ae_map.get(s.get("ae_mode"),
                                         _lc.AeExposureModeEnum.Short),
            "ExposureValue": float(s.get("ev", 0.0)),
            "AeMeteringMode": met_map.get(s.get("metering"),
                                          _lc.AeMeteringModeEnum.CentreWeighted)}


def _csi_apply_live() -> bool:
    """Aplikuje ustawienia na działającą kamerę (bez restartu)."""
    if _csi_picam2 is None:
        return False
    try:
        _csi_picam2.set_controls(_csi_controls_from_settings())
        return True
    except Exception as e:
        print(f"[Kamera/CSI] set_controls: {e}")
        return False


# ── Priorytet tablicy w ekspozycji ───────────────────────────────────────────
# Nocą tablica jest ODBLASKOWA: automatyka mierzy ciemną scenę, podnosi czas i
# wzmocnienie, a tablica wychodzi prześwietlona — znaki zlewają się w biel i OCR
# nie ma czego czytać. Gdy tablica jest w kadrze, liczymy ekspozycję Z NIEJ.
AE_PLATE_ON     = True    # przełącznik w menu „Obraz" (zapisywany w configu)
AE_PLATE_TARGET = 205.0   # docelowy 95. percentyl jasności tablicy (0–255)
AE_PLATE_DEAD   = 0.12    # martwa strefa ±12% — bez niej pętla drga w kółko
AE_PLATE_DAMP   = 0.75    # tłumienie korekty (przeciw przeregulowaniu)
AE_PLATE_SETTLE = 3       # klatki oczekiwania, aż zmiana wejdzie w życie
AE_PLATE_HOLD   = 1.0     # s bez tablicy → powrót do automatyki kadru
AE_EXP_MIN      = 60      # µs — podłoga migawki
AE_GAIN_MAX     = 12.0

_ae_plate = {"akt": False, "ost": 0.0, "next_fc": 0, "exp": 0, "gain": 1.0}


def _ae_plate_reset() -> None:
    """Koniec priorytetu tablicy — oddajemy sterowanie automatyce użytkownika."""
    if not _ae_plate["akt"]:
        return
    _ae_plate.update(akt=False, next_fc=0)
    _csi_apply_live()          # przywraca AeEnable + tryb/EV/pomiar z ustawień


def _ae_plate_tick(frame_bgr, dets, w: int, h: int, fc: int, now: float) -> None:
    """Ekspozycja liczona z wycinka tablicy, gdy tylko jakaś jest w kadrze.

    SZYBKOŚĆ vs STABILNOŚĆ: korekta jest MULTIPLIKATYWNA (jasność jest liniowa
    względem naświetlenia), więc trafia w cel w jednym–dwóch krokach zamiast
    powoli dojeżdżać. Stabilność bierze się nie ze spowolnienia pętli, tylko
    z CZEKANIA, aż zmiana faktycznie wejdzie w życie (~3 klatki) — poprawianie
    na podstawie nieaktualnej klatki to główna przyczyna „pompowania" jasności.
    """
    if not (AE_PLATE_ON and CAM_SOURCE == "csi" and _csi_picam2 is not None) \
            or CSI_CAM_SETTINGS.get("manual"):
        _ae_plate_reset()      # wyłączone albo user steruje ręcznie — nie wchodzimy
        return
    najlepsza = None                     # największa pewna tablica w kadrze
    for _tid, label, conf, xmin, ymin, xmax, ymax in dets:
        if label != "license_plate" or conf < PLATE_MIN_CONF:
            continue
        pw, ph = (xmax - xmin) * w, (ymax - ymin) * h
        if pw < 24 or ph < 10:           # za mała na sensowny pomiar
            continue
        if najlepsza is None or pw * ph > najlepsza[0]:
            najlepsza = (pw * ph, xmin, ymin, xmax, ymax)
    if najlepsza is None:
        if _ae_plate["akt"] and now - _ae_plate["ost"] > AE_PLATE_HOLD:
            _ae_plate_reset()
        return
    _ae_plate["ost"] = now
    if fc < _ae_plate["next_fc"]:        # poprzednia zmiana jeszcze nie zadziałała
        return
    _, xmin, ymin, xmax, ymax = najlepsza
    x1, x2 = max(0, int(xmin * w)), min(w, int(xmax * w))
    y1, y2 = max(0, int(ymin * h)), min(h, int(ymax * h))
    if x2 - x1 < 8 or y2 - y1 < 4:
        return
    # pełna klatka jest RGB (24.09) — szarość liczona z RGB
    szary = cv2.cvtColor(frame_bgr[y1:y2, x1:x2], cv2.COLOR_RGB2GRAY)
    # 95. percentyl, nie średnia: interesuje nas TŁO tablicy (jasne), a nie
    # znaki; średnia zależałaby od tego, ile znaków ma tablica.
    jasnosc = float(np.percentile(szary, 95))
    przepal = float(np.count_nonzero(szary >= 250)) / max(1, szary.size)
    if not _ae_plate["akt"]:             # start: od tego, co kamera ma teraz
        _ae_plate["exp"] = int(_csi_meta.get("exposure_us") or 8000)
        _ae_plate["gain"] = float(_csi_meta.get("gain") or 2.0)
        _ae_plate["akt"] = True
    if przepal > 0.15:
        # Tablica PRZYPALONA: pomiar nie mówi JAK bardzo (255 to może być 2x
        # albo 10x za dużo), więc korekta proporcjonalna dawałaby ~15% na krok
        # i schodziła sekundami. Tniemy zdecydowanie, aż wyjdzie z nasycenia —
        # to główny przypadek nocny i musi trwać ułamek sekundy.
        wsp = 0.35
    else:
        blad = AE_PLATE_TARGET / max(1.0, jasnosc)
        if abs(blad - 1.0) <= AE_PLATE_DEAD:
            return                       # w martwej strefie — nic nie ruszamy
        wsp = max(0.25, min(4.0, 1.0 + (blad - 1.0) * AE_PLATE_DAMP))
    swiatlo = _ae_plate["exp"] * _ae_plate["gain"] * wsp
    # Najpierw krótka migawka (mniej rozmycia ruchowego), wzmocnienie dopiero
    # gdy czas dobije do sufitu — szum psuje OCR mniej niż smużenie.
    sufit = int(CSI_CAM_SETTINGS.get("max_shutter_us") or 0) or 16666
    exp = int(max(AE_EXP_MIN, min(sufit, swiatlo)))
    gain = max(1.0, min(AE_GAIN_MAX, swiatlo / max(1, exp)))
    _ae_plate.update(exp=exp, gain=gain, next_fc=fc + AE_PLATE_SETTLE)
    try:
        _csi_picam2.set_controls({"AeEnable": False, "ExposureTime": exp,
                                  "AnalogueGain": float(gain)})
    except Exception as e:
        print(f"[AE-tablica] set_controls: {e}")


def _csi_shutter_label(us: int) -> str:
    """µs → czytelny czas migawki (spójnie z UI www)."""
    us = int(us)
    if us >= 1000000:
        return "%.1f s" % (us / 1e6)
    return "1/%d s" % round(1e6 / max(1, us))

# Strefa detekcji — czworokąt (4 punkty, współrzędne względne 0.0–1.0, kolejność CW)
# Edytowalna przez stronę www (/api/zone).
DETECT_ZONE: list = [
    [0.05, 0.18],   # lewy górny
    [0.95, 0.18],   # prawy górny
    [0.95, 0.88],   # prawy dolny
    [0.05, 0.88],   # lewy dolny
]
_zone_lock = threading.Lock()


# Ustawienia kamery zapisane przez www (v4l2 ctrl -> wartość);
# pusty dict = kamera zostaje na swoich domyślnych (auto)
CAMERA_OVERRIDES: dict = {}


def _apply_camera_settings() -> None:
    """Aplikuje zapisane ustawienia kamery (v4l2) — wołane przy starcie."""
    import subprocess as _sp
    for name, value in CAMERA_OVERRIDES.items():
        try:
            _sp.run(["v4l2-ctl", "-d", "/dev/video0",
                     f"--set-ctrl={name}={int(value)}"],
                    capture_output=True, timeout=5)
        except Exception as e:
            print(f"[Kamera] {name}: {e}")
    if CAMERA_OVERRIDES:
        print(f"[Kamera] zastosowano zapisane ustawienia: {CAMERA_OVERRIDES}")


def _apply_csi_settings() -> None:
    """Tryb CSI: włącza sprzętowy HDR sensora (IMX708, kontrolka
    wide_dynamic_range na v4l-subdev). MUSI być ustawione zanim libcamera
    otworzy kamerę — dlatego wołane na początku main()."""
    import glob as _glob
    import subprocess as _sp
    if not CSI_HDR:
        return
    for sd in sorted(_glob.glob("/dev/v4l-subdev*")):
        try:
            ls = _sp.run(["v4l2-ctl", "-d", sd, "--list-ctrls"],
                         capture_output=True, text=True, timeout=5)
            if "wide_dynamic_range" not in ls.stdout:
                continue
            _sp.run(["v4l2-ctl", "-d", sd,
                     "--set-ctrl=wide_dynamic_range=1"],
                    capture_output=True, timeout=5)
            print(f"[Kamera/CSI] HDR sensora włączony ({sd})")
            return
        except Exception as e:
            print(f"[Kamera/CSI] {sd}: {e}")
    print("[Kamera/CSI] UWAGA: brak kontrolki wide_dynamic_range — HDR nieaktywny")


def _patch_picamera_thread() -> None:
    """Tryb CSI: podmienia picamera_thread z hailo-apps na kopię z naszymi
    kontrolkami AE. Kluczowa: AeExposureMode=Short — automatyka preferuje
    KRÓTKIE czasy naświetlania (jasność ratuje gainem), czyli sufit migawki
    nocą — dokładnie to, co przy USB dawała ręczna migawka 4 ms, ale bez
    utraty auto. Kopia 1:1 z gstreamer_app.py (zweryfikowana 2026-07-16);
    przy jakimkolwiek braku zostaje oryginał (tylko domyślne AE)."""
    try:
        import hailo_apps.python.core.gstreamer.gstreamer_app as _ga
        from picamera2 import Picamera2
        from libcamera import Transform as _Transform
        from libcamera import controls as _lc
        from gi.repository import Gst
    except Exception as e:
        print(f"[Kamera/CSI] patch AE niedostępny (zostaje domyślne AE): {e}")
        return

    def lpr_picamera_thread(pipeline, video_width, video_height,
                            video_format, picamera_config=None):
        global _csi_picam2
        appsrc = pipeline.get_by_name("app_source")
        appsrc.set_property("is-live", True)
        appsrc.set_property("format", Gst.Format.TIME)
        # Zimny start w aucie: sensor CSI potrafi wstac PO uruchomieniu appki
        # (17.07: global_camera_info()==[] -> IndexError -> crash-loop).
        # Czekamy na kamere; jak sie nie pojawi — kontrolowany restart uslugi.
        # Czekamy na kamerę BEZ RESTARTU. Wcześniej po 60 s był os._exit(3)
        # → systemd restartował, po 3 razach usługa padała na start-limit i
        # ekran zostawał pusty (użytkownik nie wiedział, co się dzieje).
        # Teraz: pokazujemy pełnoekranowy komunikat i czekamy — kamerę można
        # wpiąć na żywo, aplikacja ruszy sama. Restart tylko awaryjnie, gdyby
        # po długim czasie sterownik się nie pozbierał.
        _i = 0
        while True:
            try:
                if Picamera2.global_camera_info():
                    break
            except Exception as e:
                _zglos("kamera_czekanie", e)
            _i += 1
            if _i <= 6:                     # ~12 s: zwykły rozruch sensora
                _set_boot("kamera", "Wykrywanie kamery…",
                          "Trwa uruchamianie sensora")
            else:
                _set_boot("kamera_blad", "Kamera niepodłączona",
                          "Sensor CSI nie odpowiada. Sprawdź taśmę "
                          "między kamerą a Raspberry Pi:", blad=True,
                          kroki=["Wyłącz zasilanie urządzenia",
                                 "Wepnij taśmę CSI do oporu (oba końce, "
                                 "styki we właściwą stronę)",
                                 "Włącz zasilanie — ruszy automatycznie"])
            print("[Kamera/CSI] czekam na wykrycie kamery przez libcamerę…")
            time.sleep(2)
            if _i >= 150:                   # ~5 min bez skutku → twardy restart
                print("[Kamera/CSI] BŁĄD: kamera niewykryta po 5 min — "
                      "restart usługi")
                os._exit(3)
        _set_boot("hailo", "Uruchamianie kamery…", "Konfiguracja obrazu")
        _apply_csi_settings()    # HDR mógł nie wejść, gdy sensora nie było
        with Picamera2(tuning=_csi_tuning_file) as picam2:
            _csi_picam2 = picam2
            cam_controls = {"FrameRate": 30}
            cam_controls.update(_csi_controls_from_settings())
            # 24.09: NIE decydujemy tu „czy nagrywać". Od 02.09 dashcam
            # startuje dopiero po rozruchu potoku (_dashcam_po_starcie), więc
            # w tym miejscu `_dash_appsrc` jest jeszcze None — dawne
            # `dash_on = _dash_appsrc is not None` zamrażało False na zawsze
            # i wideorejestrator CSI NIE NAGRYWAŁ NIC od 02.09 (sesje z
            # 0 plików). lores jest teraz zawsze przechwytywany, a w pętli
            # sprawdzamy `_dash_appsrc` NA BIEŻĄCO; `dash_on` służy już tylko
            # do wyłączenia po błędzie.
            dash_on = True
            if picamera_config is None:
                # main DOKŁADNIE w żądanym rozmiarze (ISP kamery CSI przyjmie
                # dowolny — get_camera_resolution z hailo-apps przycinało do
                # "standardowych" rozmiarów webcamow i psuło 2304×1296).
                # Format "BGR888": w picamera2 nazwy są odwrócone — BGR888
                # daje bajty w kolejności R,G,B, czyli dokładnie to, czego
                # chcą capsy RGB w pipeline. Dzięki temu WYPADA pełnoklatkowe
                # cv2.cvtColor na CPU (oryginał hailo-apps bierze "RGB888"
                # = bajty B,G,R i konwertuje każdą klatkę).
                # 24.09: `main` ma STAŁY pełny rozmiar (wycinki/zdjęcia), a
                # `lores` = rozmiar z lpr_run.sh (--width/--height, 1280×720)
                # idzie do potoku i podglądu. Oba w BGR888 (= bajty R,G,B).
                # Pi 5 pozwala na lores w RGB (na Pi 4 musiałby być YUV).
                main = {"size": (LPR_MAIN_W, LPR_MAIN_H), "format": "BGR888"}
                # Obrot 180 ROBI SENSOR, nie procesor. Kamera wisi w aucie do
                # gory nogami; do 02.09.2026 prostowal to `videoflip
                # rotate-180` w potoku. Zmierzone: 11,2 ms na klatke
                # 2304x1296 (300 klatek: 5,49 s z flipem vs 2,14 s bez),
                # czyli 33% rdzenia przy 30 fps oddane za nic. IMX708 czyta
                # piksele w odwroconej kolejnosci sprzetowo - koszt zero.
                # SPRZEZONE z lpr_run.sh: w trybie csi NIE WOLNO podawac
                # --horizontal-mirror --vertical-mirror, bo obraz obroci sie
                # dwa razy (czyli wroci do pionu, ale znow placac CPU).
                obrot = _Transform(hflip=1, vflip=1)
                # lores: jeden strumień dla potoku, podglądu I dashcamu.
                # Dashcam dostawał dotąd YUV420 wprost pod koder; teraz RGB
                # + `videoconvert` w potoku nagrywania (1280×720 @15 fps
                # ≈ 2 ms/klatkę) — użytkownik: „rejestrator mniej ważny niż
                # rozpoznawanie", a potok detekcji nie może dostawać YUV.
                lores = {"size": (video_width, video_height), "format": "BGR888"}
                config = picam2.create_preview_configuration(
                    main=main, lores=lores, controls=cam_controls,
                    transform=obrot)
                capture_stream = "lores"
                pierscien_on = True
            else:
                dash_on = False
                config = picamera_config
                capture_stream = "lores" if "lores" in config else "main"
                pierscien_on = False

            picam2.configure(config)
            stream_config = config.get(capture_stream, config["main"])
            format_str = ("RGB" if stream_config["format"] in ("RGB888",
                                                               "BGR888")
                          else video_format)
            width, height = stream_config["size"]
            appsrc.set_property(
                "caps",
                Gst.Caps.from_string(
                    f"video/x-raw, format={format_str}, width={width}, "
                    f"height={height}, framerate=30/1, pixel-aspect-ratio=1/1"))
            picam2.start()
            print(f"[Kamera/CSI] Picamera2 start {width}x{height} "
                  f"(AE=Short, HDR wg _apply_csi_settings)")
            frame_count = 0
            dash_count = 0
            dash_t0 = 0.0          # czas (mono) pierwszej klatki dashcamu
            dash_dur = Gst.util_uint64_scale_int(1, Gst.SECOND, DASHCAM_FPS)
            while True:
                take_dash = (dash_on and _dash_appsrc is not None
                             and frame_count % 2 == 0)
                if pierscien_on:
                    # main → pierścień (wycinki OCR), lores → potok (+ dashcam
                    # co 2. klatkę). Obie tablice z TEGO SAMEGO ujęcia sensora,
                    # więc numer klatki wiąże je jednoznacznie.
                    arrays, _meta = picam2.capture_arrays(["main", "lores"])
                    frame_main, frame_data = arrays
                    _pierscien_dodaj(frame_count, frame_main)
                    dash_data = frame_data if take_dash else None
                else:
                    arrays, _meta = picam2.capture_arrays([capture_stream])
                    frame_data, dash_data = arrays[0], None
                if frame_count % 15 == 0 and _meta:
                    # odczyty dla panelu KAMERA (migawka/gain/lux na żywo)
                    _csi_meta.update(
                        exposure_us=_meta.get("ExposureTime"),
                        gain=_meta.get("AnalogueGain"),
                        lux=_meta.get("Lux"),
                        colour_temp=_meta.get("ColourTemperature"))
                if frame_data is None:
                    print("[Kamera/CSI] brak klatki — koniec wątku kamery")
                    break
                # Opóźnienie PRZED potokiem: od naświetlenia matrycy do chwili,
                # gdy klatka trafia w nasze ręce. To luka, której nie widzi
                # pomiar oparty na PTS (ten stemplujemy dopiero przy wypchnięciu).
                # Tu chowa się ewentualne dławienie wątku kamery — np. gdy koder
                # wideorejestratora nie nadąża i Picamera2 zwraca coraz starsze
                # klatki ze swojego bufora.
                if _meta:
                    _ts = _meta.get("SensorTimestamp")
                    if _ts:
                        _op = time.monotonic() - _ts / 1_000_000_000.0
                        if 0.0 <= _op < 60.0:
                            with _diag_lock:
                                _diag["kam_s"] = _op
                                if _op > _diag.get("kam_max", 0.0):
                                    _diag["kam_max"] = _op
                # BGR888 z picamera2 = bajty R,G,B — bez cvtColor (patrz wyżej)
                frame = frame_data
                buffer = Gst.Buffer.new_wrapped(frame.tobytes())
                buffer_duration = Gst.util_uint64_scale_int(1, Gst.SECOND, 30)
                buffer.pts = frame_count * buffer_duration
                buffer.duration = buffer_duration
                ret = appsrc.emit("push-buffer", buffer)
                if ret == Gst.FlowReturn.FLUSHING:
                    break
                if ret != Gst.FlowReturn.OK:
                    print(f"[Kamera/CSI] push-buffer: {ret}")
                    break
                # wideorejestrator: lores YUV420 prosto do kodera (leaky
                # appsrc/queue — jak koder nie nadąża, gubi NAGRANIE,
                # nigdy nie cofa się do kamery ani LPR)
                if take_dash and dash_data is not None:
                    try:
                        # 02.10.2026: PTS = PRAWDZIWY czas z sensora, nie
                        # licznik × 1/15 s. Kamera daje 23–29 fps (nie 30),
                        # więc „60 s" pliku obejmowało 62–70 s zegara; po
                        # 35 min dryf sięgał 3 min i klip radaru trafiał poza
                        # nagranie („brak klatek w oknie"). Teraz pozycja
                        # w pliku (CAP_PROP_POS_MSEC) = czas ścienny − start
                        # segmentu, a start każdego segmentu zapisuje
                        # splitmuxsink (format-location-full).
                        _dts = _meta.get("SensorTimestamp") if _meta else None
                        _dtm = (_dts / 1_000_000_000.0) if _dts else time.monotonic()
                        if not _dash_caps_ok:
                            # klatka lores jest RGB (bajty R,G,B) — rozmiar
                            # z tablicy, nie ze stałych (24.09)
                            _dh, _dw = dash_data.shape[:2]
                            _dash_appsrc.set_property(
                                "caps", Gst.Caps.from_string(
                                    f"video/x-raw, format=RGB, "
                                    f"width={_dw}, height={_dh}, "
                                    f"framerate={DASHCAM_FPS}/1, "
                                    f"pixel-aspect-ratio=1/1"))
                            globals()['_dash_caps_ok'] = True
                            dash_count = 0        # nowy pipeline → od zera
                            dash_t0 = _dtm
                        _ses = _dash_sesja
                        if _ses is not None and "t0_mono" not in _ses:
                            # start sesji = TERAZ minus opóźnienie sensora.
                            # Nie liczymy z epoki SensorTimestamp (02.10:
                            # dało 8 h wstecz) — PTS i tak są względne.
                            _lat = time.monotonic() - dash_t0
                            if not (0.0 <= _lat < 1.0):
                                _lat = 0.1
                            _ses["t0_mono"] = dash_t0
                            _ses["start"] = datetime.now() - timedelta(seconds=_lat)
                            print(f"[Dashcam/CSI] start sesji "
                                  f"{_ses['start'].strftime('%H:%M:%S.%f')[:-3]}, "
                                  f"opóźnienie sensora {time.monotonic() - dash_t0:.3f} s "
                                  f"(SensorTimestamp {dash_t0:.3f}, monotonic "
                                  f"{time.monotonic():.3f})", flush=True)
                        dbuf = Gst.Buffer.new_wrapped(dash_data.tobytes())
                        dbuf.pts = max(0, int((_dtm - dash_t0) * 1_000_000_000))
                        dbuf.duration = dash_dur
                        dash_count += 1
                        _dash_appsrc.emit("push-buffer", dbuf)
                    except Exception as e:
                        print(f"[Dashcam/CSI] push wyłączony: {e}")
                        globals()['_dash_appsrc'] = None
                        dash_on = False
                frame_count += 1
                globals()["_kamera_klatka"] = frame_count   # odniesienie dla _diag
        _csi_picam2 = None

    def _guarded_picamera_thread(*a, **kw):
        try:
            lpr_picamera_thread(*a, **kw)
        except Exception:
            import traceback
            traceback.print_exc()
            print("[Kamera/CSI] awaria wątku kamery — restart usługi")
            os._exit(3)      # martwy wątek = zamrożony podgląd; lepiej restart

    _ga.picamera_thread = _guarded_picamera_thread
    print("[Kamera/CSI] wątek kamery podmieniony (AE=Short)")


def _find_uvc_camera() -> Optional[str]:
    """Zwraca /dev/videoN kamery USB (UVC). W trybie CSI nie można polegać na
    /dev/video0 (te węzły zajmuje PiSP) — szukamy po stabilnym /dev/v4l/by-id/,
    gdzie trafiają wyłącznie kamery USB."""
    import glob as _glob
    for link in sorted(_glob.glob("/dev/v4l/by-id/usb-*-video-index0")):
        try:
            return str(Path(link).resolve())
        except OSError:
            continue
    return None


def _load_runtime_config() -> None:
    """Wczytuje ustawienia zmienione przez www (progi + strefa + kamera)."""
    global OCR_MIN_MATCHING, OCR_MIN_CONF, PLATE_MIN_CONF, DETECT_ZONE, CAM_SOURCE
    import json as _json
    try:
        with open(RUNTIME_CONFIG_PATH) as f:
            cfg = _json.load(f)
    except FileNotFoundError:
        return
    except Exception as e:
        print(f"[Config] błąd wczytywania: {e}")
        return
    OCR_MIN_MATCHING = int(cfg.get("ocr_min_matching", OCR_MIN_MATCHING))
    OCR_MIN_CONF     = float(cfg.get("ocr_min_conf", OCR_MIN_CONF))
    if "LPR_OCR_BACKEND" not in os.environ and cfg.get("ocr_backend") in ("cpu", "hailo", "auto"):
        globals()["OCR_BACKEND"] = cfg["ocr_backend"]
    PLATE_MIN_CONF   = float(cfg.get("plate_min_conf", PLATE_MIN_CONF))
    if cfg.get("cam_source") in ("usb", "csi"):
        CAM_SOURCE = cfg["cam_source"]
    global TTS_ALL, BT_DEVICE, TTS_SPELL_MODE, TTS_GAIN, TTS_BRAND, HUD_ON
    TTS_BRAND = bool(cfg.get("tts_brand", TTS_BRAND))
    HUD_ON = bool(cfg.get("hud_on", HUD_ON))
    try:
        globals()["SCREEN_OFF_MIN"] = int(cfg.get("screen_off_min", SCREEN_OFF_MIN))
        globals()["TOUCH_SOUND"] = bool(cfg.get("touch_sound", TOUCH_SOUND))
    except (TypeError, ValueError):
        pass
    try:
        globals()["NOTIF_SCALE"] = max(0.5, min(2.0,
                                       float(cfg.get("notif_scale",
                                                     NOTIF_SCALE))))
    except (TypeError, ValueError):
        pass
    globals()["AE_PLATE_ON"] = bool(cfg.get("ae_plate", AE_PLATE_ON))
    if cfg.get("tts_spell_mode") in ("letters", "phonetic"):
        TTS_SPELL_MODE = cfg["tts_spell_mode"]
    if "tts_gain" in cfg:                   # migracja ze starej, jednej
        try:                                # głośności na trzy osobne
            TTS_GAIN = max(1.0, min(4.0, float(cfg["tts_gain"])))
            for k in TTS_GAINS:
                TTS_GAINS[k] = TTS_GAIN
        except (TypeError, ValueError):
            pass
    for k, v in (cfg.get("tts_gains") or {}).items():
        if k in TTS_GAINS:
            try:
                TTS_GAINS[k] = max(1.0, min(4.0, float(v)))
            except (TypeError, ValueError):
                pass
    WATCH_GROUPS.clear()
    for gid, g in (cfg.get("watch_groups") or {}).items():
        WATCH_GROUPS[str(gid)] = {
            "name": str(g.get("name") or gid)[:40],
            "alert": str(g.get("alert") or _watch_default_group()["alert"])[:200],
            "enabled": bool(g.get("enabled", True)),
            "color": str(g.get("color") or "red"),
            "nota": bool(g.get("nota", True)),
        }
    if not WATCH_GROUPS:
        WATCH_GROUPS["default"] = _watch_default_group()
    WATCHLIST.clear()
    for k, v in (cfg.get("watchlist") or {}).items():
        p = str(k).upper()
        if isinstance(v, dict):      # nowy format
            gid = str(v.get("group") or "default")
            WATCHLIST[p] = {"group": gid if gid in WATCH_GROUPS else "default",
                            "note": str(v.get("note") or "")[:60]}
        else:                        # stary format {tablica: notatka}
            WATCHLIST[p] = {"group": next(iter(WATCH_GROUPS)),
                            "note": str(v or "")[:60]}
    TTS_ALL = bool(cfg.get("tts_all", TTS_ALL))
    BT_DEVICE = str(cfg.get("bt_device") or "")
    # tryb radaru (25.09) — tylko znane klucze, typy jak w domyślnych
    for k, v in (cfg.get("radar") or {}).items():
        if k in RADAR_CFG:
            try:
                RADAR_CFG[k] = type(RADAR_CFG[k])(v)
            except (TypeError, ValueError):
                pass
    zone = cfg.get("detect_zone")
    if zone and len(zone) == 4:
        with _zone_lock:
            DETECT_ZONE = [[float(p[0]), float(p[1])] for p in zone]
    CAMERA_OVERRIDES.clear()
    CAMERA_OVERRIDES.update(cfg.get("camera") or {})
    saved_csi = cfg.get("csi_camera") or {}
    for k in CSI_CAM_SETTINGS:
        if k in saved_csi:
            CSI_CAM_SETTINGS[k] = saved_csi[k]
    print(f"[Config] wczytano ustawienia z {RUNTIME_CONFIG_PATH}")


def _save_runtime_config() -> None:
    import json as _json
    try:
        with _zone_lock:
            zone = [list(p) for p in DETECT_ZONE]
        cfg = {"ocr_min_matching": OCR_MIN_MATCHING,
               "ocr_min_conf": OCR_MIN_CONF,
               "ocr_backend": OCR_BACKEND,
               "plate_min_conf": PLATE_MIN_CONF,
               "detect_zone": zone,
               "camera": CAMERA_OVERRIDES,
               "csi_camera": CSI_CAM_SETTINGS,
               "cam_source": CAM_SOURCE,
               "watchlist": WATCHLIST,
               "watch_groups": WATCH_GROUPS,
               "tts_all": TTS_ALL,
               "tts_spell_mode": TTS_SPELL_MODE,
               "tts_gains": TTS_GAINS,
               "tts_brand": TTS_BRAND,
               "screen_off_min": SCREEN_OFF_MIN,
               "touch_sound": TOUCH_SOUND,
               "hud_on": HUD_ON,
               "notif_scale": NOTIF_SCALE,
               "ae_plate": AE_PLATE_ON,
               "bt_device": BT_DEVICE,
               "radar": RADAR_CFG}
        with open(RUNTIME_CONFIG_PATH, "w") as f:
            _json.dump(cfg, f, indent=2)
    except Exception as e:
        print(f"[Config] błąd zapisu: {e}")


def _point_in_zone(cx: float, cy: float, zone: list) -> bool:
    """Ray-casting: czy punkt (cx,cy) leży wewnątrz wielokąta zone."""
    n = len(zone)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = zone[i]
        xj, yj = zone[j]
        if ((yi > cy) != (yj > cy)) and (cx < (xj - xi) * (cy - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside

# Kolory podglądu
COLOR_TRACKING  = (0, 200, 255)  # żółty — śledzenie, brak pewności
COLOR_CONFIDENT = (0, 255, 80)   # zielony — numer potwierdzony
COLOR_VEHICLE   = (0, 165, 255)  # pomarańczowy — pojazd (bez tablicy)
COLOR_ROI       = (60, 60, 60)   # szary — ramka ROI na podglądzie


# ── Fuzzy OCR consensus ───────────────────────────────────────────────────────

def _levenshtein(a: str, b: str) -> int:
    """Edit distance z early-exit gdy różnica długości > OCR_FUZZY_DIST."""
    if abs(len(a) - len(b)) > OCR_FUZZY_DIST + 1:
        return OCR_FUZZY_DIST + 1
    m, n = len(a), len(b)
    row = list(range(n + 1))
    for i in range(1, m + 1):
        prev = row[0]
        row[0] = i
        for j in range(1, n + 1):
            temp = row[j]
            row[j] = prev if a[i-1] == b[j-1] else 1 + min(prev, row[j], row[j-1])
            prev = temp
    return row[n]


def _waga_odczytu(crop_px: float, conf: float) -> float:
    """Waga głosu OCR w konsensusie (05.10). Z pomiaru na 254 wycinkach z jazdy
    02.10 (model wdrożony 04.10): o trafności POJEDYNCZEGO odczytu decyduje
    pewność OCR — ≥ 0,98 → 97,7 % dobrych (177), 0,95–0,98 → 74 % (43),
    0,90–0,95 → 44 % (16), < 0,90 → 5 % (18) — oraz wysokość wycinka:
    60–120 px → 89–94 %, 45–60 px → 69 %, > 120 px → 69 % (rozmycie z bliska).
    Ostrość (wariancja Laplace'a) nie rozdzielała dobrych od złych — nie liczy się.
    Dotąd grupę wybierała LICZBA głosów, więc 3 słabe odczyty z daleka
    (np. D↔O, 7↔9) przegłosowywały 2 pewne z bliska."""
    if conf >= 0.98:
        wc = 1.0
    elif conf >= 0.95:
        wc = 0.5
    elif conf >= 0.90:
        wc = 0.25
    else:
        wc = 0.1
    if 60 <= crop_px < 120:
        wh = 1.0
    elif crop_px >= 45:          # 45–60 albo ≥ 120
        wh = 0.7
    else:
        wh = 0.5
    return wc * wh


def _fuzzy_dodaj(groups: list, text: str, conf: float, waga: float,
                 max_dist: int = OCR_FUZZY_DIST) -> None:
    """Dokłada jeden odczyt do grup (08.10: przyrostowo — przy czytaniu co
    klatkę na Hailo liczenie wszystkiego od nowa po każdym odczycie to
    ~1000 Levenshteinów w czystym Pythonie 30×/s na track)."""
    for group in groups:
        # A1: porównuj z każdym elementem grupy — nie tylko pierwszym
        if any(_levenshtein(text, g[0]) <= max_dist for g in group):
            group.append((text, conf, waga))
            return
    groups.append([(text, conf, waga)])


def _fuzzy_wynik(groups: list) -> tuple:
    """(kanon | None, liczba głosów, suma wag) ze zbudowanych grup."""
    if not groups:
        return None, 0, 0.0
    best_group = max(groups, key=lambda g: (sum(x[2] for x in g), len(g)))
    suma: dict = {}
    for text, conf, waga in best_group:
        w0, c0 = suma.get(text, (0.0, 0.0))
        suma[text] = (w0 + waga, max(c0, conf))
    canonical = max(suma.items(), key=lambda kv: kv[1])[0]
    return canonical, len(best_group), sum(x[2] for x in best_group)


def _fuzzy_consensus(
    results: list,                # [(text, conf) | (text, conf, waga), ...]
    max_dist: int = OCR_FUZZY_DIST,
) -> tuple:                       # (canonical_text | None, group_size, group_weight)
    """
    Grupuje odczyty OCR po podobieństwie Levenshtein i zwraca kanoniczny tekst
    grupy o NAJWIĘKSZEJ ŁĄCZNEJ WADZE (05.10; wcześniej: najliczniejszej).
    "WA12345" i "WA1234S" trafiają do tej samej grupy (dist=1).
    Porównanie z KAŻDYM elementem grupy (nie tylko pierwszym) zapobiega dryfowaniu.
    Kanon = wariant tekstu o największej sumie wag w grupie (remis → wyższa pewność).
    Zwraca też liczbę głosów (próg OCR_MIN_MATCHING, suwak www) i sumę wag.
    """
    # Suwak "pewność OCR" (www) działa tutaj: słabe odczyty nie głosują,
    # ale nie są zamieniane na UNKNOWN (nie zatruwają wyników)
    groups: list = []               # każda grupa to lista (text, conf, waga)
    for r in results:
        if r[0] and r[0] != "UNKNOWN" and r[1] >= OCR_MIN_CONF:
            _fuzzy_dodaj(groups, r[0], r[1], (r[2] if len(r) > 2 else 1.0), max_dist)
    return _fuzzy_wynik(groups)


# ── Deskew (wyprostowanie skośnej tablicy) ────────────────────────────────────

def _deskew_plate(crop_bgr: np.ndarray) -> np.ndarray:
    """
    Koryguje obrót tablicy przez wykrycie dominującego kąta poziomych krawędzi
    (Hough lines). Działa na małych kątkach (±20°) typowych dla kamery w aucie.
    Zwraca nową klatkę; przy braku pewnych linii — oryginał.
    """
    h, w = crop_bgr.shape[:2]
    if h < 12 or w < 30:
        return crop_bgr

    gray  = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, 40, 120)

    # Szukamy linii poziomych dłuższych niż 1/3 szerokości croppa
    lines = cv2.HoughLinesP(
        edges, 1, np.pi / 180,
        threshold=max(15, w // 5),
        minLineLength=w // 3,
        maxLineGap=w // 8,
    )
    if lines is None:
        return crop_bgr

    angles = []
    for x1, y1, x2, y2 in lines[:, 0]:
        angle = np.degrees(np.arctan2(float(y2 - y1), float(x2 - x1)))
        if abs(angle) < 22:          # tylko linie bliskie poziomowi
            angles.append(angle)

    if not angles:
        return crop_bgr

    median_angle = float(np.median(angles))
    if abs(median_angle) < 1.2:      # poniżej progu — nie ruszaj
        return crop_bgr

    # A5: powiększ canvas o margines przed rotacją — bez niego rogi tablicy są obcinane
    pad = int(max(h, w) * 0.15)
    padded = cv2.copyMakeBorder(crop_bgr, pad, pad, pad, pad, cv2.BORDER_REPLICATE)
    ph, pw = padded.shape[:2]
    M = cv2.getRotationMatrix2D((pw / 2, ph / 2), median_angle, 1.0)
    rotated = cv2.warpAffine(padded, M, (pw, ph), borderMode=cv2.BORDER_REPLICATE)
    return rotated[pad:pad + h, pad:pad + w]


# ── Atomowy zapis ─────────────────────────────────────────────────────────────

def _fsync_dir(directory: Path) -> None:
    fd = os.open(str(directory), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def safe_write_image(path: Path, img_bgr, quality: int = JPEG_QUALITY) -> bool:
    tmp = path.with_name(path.stem + ".tmp.jpg")
    try:
        if not cv2.imwrite(str(tmp), img_bgr, [cv2.IMWRITE_JPEG_QUALITY, quality]):
            raise IOError("cv2.imwrite zwróciło False")
        with open(tmp, "r+b") as f:
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(tmp), str(path))
        _fsync_dir(path.parent)
        return True
    except Exception as e:
        print(f"[Zapis] BŁĄD obrazu {path.name}: {e}")
        try:
            tmp.unlink()
        except OSError:
            pass
        return False


# ── OCR: CRNN helpers ─────────────────────────────────────────────────────────

PLATE_MIN_LEN = 4
PLATE_MAX_LEN = 9

_OCR_CHARACTERS = [
    'blank', '0', '1', '2', '3', '4', '5', '6', '7', '8', '9',
    ':', ';', '<', '=', '>', '?', '@',
    'A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'I', 'J', 'K', 'L', 'M',
    'N', 'O', 'P', 'Q', 'R', 'S', 'T', 'U', 'V', 'W', 'X', 'Y', 'Z',
    '[', '\\', ']', '^', '_', '`',
    'a', 'b', 'c', 'd', 'e', 'f', 'g', 'h', 'i', 'j', 'k', 'l', 'm',
    'n', 'o', 'p', 'q', 'r', 's', 't', 'u', 'v', 'w', 'x', 'y', 'z',
    '{', '|', '}', '~', '!', '"', '#', '$', '%', '&', "'", '(', ')',
    '*', '+', ',', '-', '.', '/', '  ', '  ',
]


def _resize_for_ocr(crop_bgr: np.ndarray,
                    target_h: int = 48, target_w: int = 320,
                    pad: int = 255) -> np.ndarray:  # A10: białe tło jak na europejskich tablicach
    h, w = crop_bgr.shape[:2]
    if h == 0 or w == 0:
        return np.full((target_h, target_w, 3), pad, dtype=np.uint8)
    scale = min(target_w / w, target_h / h)
    nw    = max(1, int(w * scale))
    nh    = max(1, int(h * scale))
    resized = cv2.resize(crop_bgr, (nw, nh), interpolation=cv2.INTER_CUBIC)
    pt = (target_h - nh) // 2
    pb = target_h - nh - pt
    pl = (target_w - nw) // 2
    pr = target_w - nw - pl
    return cv2.copyMakeBorder(resized, pt, pb, pl, pr,
                               cv2.BORDER_CONSTANT, value=[pad] * 3)


_DIGIT_TO_LETTER = str.maketrans("01258", "OIZSB")
_LETTER_TO_DIGIT = str.maketrans("OIBZD", "01820")

def _fix_pl_plate(text: str) -> str:
    """
    A2: Polskie tablice mają ściśle deterministyczny format:
      2–3 litery (prefiks powiatowy) + wyróżnik, np. WA12345, KR1234A.
    W wyróżniku (sufiksie) ZAKAZANE są tylko litery B, D, I, O, Z — więc
    tylko te wolno tłumaczyć na cyfry (B→8, D→0, I→1, O→0, Z→2).
    S jest legalne w sufiksie (WX111JS!) — NIE zamieniać na 5
    (do 2026-07-08 ta funkcja psuła każdą tablicę kończącą się na S).
    W prefiksie cyfry nie występują — tam tłumaczymy cyfry na litery.
    """
    # 04.10.2026 — WYŁĄCZONE (funkcja zwraca tekst bez zmian). Pomiar na 1362
    # etykietach ludzkich + predykcjach douczonego CCT (test 02.10, 231):
    #   reguła z „przejściem" na 3. znaku psuła 139/1362 PRAWDZIWYCH numerów
    #   (trzyliterowe wyróżniki WPI, WND, NDZ, WCI, LUB… → WP1, WN0, ND2…),
    #   na predykcjach: surowo 85,3 % → po regule 75,3 % (−10 pp), stary model
    #   78,8 % → 71,0 %. Łagodniejsza reguła (len 8 = 3 litery) nadal psuła
    #   53 numery (obce tablice AA0000XX, BB1111YY, indywidualne GTESTER).
    #   Model CCT douczony na polskich tablicach sam zna format — każda
    #   reguła tylko mu szkodzi. 188/4190 etykiet legacy (konsensus z lipca)
    #   jest zatrutych tą regułą (WX01234E = WXD1234E) — do oczyszczenia
    #   przy następnym treningu. Pełna analiza: aiHandoff 04.10.
    return text
    # --- stara reguła (zachowana do wglądu) ---
    if len(text) < 5 or len(text) > 8:
        return text
    split = None
    for i in range(2, min(4, len(text))):
        if text[i] in "0123456789OIBZD":
            split = i
            break
    if split is None:
        return text
    prefix = text[:split].translate(_DIGIT_TO_LETTER)
    suffix = text[split:].translate(_LETTER_TO_DIGIT)
    return prefix + suffix


def _decode_crnn(raw: np.ndarray):
    if raw.ndim == 2:
        raw = raw[np.newaxis]
    raw_f = raw.astype(np.float32)
    # Softmax wzdłuż osi znaków → prawdziwe prawdopodobieństwa [0,1]
    e = np.exp(raw_f - raw_f.max(axis=2, keepdims=True))
    prob = e / e.sum(axis=2, keepdims=True)
    text_idx  = raw_f.argmax(axis=2)
    text_prob = prob.max(axis=2)
    results = []
    for b in range(len(text_idx)):
        sel = np.ones(len(text_idx[b]), dtype=bool)
        sel[1:] = text_idx[b][1:] != text_idx[b][:-1]
        sel    &= text_idx[b] != 0
        chars   = [_OCR_CHARACTERS[int(i)]
                   for i in text_idx[b][sel]
                   if int(i) < len(_OCR_CHARACTERS)]
        conf = float(text_prob[b][sel].mean()) if sel.any() else 0.0
        results.append(("".join(chars), conf))
    return results


def _is_valid_plate(text: str) -> bool:
    if not (PLATE_MIN_LEN <= len(text) <= PLATE_MAX_LEN):
        return False
    return any(c.isalpha() for c in text) and any(c.isdigit() for c in text)


def _readability(crop_bgr: Optional[np.ndarray]) -> float:
    """Miara CZYTELNOŚCI cropu dla człowieka: ostrość × detal, odporna na
    szum sensora. Wyższa = ostrzejsza, mniej rozmyta klatka. Używana do
    wyboru ujęcia POKAZYWANEGO do oceny — 'największa × pewność' często
    wskazywała rozmazane ujęcie z bliska, którego i człowiek nie odczytał."""
    if crop_bgr is None or crop_bgr.size == 0:
        return 0.0
    h, w = crop_bgr.shape[:2]
    if h < 12 or w < 30:
        return 0.0
    g = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2GRAY)
    g = cv2.GaussianBlur(g, (3, 3), 0)      # tłumi szum (noc) — nie mylić z detalem
    return float(cv2.Laplacian(g, cv2.CV_64F).var())


# ── PlateTrack ────────────────────────────────────────────────────────────────

@dataclass
class PlateTrack:
    """Stan śledzenia jednej tablicy rejestracyjnej."""

    track_id:        int
    generation:      int   # rośnie gdy ByteTracker recyclinguje ten sam track_id
    first_seen_dt:   datetime
    first_seen_mono: float
    last_seen_mono:  float
    ocr_results:     list = field(default_factory=list)  # [(text, conf, waga), ...] — waga: _waga_odczytu
    pending_ocr:     int  = 0
    best_score:      float = 0.0   # area × det_conf — kryterium najlepszej klatki
    best_frame_bgr:  Optional[np.ndarray] = None
    best_crop_bgr:   Optional[np.ndarray] = None
    best_conf:       float = 0.0
    bbox:            tuple = (0.0, 0.0, 0.0, 0.0)  # ostatni xmin,ymin,xmax,ymax
    best_bbox:       tuple = ()     # bbox tablicy w momencie najlepszej klatki
    # NAJOSTRZEJSZA czytelna klatka (do OCENY przez człowieka) — osobno od
    # best (area×conf), bo największe ujęcie bywa najbardziej rozmyte
    sharp_score:     float = 0.0
    sharp_frame_bgr: Optional[np.ndarray] = None
    sharp_crop_bgr:  Optional[np.ndarray] = None
    sharp_bbox:      tuple = ()
    # bufor ostatnich cropów (mono, crop, score) — do zapisu klatek wokół
    # najlepszego ujęcia (czasem "najlepsza" jest akurat rozmyta)
    recent_crops:    deque = field(default_factory=lambda: deque(maxlen=12))
    # (t_mono, szerokość tablicy znormalizowana) — prędkość z kamery dla
    # trybu RADAR (`_kamera_predkosc`); 4 s przy 30 fps, koszt: krotka/klatkę
    szer_hist:       deque = field(default_factory=lambda: deque(maxlen=120))
    # 02.10 (radar): klatka, z której OCR dał NAJPEWNIEJSZY odczyt — na zdjęcie
    # zdarzenia idzie ona, a nie „najlepsza" wg detektora (ta bywa nieczytelna
    # dla człowieka, choć konsensus z kilku klatek dał numer). `ocr_pending`
    # trzyma (klatka, bbox) wysłanych do OCR do czasu wyniku (ref., bez kopii).
    ocr_pending:     dict  = field(default_factory=dict)   # seq → (frame, bbox)
    ocr_seq:         int   = 0
    ocr_best_conf:   float = 0.0
    ocr_best_text:   str   = ""
    ocr_frame_bgr:   Optional[np.ndarray] = None
    ocr_crop_bgr:    Optional[np.ndarray] = None
    ocr_bbox:        tuple = ()
    finalized:       bool  = False
    announced:       bool  = False  # czy pokazano już dymek powiadomienia
    announced_text:  str   = ""     # z jakim tekstem; dogrywka może go poprawić (jedna korekta)
    reannounced:     bool  = False
    ocr_silne:       int   = 0      # odczyty „silne" (pewność ≥ 0,98, wycinek 60–120 px) — koniec dogrywki przy 3
    ocr_dogrywka:    int   = 0      # ile odczytów wysłano PO konsensusie (limit OCR_DOGRYWKA_MAX)
    final_ocr_sent:  bool  = False  # czy wysłano "OCR ostatniej szansy" (best crop)
    # Cache konsensusu (24.09): `_fuzzy_consensus` to Levenshtein w czystym
    # Pythonie, O(n²) po max 50 odczytach, a właściwości niżej były wołane
    # 2-3× na track NA KAŻDĄ klatkę (dymki, ramka, etykieta) — z GIL-em
    # w ręku, na wątku potoku. Wynik zmienia się TYLKO gdy dojdzie odczyt
    # (`_drain_ocr` stawia `_cons_dirty`) albo gdy ktoś przestawi suwak
    # pewności OCR w www (klucz `_cons_conf`).
    _cons_dirty:     bool  = True
    _cons_conf:      float = -1.0
    _cons_val:       tuple = (None, 0, 0.0)
    # 08.10: grupy trzymane między wywołaniami; `_cons_n` = ile odczytów już
    # w grupach (-1 = przebuduj: przycięcie listy w _drain_ocr albo nowy próg)
    _cons_groups:    list  = field(default_factory=list)
    _cons_n:         int   = 0

    def _consensus(self) -> tuple:
        n = len(self.ocr_results)
        if self._cons_conf != OCR_MIN_CONF or self._cons_n < 0 or self._cons_n > n:
            self._cons_groups = []
            self._cons_n = 0
            self._cons_conf = OCR_MIN_CONF
            self._cons_dirty = True
        if self._cons_dirty or self._cons_n < n:
            for r in self.ocr_results[self._cons_n:]:
                if r[0] and r[0] != "UNKNOWN" and r[1] >= OCR_MIN_CONF:
                    _fuzzy_dodaj(self._cons_groups, r[0], r[1],
                                 (r[2] if len(r) > 2 else 1.0))
            self._cons_n = n
            self._cons_val = _fuzzy_wynik(self._cons_groups)
            self._cons_dirty = False
        return self._cons_val

    @property
    def confident_text(self) -> Optional[str]:
        """Tekst tablicy gdy fuzzy-consensus osiągnął ≥ OCR_MIN_MATCHING głosów,
        ALBO (05.10) o jeden mniej, lecz z dwoma głosami „silnymi" (łączna waga
        ≥ 1,9 = dwa odczyty z pewnością ≥ 0,98 i wycinkiem 60–120 px; pojedynczy
        taki odczyt ma 97,7 % trafień). Skraca drogę do pewności dla aut
        z naprzeciwka, które są czytelne przez 1–2 s."""
        text, count, waga = self._consensus()
        if count >= OCR_MIN_MATCHING:
            return text
        if count >= 2 and count >= OCR_MIN_MATCHING - 1 and waga >= 1.9:
            return text
        return None

    @property
    def best_guess_text(self) -> str:
        """Najlepszy dostępny odczyt (nawet poniżej progu pewności)."""
        text, _, _ = self._consensus()
        return text if text else "UNKNOWN"

    @property
    def matching_count(self) -> int:
        """Liczba głosów w zwycięskiej fuzzy-grupie OCR."""
        _, count, _ = self._consensus()
        return count


# ── OCR Worker ────────────────────────────────────────────────────────────────

class OcrWorker:
    """Wątek tła: OCR wyciętej tablicy → (track_id, text, conf) na result_queue."""

    def __init__(self) -> None:
        self.result_queue: _queue.Queue = _queue.Queue(maxsize=200)
        # Mala kolejka = brak dlugu: przy natłoku gubimy nadmiarowe zlecenia,
        # nie płynność (60 × 46 ms = 3 s zaległości głodziło Hailo → HAILO_TIMEOUT)
        self._wqueue:      _queue.Queue = _queue.Queue(maxsize=8)
        _diag_ref["ocr"] = self          # do pomiaru glebokosci kolejek
        self._stopped = threading.Event()
        self.backend = "cpu"
        self._hailo = None
        self._model = None
        self._zadany: Optional[str] = None   # 08.10: zmiana backendu z menu (cpu/hailo)
        if OCR_BACKEND in ("hailo", "auto"):
            try:
                from lpr_hailo_ocr import HailoOcrProcess
                self._hailo = HailoOcrProcess()
                self.backend = "hailo"
                print(f"[OCR] Hailo aktywny: {self._hailo.info}", flush=True)
            except Exception as e:
                logger.exception("[OCR] Hailo niedostępny, powrót do CPU: %s", e)
        if self._hailo is None:
            self._init_cpu()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="ocr-worker")
        self._thread.start()

    def _init_cpu(self):
        # ORT ograniczony do 1 rdzenia i bez spin-wait — pozostałe rdzenie
        # muszą karmić pipeline GStreamera i akcelerator Hailo
        import onnxruntime as _ort
        so = _ort.SessionOptions()
        so.intra_op_num_threads = 1
        so.inter_op_num_threads = 1
        so.execution_mode = _ort.ExecutionMode.ORT_SEQUENTIAL
        so.add_session_config_entry("session.intra_op.allow_spinning", "0")
        if OCR_MODEL.endswith(".onnx"):
            self._model = _LPR(onnx_model_path=OCR_MODEL,
                               plate_config_path=OCR_PLATE_CONFIG,
                               sess_options=so)
        else:
            self._model = _LPR(OCR_MODEL, sess_options=so)
        print(f"[OCR] fast-plate-ocr {OCR_MODEL} aktywny")
        self.backend = "cpu"

    def submit(self, track_id: int, generation: int, crop_bgr: np.ndarray,
               seq: int = -1) -> bool:
        """Non-blocking. Zwraca True jeśli przyjęto do kolejki. `seq` wraca
        z wynikiem (02.10: dopasowanie wyniku do klatki, z której był crop)."""
        if self._wqueue.full():          # sprawdź PRZED kopią — bez kopii na marne
            return False
        try:
            self._wqueue.put_nowait((track_id, generation, crop_bgr.copy(), seq))
            return True
        except _queue.Full:
            return False

    def stop(self) -> None:
        self._stopped.set()
        try:
            self._wqueue.put_nowait(None)
        except _queue.Full:
            pass
        self._thread.join(timeout=5)
        if self._hailo is not None:
            self._hailo.close()

    def ustaw_backend(self, nazwa: str) -> None:
        """Przełączenie OCR na żywo (08.10, kafel „OCR na" w Narzędziach).
        Samą zmianę robi WĄTEK OCR (`_przelacz`), bo to on używa `_hailo`
        i `_model`; tu tylko zapisujemy życzenie i budzimy pętlę."""
        self._zadany = nazwa
        try:
            self._wqueue.put_nowait(("__backend__", 0, None, -1))
        except _queue.Full:
            pass            # pętla i tak sprawdzi `_zadany` przy następnym zleceniu

    def _przelacz(self) -> None:
        nazwa, self._zadany = self._zadany, None
        if nazwa == "hailo" and self._hailo is None:
            try:
                from lpr_hailo_ocr import HailoOcrProcess
                self._hailo = HailoOcrProcess()
                self.backend = "hailo"
                print(f"[OCR] przełączono na Hailo: {self._hailo.info}", flush=True)
                _menu_banner("OCR: Hailo aktywny", "green", 5.0)
            except Exception as e:
                logger.exception("[OCR] Hailo nie wystartował, zostaje CPU: %s", e)
                if self._model is None:
                    self._init_cpu()
                self.backend = "cpu"
                _menu_banner("OCR: Hailo nie wystartował — procesor", "red", 6.0)
        elif nazwa == "cpu" and self._hailo is not None:
            try:
                self._hailo.close()
            except Exception as e:
                _zglos("ocr_hailo_close", e)
            self._hailo = None
            if self._model is None:
                self._init_cpu()
            self.backend = "cpu"
            print("[OCR] przełączono na procesor (CCT)", flush=True)
            _menu_banner("OCR: procesor aktywny", "blue", 5.0)

    def _odczyt(self, crop_rgb):
        if self._hailo is not None:
            try:
                return self._hailo.run_bgr(cv2.cvtColor(crop_rgb, cv2.COLOR_RGB2BGR))
            except Exception as e:
                logger.exception("[OCR] Awaria Hailo, powrót do CPU: %s", e)
                self._hailo.close()
                self._hailo = None
                self._init_cpu()
        preds = self._model.run(crop_rgb, return_confidence=True)
        if not preds:
            return "", 0.0
        pred = preds[0]
        text = re.sub(r'[^A-Z0-9]', '', (pred.plate or '').upper())
        cp = pred.char_probs
        return text, (float(cp.mean()) if cp is not None and len(cp) else 0.0)

    def _run_ocr(self, crop_bgr: np.ndarray):
        try:
            crop_rgb = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2RGB)
            text, conf = self._odczyt(crop_rgb)
            h, w = crop_rgb.shape[:2]
            # 05.10: tablica DWURZĘDOWA (motocykl, „zmniejszona" 305×214 mm; wycinek
            # z marginesami ma proporcję < ~2,2) — model jednowierszowy czyta z niej
            # śmieci (58 ocenionych, 2 dobre). Czytamy górny i dolny rząd osobno
            # i sklejamy; wynik z całości zostaje, gdy jest lepszy.
            if h > 0 and w / h < 2.2 and h >= 40:
                g, cg = self._odczyt(crop_rgb[: int(h * 0.56)])
                d, cd = self._odczyt(crop_rgb[int(h * 0.44):])
                if len(g) >= 2 and len(d) >= 2 and 5 <= len(g + d) <= 8:
                    c2 = (cg + cd) / 2.0
                    if (not text) or (not _is_valid_plate(text)) or c2 > conf:
                        text, conf = g + d, c2
            text = _fix_pl_plate(text)
            if text and _is_valid_plate(text):
                return text, conf
            # Odczyty o złym formacie NIE głosują — halucynacje na teksturach
            # (np. "WWWWWWW" na płocie) potrafiły przejść konsensus
            return "UNKNOWN", 0.0
        except Exception as e:
            logger.debug("OCR error: %s", e)
            return "UNKNOWN", 0.0

    def _loop(self) -> None:
        # OCR lekko ustępuje potokowi (detekcji na Hailo). Zator potoku to
        # utrata WSZYSTKICH klatek na 10 s (limit HailoRT), a opóźniony OCR
        # to tylko chwilę późniejszy odczyt tej samej tablicy.
        _ustap_procesor(3)
        while not self._stopped.is_set():
            item = self._wqueue.get()
            if item is None:
                break
            if self._zadany:
                self._przelacz()
            if item[0] == "__backend__":        # samo obudzenie pętli
                continue
            track_id, generation, crop_bgr, seq = item
            text, conf = self._run_ocr(crop_bgr)
            # Gdy kolejka pełna: blokuj chwilę zamiast cicho gubić wynik.
            # Gdyby wynik przepadł, pending_ocr nigdy nie zostałby zdekremntowany
            # i track utknąłby w stanie "oczekiwania" na zawsze.
            try:
                self.result_queue.put((track_id, generation, text, conf, seq, crop_bgr),
                                      timeout=1.0)
            except _queue.Full:
                logger.warning("[OCR] result_queue pełna — wynik track %d utracony", track_id)


# ── Save Worker ───────────────────────────────────────────────────────────────

class SaveWorker:
    """Wątek tła: zapis JPG (pełna klatka + crop) + SQLite."""

    def __init__(self, save_dir: Path, db_path: Path) -> None:
        self.save_dir = save_dir
        self._db_path = str(db_path)
        self._setup_db()
        self._wqueue:  _queue.Queue = _queue.Queue(maxsize=30)
        _diag_ref["zapis"] = self        # do pomiaru glebokosci kolejek
        self._stopped = threading.Event()
        self._thread  = threading.Thread(target=self._loop, daemon=True, name="save-worker")
        self._thread.start()

    def _setup_db(self) -> None:
        con = sqlite3.connect(self._db_path, timeout=10)
        con.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=NORMAL;
            CREATE TABLE IF NOT EXISTS plates (
                plate_text  TEXT PRIMARY KEY,
                first_seen  TEXT NOT NULL,
                last_seen   TEXT NOT NULL,
                total_count INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS sightings (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                plate_text     TEXT NOT NULL,
                seen_at        TEXT NOT NULL,
                full_image     TEXT,
                crop_image     TEXT,
                det_confidence REAL,
                ocr_confidence REAL,
                ocr_reads      INTEGER,
                track_duration REAL
            );
        """)
        try:    # pozycja tablicy na pełnej klatce (x1;y1;x2;y2 znormalizowane)
            con.execute("ALTER TABLE sightings ADD COLUMN bbox TEXT")
        except sqlite3.OperationalError:
            pass   # kolumna już istnieje
        try:    # klatki-kandydaci wokół najlepszego ujęcia (nazwy po średniku)
            con.execute("ALTER TABLE sightings ADD COLUMN extra_crops TEXT")
        except sqlite3.OperationalError:
            pass
        # Weryfikacja odczytów przez człowieka (schemat zgodny ze starym lpr_review):
        # is_correct: 1=poprawny, 0=poprawiony (correct_text), -1=to nie tablica
        con.execute("""
            CREATE TABLE IF NOT EXISTS annotations (
                sighting_id  INTEGER PRIMARY KEY,
                is_correct   INTEGER,
                correct_text TEXT,
                reviewed_at  TEXT,
                notes        TEXT
            )
        """)
        try:    # recznie poprawiona ramka tablicy (x1;y1;x2;y2 znormalizowane)
            con.execute("ALTER TABLE annotations ADD COLUMN correct_bbox TEXT")
        except sqlite3.OperationalError:
            pass
        try:    # klatka wskazana przez czlowieka jako LEPSZA niz zapisana
            # (czasem sasiednia jest ostrzejsza). Do niej ma trafic uczenie.
            con.execute("ALTER TABLE annotations ADD COLUMN lepsza_klatka TEXT")
        except sqlite3.OperationalError:
            pass
        con.commit()
        con.close()

    def submit(self, *, plate_text: str, full_rgb: np.ndarray, crop_bgr: np.ndarray,
               seen_at: datetime, det_conf: float, ocr_conf: float,
               ocr_reads: int, track_duration: float, bbox: tuple = (),
               extra_crops: list = (), extra_rgb: bool = False) -> None:
        """`full_rgb` = pełna klatka w RGB (tak trzyma ją potok od 24.09);
        konwersja do BGR robi tu to, co dawniej robiła kopia 9 MB — koszt
        ten sam (jedno przejście), a pełnoklatkowe cvtColor na każdą klatkę
        zniknęło z callbacku. `crop_bgr` i `extra_crops` to wycinki BGR;
        `extra_rgb=True` (ręczne FOTO) = klatki pełne w RGB do konwersji."""
        if not _usb_ready.is_set():
            # pendrive wyjęty albo przygotowany do wyjęcia — NIE piszemy
            print(f"[Zapis] pendrive niedostępny — pomijam {plate_text}")
            return
        try:
            self._wqueue.put_nowait((
                plate_text, cv2.cvtColor(full_rgb, cv2.COLOR_RGB2BGR),
                crop_bgr.copy(),
                seen_at, det_conf, ocr_conf, ocr_reads, track_duration, bbox,
                [(s, cv2.cvtColor(c, cv2.COLOR_RGB2BGR) if extra_rgb else c.copy())
                 for s, c in extra_crops],
            ))
        except _queue.Full:
            print(f"[Zapis] UWAGA: kolejka pełna — pomijam {plate_text}")

    def stop(self) -> None:
        self._stopped.set()
        try:
            self._wqueue.put_nowait(None)
        except _queue.Full:
            pass
        self._thread.join(timeout=10)

    def _loop(self) -> None:
        _ustap_procesor(5)       # zapis JPEG ustępuje rozpoznawaniu
        while not self._stopped.is_set():
            item = self._wqueue.get()
            if item is None:
                break
            try:
                self._process(*item)
            except Exception as e:
                print(f"[Zapis] błąd: {e}")

    def _process(self, plate_text, full_bgr, crop_bgr, seen_at,
                 det_conf, ocr_conf, ocr_reads, track_duration, bbox=(),
                 extra_crops=()) -> None:
        ts        = seen_at.strftime("%Y%m%d_%H%M%S")
        now_str   = seen_at.isoformat(sep=" ", timespec="seconds")
        full_name = f"{plate_text}_{ts}.jpg"
        crop_name = f"{plate_text}_{ts}_crop.jpg"

        # Folder dzienny: lpr/2026-06-24/
        day_dir = self.save_dir / seen_at.strftime("%Y-%m-%d")
        day_dir.mkdir(exist_ok=True)

        # B1: sprawdź wolne miejsce zanim zaczniesz pisać (FAT32, brak wyjątków przy pełnym dysku)
        try:
            free_mb = os.statvfs(str(self.save_dir)).f_bavail * os.statvfs(str(self.save_dir)).f_frsize / 1_048_576
            if free_mb < 100:
                print(f"[Zapis] STOP: za mało miejsca na dysku ({free_mb:.0f} MB) — pominięto {full_name}")
                return
        except OSError:
            pass

        ok1 = safe_write_image(day_dir / full_name, full_bgr)
        ok2 = safe_write_image(day_dir / crop_name, crop_bgr)
        if not ok1:
            print(f"[Zapis] BŁĄD: nie udało się zapisać {full_name} — sprawdź pendrive")
            return

        # klatki-kandydaci wokół najlepszego ujęcia (suffix = pozycja wzgl. najlepszej)
        extra_names = []
        for suffix, ec in (extra_crops or ()):
            name = f"{plate_text}_{ts}_{suffix}.jpg"
            if safe_write_image(day_dir / name, ec):
                extra_names.append(name)

        con = sqlite3.connect(self._db_path)
        con.execute("""
            INSERT INTO plates (plate_text, first_seen, last_seen, total_count)
            VALUES (?,?,?,1)
            ON CONFLICT(plate_text) DO UPDATE SET
                last_seen   = excluded.last_seen,
                total_count = total_count + 1
        """, (plate_text, now_str, now_str))
        bbox_str = (";".join("%.4f" % v for v in bbox) if bbox else None)
        con.execute("""
            INSERT INTO sightings
                (plate_text, seen_at, full_image, crop_image,
                 det_confidence, ocr_confidence, ocr_reads, track_duration,
                 bbox, extra_crops)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (plate_text, now_str, full_name, crop_name,
              det_conf, ocr_conf, ocr_reads, round(track_duration, 1), bbox_str,
              ";".join(extra_names) if extra_names else None))
        con.commit()
        con.close()

        if plate_text == "UNKNOWN":
            info = "brak pewnego OCR"
        else:
            info = f"OCR: {ocr_reads}× zgodnie"
        print(f"[LPR]  {full_name}  ({info}, track {track_duration:.1f}s)")


# ── TrackManager ──────────────────────────────────────────────────────────────

class TrackManager:
    """Zarządza cyklem życia tracków tablic."""

    def __init__(self, ocr: OcrWorker, save: SaveWorker) -> None:
        self._ocr         = ocr
        self._save        = save
        self._tracks:     dict[int, PlateTrack] = {}
        self._recent:     dict[str, float] = {}  # plate_text → mono czas ostatniego zapisu
        self._generations: dict[int, int]  = {}  # tid → bieżąca generacja (zabezpiecza przed recyclingiem track_id)
        # Dane dla HUD
        self.last_confirmed:   tuple = ("", 0.0)  # (plate_text, mono_time)
        self.total_saved:      int   = 0
        self._last_unknown_save: float = 0.0     # A9: rate-limit dla zapisów UNKNOWN
        self.notifications:    list  = []        # dymki: {"crop", "text", "t0"}

    def update(self, detections: list, frame_bgr: np.ndarray,
               w: int, h: int, frame_count: int, now: float) -> None:
        """
        Wywołać raz na klatkę.
        detections: [(track_id, label, conf, xmin, ymin, xmax, ymax)]
        """
        # Wciągnij wyniki OCR z kolejki
        self._drain_ocr()

        # Przetwórz aktywne detekcje tablic
        active_ids: set = set()
        for tid, label, conf, xmin, ymin, xmax, ymax in detections:
            if label != "license_plate" or conf < PLATE_MIN_CONF or tid < 0:
                continue

            # ROI masking — ignoruj detekcje poza strefą (niebo, deska, boki)
            cx = (xmin + xmax) / 2
            cy = (ymin + ymax) / 2
            with _zone_lock:
                zone_snap = list(DETECT_ZONE)
            if not _point_in_zone(cx, cy, zone_snap):
                continue

            active_ids.add(tid)

            if tid not in self._tracks:
                gen = self._generations.get(tid, 0) + 1
                self._generations[tid] = gen
                self._tracks[tid] = PlateTrack(
                    track_id=tid,
                    generation=gen,
                    first_seen_dt=datetime.now(),
                    first_seen_mono=now,
                    last_seen_mono=now,
                )
            track = self._tracks[tid]
            track.last_seen_mono = now
            track.bbox = (xmin, ymin, xmax, ymax)
            track.szer_hist.append((now, xmax - xmin))

            # Tablica ucięta krawędzią kadru? Nie czytaj jej i nie bierz jako
            # najlepszego ujęcia — częściowy tekst zatruwa konsensus, a zdjęcie
            # jest bezużyteczne. Track żyje dalej i łapie pełne ujęcie, gdy auto
            # w całości wjedzie w kadr; jeśli nigdy nie wjedzie — brak zapisu.
            at_edge = (xmin <= 0.005 or ymin <= 0.005
                       or xmax >= 0.995 or ymax >= 0.995)
            if at_edge:
                continue

            # Aktualizuj najlepszą klatkę (największy bbox × najwyższa pewność).
            # Próg +10%: przy zbliżającym się aucie score rośnie co klatkę,
            # a każda aktualizacja to kopia 6 MB (1080p) — 30×/s dusiło CPU
            area  = (xmax - xmin) * (ymax - ymin)
            score = area * conf
            if score > track.best_score * 1.10:
                track.best_score     = score
                track.best_conf      = conf
                # BEZ .copy() — callback rysuje nakładkę na KOPII (patrz
                # app_callback tuż za update()), więc ta klatka pozostaje
                # czysta i można ją trzymać przez referencję. Kopiowanie 9 MB
                # OSOBNO DLA KAŻDEJ tablicy dławiło callback: przy kilku
                # autach naraz to kilkadziesiąt MB na jedną klatkę, przy
                # budżecie 33 ms. Stąd „ekran łapie laga, gdy pojawia się
                # rejestracja" (jazda 04.08: zaległość rosła do 1089 klatek).
                track.best_frame_bgr = frame_bgr
                track.best_crop_bgr  = self._crop(frame_bgr, xmin, ymin, xmax, ymax, w, h)
                track.best_bbox      = (xmin, ymin, xmax, ymax)

            # Bufor kandydatów co 4 klatki (12 × 4/30 fps ≈ 1.6 s okna) —
            # przy zapisie wybierzemy klatki wokół najlepszej
            if frame_count % 4 == 0:
                cand = self._crop(frame_bgr, xmin, ymin, xmax, ymax, w, h)
                track.recent_crops.append((now, cand, score))
                # najostrzejsze CZYTELNE ujęcie do oceny (min. rozmiar tablicy,
                # żeby nie wybrać ostrego-ale-dalekiego, nieczytelnego cropu)
                if (ymax - ymin) * h >= OCR_MIN_PLATE_PX:
                    r = _readability(cand)
                    if r > track.sharp_score:
                        track.sharp_score     = r
                        track.sharp_crop_bgr  = cand
                        track.sharp_frame_bgr = frame_bgr   # bez .copy() — jw.
                        track.sharp_bbox      = (xmin, ymin, xmax, ymax)

            # LOGIKA CZYTANIA: dopóki tablica NIE jest rozpoznana — czytamy AŻ
            # SIĘ UDA (żadnych twardych limitów). Tempo (05.10 wg pomiaru
            # trafności pojedynczego odczytu, patrz _waga_odczytu):
            #  - STREFA CZYTELNOŚCI (wycinek 60–120 px, tj. tablica ~45–90 px):
            #    co OCR_INTERVAL_BLISKO (3) — tu odczyty są najpewniejsze, a auto
            #    mijane/mijające jest w niej 1–3 s; to one są najważniejsze
            #  - młody track (<1.5 s): co 4 klatki — pojazd z naprzeciwka ma
            #    okno czytelności ~1-2 s, trzeba łapać każdą szansę
            #  - normalnie: co OCR_INTERVAL_FRAMES (8)
            #  - trudna tablica (25+ pełnych odczytów wciąż bez konsensusu):
            #    co 30 klatek — NIE przestajemy, tylko oszczędzamy CPU;
            #    światło/kąt się zmienia i kolejna próba może w końcu trafić
            # Po konsensusie (announced) — DOGRYWKA: jeśli pewność dały głosy
            # z daleka (mało odczytów silnych), a tablica właśnie jest w strefie
            # czytelności, czytamy dalej (co 4 klatki, max OCR_DOGRYWKA_MAX);
            # ważony konsensus może wtedy poprawić tekst przed zapisem.
            plate_px  = (ymax - ymin) * h
            crop_px   = plate_px * (1.0 + 2.0 * CROP_MARGIN_H)   # wysokość wycinka jak w pomiarze
            w_strefie = 60.0 <= crop_px < 120.0
            n_pelnych = sum(1 for r in track.ocr_results if r[0] != "UNKNOWN")
            hailo = self._ocr.backend == "hailo"
            if track.announced:
                czytaj = (w_strefie and track.ocr_silne < 3
                          and track.ocr_dogrywka < (OCR_DOGRYWKA_MAX_HAILO if hailo
                                                    else OCR_DOGRYWKA_MAX)
                          and frame_count % (1 if hailo else 4) == 0)
            else:
                trudna = n_pelnych >= (45 if hailo else 25)
                mlody = now - track.first_seen_mono < 1.5
                if hailo:
                    # 08.10: OCR na Hailo (~9 ms, poza procesorem): w strefie
                    # czytelności (auto mijane) i dla młodego tracku (auto
                    # z naprzeciwka) KAŻDA klatka, poza nią co 2, trudna co 8.
                    # Kolejka OCR (8) sama ogranicza przy kilku tablicach naraz.
                    interval = 8 if trudna else (1 if (w_strefie or mlody) else 2)
                elif w_strefie and not trudna:
                    interval = OCR_INTERVAL_BLISKO
                elif mlody:
                    interval = 4
                elif not trudna:
                    interval = OCR_INTERVAL_FRAMES
                else:
                    interval = 30
                czytaj = frame_count % interval == 0 and plate_px >= OCR_MIN_PLATE_PX
            if czytaj:
                crop = self._crop(frame_bgr, xmin, ymin, xmax, ymax, w, h)
                track.ocr_seq += 1
                if self._ocr.submit(tid, track.generation, crop, track.ocr_seq):
                    track.pending_ocr += 1
                    if track.announced:
                        track.ocr_dogrywka += 1
                    track.ocr_pending[track.ocr_seq] = (frame_bgr, (xmin, ymin, xmax, ymax))
                    while len(track.ocr_pending) > 8:       # zgubione wyniki → nie trzymaj klatek
                        track.ocr_pending.pop(next(iter(track.ocr_pending)))

        # Dymki: ogłoś tablice, które właśnie osiągnęły pewny odczyt
        # (05.10: albo którym dogrywka z bliska POPRAWIŁA tekst — jedna korekta)
        for track in self._tracks.values():
            if track.finalized or track.best_crop_bgr is None:
                continue
            ct = track.confident_text
            if not ct:
                continue
            if track.announced:
                if ct == track.announced_text or track.reannounced:
                    continue
                track.reannounced = True
                logger.info("dogrywka poprawiła odczyt: %s → %s (silnych %d)",
                            track.announced_text, ct, track.ocr_silne)
            track.announced = True
            track.announced_text = ct
            # Watchlista: alert (ekran+głos); opcjonalnie czytaj każdą tablicę
            trafiona, pewne = _watch_dopasuj(ct)
            if trafiona:
                # wytnij POJAZD (nie samą tablicę) do pokazania na ekranie
                _alert_trigger(ct, _vehicle_crop(
                    frame_bgr, detections,
                    track.best_bbox or track.bbox, w, h),
                    pewne=pewne, z_bazy=trafiona)
            elif TTS_ALL and _mowa_dozwolona(ct):
                SPEECH.say(f"Tablica {_spell_plate(ct)}.", "plate")
            for n in self.notifications:      # ta sama tablica → odśwież zamiast dublować
                if n["text"] == ct:
                    n["t0"] = now
                    n["crop"] = track.best_crop_bgr.copy()
                    break
            else:
                self.notifications.append(
                    {"crop": track.best_crop_bgr.copy(), "text": ct, "t0": now})
                del self.notifications[:-NOTIF_MAX]
        # sprzątnij wygasłe dymki
        self.notifications = [n for n in self.notifications
                              if now - n["t0"] <= NOTIF_TTL]

        # Obsłuż martwe tracki (nie widziane przez TRACK_DEAD_SEC)
        for tid, track in list(self._tracks.items()):
            if tid in active_ids:
                continue
            time_dead = now - track.last_seen_mono
            if time_dead < TRACK_DEAD_SEC:
                continue
            # OCR ostatniej szansy: najlepszy (największy) crop często czyta się
            # perfekcyjnie, choć wcześniejsze małe cropy dały UNKNOWN — a to
            # właśnie best_crop trafia na dysk. Bez tego zdarzały się zapisy
            # UNKNOWN z doskonale czytelną tablicą na zdjęciu.
            if (not track.final_ocr_sent and not track.finalized
                    and track.confident_text is None
                    and track.best_crop_bgr is not None):
                track.final_ocr_sent = True
                # Nie tylko best_crop: dorzuć 2 NAJOSTRZEJSZE klatki z bufora
                # (wariancja Laplace'a). Pojazdy z naprzeciwka często kończyły
                # jako UNKNOWN, bo żaden pojedynczy odczyt nie przebił progu —
                # 3 strzały w najlepsze klatki znacznie zwiększają szansę.
                cands = [track.best_crop_bgr]
                try:
                    extra_c = sorted(
                        (c for _, c, _ in track.recent_crops
                         if c is not None and c.size),
                        key=lambda im: -cv2.Laplacian(
                            cv2.cvtColor(im, cv2.COLOR_BGR2GRAY),
                            cv2.CV_64F).var())[:2]
                    cands.extend(extra_c)
                except Exception as e:
                    # gubimy tu KLATKI-DOWODY, więc musi być widoczne
                    _zglos("wybor_kandydatow", e)
                for c in cands:
                    if self._ocr.submit(tid, track.generation, c):
                        track.pending_ocr += 1
            # Finalizuj gdy OCR gotowy lub minął timeout oczekiwania
            if track.pending_ocr <= 0 or time_dead > TRACK_DEAD_SEC + TRACK_OCR_TIMEOUT:
                if not track.finalized:
                    self._finalize(track, now)
                del self._tracks[tid]
                # B4: wyczyść generację — track_id nie będzie ponownie użyty przez długi czas
                self._generations.pop(tid, None)

        # Tracki żyjące dłużej niż TRACK_MAX_ALIVE_SEC (długi postój / jazda
        # za tym samym pojazdem):
        #  - PEWNIE rozpoznane: zapisz RAZ i zostaw track w spokoju — tablica
        #    cały czas w kadrze NIE jest "wykrywana od nowa" (bez resetu nie
        #    ma ponownego OCR, drugiego dymka ani migania napisu na ekranie);
        #    finalized=True wyłącza go z dalszych zapisów, a przy zniknięciu
        #    z kadru zostanie po prostu usunięty
        #  - NIEROZPOZNANE: nic nie robimy — czytamy aż się uda (tempo
        #    kontroluje interval wyżej), głosy się kumulują (ocr_results
        #    trzyma ostatnie 50, więc stare błędne wykruszają się same),
        #    a zapis (UNKNOWN) nastąpi dopiero gdy pojazd zniknie z kadru.
        #    Dawny reset co 120 s kasował zebrane głosy i produkował
        #    śmieciowe wpisy UNKNOWN mimo trwającego śledzenia.
        for tid, track in list(self._tracks.items()):
            if tid not in active_ids or track.finalized:
                continue
            if (now - track.first_seen_mono > TRACK_MAX_ALIVE_SEC
                    and track.confident_text and track.pending_ocr <= 0):
                self._finalize(track, now)    # zapis od razu; track zostaje

        # Czyszczenie dedup cache
        self._recent = {k: t for k, t in self._recent.items()
                        if now - t < DEDUP_WINDOW_SEC}

    def _drain_ocr(self) -> None:
        try:
            while True:
                tid, generation, text, conf, seq, crop_bgr = self._ocr.result_queue.get_nowait()
                track = self._tracks.get(tid)
                # Odrzuć wyniki ze starych generacji (recycling track_id przez ByteTracker)
                if track and track.generation == generation:
                    meta = track.ocr_pending.pop(seq, None)
                    if (meta is not None and text != "UNKNOWN"
                            and conf > track.ocr_best_conf):
                        track.ocr_best_conf, track.ocr_best_text = conf, text
                        track.ocr_frame_bgr, track.ocr_bbox = meta
                        track.ocr_crop_bgr = crop_bgr
                    # waga głosu wg wysokości WYCINKA (z marginesami, jak w pomiarze)
                    if crop_bgr is not None and getattr(crop_bgr, "shape", None):
                        crop_px = float(crop_bgr.shape[0])
                    elif meta is not None:
                        crop_px = (meta[1][3] - meta[1][1]) * meta[0].shape[0] * (1.0 + 2.0 * CROP_MARGIN_H)
                    else:
                        crop_px = 60.0
                    waga = _waga_odczytu(crop_px, conf) if text != "UNKNOWN" else 0.0
                    if waga >= 0.99:
                        track.ocr_silne += 1
                    track.ocr_results.append((text, conf, waga))
                    track._cons_dirty = True        # konsensus do przeliczenia
                    # B4: ogranicz pamięć — przy 120s postoju bez limitu = 720 wyników
                    # 08.10: przycinamy blokami (80 → 50), nie po każdym odczycie —
                    # przycięcie wymusza przebudowę grup konsensusu (_cons_n = -1)
                    if len(track.ocr_results) > 80:
                        track.ocr_results = track.ocr_results[-50:]
                        track._cons_n = -1
                    track.pending_ocr = max(0, track.pending_ocr - 1)
        except _queue.Empty:
            pass

    def _crop(self, frame_bgr, xmin, ymin, xmax, ymax, w, h) -> np.ndarray:
        bw = xmax - xmin
        bh = ymax - ymin
        x1 = max(0, int((xmin - bw * CROP_MARGIN_W) * w))
        y1 = max(0, int((ymin - bh * CROP_MARGIN_H) * h))
        x2 = min(w, int((xmax + bw * CROP_MARGIN_W) * w))
        y2 = min(h, int((ymax + bh * CROP_MARGIN_H) * h))
        # Minimalna wysokość 30 px — CRNN nie radzi sobie z mniejszymi cropami
        if y2 - y1 < 30:
            cy = (y1 + y2) // 2
            y1 = max(0, cy - 15)
            y2 = min(h, cy + 15)
        if x2 <= x1 or y2 <= y1:
            return np.zeros((30, 100, 3), dtype=np.uint8)
        # pełna klatka jest RGB (bez konwersji, patrz _klatka_rgb_z_bufora);
        # cvtColor na wycinku = kopia + BGR w jednym — wszystko dalej (OCR,
        # zapis, dymki, ocena ostrości) dostaje BGR jak dotąd
        return cv2.cvtColor(frame_bgr[y1:y2, x1:x2], cv2.COLOR_RGB2BGR)

    def _finalize(self, track: PlateTrack, now: float) -> None:
        track.finalized = True
        track.ocr_pending.clear()
        if track.best_frame_bgr is None:
            return

        text     = track.confident_text or track.best_guess_text
        duration = now - track.first_seen_mono

        # Dedup: ta sama (rozpoznana) tablica w ciągu DEDUP_WINDOW_SEC → pomiń
        if text != "UNKNOWN":
            last = self._recent.get(text)
            if last and (now - last) < DEDUP_WINDOW_SEC:
                logger.debug("dedup: %s widziany %ds temu — pomijam", text, int(now - last))
                return
            self._recent[text] = now
        else:
            # A9: rate-limit UNKNOWN — max 1 zapis na minutę
            # Bez tego postój przy billboardzie = dziesiątki plików UNKNOWN_*.jpg
            if now - self._last_unknown_save < 60.0:
                return
            self._last_unknown_save = now

        valid_confs = [r[1] for r in track.ocr_results if r[0] == text]
        avg_ocr = float(np.mean(valid_confs)) if valid_confs else 0.0

        # Do OCENY pokazujemy NAJOSTRZEJSZĄ czytelną klatkę (crop + pełny kadr
        # + bbox z tego samego ujęcia — spójne w lightboxie). Fallback: best.
        if track.sharp_crop_bgr is not None:
            review_crop  = track.sharp_crop_bgr
            review_frame = track.sharp_frame_bgr
            review_bbox  = track.sharp_bbox
        else:
            review_crop  = track.best_crop_bgr
            review_frame = track.best_frame_bgr
            review_bbox  = track.best_bbox
        crop = review_crop if review_crop is not None else review_frame

        # Klatki-kandydaci wokół najostrzejszego ujęcia (±2) z oznaczeniem
        # pozycji: km2/km1 = wcześniejsze, kp1/kp2 = późniejsze (edytor → kolumny)
        extra = []
        rc = list(track.recent_crops)
        if rc:
            bi = max(range(len(rc)), key=lambda i: _readability(rc[i][1]))
            for off in (-2, -1, 1, 2):
                j = bi + off
                if 0 <= j < len(rc):
                    suffix = ("km%d" % -off) if off < 0 else ("kp%d" % off)
                    extra.append((suffix, rc[j][1]))

        # Tryb RADAR (25.09): zakończony track jest kandydatem dla czekającego
        # zdarzenia prędkości; zwykłe przejazdy zapisujemy tylko na życzenie.
        if RADAR_CFG["tryb"]:
            v_kam = _kamera_predkosc(track.szer_hist)
            # para do kalibracji K: prędkość z kamery vs maksimum radaru z 3 s
            try:
                tw = time.time()
                vr = max((abs(v) for t, v in list(_radar_v_hist) if tw - t <= 3.0),
                         default=0.0)
                _radar_csv("kalibracja", f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')};"
                           f"{text};{track.track_id};"
                           f"{'%.1f' % v_kam if v_kam is not None else ''};{vr:.1f};"
                           f"{RADAR_CFG['prog_kmh']:.0f};1\n")
                print(f"[Radar] track {track.track_id} {text}: kamera "
                      f"{'%.0f' % v_kam if v_kam is not None else '-'} km/h, "
                      f"radar (3 s) {vr:.0f} km/h", flush=True)
            except Exception as e:
                _zglos("radar_kalibracja", e)
            # zdjęcie zdarzenia: klatka, z której OCR odczytał KOŃCOWY numer
            # z największą pewnością (02.10); gdy brak — najostrzejsza / best
            if (track.ocr_frame_bgr is not None and text != "UNKNOWN"
                    and track.ocr_best_text == text):
                r_frame, r_bbox, r_crop = track.ocr_frame_bgr, track.ocr_bbox, track.ocr_crop_bgr
                skad = f"klatka OCR (pewność {track.ocr_best_conf:.2f})"
            else:
                r_frame = review_frame if review_frame is not None else track.best_frame_bgr
                r_bbox = review_bbox if review_bbox else track.best_bbox
                r_crop, skad = crop, "najostrzejsza/best"
            print(f"[Radar] zdjęcie kandydata {text}: {skad}", flush=True)
            track.ocr_pending.clear()
            _radar_track_zakonczony(text, r_frame, r_bbox, r_crop, avg_ocr,
                                    tid=track.track_id, v_kam=v_kam)
            if not RADAR_CFG["zapis_zwykly"]:
                return

        self._save.submit(
            plate_text=text,
            full_rgb=review_frame if review_frame is not None
                     else track.best_frame_bgr,     # pełne klatki są RGB (24.09)
            crop_bgr=crop,
            seen_at=track.first_seen_dt,
            det_conf=track.best_conf,
            ocr_conf=avg_ocr,
            ocr_reads=(track.matching_count if track.confident_text
                       else len(track.ocr_results)),
            track_duration=duration,
            bbox=review_bbox if review_bbox else track.best_bbox,
            extra_crops=extra,
        )
        self.total_saved += 1
        if text != "UNKNOWN":
            self.last_confirmed = (text, track.last_seen_mono)

    def flush(self) -> None:
        """Finalizuj wszystkie aktywne tracki przy zamknięciu programu."""
        now = time.monotonic()
        for track in list(self._tracks.values()):
            if not track.finalized and track.best_frame_bgr is not None:
                self._finalize(track, now)

    @property
    def tracks(self) -> dict:
        return self._tracks


# ── Rysowanie nakładki ────────────────────────────────────────────────────────

def _draw_corners(img, x1, y1, x2, y2, color, thick=2, ratio=0.28):
    """Narożniki (L-kształty) zamiast pełnego prostokąta — styl kamer ANPR."""
    lx = max(8, int((x2 - x1) * ratio))
    ly = max(8, int((y2 - y1) * ratio))
    segs = [
        [(x1, y1 + ly), (x1, y1), (x1 + lx, y1)],
        [(x2 - lx, y1), (x2, y1), (x2, y1 + ly)],
        [(x1, y2 - ly), (x1, y2), (x1 + lx, y2)],
        [(x2 - lx, y2), (x2, y2), (x2, y2 - ly)],
    ]
    for pts in segs:
        cv2.polylines(img, [np.array(pts)], False, color, thick, cv2.LINE_AA)


def _label_box(img, text, x1, y1, color, font_scale=0.58, font_thick=1):
    """
    Etykieta: biały tekst na półprzezroczystym grafitowym panelu, kolor stanu
    tylko jako cienka ramka. UWAGA: getTextSize zwraca ((w,h), baseline) —
    wcześniejsze błędne rozpakowanie brało baseline za wysokość i tekst
    wystawał poza tło.
    """
    font = cv2.FONT_HERSHEY_DUPLEX
    (tw, th), base = cv2.getTextSize(text, font, font_scale, font_thick)
    pad   = 6
    box_h = th + base + pad * 2
    bx1 = max(0, x1)
    by2 = max(box_h, y1 - 4)          # 4 px odstępu nad ramką tablicy
    by1 = by2 - box_h
    bx2 = min(img.shape[1] - 1, bx1 + tw + pad * 2)
    if bx2 <= bx1:
        return
    sub = img[by1:by2, bx1:bx2]
    panel = np.full_like(sub, (26, 24, 22))
    cv2.addWeighted(panel, 0.82, sub, 0.18, 0, sub)
    cv2.rectangle(img, (bx1, by1), (bx2, by2), color, 1)
    cv2.putText(img, text, (bx1 + pad, by2 - pad - base + 2),
                font, font_scale, (245, 245, 245), font_thick, cv2.LINE_AA)


_hud_strip_cache: Optional[np.ndarray] = None
_hud_strip_key:   tuple = ()

def _draw_hud(frame, w, h, track_mgr, now):
    """HUD z cache — PIL renderowany tylko gdy treść się zmienia (~1×/s zamiast 30×/s)."""
    _draw_hud_migawka(frame, w, h, _hud_migawka(track_mgr, now), now)


def _draw_hud_migawka(frame, w, h, migawka: tuple, now):
    """Jak `_draw_hud`, ale z danych zebranych wcześniej (`_hud_migawka`) —
    wątek nakładki (24.09) nie zagląda do słownika tracków, który w tym
    czasie zmienia wątek potoku."""
    global _hud_strip_cache, _hud_strip_key

    # Ten pasek rysuje się na PEŁNEJ klatce (np. 2304 px), a ekran ma 800 px —
    # po przeskalowaniu tekst kurczył się ~3× i był nieczytelny. Skalujemy go
    # więc proporcjonalnie do szerokości klatki, żeby na ekranie wyglądał tak
    # samo jak górny pasek (ten rysowany już po zmniejszeniu).
    skala = max(1.0, w / 800.0)
    bar_h = int(40 * skala)
    pad   = int(12 * skala)
    fsize = int(17 * skala)
    ty    = (bar_h - fsize) // 2 - 2

    time_str = datetime.now().strftime("%H:%M:%S")
    active, saved, (last_text, last_time) = migawka

    brightness = 0
    if last_text and (now - last_time) < 30.0:
        elapsed    = now - last_time
        brightness = int(255 * max(0.4, 1.0 - max(0.0, elapsed - 20.0) / 10.0))

    # ostrzeżenie o zasilaniu (02.10) zastępuje „Ostatnia:" — ważniejsze
    zas = globals().get("_stat_zasilanie") or {}
    ostrz = ("SPADEK NAPIĘCIA" if zas.get("spadek")
             else ("CPU DŁAWIONE" if zas.get("dlawienie") else ""))
    cache_key = (time_str, active, last_text, brightness // 16, saved, w, ostrz)

    if cache_key != _hud_strip_key:
        _hud_strip_key = cache_key
        strip = np.full((bar_h, w, 3), (20, 20, 20), dtype=np.uint8)
        texts: list[tuple] = [
            (time_str,               (pad, ty),        (160, 160, 160), fsize),
            (f"Sledzenie: {active}", (pad + int(110 * skala), ty),
                                                       (140, 140, 140), fsize),
        ]
        if ostrz:
            tw, _ = _text_size(ostrz, fsize)
            texts.append((ostrz, (w // 2 - tw // 2, ty), (0, 0, 255), fsize))
        elif last_text and brightness:
            col   = (0, brightness, int(brightness * 0.31))
            label = f"Ostatnia:  {last_text}"
            tw, _ = _text_size(label, fsize)
            texts.append((label, (w // 2 - tw // 2, ty), col, fsize))
        saved_str = f"Zapisane: {saved}"
        sw2, _ = _text_size(saved_str, fsize)
        texts.append((saved_str, (w - sw2 - pad, ty), (160, 160, 160), fsize))
        _put_text_batch(strip, texts)
        cv2.line(strip, (0, 0), (w, 0), (50, 50, 50), 1)
        _hud_strip_cache = strip

    # Wklej gotowy strip — zero PIL, tylko memcpy (~0.1ms)
    frame[h - bar_h:h, :] = _hud_strip_cache


NOTIF_TTL = 6.0   # ile sekund żyje dymek powiadomienia
NOTIF_MAX = 3     # maksymalna liczba dymków naraz
NOTIF_IMG_H = 155  # wysokość zdjęcia tablicy na karcie (px) przy skali 1.0
NOTIF_FONT  = 2.1  # skala czcionki numeru na karcie przy skali 1.0
NOTIF_SCALE = 1.0  # mnożnik wielkości dymków (0.5–2.0), regulowany w menu

# Podgląd przy regulacji: przykładowy dymek rysowany NA WIERZCHU, żeby od razu
# było widać ustawianą wielkość (panel „Obraz" zajmuje tylko dolny pasek).
_notif_podglad_do = 0.0
_notif_demo_crop = None


def _notif_demo() -> np.ndarray:
    """Sztuczne zdjęcie tablicy do podglądu wielkości dymka (raz, w cache)."""
    global _notif_demo_crop
    if _notif_demo_crop is None:
        img = np.full((104, 470, 3), (238, 238, 238), np.uint8)
        cv2.rectangle(img, (0, 0), (469, 103), (70, 70, 70), 3)
        cv2.rectangle(img, (3, 3), (58, 100), (130, 50, 20), -1)   # pasek UE
        cv2.putText(img, "PL", (12, 88), cv2.FONT_HERSHEY_SIMPLEX, 0.7,
                    (255, 255, 255), 2, cv2.LINE_AA)
        cv2.putText(img, "WX 1234A", (76, 80), cv2.FONT_HERSHEY_DUPLEX, 1.9,
                    (30, 30, 30), 4, cv2.LINE_AA)
        _notif_demo_crop = img
    return _notif_demo_crop


def _draw_notifications(frame, w, notifs, now):
    """
    Karty powiadomień (prawy górny róg, pod paskiem statystyk): duże zdjęcie
    tablicy + numer białą czcionką na grafitowej karcie, zielony pasek-akcent
    z boku. Najnowsza na górze, znika z wygaszeniem po NOTIF_TTL sekundach.
    """
    y = 56
    # Od 05.08 nakładka rysuje się na klatce W ROZMIARZE EKRANU (800×480),
    # a nie na pełnych 2304×1296 zmniejszanych potem 2,88×. Stałe poniżej
    # (30 px czcionki, NOTIF_IMG_H) były dobrane pod tamten, zmniejszany
    # kontekst — bez tej korekty dymki wyszłyby ~3× za duże i przestałyby się
    # mieścić. Skala użytkownika (NOTIF_SCALE z menu) działa jak dotąd.
    s = NOTIF_SCALE * 0.42               # wspólny mnożnik: zdjęcie + tekst + ramki
    # numer tablicy rysowany przez PIL (pikselowy VT323) — spójnie z reszta
    # motywu; cv2.putText dawał obcy, „gładki" krój
    fpx = max(12, int(round(30 * s)))
    for n in reversed(notifs):
        age = now - n["t0"]
        if age > NOTIF_TTL:
            continue
        alpha = 1.0 if age < NOTIF_TTL - 1.0 else max(0.15, NOTIF_TTL - age)

        crop = n["crop"]
        ch, cw = crop.shape[:2]
        tw2, th2 = _text_size(n["text"], fpx)
        base = max(2, fpx // 6)
        pad, accent = max(6, int(12 * s)), max(3, int(5 * s))
        # Najpierw ile MIEJSCA zostało, potem rozmiar zdjęcia. Odwrotna kolejność
        # powodowała, że przy dużej skali karta nie mieściła się i znikała
        # całkiem (użytkownik powiększa, a dymek nagle przepada).
        wolne_h = frame.shape[0] - 50 - y - (th2 + base + pad * 3)
        if wolne_h < 40:
            break
        disp_h = max(40, min(int(NOTIF_IMG_H * s), wolne_h))
        disp_w = max(int(160 * s), min(int(620 * s),
                                       int(cw * disp_h / max(1, ch))))
        disp_w = min(disp_w, max(80, w - 2 * pad - accent - 20))
        tile_w = min(max(disp_w, tw2) + pad * 2 + accent, max(120, w - 28))
        tile_h = disp_h + th2 + base + pad * 3
        x1 = w - tile_w - 14
        y1, y2 = y, y + tile_h
        x2 = x1 + tile_w
        if x1 < 0 or y2 > frame.shape[0] - 50:   # nie wjeżdżaj na dolny HUD
            break

        # Kafel z CACHE (24.09): resize INTER_CUBIC + PIL (2× cvtColor,
        # fromarray, draw.text, np.array) robione były NA KAŻDĄ klatkę przez
        # 6 s po każdym rozpoznaniu, dla max 3 dymków — czyli najdrożej
        # dokładnie wtedy, gdy jest ruch. Kafel nie zależy od tła (całość
        # zamalowana), więc renderujemy raz i tylko mieszamy alfę.
        klucz = (id(crop), n["text"], disp_w, disp_h, tile_w, tile_h, fpx)
        tile = n.get("_tile") if n.get("_tile_klucz") == klucz else None
        if tile is None:
            disp = cv2.resize(crop, (disp_w, disp_h), interpolation=cv2.INTER_CUBIC)
            tile = np.empty((tile_h, tile_w, 3), dtype=np.uint8)
            cv2.rectangle(tile, (0, 0), (tile_w - 1, tile_h - 1), (28, 26, 24), -1)
            cv2.rectangle(tile, (0, 0), (tile_w - 1, tile_h - 1), (90, 90, 90), 1)
            cv2.rectangle(tile, (0, 0), (accent - 1, tile_h - 1), COLOR_CONFIDENT, -1)
            cx = accent + (tile_w - accent - disp_w) // 2
            tile[pad:pad + disp_h, cx:cx + disp_w] = disp
            cv2.rectangle(tile, (cx - 1, pad - 1), (cx + disp_w, pad + disp_h),
                          (70, 70, 70), 1)
            tx = accent + (tile_w - accent - tw2) // 2
            _put_text_batch(tile, [(n["text"], (max(0, tx), pad + disp_h + pad // 2),
                                    (245, 245, 245), fpx)])
            n["_tile"], n["_tile_klucz"] = tile, klucz
        if alpha >= 1.0:
            frame[y1:y2, x1:x2] = tile
        else:
            cv2.addWeighted(tile, alpha, frame[y1:y2, x1:x2], 1.0 - alpha, 0,
                            frame[y1:y2, x1:x2])
        y = y2 + 12


# ── Powiadomienia systemowe (toasty jak w telefonie) ─────────────────────────
# Dolny środek, ciemna karta z ikoną, wjazd i wygaszenie. Osobne od dymków
# z tablicami (prawy górny) i od dolnego banera `_menu_banner`, bo dotyczą
# sprzętu (głośnik, pendrive), a nie rozpoznawania.
TOAST_TTL = 4.0
_toast_lock = threading.Lock()
_toasty: list = []

# kolory z motywu PiLPR (BGR)
_TOAST_KOLORY = {"blue": (216, 127, 111), "green": (94, 154, 74),
                 "red": (82, 75, 209), "gold": (58, 155, 202),
                 "gray": (150, 140, 155)}


def toast(txt: str, ikona: str = "info", kolor: str = "blue",
          secs: float = TOAST_TTL) -> None:
    """Pokaż powiadomienie. Bezpieczne z KAŻDEGO wątku (tylko dopisanie do
    listy pod blokadą — rysuje wątek wyświetlania)."""
    with _toast_lock:
        _toasty.append({"txt": str(txt)[:46], "ikona": ikona, "kolor": kolor,
                        "t0": time.monotonic(), "ttl": float(secs)})
        del _toasty[:-4]


def _ikona_rysuj(img, rodzaj: str, cx: int, cy: int, r: int, kolor) -> None:
    """Ikonki rysowane wektorowo — bez plików i bez zależności od fontów."""
    if rodzaj == "bt":
        p = np.array([[-0.5, -0.35], [0.5, 0.35], [0.0, 0.85], [0.0, -0.85],
                      [0.5, -0.35], [-0.5, 0.35]], np.float32)
        pts = (p * r + np.array([cx, cy], np.float32)).astype(np.int32)
        cv2.polylines(img, [pts], False, kolor, 2, cv2.LINE_AA)
    elif rodzaj == "ok":
        cv2.line(img, (cx - r + 2, cy), (cx - 2, cy + r - 3), kolor, 2,
                 cv2.LINE_AA)
        cv2.line(img, (cx - 2, cy + r - 3), (cx + r, cy - r + 2), kolor, 2,
                 cv2.LINE_AA)
    elif rodzaj == "x":
        cv2.line(img, (cx - r + 3, cy - r + 3), (cx + r - 3, cy + r - 3),
                 kolor, 2, cv2.LINE_AA)
        cv2.line(img, (cx - r + 3, cy + r - 3), (cx + r - 3, cy - r + 3),
                 kolor, 2, cv2.LINE_AA)
    elif rodzaj == "glosnik":
        cv2.rectangle(img, (cx - r + 4, cy - r + 2), (cx + r - 4, cy + r - 2),
                      kolor, 2)
        cv2.circle(img, (cx, cy + 2), max(2, r // 3), kolor, 1, cv2.LINE_AA)
    elif rodzaj == "usb":
        cv2.line(img, (cx, cy - r + 2), (cx, cy + r - 2), kolor, 2, cv2.LINE_AA)
        cv2.circle(img, (cx, cy + r - 2), 2, kolor, -1, cv2.LINE_AA)
        cv2.line(img, (cx, cy - 2), (cx + r - 4, cy - r + 5), kolor, 2,
                 cv2.LINE_AA)
    else:                                    # info
        cv2.circle(img, (cx, cy), r - 2, kolor, 2, cv2.LINE_AA)
        cv2.line(img, (cx, cy - 3), (cx, cy + 4), kolor, 2, cv2.LINE_AA)


def _draw_toasts(frame) -> None:
    """Stos powiadomień: najnowsze na dole, starsze wyżej."""
    teraz = time.monotonic()
    with _toast_lock:
        zywe = [t for t in _toasty if teraz - t["t0"] < t["ttl"]]
        if len(zywe) != len(_toasty):
            _toasty[:] = zywe
        lista = list(zywe)
    if not lista:
        return
    h, w = frame.shape[:2]
    # Tekst przez PIL (_put_text_batch), NIE cv2.putText — czcionki Hershey
    # w OpenCV nie mają polskich znaków i „Połączono" wychodziło krzaczkami.
    fsize = 15 if w < 900 else 17
    y = h - 58
    for t in reversed(lista[-3:]):
        wiek = teraz - t["t0"]
        zostalo = t["ttl"] - wiek
        alpha = min(1.0, wiek / 0.18)                 # wjazd
        if zostalo < 0.5:                             # wygaszenie
            alpha = min(alpha, max(0.0, zostalo / 0.5))
        if alpha <= 0.02:
            continue
        tw, _th = _text_size(t["txt"], fsize)
        ir, pad = 10, 12
        kw = min(pad * 2 + ir * 2 + 10 + tw, w - 16)
        kh = max(ir * 2 + pad, fsize + pad + 12)
        x1, y1 = (w - kw) // 2, y - kh
        if y1 < 46:
            break
        kafel = frame[y1:y1 + kh, x1:x1 + kw].copy()
        cv2.rectangle(kafel, (0, 0), (kw - 1, kh - 1), (34, 18, 26), -1)
        cv2.rectangle(kafel, (0, 0), (kw - 1, kh - 1), (70, 52, 84), 1)
        kolor = _TOAST_KOLORY.get(t["kolor"], _TOAST_KOLORY["blue"])
        _ikona_rysuj(kafel, t["ikona"], pad + ir, kh // 2, ir, kolor)
        _put_text_batch(kafel, [(t["txt"],
                                 (pad + ir * 2 + 10, (kh - fsize) // 2 - 2),
                                 (235, 230, 238), fsize)])
        cv2.addWeighted(kafel, alpha, frame[y1:y1 + kh, x1:x1 + kw],
                        1.0 - alpha, 0, frame[y1:y1 + kh, x1:x1 + kw])
        y = y1 - 8


# ── GStreamer App ─────────────────────────────────────────────────────────────

def _has_display() -> bool:
    """B10: sprawdź czy mamy wyświetlacz — przy autostarcie bez monitora zwróć False."""
    if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        return True
    try:
        import subprocess
        result = subprocess.run(["xdpyinfo"], capture_output=True, timeout=2)
        return result.returncode == 0
    except Exception:
        return False


# ── Statystyki wydajności (FPS / CPU / Hailo temp) ───────────────────────────

_stat_fps        = 0.0
_stat_disp_fps   = 0.0      # realna płynność ekranu (ticki wyświetlania)
_stat_cpu        = 0.0
_stat_ram        = 0.0      # zużyty RAM w GB
_stat_cpu_temp   = 0.0
_stat_hailo_temp = 0.0
_stat_cb_count   = 0        # inkrementowany w callbacku każdą klatką
_stat_disp_count = 0        # inkrementowany przy każdej wyświetlonej klatce
_stat_lock       = threading.Lock()

_stats_strip_cache: Optional[np.ndarray] = None
_stats_strip_key:   tuple = ()


def _ustap_procesor(o: int = 10) -> None:
    """Obniża priorytet BIEŻĄCEGO wątku. Na Linuksie „nice" działa per-wątek,
    więc wątki pomocnicze (mowa, zapis, kopie) ustępują rozpoznawaniu —
    detekcja i OCR dostają procesor pierwsze. Nie zmienia to ich pracy,
    tylko kolejność przy zatłoczeniu."""
    try:
        os.setpriority(os.PRIO_PROCESS, 0, o)
    except (OSError, AttributeError, PermissionError) as e:
        print(f"[Priorytet] nie ustawiono ({e})")


def _watki_teraz() -> set:
    try:
        return set(os.listdir("/proc/self/task"))
    except OSError:
        return set()


# Wątki dashcamu, które WOLNO spowolnić — wyłącznie po dokładnej nazwie.
#
# NIE WOLNO rozpoznawać ich po tym, że „powstały po starcie nagrywania":
# GStreamer nadaje wątkom nazwy DOPIERO PO utworzeniu, więc w chwili
# sprawdzania cały tor rozpoznawania nazywa się jeszcze „Hailo Detection"
# i trafia pod spowolnienie. Kosztowało to nice 12 na identity_callback,
# inference_* i hailo_* — czyli dokładnie odwrotnie, niż zamierzano.
_DASH_WATKI = ("dash_src", "queue_video", "muxer:")


_TOR_WATKI = ("inference", "identity", "hailo_", "source_scale",
              "source_convert", "queue0", "app_source", "camera",
              "ipaproxy", "python3")


def _ustap_watki_dashcam(przed: set, o: int = 12) -> int:
    """Spowalnia wątki nagrywania — z koderem x264 włącznie.

    Koder dziedziczy nazwę procesu, więc rozpoznajemy go po tym, że powstał
    przy starcie nagrywania. Jest to bezpieczne TYLKO dlatego, że nagrywanie
    startuje dopiero, gdy potok rozpoznawania już płynie i jego wątki mają
    swoje nazwy. Dodatkowo `_przywroc_priorytety_toru()` cyklicznie zeruje
    priorytet wszystkiemu, co należy do rozpoznawania — gdyby mimo to coś
    się prześlizgnęło, wróci do normy w ciągu pół minuty."""
    ile = 0
    for tid in (_watki_teraz() - przed):
        try:
            with open(f"/proc/self/task/{tid}/comm") as fh:
                comm = fh.read().strip().lower()
        except OSError:
            continue
        if any(k in comm for k in _TOR_WATKI):
            continue                      # nigdy nie ruszamy rozpoznawania
        try:
            os.setpriority(os.PRIO_PROCESS, int(tid), o)
            ile += 1
        except (OSError, ValueError):
            pass
    for tid in _watki_teraz():             # jawnie nazwane elementy nagrywania
        try:
            with open(f"/proc/self/task/{tid}/comm") as fh:
                comm = fh.read().strip().lower()
            if any(c in comm for c in _DASH_WATKI):
                os.setpriority(os.PRIO_PROCESS, int(tid), o)
                ile += 1
        except (OSError, ValueError):
            pass
    return ile


def _przywroc_priorytety_toru() -> int:
    """Zeruje nice wątkom rozpoznawania. Wołane cyklicznie — to sieć
    bezpieczeństwa: rozpoznawanie MUSI mieć pełny priorytet, cokolwiek by
    się wcześniej wydarzyło."""
    tor = ("inference", "identity", "hailo_", "source_scale", "source_convert",
           "queue0", "app_source")
    ile = 0
    for tid in _watki_teraz():
        try:
            with open(f"/proc/self/task/{tid}/comm") as fh:
                comm = fh.read().strip().lower()
            if not any(k in comm for k in tor):
                continue
            # tylko gdy coś je SPOWOLNIŁO (nice > 0). Ujemnego nie ruszamy —
            # to podniesiony priorytet od strażnika i ma tak zostać.
            if os.getpriority(os.PRIO_PROCESS, int(tid)) > 0:
                os.setpriority(os.PRIO_PROCESS, int(tid), 0)
                ile += 1
        except (OSError, ValueError):
            pass
    return ile


def _tidy_toru() -> list:
    """TID-y wątków należących do rozpoznawania (potok + callback)."""
    tor = ("inference", "identity", "hailo_", "source_scale", "source_convert",
           "queue0", "app_source")
    out = []
    for tid in _watki_teraz():
        try:
            with open(f"/proc/self/task/{tid}/comm") as fh:
                comm = fh.read().strip().lower()
        except OSError:
            continue
        if any(k in comm for k in tor):
            out.append(tid)
    return out


_prio_podniesiony = False
_prio_tidy_ost = frozenset()   # ostatni zestaw TID-ow z udanego renice


def _podnies_priorytet_toru(n: int = -5) -> int:
    """Daje rozpoznawaniu priorytet WYŻSZY niż cokolwiek innego w systemie.

    Samo spowalnianie zadań pobocznych nie wystarcza: przy pełnym obciążeniu
    procesora wątki potoku (nice 0) konkurują na równi z każdym innym
    procesem, głodzą się i HailoRT przerywa po 10 s — a to realnie ominięte
    tablice. Ujemny nice wymaga uprawnień, więc idziemy przez `sudo renice`;
    zmiana limitów systemowych wymagałaby restartu menedżera sesji, co
    ubiłoby pulpit w aucie.

    Gdy sudo nie zadziała, zostaje stan dotychczasowy (nice 0) — gorszy,
    ale poprawny. Wtedy w logu jest wyraźny ślad."""
    global _prio_podniesiony, _prio_tidy_ost
    import subprocess as _sp
    tidy = _tidy_toru()
    if not tidy:
        return 0
    # Straznik chodzi co 30 s, ale watki potoku zmieniaja sie tylko przy
    # restarcie kamery. Bezwarunkowe `sudo renice` co 30 s to 2880 wywolan
    # sudo na dobe — kazde z wpisem do journala, czyli zapisy na karte SD
    # bez zadnego powodu (a karta juz raz zawiesila sie przy spadku
    # napiecia, 06.08.2026). Powtarzamy tylko, gdy zestaw TID-ow sie zmienil.
    biezacy = frozenset(tidy)
    if _prio_podniesiony and biezacy == _prio_tidy_ost:
        return len(tidy)
    try:
        r = _sp.run(["sudo", "-n", "renice", "-n", str(n), "-p"] + tidy,
                    capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            if not _prio_podniesiony:
                print("[Priorytet] NIE podniosłem priorytetu rozpoznawania "
                      f"({(r.stderr or '').strip()[:80]}) — działa na zwykłym",
                      flush=True)
            return 0
    except Exception as e:
        print(f"[Priorytet] renice: {e}", flush=True)
        return 0
    _prio_tidy_ost = biezacy
    if not _prio_podniesiony:
        print(f"[Priorytet] rozpoznawanie na nice {n} "
              f"({len(tidy)} wątków) — ma pierwszeństwo przed wszystkim",
              flush=True)
        _prio_podniesiony = True
    return len(tidy)


def _straznik_priorytetow() -> None:
    """Pilnuje, żeby rozpoznawanie ZAWSZE miało pełny priorytet.

    Rozpoznawanie jest najważniejsze, a wywłaszczony wątek potoku to klatki
    oddane bezpowrotnie. Dlatego zamiast ufać, że nic go nie spowolni,
    sprawdzamy to cyklicznie i naprawiamy.

    UWAGA na historię: zacięcia „równo 10 s" z 20-30.07.2026 NIE brały się
    z zatoru potoku — to był odczyt temperatury Hailo blokujący GIL
    (patrz _hailo_temp_odczyt). Ten strażnik ich nie naprawiał i nie po to
    tu jest."""
    _ustap_procesor(15)
    # Poczekaj, aż potok ruszy i wątki dostaną nazwy
    czekaj = time.monotonic() + 60.0
    while time.monotonic() < czekaj and _stat_cb_count < 30:
        time.sleep(1.0)
    time.sleep(3.0)
    _podnies_priorytet_toru(-5)
    while True:
        time.sleep(30.0)
        try:
            # nowe wątki potoku (np. po wznowieniu) też mają dostać priorytet
            _podnies_priorytet_toru(-5)
        except Exception as e:
            print(f"[Priorytet] strażnik: {e}")


# ── Temperatura układu Hailo — WYŁĄCZNIE w osobnym procesie ────────────────
# NIE WOLNO wołać get_chip_temperature() wprost z aplikacji. To wywołanie czeka
# do 10 s na odpowiedź firmware'u i NIE zwalnia GIL-a, więc gdy firmware nie
# odpowie (losowo, kilka razy na godzinę), zamiera CAŁY interpreter — łącznie
# z pętlą rysowania. To była przyczyna zacięć „równo 10 s" z 20-30.07.2026:
# w logach HailoRT każdy HAILO_TIMEOUT to [get_chip_temperature], ANI JEDEN
# nie dotyczy inferencji. Detekcja nigdy się nie zacinała — zacinał się
# termometr, a błąd połykało `except: pass`, więc nie było po nim śladu.
# W procesie potomnym może wisieć do woli: ubijamy go po limicie i zostajemy
# przy poprzednim odczycie. Zmierzony koszt: 0,53 s realnie, 0,47 s CPU.
_HAILO_TEMP_KOD = (
    "from hailo_platform.pyhailort._pyhailort import Device as D;"
    "d=D.scan();"
    "t=D.create(d[0]).get_chip_temperature();"
    "print(max(t.ts0_temperature, t.ts1_temperature))"
)
_HAILO_TEMP_CO_S = 60.0    # układ stygnie wolno, częściej nie ma po co


def _hailo_temp_odczyt(poprzednia: float) -> float:
    """Temperatura układu Hailo albo poprzednia wartość, gdy się nie udało."""
    import subprocess as _sp
    try:
        p = _sp.run([sys.executable, "-c", _HAILO_TEMP_KOD],
                    capture_output=True, text=True, timeout=5.0)
        if p.returncode == 0:
            return float(p.stdout.strip())
    except Exception as e:
        # zawisł albo padł — mamy poprzedni odczyt, ale MA TO BYĆ WIDOCZNE:
        # to jest dokładnie ten kanał, który wcześniej zamrażał aplikację
        _zglos("hailo_temperatura", e)
    return poprzednia


_stat_zasilanie = {"hex": 0, "spadek": False, "dlawienie": False,
                   "spadek_bylo": False, "dlawienie_bylo": False,
                   "n_spadkow": 0, "od": 0.0, "ostatni": "", "laczny_s": 0.0}
_zasilanie_ost = 0.0


def _zasilanie_tik(now: float) -> None:
    """Co 10 s `vcgencmd get_throttled` (02.10.2026, zad. 1 z 01.10): bit 0 =
    spadek napięcia TERAZ, bit 2 = CPU dławione TERAZ, bity 16/18 = było od
    startu. W aucie 01.10 zasilanie siadało (EXT5V 4,7 V, throttled 0x50005)
    i CPU pokazywało 60–78 % zamiast ~30 % — bez tego sygnału wnioski o
    wydajności były błędne. Zmiany stanu idą do logu (z godziną), HUD-u,
    [Diag], www (/api/stats → pasek statystyk) i toastu."""
    global _zasilanie_ost
    if now - _zasilanie_ost < 10.0:
        return
    _zasilanie_ost = now
    try:
        r = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True,
                           text=True, timeout=3)
        hx = int(r.stdout.strip().split("=")[1], 16)
    except Exception as e:
        _zglos("zasilanie", e)
        return
    z = _stat_zasilanie
    spadek, dlaw = bool(hx & 0x1), bool(hx & 0x4)
    kiedy = datetime.now().strftime("%H:%M:%S")
    if spadek and not z["spadek"]:
        z["n_spadkow"] += 1
        z["od"], z["ostatni"] = now, kiedy
        print(f"[{kiedy}] [Zasilanie] SPADEK NAPIĘCIA (throttled=0x{hx:x}, "
              f"nr {z['n_spadkow']})", flush=True)
        try:
            toast("SPADEK NAPIĘCIA", "info", "red", 6.0)
        except Exception:
            pass
    elif not spadek and z["spadek"]:
        z["laczny_s"] += now - z["od"]
        print(f"[{kiedy}] [Zasilanie] napięcie OK po {now - z['od']:.0f} s "
              f"(0x{hx:x})", flush=True)
    if dlaw != z["dlawienie"]:
        print(f"[{kiedy}] [Zasilanie] CPU {'DŁAWIONE' if dlaw else 'bez dławienia'} "
              f"(0x{hx:x})", flush=True)
    z.update(hex=hx, spadek=spadek, dlawienie=dlaw,
             spadek_bylo=bool(hx & 0x10000), dlawienie_bylo=bool(hx & 0x40000))


def _stats_worker() -> None:
    """Wątek tła: co sekundę liczy FPS, CPU%, temp CPU i temp Hailo."""
    global _stat_fps, _stat_disp_fps, _stat_cpu, _stat_ram, _stat_cpu_temp, _stat_hailo_temp
    ostatnia_hailo = 0.0
    ostatnia_diag = 0.0
    _psutil.cpu_percent(interval=None)   # pierwsze wywołanie inicjuje pomiar
    last_count = 0
    last_disp  = 0
    last_t     = time.monotonic()
    while True:
        time.sleep(2.0)   # co 2 s — odczyt temperatur przez psutil jest drogi
        now = time.monotonic()
        with _stat_lock:
            cur  = _stat_cb_count
            curd = _stat_disp_count
        fps  = (cur - last_count) / max(now - last_t, 0.001)
        dfps = (curd - last_disp) / max(now - last_t, 0.001)
        cpu  = _psutil.cpu_percent(interval=None)
        ram  = _psutil.virtual_memory().used / 1_073_741_824  # GB
        # Temperatura CPU (RPi: 'cpu_thermal', inne: 'coretemp')
        cpu_t = 0.0
        try:
            for key in ('cpu_thermal', 'coretemp', 'k10temp', 'acpitz'):
                bucket = _psutil.sensors_temperatures().get(key)
                if bucket:
                    cpu_t = bucket[0].current
                    break
        except Exception as e:
            _zglos("temperatura_cpu", e)
        hailo_t = _stat_hailo_temp
        if now - ostatnia_hailo >= _HAILO_TEMP_CO_S:
            ostatnia_hailo = now
            hailo_t = _hailo_temp_odczyt(hailo_t)
        _zasilanie_tik(now)            # spadek napięcia / dławienie (02.10)
        # Diagnostyka opóźnienia obrazu — co 30 s linia do logu. Dzięki temu
        # dane z JAZDY zbierają się same i można je odczytać po powrocie,
        # bez siedzenia na SSH (co i tak zaburza pomiar).
        # Gdy zaległość rośnie, mierzymy GĘSTO (co 2 s) — inaczej zdarzenie
        # trwające kilkanaście sekund mieści się między dwoma pomiarami
        # i nie wiadomo, co się działo. W spoczynku co 30 s, żeby nie zalać logu.
        with _diag_lock:
            _pilne = _diag["locie"] > 30
        if now - ostatnia_diag >= (2.0 if _pilne else 30.0):
            ostatnia_diag = now
            try:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] [Diag] "
                      f"{_diag_podsumowanie()}  ·  fps {fps:.1f}", flush=True)
            except Exception as e:
                _zglos("diag_podsumowanie", e)
        with _stat_lock:
            _stat_fps        = fps
            _stat_disp_fps   = dfps
            _stat_cpu        = cpu
            _stat_ram        = ram
            _stat_cpu_temp   = cpu_t
            _stat_hailo_temp = hailo_t
        last_count = cur
        last_disp  = curd
        last_t     = now


# ── Menu ekranowe w stylu aplikacji: kafle z ikonami ─────────────────────
# Rysowane Pillow, nie cv2 — dzięki temu są polskie znaki (cv2.putText ich
# nie umie) i antyaliasing. Kształty rysujemy na płótnie powiększonym
# _MS razy i zmniejszamy: PIL nie wygładza linii, a downscale owszem.
# Tekst dokładamy dopiero po zmniejszeniu, żeby został ostry.
_UI_FONT_PIXEL = "/home/admin/hailo-apps/local_resources/web/VT323-Regular.ttf"
_UI_FONT_HEAD  = "/home/admin/hailo-apps/local_resources/web/PressStart2P-Regular.ttf"
_UI_FONT_REG = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
_UI_FONT_BLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
_UI_FONT_CACHE: dict = {}
_MS = 3                                  # krotność nadpróbkowania
# VT323 jest wąski i niski w stosunku do rozmiaru punktowego — bez tego
# mnożnika napisy w kaflach byłyby wyraźnie mniejsze niż w DejaVu.
_UI_PIXEL_SKALA = 1.45
_UI_PIXEL_OK = os.path.exists(_UI_FONT_PIXEL)
# UWAGA: VT323 NIE MA tych znaków — renderują się jako NIC (pusty glif), więc
# napis po cichu traci fragment i nikt tego nie zauważy. Sprawdzone renderem
# 30.07.2026 (test: czy bitmapa glifu jest pusta). Polskie ogonki, ×, ·, —, …,
# ° i cudzysłowy „” są OK. Zanim wstawisz nowy znak specjalny do napisu
# rysowanego przez PIL — sprawdź go, nie zakładaj.
_ZNAKI_ZAKAZANE = "←→↑↓★"

# ── Paleta: ta SAMA co w interfejsie webowym (motyw „PiLPR VHS") ────────────
# Wartości 1:1 ze zmiennych CSS :root, żeby ekran w aucie i strona wyglądały
# jak jedno urządzenie, a nie dwa różne programy. Kolejność kanałów to RGB —
# na BGR przeliczamy dopiero przy składaniu klatki (koniec _menu_surface).
C_SCRIM  = (14, 9, 16)                   # --bg      #0e0910
C_TILE   = (21, 14, 24)                  # --bg-row  #150e18
C_TILE_S = (46, 34, 62)                  # kafel wybrany — rozjaśnione --bg-th
C_BORDER = (43, 32, 52)                  # --border  #2b2034
C_ACCENT = (111, 127, 216)               # --blue    #6f7fd8
C_TEXT   = (194, 180, 189)               # --text    #c2b4bd
C_MUTED  = (109, 95, 116)                # --dim     #6d5f74
C_VALUE  = (143, 157, 230)               # --blue rozjaśniony (wartości)
C_DANGER = (209, 75, 82)                 # --red     #d14b52
C_WARN   = (202, 155, 58)                # --yellow  #ca9b3a
C_OK     = (74, 154, 94)                 # --green   #4a9a5e


# ── Stan uruchamiania: ekran ładowania / błędu na pełny ekran ────────────────
# Urządzenie ma zachowywać się jak gotowa aplikacja: od włączenia widać
# ekran startowy (nie pulpit), a gdy coś nie działa — pełnoekranowy komunikat
# CO i DLACZEGO, zamiast pustego okna, przy którym nie wiadomo, co się dzieje.
_boot_lock = threading.Lock()
_boot = {"faza": "start", "tytul": "Uruchamianie…", "opis": "",
         "kroki": [], "blad": False, "od": time.monotonic()}


def _set_boot(faza: str, tytul: str, opis: str = "",
              blad: bool = False, kroki=None) -> None:
    with _boot_lock:
        if _boot["faza"] != faza or _boot["tytul"] != tytul:
            _boot["od"] = time.monotonic()
        _boot.update(faza=faza, tytul=tytul, opis=opis, blad=blad,
                     kroki=kroki or [])


def _get_boot() -> dict:
    with _boot_lock:
        return dict(_boot)


def _ui_font(size: int, bold: bool = False):
    """Czcionka interfejsu — pixelowa (VT323), spójna z motywem i ze stroną.

    VT323 nie ma odmiany pogrubionej, więc `bold` nie zmienia kroju; hierarchię
    robimy rozmiarem i kolorem. Gdyby pliku zabrakło, wracamy do DejaVu —
    brzydziej, ale czytelnie, a to ekran kierowcy."""
    key = (size, bold)
    if key not in _UI_FONT_CACHE:
        try:
            if _UI_PIXEL_OK:
                _UI_FONT_CACHE[key] = _PILFont.truetype(
                    _UI_FONT_PIXEL, int(size * _UI_PIXEL_SKALA))
            else:
                _UI_FONT_CACHE[key] = _PILFont.truetype(
                    _UI_FONT_BLD if bold else _UI_FONT_REG, size)
        except OSError:
            _UI_FONT_CACHE[key] = _PILFont.load_default()
    return _UI_FONT_CACHE[key]


def _ui_font_head(size: int):
    """Nagłówki — Press Start 2P. Krój bardzo szeroki, więc TYLKO na krótkie
    napisy (tytuł ekranu); w etykietach kafli rozsadziłby układ."""
    key = ("head", size)
    if key not in _UI_FONT_CACHE:
        try:
            _UI_FONT_CACHE[key] = _PILFont.truetype(_UI_FONT_HEAD, size)
        except OSError:
            _UI_FONT_CACHE[key] = _ui_font(size, bold=True)
    return _UI_FONT_CACHE[key]


# Siatka ikony. Im mniejsza wobec docelowego boku, tym grubszy piksel.
# 16 przy ikonie ~52 px daje powiększenie ~3,3× — piksel widać jednoznacznie,
# a kształty jeszcze się nie rozpadają (przy 12 znikały cienkie elementy,
# np. promienie słońca i kreski w ikonie tablicy).
_IKONA_SIATKA = 16


def _menu_ikona_pixel(act: str, bok: int, col):
    """Ikona kafla w stylu pixel-art.

    Kształty zostają te same (są czytelne i sprawdzone), zmienia się sposób
    rysowania: powstają na siatce 20×20 kreską grubości 1 i dopiero potem
    idą w górę interpolacją NEAREST. Dzięki temu piksel jest widoczny
    CELOWO — inaczej niż przy dotychczasowym nadpróbkowaniu ×3 z LANCZOS,
    które wygładzało wszystko i gryzło się z motywem."""
    g = _IKONA_SIATKA
    im = _PILImage.new("RGBA", (g, g), (0, 0, 0, 0))
    d = _PILDraw.Draw(im)
    _menu_icon(d, act, g // 2, g // 2, g // 2 - 2, col, 1)
    return im.resize((bok, bok), _PILImage.NEAREST)


def _menu_icon(d, act: str, cx: int, cy: int, r: int, col, sw: int) -> None:
    """Ikona kafla — czysta geometria, bez plików graficznych.
    (cx, cy) środek, r promień pola ikony, sw grubość kreski."""
    def ln(x1, y1, x2, y2, w=0):
        d.line((cx + x1, cy + y1, cx + x2, cy + y2), fill=col, width=w or sw)

    def circle(rr, w=0, fill=None):
        d.ellipse((cx - rr, cy - rr, cx + rr, cy + rr),
                  outline=None if fill else col, width=w or sw, fill=fill)

    def box(x1, y1, x2, y2, rad=0, w=0, fill=None):
        # `rad` celowo ignorujemy: ikony powstają na siatce 16×16, gdzie
        # zaokrąglenie i tak zeszłoby do 1-2 pikseli, a psuje styl pixel-art.
        args = dict(outline=None if fill else col, width=w or sw, fill=fill)
        d.rectangle((cx + x1, cy + y1, cx + x2, cy + y2), **args)

    h = r                                   # skrót: pół wysokości ikony

    if act == "zoom":                       # lupa
        d.ellipse((cx - h, cy - h, cx + h // 3, cy + h // 3),
                  outline=col, width=sw)
        ln(h // 4, h // 4, h, h, sw + sw // 2)
    elif act == "ev":                       # słońce z promieniami
        circle(h // 2)
        for a in range(0, 360, 45):
            dx, dy = math.cos(math.radians(a)), math.sin(math.radians(a))
            ln(int(dx * h * 0.72), int(dy * h * 0.72),
               int(dx * h), int(dy * h))
    elif act == "ae":                       # przysłona
        circle(h)
        for a in (30, 150, 270):
            dx, dy = math.cos(math.radians(a)), math.sin(math.radians(a))
            ln(0, 0, int(dx * h), int(dy * h))
    elif act == "met":                       # celownik pomiaru
        circle(h)
        circle(h // 2)
        ln(-h - sw, 0, -h + h // 3, 0)
        ln(h - h // 3, 0, h + sw, 0)
    elif act == "det":                       # ramka detekcji (narożniki)
        k = h // 2
        for sx in (-1, 1):
            for sy in (-1, 1):
                ln(sx * h, sy * h, sx * (h - k), sy * h)
                ln(sx * h, sy * h, sx * h, sy * (h - k))
    elif act == "ocr":                       # tablica z tekstem
        box(-h, -h * 2 // 3, h, h * 2 // 3, rad=h // 3)
        for i, yy in enumerate((-h // 4, h // 6)):
            ln(-h + h // 3, yy, h - h // 3 - i * h // 3, yy)
    elif act == "reads":                     # lista z ptaszkiem
        for yy in (-h * 2 // 3, 0, h * 2 // 3):
            ln(-h // 4, yy, h, yy)
        for yy in (-h * 2 // 3, 0):
            ln(-h, yy, -h * 2 // 3, yy + h // 3)
            ln(-h * 2 // 3, yy + h // 3, -h // 2, yy - h // 3)
    elif act == "hud":                       # słupki statystyk
        for i, hh in enumerate((h // 2, h, h * 2 // 3)):
            x = -h + i * h
            d.rectangle((cx + x, cy + h - hh, cx + x + sw * 2, cy + h),
                        fill=col)
    elif act == "usb":                       # pendrive
        box(-h // 2, -h, h // 2, h // 3, rad=h // 4)
        box(-h // 3, h // 3, h // 3, h, fill=col)
    elif act == "backup":                    # dysk ze strzałką w dół
        d.arc((cx - h, cy - h, cx + h, cy + h), 20, 160, fill=col, width=sw)
        ln(0, -h // 2, 0, h // 2)
        ln(-h // 3, h // 6, 0, h // 2)
        ln(h // 3, h // 6, 0, h // 2)
    elif act in ("bt", "btpair"):            # runa bluetooth
        k = h if act == "bt" else int(h * 0.8)
        ln(0, -k, 0, k)
        ln(0, -k, k // 2, -k // 2)
        ln(k // 2, -k // 2, -k // 2, k // 2)
        ln(-k // 2, -k // 2, k // 2, k // 2)
        ln(k // 2, k // 2, 0, k)
        if act == "btpair":                  # plus przy runie
            ln(int(h * 0.55), -h, int(h * 1.15), -h)
            ln(int(h * 0.85), -h - h // 3, int(h * 0.85), -h + h // 3)
    elif act in ("ttsall", "ttstest", "ttsbrand"):
        if act == "ttstest":                 # trójkąt play
            d.polygon((cx - h // 2, cy - h, cx + h, cy,
                       cx - h // 2, cy + h), fill=col)
        else:                                # głośnik + fale
            d.polygon((cx - h, cy - h // 3, cx - h // 3, cy - h // 3,
                       cx + h // 6, cy - h, cx + h // 6, cy + h,
                       cx - h // 3, cy + h // 3, cx - h, cy + h // 3),
                      fill=col)
            for i, rr in enumerate((h // 2, h)):
                d.arc((cx + h // 6 - rr, cy - rr, cx + h // 6 + rr, cy + rr),
                      -55, 55, fill=col, width=sw)
            if act == "ttsbrand":            # kropka = konkret (marka)
                d.ellipse((cx + h - sw, cy + h - sw, cx + h + sw,
                           cy + h + sw), fill=col)
    elif act == "full":                      # strzałki na zewnątrz
        for sx in (-1, 1):
            for sy in (-1, 1):
                ln(sx * h // 3, sy * h // 3, sx * h, sy * h)
                ln(sx * h, sy * h, sx * h // 3, sy * h)
                ln(sx * h, sy * h, sx * h, sy * h // 3)
    elif act == "close":                     # symbol zasilania
        d.arc((cx - h, cy - h, cx + h, cy + h), -60, 240, fill=col, width=sw)
        ln(0, -h - h // 4, 0, -h // 4)
    elif act == "snap":                      # aparat
        box(-h, -h * 2 // 3, h, h * 2 // 3, rad=h // 4)
        circle(h // 3)
        d.rectangle((cx - h // 3, cy - h - sw, cx - h // 12, cy - h * 2 // 3),
                    fill=col)
    elif act == "widoczny":                  # fale radiowe = malinka nadaje
        d.arc((cx - h // 3, cy - h // 3, cx + h // 3, cy + h // 3),
              0, 360, fill=col, width=sw)
        d.ellipse((cx - sw, cy - sw, cx + sw, cy + sw), fill=col)
        for r in (h * 2 // 3, h):
            d.arc((cx - r, cy - r, cx + r, cy + r), -60, 60, fill=col, width=sw)
            d.arc((cx - r, cy - r, cx + r, cy + r), 120, 240, fill=col, width=sw)
    elif act == "sound":                     # głośnik z suwakami głośności
        d.polygon((cx - h, cy - h // 3, cx - h // 3, cy - h // 3,
                   cx + h // 6, cy - h, cx + h // 6, cy + h,
                   cx - h // 3, cy + h // 3, cx - h, cy + h // 3), fill=col)
        for i, yy in enumerate((-h // 2, 0, h // 2)):
            ln(h // 2, yy, h, yy)
            kx = (h * 3 // 4, h * 5 // 8, h * 7 // 8)[i]
            d.ellipse((cx + kx - sw, cy + yy - sw, cx + kx + sw,
                       cy + yy + sw), fill=col)
    elif act == "image":                     # zdjęcie/krajobraz w ramce
        box(-h, -h * 3 // 4, h, h * 3 // 4, rad=h // 4)
        d.polygon((cx - h // 2, cy + h // 2, cx - h // 8, cy - h // 5,
                   cx + h // 4, cy + h // 2), fill=col)
        d.ellipse((cx + h // 3, cy - h // 2, cx + h * 2 // 3, cy - h // 5),
                  fill=col)
    elif act == "tools":                     # suwaki = opcje zaawansowane
        for i, yy in enumerate((-h * 2 // 3, 0, h * 2 // 3)):
            ln(-h, yy, h, yy)
            kx = (-h // 2, h // 3, -h // 4)[i]
            d.ellipse((cx + kx - sw * 2, cy + yy - sw * 2,
                       cx + kx + sw * 2, cy + yy + sw * 2), fill=col)
    else:                                    # zapas: kropka
        circle(h // 2, fill=col)


def _img_geom(w: int, h: int, n: int = 4) -> dict:
    """Ekran „Obraz": pasek sterowania na dole, reszta to ŻYWY podgląd.
    Kafle by tu nie zadziałały — przy strojeniu ekspozycji trzeba widzieć,
    co robi kamera, a pełnoekranowa nakładka to właśnie zasłaniała."""
    pad = 10 if w < 900 else 16
    gap = 8
    strip = 136 if h < 560 else 156
    y0 = h - strip
    n = max(1, n)
    chip_w = (w - 2 * pad - (n - 1) * gap) // n
    chip_h = 44 if h < 560 else 50      # 44 px to minimum dla palca
    r = 30 if h < 560 else 34
    return {"pad": pad, "gap": gap, "strip": strip, "y0": y0,
            "chip_w": chip_w, "chip_h": chip_h, "chip_y": y0 + 10,
            "r": r, "cy": y0 + 10 + chip_h + 8 + r,
            "minus_x": pad + 8 + r, "plus_x": w - pad - 8 - r}


def _img_chip_x(g: dict, i: int) -> int:
    return g["pad"] + i * (g["chip_w"] + g["gap"])


def _img_surface(w: int, h: int, items: list, sel: int,
                 tytul: str = "Obraz", scrim: float = 0.0,
                 info: str = "") -> tuple:
    """Nakładka ekranu sterowania. Dla „Obrazu" przezroczysta wszędzie poza
    paskiem u dołu i nagłówkiem — kadr z kamery zostaje nietknięty. Dla
    „Dźwięku" nie ma czego podglądać, więc tło przyciemniamy (scrim)."""
    g = _img_geom(w, h, len(items))
    head = 46 if h < 560 else 58
    S = _MS

    ksz = _PILImage.new("RGBA", (w * S, h * S), (0, 0, 0, 0))
    dk = _PILDraw.Draw(ksz)
    # pasek narzędziowy
    dk.rectangle((g["pad"] * S, g["y0"] * S,
                  (w - g["pad"]) * S, (h - 8) * S),
                 fill=C_TILE + (232,),
                 outline=C_BORDER + (255,), width=1 * S)
    for i, (_lab, _val, _act) in enumerate(items):
        x = _img_chip_x(g, i)
        wyb = (i == sel)
        dk.rectangle((x * S, g["chip_y"] * S,
                      (x + g["chip_w"]) * S,
                      (g["chip_y"] + g["chip_h"]) * S),
                     fill=(C_ACCENT + (255,)) if wyb
                     else (C_TILE_S + (180,)),
                     outline=C_BORDER + (255,), width=1 * S)
    for bx, plus in ((g["minus_x"], False), (g["plus_x"], True)):
        dk.rectangle(((bx - g["r"]) * S, (g["cy"] - g["r"]) * S,
                      (bx + g["r"]) * S, (g["cy"] + g["r"]) * S),
                     fill=C_ACCENT + (255,))
        dk.line(((bx - 12) * S, g["cy"] * S, (bx + 12) * S, g["cy"] * S),
                fill=(12, 16, 24, 255), width=4 * S)
        if plus:
            dk.line((bx * S, (g["cy"] - 12) * S, bx * S, (g["cy"] + 12) * S),
                    fill=(12, 16, 24, 255), width=4 * S)
    # tło nagłówka: wąski pasek, żeby tytuł był czytelny na jasnym niebie
    dk.rectangle((0, 0, w * S, head * S), fill=(10, 12, 16, 214))
    ksz = ksz.resize((w, h), _PILImage.LANCZOS)

    d = _PILDraw.Draw(ksz)
    f_head = _ui_font(21 if h < 560 else 25, bold=True)
    f_hint = _ui_font(12 if h < 560 else 14)
    f_chip = _ui_font(13 if h < 560 else 15)
    f_val = _ui_font(15 if h < 560 else 17)
    f_big = _ui_font(34 if h < 560 else 40, bold=True)
    d.text((_overlay_close_box(w, h)[2] + 10, head // 2), tytul,
           font=f_head, fill=C_TEXT + (255,), anchor="lm")
    d.text((w - g["pad"] - 2, head // 2),
           # bez strzałek: VT323 nie ma ich glifów (patrz _ZNAKI_ZAKAZANE)
           "strzałki — zmieniaj i wybieraj  ·  Esc — wróć",
           font=f_hint, fill=C_MUTED + (255,), anchor="rm")
    _draw_overlay_close(d, w, h)

    for i, (lab, val, _act) in enumerate(items):
        x = _img_chip_x(g, i) + g["chip_w"] // 2
        wyb = (i == sel)
        d.text((x, g["chip_y"] + g["chip_h"] // 2), lab, font=f_chip,
               anchor="mm",
               fill=((12, 16, 24) if wyb else C_MUTED) + (255,))
    if info:
        d.text((w // 2, (head + g["y0"]) // 2 - 14), info.split("|")[0],
               font=_ui_font(20 if h < 560 else 23, bold=True),
               fill=C_TEXT + (255,), anchor="mm")
        if "|" in info:
            d.text((w // 2, (head + g["y0"]) // 2 + 16), info.split("|")[1],
                   font=f_val, fill=C_MUTED + (255,), anchor="mm")
    lab, val, _act = items[sel]
    d.text((w // 2, g["cy"] - 12), val, font=f_big,
           fill=C_TEXT + (255,), anchor="mm")
    d.text((w // 2, g["cy"] + 20), lab, font=f_val,
           fill=C_MUTED + (255,), anchor="mm")

    rgba = np.array(ksz, dtype=np.uint8)
    alpha = rgba[:, :, 3:4].astype(np.float32) / 255.0
    rgb = rgba[:, :, 2::-1].astype(np.float32)
    if scrim > 0:
        tlo = np.array(C_SCRIM[::-1], dtype=np.float32)
        krycie = alpha + (1.0 - alpha) * scrim
        pre = rgb * alpha + tlo * (1.0 - alpha) * scrim
        inv = np.repeat((1.0 - krycie) * 255.0, 3, axis=2)
    else:
        # BEZ przyciemniania kadru — o to w ekranie „Obraz" chodzi
        pre = rgb * alpha
        inv = np.repeat((1.0 - alpha) * 255.0, 3, axis=2)
    return (np.clip(pre, 0, 255).astype(np.uint8),
            np.clip(inv, 0, 255).astype(np.uint8))


def _menu_geom(w: int, h: int, n: int = 12) -> dict:
    """Siatka kafli. Liczona raz i używana zarówno przy rysowaniu, jak
    i przy trafianiu myszą — inaczej klik rozjeżdża się z obrazem.
    Kafle rozciągamy tak, żeby wypełniły ekran bez pustego pasa u dołu."""
    pad = 10 if w < 900 else 16
    gap = 8 if w < 900 else 12
    # 3 kolumny, nie 4: kafel 254×126 zamiast 189×126 mieści większy tekst.
    # Etykiety 13 px miały ~1,9 mm wysokości wersalika — z fotela kierowcy
    # (ok. 70 cm) to poniżej progu czytelności dla wyświetlaczy w aucie.
    cols = 3 if w < 1100 else 4
    head = 46 if h < 560 else 58
    foot = 30 if h < 560 else 38
    wolne = h - head - foot - pad
    maks = max(1, wolne // ((96 if h < 560 else 118) + gap))
    # 05.10 (ekran dotykowy 3,5"): KAŻDY poziom menu musi zmieścić się na
    # JEDNEJ stronie — palcem nie ma jak przewinąć, a druga strona (przy
    # 11 kaflach i 3×3) była osiągalna wyłącznie strzałkami z klawiatury.
    # Gdy 3 kolumny nie mieszczą wszystkich kafli, bierzemy 4 (189×126 px
    # na 800×480 — wciąż ~18×12 mm na 3,5", palec trafia bez problemu).
    if n > cols * maks and cols < 4:
        cols = 4
    tw = (w - 2 * pad - (cols - 1) * gap) // cols
    # tyle wierszy, ile naprawdę trzeba — przy 6 pozycjach kafle robią się
    # wyższe zamiast zostawiać pusty rząd
    rows = max(1, min(maks, (n + cols - 1) // cols))
    th = (wolne - (rows - 1) * gap) // rows
    return {"pad": pad, "gap": gap, "cols": cols, "head": head, "foot": foot,
            "tw": tw, "th": th, "rows": rows, "per": cols * rows}


def _overlay_close_box(w: int, h: int) -> tuple:
    """Prostokąt przycisku powrotu w nagłówku nakładki (rysowanie i klik
    z jednego miejsca). Bez niego myszą nie dało się wyjść, bo pasek okienny
    przy otwartych ustawieniach jest ukryty.

    UWAGA: przycisk jest po LEWEJ. Po prawej stronie paska okiennego siedzi
    krzyżyk zamykający CAŁY PROGRAM — gdyby powrót był w tym samym rogu,
    drugie kliknięcie w to samo miejsce ubijałoby aplikację."""
    pad = 10 if w < 900 else 16
    head = 46 if h < 560 else 58
    r = 17                      # 05.10: 30 → 34 px — na 3,5" to ledwie 3 mm
    cx, cy = pad + r, head // 2
    return (cx - r, cy - r, cx + r, cy + r)


def _w_polu_powrotu(x: int, y: int, w: int, h: int) -> bool:
    """Czy dotknięcie trafiło w przycisk powrotu. Pole trafienia jest o 14 px
    szersze z każdej strony niż rysunek (05.10, ekran dotykowy 3,5": palec
    zasłania cel ~6 mm, a sam rysunek ma 3 mm). Po prawej nadal zostaje
    bezpieczny odstęp od tytułu."""
    x1, y1, x2, y2 = _overlay_close_box(w, h)
    return (x1 - 14) <= x < (x2 + 14) and 0 <= y < max(y2 + 14, 46)


def _draw_overlay_close(d, w: int, h: int) -> None:
    x1, y1, x2, y2 = _overlay_close_box(w, h)
    cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
    d.rectangle((x1, y1, x2, y2), outline=C_BORDER + (255,), width=1)
    d.line((cx + 6, cy - 6, cx - 4, cy), fill=C_TEXT + (255,), width=2)
    d.line((cx - 4, cy, cx + 6, cy + 6), fill=C_TEXT + (255,), width=2)
    d.line((cx - 4, cy, cx + 7, cy), fill=C_TEXT + (255,), width=2)


def _menu_tile_xy(g: dict, vi: int) -> tuple:
    row, col = divmod(vi, g["cols"])
    return (g["pad"] + col * (g["tw"] + g["gap"]),
            g["head"] + row * (g["th"] + g["gap"]))


_TILE_CACHE: dict = {}


def _menu_tile_shape(act: str, tw: int, th: int, stan: int):
    """Tło kafla z ikoną, gotowe do wklejenia. Nadpróbkowanie robimy tylko
    na jednym kaflu i wynik zapamiętujemy — inaczej każdy ruch myszy
    kosztowałby przerysowanie całego ekranu w potrójnej skali.
    stan: 0 zwykły, 1 pod kursorem, 2 wybrany, 3 wybrany w edycji."""
    klucz = (act, tw, th, stan)
    gotowy = _TILE_CACHE.get(klucz)
    if gotowy is not None:
        return gotowy
    # Pixel-art: kwadratowe rogi i twarda ramka. Żadnego zaokrąglania ani
    # nadpróbkowania — wygładzone krawędzie gryzły się z motywem VHS,
    # a przy okazji kafel powstaje teraz w skali 1:1, czyli 9× taniej.
    im = _PILImage.new("RGBA", (tw, th), (0, 0, 0, 0))
    d = _PILDraw.Draw(im)
    wybrany = stan >= 2
    akcent = C_DANGER if act == "close" else C_ACCENT
    d.rectangle((0, 0, tw - 1, th - 1),
                fill=(C_TILE_S if wybrany else C_TILE) + (245,),
                outline=(akcent if stan >= 1 else C_BORDER) + (255,),
                width=2 if stan >= 1 else 1)
    kolor = C_DANGER if act == "close" else (C_ACCENT if wybrany else C_TEXT)
    cy = int(th * 0.34)
    bok = max(_IKONA_SIATKA * 2, (int(th * 0.40) // _IKONA_SIATKA)
              * _IKONA_SIATKA)          # wielokrotność siatki = równe piksele
    ikona = _menu_ikona_pixel(act, bok, kolor + (255,))
    im.paste(ikona, (tw // 2 - bok // 2, cy - bok // 2), ikona)
    if stan == 3:
        # Pola − / + po bokach ikony. Bok _PM_R*2 = 52 px, bo docelowo ma to
        # być ekran dotykowy — palec potrzebuje celu ok. 48 px, a wcześniejsze
        # 24 px nadawało się wyłącznie pod mysz. Kwadraty zamiast kółek:
        # w pixel-arcie okrąg tej wielkości i tak wychodzi schodkowany.
        for sx, plus in ((-1, False), (1, True)):
            bx = tw // 2 + sx * (tw // 2 - _PM_R - 6)
            d.rectangle((bx - _PM_R, cy - _PM_R, bx + _PM_R, cy + _PM_R),
                        fill=C_ACCENT + (255,))
            d.rectangle((bx - 9, cy - 2, bx + 9, cy + 1),
                        fill=C_SCRIM + (255,))
            if plus:
                d.rectangle((bx - 2, cy - 9, bx + 1, cy + 9),
                            fill=C_SCRIM + (255,))
    if len(_TILE_CACHE) > 240:            # zmiana rozdzielczości okna
        _TILE_CACHE.clear()
    _TILE_CACHE[klucz] = im
    return im


def _menu_surface(w: int, h: int, items: list, sel: int, hover: int,
                  edit: bool, tytul: str = "Ustawienia",
                  stopka: str = "") -> tuple:
    """Buduje nakładkę menu → (RGB uint8, alpha float32 0..1).
    Funkcja modułowa (nie metoda), żeby dało się ją wywołać w teście
    bez uruchamiania całej aplikacji."""
    g = _menu_geom(w, h, len(items))
    per, page = g["per"], sel // g["per"]
    widoczne = items[page * per:(page + 1) * per]
    stron = max(1, (len(items) + per - 1) // per)

    shapes = _PILImage.new("RGBA", (w, h), (0, 0, 0, 0))
    for vi, (label, val, act, _ic) in enumerate(widoczne):
        i = page * per + vi
        stan = (3 if (edit and act in _MENU_ADJUST) else 2) if i == sel \
            else (1 if i == hover else 0)
        kafel = _menu_tile_shape(act, g["tw"], g["th"], stan)
        shapes.paste(kafel, _menu_tile_xy(g, vi), kafel)

    # tekst rysujemy w docelowej skali (Pillow i tak go wygładza) — dzięki
    # temu jest ostry, a nie rozmyty zmniejszaniem
    d = _PILDraw.Draw(shapes)
    # Tytuł blokową Press Start 2P (jak nagłówki na stronie), reszta VT323 —
    # ten krój jest bardzo szeroki, więc nadaje się WYŁĄCZNIE na krótki napis.
    f_head = _ui_font_head(13 if h < 560 else 15)
    f_hint = _ui_font(12 if h < 560 else 14)
    f_lab = _ui_font(17 if h < 560 else 19)
    f_val = _ui_font(19 if h < 560 else 21, bold=True)
    d.text((_overlay_close_box(w, h)[2] + 10, g["head"] // 2), tytul,
           font=f_head, fill=C_TEXT + (255,), anchor="lm")
    d.text((w - g["pad"] - 2, g["head"] // 2),
           "dotknij kafel — zmień   ·   przycisk wstecz / Esc — zamknij" if not edit
           else "− / + lub strzałki — zmieniaj   ·   Enter — gotowe",
           font=f_hint, fill=C_MUTED + (255,), anchor="rm")
    _draw_overlay_close(d, w, h)

    for vi, (label, val, act, _ic) in enumerate(widoczne):
        i = page * per + vi
        x, y = _menu_tile_xy(g, vi)
        wybrany = (i == sel)
        cx = x + g["tw"] // 2
        kolor = C_DANGER if act == "close" else C_TEXT
        # etykieta łamana na dwie linie, żeby nie wychodziła poza kafel
        slowa, linie, biez = label.split(), [], ""
        for sl in slowa:
            probny = (biez + " " + sl).strip()
            if d.textlength(probny, font=f_lab) <= g["tw"] - 12:
                biez = probny
            else:
                linie.append(biez)
                biez = sl
        linie.append(biez)
        linie = [l for l in linie if l][:2]
        ly = y + int(g["th"] * 0.60)
        for l in linie:
            d.text((cx, ly), l, font=f_lab, fill=kolor + (255,), anchor="mm")
            ly += 15 if h < 560 else 17
        if val:
            d.text((cx, y + g["th"] - (13 if h < 560 else 16)), val[:16],
                   font=f_val,
                   fill=((C_ACCENT if wybrany else C_VALUE) + (255,)),
                   anchor="mm")

    # 3) stopka: skróty klawiszowe (nigdzie indziej nie są napisane).
    # Kropek stron już nie ma — oba poziomy menu mieszczą się na jednym
    # ekranie, a wcześniej i tak nie dało się nimi przewijać myszą.
    if stopka:
        # 05.10: nieprzezroczysty pas pod stopką — dolny pasek z klatki
        # („Sledzenie / Zapisane") przebijał przez półprzezroczystą nakładkę
        # i nakładał się na podpowiedź (na 3,5" nie dało się tego przeczytać)
        d.rectangle((0, h - g["foot"] - 2, w, h), fill=C_SCRIM + (255,))
        d.text((w // 2, h - g["foot"] // 2), stopka, font=f_hint,
               fill=C_MUTED + (255,), anchor="mm")

    rgba = np.array(shapes, dtype=np.uint8)
    alpha = rgba[:, :, 3:4].astype(np.float32) / 255.0
    rgb = rgba[:, :, 2::-1].astype(np.float32)    # RGB → BGR pod OpenCV
    # przyciemnienie obrazu z kamery pod całą nakładką
    scrim = np.array(C_SCRIM[::-1], dtype=np.float32)
    tlo_a = 0.82
    krycie = alpha + (1.0 - alpha) * tlo_a
    # Warstwa z góry przemnożona przez krycie — złożenie klatki to wtedy
    # jedno mnożenie i jedno dodawanie. Oba w uint8, żeby poszły przez
    # SIMD OpenCV; float32 na 800×480 kosztował trzy razy więcej.
    pre = rgb * alpha + scrim * (1.0 - alpha) * tlo_a
    inv = np.repeat((1.0 - krycie) * 255.0, 3, axis=2)
    return (np.clip(pre, 0, 255).astype(np.uint8),
            np.clip(inv, 0, 255).astype(np.uint8))


# pozycje, które mają wartość do zmieniania (reszta to przełączniki/akcje)
_MENU_ADJUST = {"zoom", "ev", "ae", "met", "det", "ocr", "reads", "scr_off"}
# promień kółek − / +; 52 px średnicy to minimum, w które trafia palec
_PM_R = 26
# akcje nieodwracalne — wymagają drugiego dotknięcia („na pewno?")
_MENU_CONFIRM = {"close"}


def _bt_geom(w: int, h: int) -> dict:
    """Lista urządzeń Bluetooth — geometria wspólna dla rysowania i myszy."""
    pad = 10 if w < 900 else 16
    head = 46 if h < 560 else 58
    foot = 30 if h < 560 else 38
    rh = 54 if h < 560 else 64
    gap = 6
    wolne = h - head - foot
    return {"pad": pad, "head": head, "foot": foot, "rh": rh, "gap": gap,
            "rw": w - 2 * pad, "rows": max(1, wolne // (rh + gap))}


def _bt_row_y(g: dict, vi: int) -> int:
    return g["head"] + vi * (g["rh"] + g["gap"])


def _bt_rows(devs: list) -> list:
    """Wiersze panelu. Pierwszy zawsze = widoczność dla auta (połączenie
    w drugą stronę: to zestaw samochodowy szuka malinki)."""
    left = _bt_vis_left()
    return [{"kind": "vis", "on": left > 0,
             "name": "Widoczny dla auta",
             "sub": "szukaj „%s” w Bluetooth samochodu" % BT_ALIAS,
             "stan": ("jeszcze %d:%02d" % (int(left) // 60, int(left) % 60))
                     if left > 0 else "wyłączone"}] \
        + [dict(d, kind="dev") for d in devs]


def _bt_surface(w: int, h: int, devs: list, sel: int, status: str,
                skan: bool) -> tuple:
    """Nakładka wyboru głośnika. Ten sam język graficzny co menu, ale lista
    zamiast siatki — nazwy urządzeń bywają długie, w kaflu by się nie
    zmieściły."""
    g = _bt_geom(w, h)
    wiersze = _bt_rows(devs)
    top = max(0, min(sel - g["rows"] + 1, max(0, len(wiersze) - g["rows"]))) \
        if sel >= g["rows"] else 0
    widoczne = wiersze[top:top + g["rows"]]

    im = _PILImage.new("RGBA", (w, h), (0, 0, 0, 0))
    S = _MS
    ksz = _PILImage.new("RGBA", (w * S, h * S), (0, 0, 0, 0))
    dk = _PILDraw.Draw(ksz)
    for vi, d in enumerate(widoczne):
        i = top + vi
        y = _bt_row_y(g, vi)
        wybrany = (i == sel)
        dk.rectangle(
            (g["pad"] * S, y * S, (g["pad"] + g["rw"]) * S,
             (y + g["rh"]) * S),
            fill=(C_TILE_S if wybrany else C_TILE) + (245,),
            outline=(C_ACCENT + (255,)) if wybrany else C_BORDER + (255,),
            width=(3 if wybrany else 1) * S)
        wid = d.get("kind") == "vis"
        kolor = ((74, 222, 128) if (wid and d.get("on"))
                 else C_ACCENT if wybrany else C_TEXT) + (255,)
        _menu_icon(dk, "widoczny" if wid else "bt",
                   (g["pad"] + 34) * S, (y + g["rh"] // 2) * S,
                   14 * S, kolor, 2 * S)
        if d.get("connected"):            # zielona kropka = w użyciu
            dk.ellipse(((g["pad"] + 52) * S, (y + g["rh"] // 2 - 16) * S,
                        (g["pad"] + 62) * S, (y + g["rh"] // 2 - 6) * S),
                       fill=(74, 222, 128, 255))
    ksz = ksz.resize((w, h), _PILImage.LANCZOS)
    im.alpha_composite(ksz)

    d = _PILDraw.Draw(im)
    f_head = _ui_font(20 if h < 560 else 24, bold=True)
    f_hint = _ui_font(12 if h < 560 else 14)
    f_nm = _ui_font(15 if h < 560 else 17, bold=True)
    f_sub = _ui_font(12 if h < 560 else 14)
    d.text((_overlay_close_box(w, h)[2] + 10, g["head"] // 2),
           "Głośnik Bluetooth", font=f_head, fill=C_TEXT + (255,), anchor="lm")
    d.text((w - g["pad"] - 2, g["head"] // 2),
           "dotknij 2× — użyj  ·  dotknij dół — szukaj  ·  wstecz / Esc",
           font=f_hint, fill=C_MUTED + (255,), anchor="rm")
    _draw_overlay_close(d, w, h)

    for vi, dev in enumerate(widoczne):
        y = _bt_row_y(g, vi)
        x = g["pad"] + 74
        wid = dev.get("kind") == "vis"
        nazwa = (dev.get("name") or dev.get("mac") or "?")[:34]
        d.text((x, y + g["rh"] // 2 - 9), nazwa, font=f_nm,
               fill=C_TEXT + (255,), anchor="lm")
        opis = dev.get("sub", "") if wid else dev.get("mac", "")
        if not wid and dev.get("battery") is not None:
            opis += "   %d%%" % dev["battery"]
        d.text((x, y + g["rh"] // 2 + 11), opis, font=f_sub,
               fill=C_MUTED + (255,), anchor="lm")
        etykieta = (dev.get("stan", "") if wid
                    else "połączony" if dev.get("connected")
                    else "sparowany" if dev.get("paired") else "nowy")
        d.text((g["pad"] + g["rw"] - 14, y + g["rh"] // 2), etykieta,
               font=f_sub, anchor="rm",
               fill=((74, 222, 128) if (dev.get("connected") or
                                        (wid and dev.get("on")))
                     else C_VALUE if dev.get("paired") else C_MUTED) + (255,))

    if len(widoczne) <= 1:            # tylko wiersz widoczności = brak urządzeń
        yp = g["head"] + int(g["rh"] * 1.6) + 30
        d.text((w // 2, yp),
               "Szukam urządzeń…" if skan else "Brak urządzeń w pobliżu",
               font=f_nm, fill=C_TEXT + (255,), anchor="mm")
        d.text((w // 2, yp + 26),
               "Głośnik: włącz mu parowanie i naciśnij S", font=f_sub,
               fill=C_MUTED + (255,), anchor="mm")
        d.text((w // 2, yp + 48),
               "Zestaw w aucie: włącz „Widoczny dla auta” u góry",
               font=f_sub, fill=C_MUTED + (255,), anchor="mm")
    if status:
        d.text((w // 2, h - g["foot"] // 2), status, font=f_sub,
               fill=C_ACCENT + (255,), anchor="mm")

    rgba = np.array(im, dtype=np.uint8)
    alpha = rgba[:, :, 3:4].astype(np.float32) / 255.0
    rgb = rgba[:, :, 2::-1].astype(np.float32)
    scrim = np.array(C_SCRIM[::-1], dtype=np.float32)
    tlo_a = 0.88
    krycie = alpha + (1.0 - alpha) * tlo_a
    pre = rgb * alpha + scrim * (1.0 - alpha) * tlo_a
    inv = np.repeat((1.0 - krycie) * 255.0, 3, axis=2)
    return (np.clip(pre, 0, 255).astype(np.uint8),
            np.clip(inv, 0, 255).astype(np.uint8))


class LprApp(GStreamerDetectionApp):
    """GStreamer pipeline z fakesink — podgląd przez GLib.timeout_add na głównym wątku."""

    def __init__(self, callback, user_data):
        self._headless = not _has_display()
        if self._headless:
            print("[LPR] Brak wyświetlacza — tryb headless (bez okna podglądu)")
        super().__init__(callback, user_data)
        user_data.use_frame = True   # callback wołuje set_frame()
        # options_menu.use_frame=False → framework NIE startuje multiprocessing display_process
        # (forked cv2 GUI = problem z GTK/Wayland po inicjalizacji w parensie)
        self._user_data = user_data
        _diag_ref["app"] = self          # dostep do potoku dla _diag_kolejki_potoku
        self._disp_sw = 0
        self._disp_sh = 0
        self._mouse_xy = (-1, -1)
        self._pending_action: Optional[str] = None
        self._is_fullscreen = True
        # wygaszanie ekranu (05.10): ostatnia aktywność (dotyk/klawisz/
        # tablica w kadrze), stan, blokada na dotknięcie, które obudziło
        self._ekran_wyl = False
        self._ekran_aktywnosc = time.monotonic()
        self._ekran_dotyk_t = 0.0        # ustawia _dotyk_watek (evdev)
        self._ekran_wyl_t = 0.0
        self._ekran_laska_do = 0.0       # po obudzeniu: ignoruj dotyk 0,8 s
        if not os.environ.get("LPR_HEADLESS"):
            threading.Thread(target=_dotyk_watek, args=(self,), daemon=True,
                             name="dotyk-budzik").start()
        # menu lokalne (ekran + klawiatura w aucie)
        self._menu_open = False
        self._menu_sel = 0
        self._menu_edit = False     # tryb zmiany wartości (strzałki ← →)
        self._menu_cache: Optional[tuple] = None   # (klucz, RGB, alpha)
        self._bt_panel: Optional[dict] = None      # wybierak głośnika
        self._bt_surf: Optional[tuple] = None      # cache nakładki wybieraka
        self._menu_view = "main"                   # "main" | "tools"
        self._disp_skip = 0                        # licznik pomijanych klatek
        self._boot_done = False    # czy przyszła choć jedna klatka z kamery
        # 09.10: płynne przejścia. Splash → podgląd: przenikanie 0,8 s od
        # pierwszej klatki (obraz splasha z cache ekranu stanu). Nakładki:
        # alfa 0..1 dochodzi do celu w 150 ms; przy znikaniu mieszamy z
        # ostatnią skomponowaną klatką z nakładką.
        self._fade_boot_t0 = 0.0
        self._fade_boot_img = None
        self._nakl_a = 0.0
        self._nakl_t = 0.0
        self._nakl_ostatnia = None
        self._last_frame_t = 0.0   # kiedy ostatnio (do wykrycia utraty obrazu)
        self._boot_cache = None    # (klucz, obraz) ekranu statusu
        self._img_panel = False                    # ekran „Obraz"/„Dźwięk"
        self._img_mode = "image"
        self._img_sel = 0
        self._img_surf: Optional[tuple] = None
        self._menu_confirm: Optional[str] = None   # akcja czekająca na drugie
        self._menu_confirm_until = 0.0             # dotknięcie
        self._menu_click: Optional[tuple] = None   # (idx, kierunek)
        self._tb_drawn_at = 0.0     # kiedy pasek okienny był ostatnio narysowany (dotyk)
        self._disp_zoom = 1
        self._okno_gotowe = False   # 09.10: okno tworzone leniwie w _display_tick
        self._okno_tiki = 0

    def _okno_utworz(self) -> None:
        """Okno lpr + pierwsza klatka ekranu stanu; po kilku tikach prosimy
        proces splasha o zakończenie (jego okno jest pod naszym)."""
        self._disp_sw, self._disp_sh = _get_screen_size()
        cv2.namedWindow("LPR", cv2.WINDOW_NORMAL)
        cv2.setWindowProperty("LPR", cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)
        cv2.setMouseCallback("LPR", self._on_mouse)
        cv2.imshow("LPR", _boot_frame(self._disp_sw or 800, self._disp_sh or 480,
                                      _get_boot(), time.monotonic()))
        cv2.waitKey(1)
        self._okno_gotowe = True

    def run(self):
        t = threading.Thread(target=_stats_worker, daemon=True, name="lpr-stats")
        t.start()
        if not self._headless:
            # Okno MUSI powstać tutaj, przed super().run(): próba tworzenia
            # go w tiku GLib (09.10) zawieszała główny wątek (OpenCV z Qt),
            # okno zostawało szare, a watchdog ubijał usługę. Przez ~1–2 s
            # budowy potoku okno stoi z jedną klatką; wcześniej (import)
            # animuje proces lpr_splash.py, a od pierwszego tiku my.
            self._okno_utworz()
            from gi.repository import GLib as _GLib
            # 20 fps podglądu: rozpoznawanie ma priorytet, oko różnicy nie widzi,
            # a resize+imshow 1080p to zauważalny koszt przy 30 fps
            # 25 ms = 40 Hz ODPYTYWANIA, nie 40 fps rysowania: tik bez
            # nowej klatki konczy sie natychmiast (get_frame -> None).
            # Potok dowozi 30 fps; przy poprzednich 50 ms ekran malowal
            # 20 razy na sekunde, wiec co trzecia klatka szla do kosza,
            # a pokazywane mialy raz 1, raz 2 klatki opoznienia - i to
            # wlasnie widac jako szarpanie, mimo ze licznik fps pokazuje
            # 30. Odpytywanie szybciej niz zrodlo = kazda klatka trafia
            # na ekran w <=25 ms, rowno.
            _GLib.timeout_add(25, self._display_tick)
            _GLib.timeout_add(25, self._key_tick)
        super().run()

    # ── Pasek okienny (pojawia się po najechaniu myszką na górę ekranu) ──────

    _TB_H  = 46          # wysokość paska
    _TB_BW = 84          # szerokość przycisku

    # geometrię menu liczy _menu_geom() — jedno źródło dla rysowania i klikania

    def _on_mouse(self, event, x, y, flags, param):
        teraz = time.monotonic()
        self._ekran_aktywnosc = teraz
        if self._ekran_wyl or teraz < self._ekran_laska_do:
            # zgaszony ekran: dotknięcie TYLKO budzi (nic nie klika na
            # ślepo); przez 0,8 s po obudzeniu też ignorujemy — ten sam
            # palec mógł już obudzić przez evdev
            if self._ekran_wyl and event in (cv2.EVENT_LBUTTONDOWN,
                                             cv2.EVENT_LBUTTONUP):
                self._ekran_dotyk_t = teraz
            return
        self._mouse_xy = (x, y)
        if event not in (cv2.EVENT_LBUTTONUP, cv2.EVENT_RBUTTONUP):
            return
        # 09.10: klik jak w telefonie — przy dotknięciu nakładki (menu,
        # Obraz/Dźwięk, Bluetooth) albo górnego pasa; „back" niżej dla
        # przycisku wstecz. Zwykłe dotknięcie podglądu jest ciche.
        if event == cv2.EVENT_LBUTTONUP:
            _nakl = (self._menu_open or self._img_panel
                     or self._bt_panel is not None)
            _dzwiek_dotyku("back" if (_nakl and _w_polu_powrotu(
                x, y, self._disp_sw or 1920, self._disp_sh or 1080))
                           else "tap")
        # ekran „Obraz": dotknięcie działa od razu — chip wybiera pokrętło,
        # duże − / + zmieniają wartość, a obraz pod spodem jest żywy
        if self._img_panel:
            w = self._disp_sw or 1920
            h = self._disp_sh or 1080
            if _w_polu_powrotu(x, y, w, h):
                self._img_panel = False
                self._img_surf = None
                self._menu_open = True
                return
            rodzaj, idx = self._img_hit(x, y, w, h)
            if rodzaj == "chip":
                if self._img_mode == "sound" and idx == self._img_sel:
                    k = self._snd_def()[idx][2][4:]   # drugi klik = próbka
                    SPEECH.say(self._PROBKI.get(k, "Test głosu."), k)
                self._img_sel = idx
            elif rodzaj in ("minus", "plus"):
                self._menu_click = (-1, 1 if rodzaj == "plus" else -1)
            return
        # klik w wybieraku głośnika: pierwszy zaznacza, drugi łączy
        if self._bt_panel is not None:
            w = self._disp_sw or 1920
            h = self._disp_sh or 1080
            if _w_polu_powrotu(x, y, w, h):
                self._bt_panel = None
                self._bt_surf = None
                return
            idx = self._bt_hit(x, y, w, h)
            if idx >= 0:
                if idx != self._bt_panel["sel"]:
                    self._bt_panel["sel"] = idx
                else:
                    self._bt_use_selected()
            elif y >= h - _bt_geom(w, h)["foot"] - 8:
                # 05.10 (dotyk): „szukaj" było tylko pod klawiszem S —
                # dotknięcie dolnego pasa (linia statusu) ponawia skanowanie
                p = self._bt_panel
                if not p["busy"] and not p["scanning"]:
                    _bt_start_scan(12)
                    p["scanning"] = True
                    p["status"] = "Szukam urządzeń…"
            elif y >= self._TB_H:
                self._bt_panel = None
                self._bt_surf = None
            return
        # klik w otwarte menu: lewy = wybór/zmiana, prawy = wartość w dół
        if self._menu_open:
            w = self._disp_sw or 1920
            h = self._disp_sh or 1080
            if _w_polu_powrotu(x, y, w, h):
                if self._menu_view == "tools":    # z narzędzi wracamy wyżej
                    self._menu_view = "main"
                    self._menu_sel = 0
                else:
                    self._menu_open = False
                self._menu_edit = False
                return
            idx = self._menu_hit(x, y, w, h)
            if idx >= 0:
                akcja = self._menu_def()[idx][2]
                self._menu_sel = idx
                if akcja in _MENU_ADJUST:
                    # Docelowo ekran dotykowy: jedno dotknięcie ma działać,
                    # a nie tylko zaznaczać. Na kaflu z wartością lewa połowa
                    # to −, prawa +, a kafel od razu wchodzi w tryb edycji,
                    # żeby było widać duże kółka − / +.
                    g = _menu_geom(w, h, len(self._menu_def()))
                    tx, _ty = _menu_tile_xy(g, idx % g["per"])
                    srodek = tx + g["tw"] // 2
                    self._menu_edit = True
                    self._menu_click = (
                        idx, -1 if (x < srodek
                                    or event == cv2.EVENT_RBUTTONUP) else 1)
                else:
                    self._menu_edit = False
                    self._menu_click = (idx, 1)
                return
            # Klik poza kaflami zamyka menu. WAŻNE: bezwarunkowy return —
            # bez niego klik w pas nagłówka spadał niżej, do obsługi paska
            # okiennego, który przy otwartym menu jest NIEWIDOCZNY. Trafienie
            # w prawy górny róg ubijało wtedy cały program.
            self._menu_open = False
            self._menu_edit = False
            return
        if event != cv2.EVENT_LBUTTONUP:
            return
        # 09.10 (kiosk): KAŻDE dotknięcie podglądu otwiera menu — bez ukrytego
        # paska okiennego i bez przycisków minimalizuj/zamknij w rogu
        # (zamknięcie zostało w Narzędziach, z potwierdzeniem).
        self._menu_open = True
        self._menu_edit = False

    def _apply_action(self):
        act, self._pending_action = self._pending_action, None
        if act == "close":
            print("[LPR] Zamknięcie z paska — czyste wyjście")
            os.kill(os.getpid(), signal.SIGINT)      # exit 0 → systemd NIE restartuje
        elif act == "restore":
            self._is_fullscreen = not self._is_fullscreen
            if self._is_fullscreen:
                cv2.setWindowProperty("LPR", cv2.WND_PROP_FULLSCREEN,
                                      cv2.WINDOW_FULLSCREEN)
            else:
                cv2.setWindowProperty("LPR", cv2.WND_PROP_FULLSCREEN,
                                      cv2.WINDOW_NORMAL)
                # NIE na sztywno 1280×720: na ekranie 800×480 okno wychodziło
                # poza kadr razem z kaflem, którym dało się to cofnąć.
                cv2.resizeWindow("LPR", max(480, (self._disp_sw or 1280) - 80),
                                 max(320, (self._disp_sh or 720) - 80))
        elif act == "min":
            import subprocess as _sp
            try:                                     # wymaga xdotool
                _sp.Popen(["xdotool", "search", "--name", "^LPR$",
                           "windowminimize"],
                          stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
            except FileNotFoundError:
                print("[LPR] brak xdotool — minimalizacja niedostępna")

    # ── Menu lokalne (ekran + klawiatura) ────────────────────────────────────
    # ASCII w etykietach: cv2.putText nie umie polskich ogonkow

    def _menu_def(self) -> list:
        """(etykieta, wartość, akcja, ikona). Polskie znaki są OK — menu
        rysuje Pillow, nie cv2.

        Dwa poziomy: ekran główny to rzeczy używane w aucie w trakcie jazdy,
        a rzadkie i ryzykowne pokrętła siedzą w „Narzędziach". Każdy poziom
        mieści się na JEDNEJ stronie — przewijanie palcem po ekranie kierowcy
        to proszenie się o kłopoty."""
        csi = CAM_SOURCE == "csi"
        s = CSI_CAM_SETTINGS
        # UWAGA: `x and y` zwraca y, więc wersja z `and` dawała True zamiast
        # nazwy akcji i kafel nigdy nie pokazywał „na pewno?".
        pot = (self._menu_confirm
               if time.monotonic() < self._menu_confirm_until else None)

        if self._menu_view == "tools":
            ae_lbl = {"short": "krótkie czasy", "normal": "normalny",
                      "long": "długie czasy"}
            met_lbl = {"centre": "centralny", "spot": "punktowy",
                       "matrix": "matrycowy"}
            return [
                ("Próg detekcji",    f"{PLATE_MIN_CONF:.2f}",         "det", ""),
                ("Próg OCR",         f"{OCR_MIN_CONF:.2f}",           "ocr", ""),
                ("Min. zgodnych odczytów", str(OCR_MIN_MATCHING),     "reads", ""),
                ("Pasek statystyk",  "włączony" if HUD_ON
                                     else "wyłączony",                "hud", ""),
                ("Pełny ekran",      "tak" if self._is_fullscreen
                                     else "nie",                      "full", ""),
                ("Kopia zapasowa",   ("trwa…" if _photo_stat["busy"]
                                      else (_photo_stat["when"][-5:]
                                            if _photo_stat["when"]
                                            else "wykonaj")),         "backup", ""),
                ("OCR na",           ("Hailo" if getattr(_diag_ref.get("ocr"), "backend", "cpu") == "hailo"
                                      else "procesor"),                 "ocr_backend", ""),
                ("Zamknij program",  "na pewno?" if pot == "close"
                                     else "wyjście",                  "close", ""),
            ]

        return [
            # obraz — wszystkie ustawienia kamery pod jednym kaflem,
            # bo stroi się je patrząc na podgląd, a nie na listę
            ("Obraz",               ("%+.1f EV · %d×" % (s["ev"], self._disp_zoom))
                                    if csi else f"{self._disp_zoom}×",    "image", ""),
            ("Dźwięk",              "%.1f · %.1f · %.1f" % (
                                    TTS_GAINS["alert"], TTS_GAINS["plate"],
                                    TTS_GAINS["system"]),                 "sound", ""),
            ("Zrób zdjęcie",        "3 klatki",                           "snap", ""),
            # tryb fotoradaru (25.09): przełącznik + próg strzałkami
            ("Radar",               ("WŁĄCZONY" if RADAR_CFG["tryb"]
                                     else "wyłączony")
                                    + ("" if _radar_stan["ok"] else " · brak portu"),
                                                                          "radar", ""),
            ("Próg radaru",         "%.0f km/h" % RADAR_CFG["prog_kmh"],  "radar_prog", ""),
            # dźwięk
            ("Głośnik Bluetooth",   self._bt_label(),                     "bt", ""),
            ("Czytaj każdą tablicę", "włączone" if TTS_ALL
                                     else "wyłączone",                    "ttsall", ""),
            ("Czytaj markę pojazdu", "włączone" if TTS_BRAND
                                     else "wyłączone",                    "ttsbrand", ""),
            # dane
            ("Pendrive",            ("WYJĘTY" if _usb_state == "gone" else
                                     "przygotowuję…" if _usb_busy.is_set()
                                     else "można wyjąć"
                                     if not _usb_ready.is_set()
                                     else "zapis działa"),                "usb", ""),
            # ekran (05.10): wygaszanie po bezczynności + zgaszenie od razu
            # (noc w aucie) — „Kopia zapasowa" przeniesiona do Narzędzi,
            # żeby 12 kafli weszło na jedną stronę 4×3
            ("Wygaszanie ekranu",   ("nigdy" if SCREEN_OFF_MIN <= 0
                                     else "po %d min" % SCREEN_OFF_MIN),   "scr_off", ""),
            ("Wyłącz ekran",        "budzi dotyk",                        "scr_now", ""),
            ("Narzędzia",           "zaawansowane",                       "tools", ""),
        ]

    def _bt_label(self) -> str:
        """Tylko odczyt pamięci podręcznej — wołane z wątku rysowania."""
        mac, nm = _bt_live_get()
        return nm[:18] if mac else "wybierz…"

    # ── Wybierak głośnika Bluetooth (ekran w aucie) ─────────────────────────

    def _bt_open_panel(self) -> None:
        """Enter na kaflu Bluetooth: lista urządzeń zamiast cichej próby
        połączenia. Wcześniej przy zerze sparowanych urządzeń akcja kończyła
        się komunikatem, który nigdzie nie trafiał — z ekranu wyglądało to
        jak martwy przycisk."""
        self._menu_open = False
        self._menu_edit = False
        self._bt_panel = {"sel": 0, "devs": [], "busy": False,
                          "scanning": True, "status": "Szukam urządzeń…"}
        _bt_start_scan(12)
        threading.Thread(target=self._bt_poll, daemon=True,
                         name="bt-panel").start()

    def _bt_poll(self) -> None:
        """Odświeżanie listy w tle. bluetoothctl potrafi mielić kilkaset ms,
        a to jest wątek pętli GLib — w niej nie wolno się zatrzymywać."""
        while self._bt_panel is not None:
            try:
                st = _bt_status()
                p = self._bt_panel
                if p is None:
                    break
                p["devs"] = st.get("devices", [])
                p["scanning"] = bool(st.get("scanning"))
                if not p["busy"]:
                    p["status"] = ("Szukam urządzeń…" if p["scanning"]
                                   else ("%d urządzeń" % len(p["devs"])
                                         if p["devs"] else ""))
            except Exception as e:
                print(f"[BT] panel: {e}")
            time.sleep(1.5)

    def _bt_use_selected(self) -> None:
        p = self._bt_panel
        if not p or p["busy"]:
            return
        if p["sel"] == 0:                # wiersz 0 = widoczność dla auta
            p["busy"] = True
            wlacz = _bt_vis_left() <= 0
            p["status"] = ("Włączam widoczność…" if wlacz
                           else "Wyłączam widoczność…")

            def widocznosc():
                try:
                    if wlacz:
                        ok = _bt_set_visible(300)
                        p["status"] = (
                            "Szukaj „%s” w Bluetooth samochodu (5 min)"
                            % BT_ALIAS) if ok else "Nie udało się"
                    else:
                        _bt_set_hidden()
                        p["status"] = "Malinka znów ukryta"
                finally:
                    p["busy"] = False
            threading.Thread(target=widocznosc, daemon=True,
                             name="bt-vis").start()
            return
        if not p["devs"]:
            return
        dev = dict(p["devs"][min(p["sel"] - 1, len(p["devs"]) - 1)])
        p["busy"] = True
        nazwa = dev.get("name") or dev.get("mac", "")
        p["status"] = ("Rozłączam…" if dev.get("connected")
                       else f"Łączę z {nazwa}…")

        def work():
            mac = dev.get("mac", "")
            try:
                if dev.get("connected"):
                    _bt_run(["disconnect", mac], timeout=15)
                    p["status"] = f"Rozłączono {nazwa}"
                else:
                    if not dev.get("paired"):
                        p["status"] = f"Paruję {nazwa}…"
                        _bt_run(["agent", "NoInputNoOutput"], timeout=5)
                        _bt_run(["pair", mac], timeout=30)
                        _bt_run(["trust", mac], timeout=8)
                    if _bt_connect(mac):     # zapamiętuje MAC w konfiguracji
                        _audio_bt_sink(mac)
                        p["status"] = f"Połączono: {nazwa}"
                        SPEECH.say("Głośnik połączony.")
                    else:
                        p["status"] = (f"Nie udało się połączyć z {nazwa} — "
                                       "włącz go i spróbuj ponownie")
            except Exception as e:
                p["status"] = f"Błąd: {e}"
                print(f"[BT] łączenie {mac}: {e}")
            finally:
                _bt_live_refresh()
                p["busy"] = False
        threading.Thread(target=work, daemon=True, name="bt-connect").start()

    def _bt_hit(self, x: int, y: int, w: int, h: int) -> int:
        p = self._bt_panel
        if not p:
            return -1
        g = _bt_geom(w, h)
        n = len(p["devs"]) + 1              # +1 = wiersz widoczności dla auta
        top = max(0, min(p["sel"] - g["rows"] + 1, max(0, n - g["rows"]))) \
            if p["sel"] >= g["rows"] else 0
        for vi in range(min(g["rows"], n - top)):
            ry = _bt_row_y(g, vi)
            if (g["pad"] <= x < g["pad"] + g["rw"]
                    and ry <= y < ry + g["rh"]):
                return top + vi
        return -1

    def _draw_bt_panel(self, frame) -> None:
        h, w = frame.shape[:2]
        p = self._bt_panel
        devs = p["devs"]
        if devs:
            p["sel"] = max(0, min(p["sel"], len(devs) - 1))
        klucz = (w, h, p["sel"], p["status"], p["scanning"],
                 int(_bt_vis_left()),      # inaczej odliczanie by stało
                 tuple((d.get("mac"), d.get("name"), d.get("paired"),
                        d.get("connected"), d.get("battery")) for d in devs))
        if self._bt_surf is None or self._bt_surf[0] != klucz:
            pre, inv = _bt_surface(w, h, devs, p["sel"], p["status"],
                                   p["scanning"])
            self._bt_surf = (klucz, pre, inv)
        _, pre, inv = self._bt_surf
        cv2.add(cv2.multiply(frame, inv, scale=1.0 / 255.0), pre, dst=frame)

    def _menu_act(self, act: str, d: int = 1) -> None:
        global PLATE_MIN_CONF, OCR_MIN_CONF, OCR_MIN_MATCHING, TTS_ALL
        global TTS_BRAND
        csi = CAM_SOURCE == "csi"
        if act == "zoom":
            order = (1, 2, 3)
            i = order.index(self._disp_zoom)
            self._disp_zoom = order[(i + d) % len(order)]
        elif act == "ev" and csi:
            # Zakres ±8 EV, nie ±4: libcamera tyle przyjmuje, a użytkownik
            # potrzebuje mocniejszego zjazdu w dół — nocą tablice odbijają
            # światła i przy -4 potrafią być nadal prześwietlone.
            CSI_CAM_SETTINGS["ev"] = max(-8.0, min(8.0,
                CSI_CAM_SETTINGS["ev"] + 0.5 * d))
            _csi_apply_live()
            _save_runtime_config()
        elif act == "ae" and csi:
            order = ("short", "normal", "long")
            i = order.index(CSI_CAM_SETTINGS.get("ae_mode", "short"))
            CSI_CAM_SETTINGS["ae_mode"] = order[(i + d) % len(order)]
            _csi_apply_live()
            _save_runtime_config()
        elif act == "met" and csi:
            order = ("centre", "spot", "matrix")
            i = order.index(CSI_CAM_SETTINGS.get("metering", "centre"))
            CSI_CAM_SETTINGS["metering"] = order[(i + d) % len(order)]
            _csi_apply_live()
            _save_runtime_config()
        elif act == "aeplate":
            globals()["AE_PLATE_ON"] = not AE_PLATE_ON
            if not AE_PLATE_ON:
                _ae_plate_reset()        # od razu oddaj kamerę automatyce
            _save_runtime_config()
        elif act == "notif":
            # skok 0.1 w zakresie 0.5–2.0 + podgląd, żeby OD RAZU było widać
            # ustawianą wielkość (bez czekania na kolejną rozpoznaną tablicę)
            globals()["NOTIF_SCALE"] = round(max(0.5, min(2.0,
                                             NOTIF_SCALE + 0.1 * d)), 1)
            globals()["_notif_podglad_do"] = time.monotonic() + 3.0
            _save_runtime_config()
        elif act == "mode" and csi:
            # przełącz auto <-> ręczny; panel przebuduje pokrętła (inny klucz)
            CSI_CAM_SETTINGS["manual"] = not bool(CSI_CAM_SETTINGS.get("manual"))
            _csi_apply_live()
            _save_runtime_config()
        elif act == "exp" and csi:
            # migawka po „stopach" (multiplikatywnie), nie liniowo co 100 µs
            steps = [100, 150, 200, 300, 500, 750, 1000, 1500, 2000, 3000,
                     4000, 6000, 8000, 12000, 16000, 24000, 33000]
            cur = int(CSI_CAM_SETTINGS.get("exposure_us", 4000))
            i = min(range(len(steps)), key=lambda j: abs(steps[j] - cur))
            CSI_CAM_SETTINGS["exposure_us"] = steps[max(0, min(len(steps) - 1,
                                                               i + d))]
            _csi_apply_live()
            _save_runtime_config()
        elif act == "gn" and csi:
            CSI_CAM_SETTINGS["gain"] = round(max(1.0, min(16.0,
                CSI_CAM_SETTINGS.get("gain", 4.0) + 0.5 * d)), 1)
            _csi_apply_live()
            _save_runtime_config()
        elif act == "det":
            PLATE_MIN_CONF = round(max(0.10, min(0.95,
                PLATE_MIN_CONF + 0.05 * d)), 2)
            _save_runtime_config()
        elif act == "ocr":
            OCR_MIN_CONF = round(max(0.05, min(0.95,
                OCR_MIN_CONF + 0.05 * d)), 2)
            _save_runtime_config()
        elif act == "reads":
            OCR_MIN_MATCHING = max(1, min(9, OCR_MIN_MATCHING + d))
            _save_runtime_config()
        elif act == "scr_off":
            op = list(SCREEN_OFF_OPCJE)
            i = op.index(SCREEN_OFF_MIN) if SCREEN_OFF_MIN in op else 3
            globals()["SCREEN_OFF_MIN"] = op[max(0, min(len(op) - 1, i + d))]
            _save_runtime_config()
        elif act == "scr_now":
            self._ekran_ustaw(False)
        elif act == "hud":
            globals()["HUD_ON"] = not HUD_ON
            _save_runtime_config()       # inaczej wracał „włączony" po restarcie
        elif act in ("image", "sound"):
            self._menu_open = False
            self._menu_edit = False
            self._img_panel = True
            self._img_mode = act
            self._img_sel = 0
            self._img_surf = None
        elif act == "vol_master":
            state = _audio_live_get()
            if state["volume"] is not None:
                _audio_set_volume_percent(state["volume"] + 5 * d)
        elif act == "vol_wired":
            # 09.10: wyciszenie głośnika przewodowego (JBL osobno)
            wm = _wired_muted()
            _wired_set_mute(not bool(wm))
        elif act == "vol_tap":
            globals()["TOUCH_SOUND"] = not TOUCH_SOUND
            _save_runtime_config()
            if TOUCH_SOUND:
                _dzwiek_dotyku("tap")
        elif act.startswith("vol_"):
            k = act[4:]
            if k in TTS_GAINS:
                TTS_GAINS[k] = round(max(1.0, min(4.0,
                                     TTS_GAINS[k] + 0.2 * d)), 1)
                _save_runtime_config()
        elif act == "tools":
            self._menu_view = "tools"
            self._menu_sel = 0
        elif act == "snap":
            request_manual_snap()
            _menu_banner("Robie zdjecie - 3 klatki", "blue", 4.0)
        elif act == "radar":
            RADAR_CFG["tryb"] = not RADAR_CFG["tryb"]
            _save_runtime_config()
            _menu_banner("RADAR: zapisuje tylko przekroczenia %.0f km/h"
                         % RADAR_CFG["prog_kmh"] if RADAR_CFG["tryb"]
                         else "Tryb radaru wylaczony - zwykle rozpoznawanie",
                         "red" if RADAR_CFG["tryb"] else "blue", 5.0)
        elif act == "radar_prog":
            # Enter/dotyk (d=+1 z menu) = NASTĘPNY próg z listy, w kółko —
            # 01.10: użytkownik klikając doszedł do 200 i nie mógł wrócić.
            # Strzałki (d=±1 z klawiatury) zostają jako ±5 km/h? Nie — menu
            # podaje d=+1 w obu przypadkach, więc jedna droga: lista w kółko.
            progi = [30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 100.0, 120.0]
            cur = float(RADAR_CFG["prog_kmh"])
            nast = next((v for v in progi if v > cur + 0.1), progi[0])
            if d < 0:
                nast = next((v for v in reversed(progi) if v < cur - 0.1), progi[-1])
            RADAR_CFG["prog_kmh"] = nast
            _save_runtime_config()
        elif act == "backup":
            _menu_backup_now()
        elif act == "usb":
            # przygotuj do wyjęcia / wznów — operacja w tle (sync bywa długi)
            if _usb_ready.is_set():
                threading.Thread(
                    target=lambda: _usb_prepare_eject(str(SAVE_DB_PATH)),
                    daemon=True, name="usb-eject").start()
            else:
                _usb_resume()
        elif act == "bt":
            # ZAWSZE lista urządzeń. Wcześniej kafel przy połączonym głośniku
            # od razu rozłączał — kliknięcie „żeby zobaczyć ustawienia"
            # wyrzucało dźwięk z głośnika. Rozłączanie jest teraz w panelu,
            # na wybranym urządzeniu.
            self._bt_open_panel()
        elif act == "ttsall":
            TTS_ALL = not TTS_ALL
            _save_runtime_config()
        elif act == "ttsbrand":
            TTS_BRAND = not TTS_BRAND
            _save_runtime_config()
        elif act == "ttstest":
            # Piper syntezuje ~3 s — bez tego kafel przez ten czas wygląda
            # na martwy i użytkownik naciska drugi raz.
            mac, nm = _bt_live_get()
            _menu_banner("Syntezuje mowe... (%s)" % (nm if mac else "HDMI"),
                         "blue", 6.0)
            SPEECH.say("Cześć! Tu system rozpoznawania tablic. Test głosu.")
        elif act == "full":
            self._pending_action = "restore"
        elif act == "ocr_backend":
            # 08.10: OCR na Hailo (HEF lvl3_calibdiverse512: 213/254 na sprzęcie,
            # tyle co CCT na CPU, 9 ms zamiast 117 ms) albo na procesorze.
            # Wybór zapisuje się do konfiguracji — obowiązuje też po restarcie.
            w = _diag_ref.get("ocr")
            nowy = "cpu" if getattr(w, "backend", "cpu") == "hailo" else "hailo"
            globals()["OCR_BACKEND"] = nowy
            _save_runtime_config()
            if w is not None:
                _menu_banner("OCR: przełączam na %s…" % ("Hailo" if nowy == "hailo"
                                                         else "procesor"), "blue", 4.0)
                w.ustaw_backend(nowy)
        elif act == "close":
            # Nieodwracalne (exit 0 → systemd NIE podniesie usługi), a kafel
            # bywa o jedno naciśnięcie od krawędzi siatki. Drugie dotknięcie
            # w ciągu 4 s dopiero wychodzi.
            if (self._menu_confirm == "close"
                    and time.monotonic() < self._menu_confirm_until):
                self._menu_confirm = None
                self._pending_action = "close"
            else:
                self._menu_confirm = "close"
                self._menu_confirm_until = time.monotonic() + 4.0

    # ── Ekran „Obraz": strojenie kamery na żywym podglądzie ────────────────

    _PROBKI = {"alert": "Uwaga! Nieoznakowany radiowóz.",
               "plate": "Tablica wu a, jeden dwa trzy cztery pięć.",
               "system": "Test głosu. Tak brzmią komunikaty."}

    def _snd_def(self) -> list:
        state = _audio_live_get()
        master = ("—" if state["volume"] is None else
                  f"{state['volume']}%" + (" · wyciszony"
                                          if state["muted"] else ""))
        wm = _wired_muted()
        return [("Głośność JBL", master, "vol_master")] + [
            (TTS_KIND_NAZWA[k], "×%.1f" % TTS_GAINS[k], "vol_" + k)
            for k in ("alert", "plate", "system")] + [
            ("Przewodowy", ("brak" if wm is None else
                            "wyciszony" if wm else "gra"), "vol_wired"),
            ("Dotyk", "klik wł." if TOUCH_SOUND else "klik wył.", "vol_tap")]

    def _img_def(self) -> list:
        """(etykieta, wartość, akcja) — pokrętła obrazu w jednym miejscu.
        Tryb obrazu przełącza między auto (EV/AE/pomiar) a ręcznym
        (migawka/wzmocnienie) — ręczny daje wyraźny, natychmiastowy efekt,
        auto tylko koryguje. Wcześniej ręcznego w ogóle tu nie było."""
        s = CSI_CAM_SETTINGS
        csi = CAM_SOURCE == "csi"
        ae_lbl = {"short": "krótkie czasy", "normal": "normalny",
                  "long": "długie czasy"}
        met_lbl = {"centre": "centralny", "spot": "punktowy",
                   "matrix": "matrycowy"}
        items = [("Zoom podglądu", f"{self._disp_zoom}×", "zoom"),
                 ("Wielkość dymków", "%.1f×" % NOTIF_SCALE, "notif"),
                 ("Priorytet tablicy", "wł." if AE_PLATE_ON else "wył.",
                  "aeplate")]
        if not csi:
            return items + [("Ekspozycja", "—", "ev"),
                            ("Tryb ekspozycji", "—", "ae"),
                            ("Pomiar światła", "—", "met")]
        manual = bool(s.get("manual"))
        items.append(("Tryb obrazu", "ręczny" if manual else "auto", "mode"))
        if manual:
            items.append(("Migawka", _csi_shutter_label(s["exposure_us"]), "exp"))
            items.append(("Wzmocnienie", "×%.1f" % s["gain"], "gn"))
        else:
            items.append(("Ekspozycja", "%+.1f EV" % s["ev"], "ev"))
            items.append(("Tryb AE", ae_lbl.get(s["ae_mode"], "—"), "ae"))
            items.append(("Pomiar światła", met_lbl.get(s["metering"], "—"), "met"))
        return items

    def _img_hit(self, x: int, y: int, w: int, h: int) -> tuple:
        """(rodzaj, indeks): ("chip", i) | ("minus", 0) | ("plus", 0) | ("", -1)"""
        items = (self._snd_def() if self._img_mode == "sound"
                 else self._img_def())
        g = _img_geom(w, h, len(items))
        if (g["chip_y"] <= y < g["chip_y"] + g["chip_h"]):
            for i in range(len(items)):
                cx = _img_chip_x(g, i)
                if cx <= x < cx + g["chip_w"]:
                    return ("chip", i)
        for nazwa, bx in (("minus", g["minus_x"]), ("plus", g["plus_x"])):
            if (x - bx) ** 2 + (y - g["cy"]) ** 2 <= (g["r"] + 6) ** 2:
                return (nazwa, 0)
        return ("", -1)

    def _draw_image_panel(self, frame) -> None:
        h, w = frame.shape[:2]
        dzwiek = self._img_mode == "sound"
        items = self._snd_def() if dzwiek else self._img_def()
        sel = max(0, min(self._img_sel, len(items) - 1))
        info = ""
        if dzwiek:
            mac, nm = _bt_live_get()   # NIGDY bluetoothctl tutaj
            info = ((nm if mac else "Głośnik przewodowy (JBL niepołączony)")
                    + "|Enter — posłuchaj wybranego rodzaju")
        klucz = (w, h, sel, self._img_mode, info,
                 tuple(v for _, v, _ in items))
        if self._img_surf is None or self._img_surf[0] != klucz:
            pre, inv = _img_surface(
                w, h, items, sel,
                "Dźwięk" if dzwiek else "Obraz",
                0.86 if dzwiek else 0.0, info)
            self._img_surf = (klucz, pre, inv)
        _, pre, inv = self._img_surf
        cv2.add(cv2.multiply(frame, inv, scale=1.0 / 255.0), pre, dst=frame)

    def _menu_hit(self, x: int, y: int, w: int, h: int) -> int:
        """Który kafel jest pod (x, y)? -1 = poza siatką.
        Ta sama geometria co przy rysowaniu, więc klik trafia w to,
        co widać."""
        items = self._menu_def()
        g = _menu_geom(w, h, len(items))
        page = self._menu_sel // g["per"]
        for vi in range(min(g["per"], len(items) - page * g["per"])):
            tx, ty = _menu_tile_xy(g, vi)
            if tx <= x < tx + g["tw"] and ty <= y < ty + g["th"]:
                return page * g["per"] + vi
        return -1

    def _draw_menu(self, frame) -> None:
        h, w = frame.shape[:2]
        items = self._menu_def()
        self._menu_sel = max(0, min(self._menu_sel, len(items) - 1))
        mx, my = self._mouse_xy
        hover = self._menu_hit(mx, my, w, h) if my >= 0 else -1
        # przerysowujemy tylko, gdy coś się zmieniło — nakładka jest droga,
        # a przy otwartym menu i tak lecimy 20 fps
        tytul = ("Narzędzia deweloperskie" if self._menu_view == "tools"
                 else "Ustawienia")
        stopka = ("m — menu  ·  p — zdjęcie  ·  z — zoom  ·  f — pełny ekran"
                  if self._menu_view == "main" else
                  "ustawienia zaawansowane — w codziennej jeździe niepotrzebne")
        klucz = (w, h, self._menu_sel, self._menu_edit, hover, tytul,
                 tuple(v for _, v, _, _ in items))
        if self._menu_cache is None or self._menu_cache[0] != klucz:
            pre, inv = _menu_surface(w, h, items, self._menu_sel,
                                     hover, self._menu_edit, tytul, stopka)
            self._menu_cache = (klucz, pre, inv)
        _, pre, inv = self._menu_cache
        cv2.add(cv2.multiply(frame, inv, scale=1.0 / 255.0), pre, dst=frame)

    def _handle_key(self, k: int) -> None:
        if k in (-1, 255):
            return
        self._ekran_aktywnosc = time.monotonic()
        if self._ekran_wyl:                 # klawisz budzi, nic więcej
            self._ekran_dotyk_t = time.monotonic()
            return
        UP, DOWN, LEFT, RIGHT = 65362, 65364, 65361, 65363
        if self._img_panel:                    # ekran „Obraz" / „Dźwięk"
            dzwiek = self._img_mode == "sound"
            items = self._snd_def() if dzwiek else self._img_def()
            if k in (13, 10) and dzwiek:       # Enter = posłuchaj
                rodzaj = items[self._img_sel][2][4:]
                if rodzaj in self._PROBKI:
                    SPEECH.say(self._PROBKI[rodzaj], rodzaj)
                return
            if k == 27:
                self._img_panel = False
                self._img_surf = None
                self._menu_open = True
            elif k == 65362:                   # góra
                self._img_sel = (self._img_sel - 1) % len(items)
            elif k == 65364:                   # dół
                self._img_sel = (self._img_sel + 1) % len(items)
            elif k in (65361, 65363):          # lewo / prawo = wartość
                self._menu_act(items[self._img_sel][2],
                               1 if k == 65363 else -1)
            return
        if self._bt_panel is not None:          # wybierak głośnika ma pierwszeństwo
            p = self._bt_panel
            n = len(p["devs"]) + 1              # +1 = wiersz widoczności
            if k == 27:
                self._bt_panel = None
                self._bt_surf = None
            elif k in (ord("s"), ord("S")):
                _bt_start_scan(12)
                p["scanning"] = True
                p["status"] = "Szukam urządzeń…"
            elif k in (ord("w"), ord("W")):     # skrót na widoczność
                p["sel"] = 0
                self._bt_use_selected()
            elif k in (13, 10):
                self._bt_use_selected()
            elif k == 65362:                    # góra
                p["sel"] = (p["sel"] - 1) % n
            elif k == 65364:                    # dół
                p["sel"] = (p["sel"] + 1) % n
            return
        if k in (ord("m"), ord("M")):
            self._menu_open = not self._menu_open
            self._menu_edit = False
            return
        if self._menu_open:
            items = self._menu_def()
            n = len(items)
            akcja = items[self._menu_sel][2]
            cols = _menu_geom(self._disp_sw or 1920,
                              self._disp_sh or 1080, n)["cols"]
            if k == 27:                      # Esc: edycja → narzędzia → wyjście
                if self._menu_edit:
                    self._menu_edit = False
                elif self._menu_view == "tools":
                    self._menu_view = "main"
                    self._menu_sel = 0
                else:
                    self._menu_open = False
            elif self._menu_edit and k in (LEFT, RIGHT):
                self._menu_act(akcja, 1 if k == RIGHT else -1)
            elif k in (13, 10):              # Enter: zmień / włącz / gotowe
                if akcja in _MENU_ADJUST:
                    if self._menu_edit:
                        self._menu_edit = False
                    else:
                        self._menu_edit = True
                else:
                    self._menu_act(akcja, 1)
            else:                            # nawigacja po siatce
                self._menu_edit = False
                if k == UP:
                    self._menu_sel = (self._menu_sel - cols) % n
                elif k == DOWN:
                    self._menu_sel = (self._menu_sel + cols) % n
                elif k == LEFT and self._menu_sel % cols:
                    self._menu_sel -= 1      # bez zawijania na krawędzi:
                elif k == RIGHT and (self._menu_sel % cols) < cols - 1 \
                        and self._menu_sel + 1 < n:
                    self._menu_sel += 1      # ← z pierwszej kolumny skakało
                                             # wprost na „Zamknij program"
            return
        # skroty bez menu
        if k in (ord("p"), ord("P")):
            request_manual_snap()
        elif k in (ord("z"), ord("Z")):
            self._menu_act("zoom", 1)
        elif k in (ord("+"), ord("=")):
            self._menu_act("ev", 1)
        elif k == ord("-"):
            self._menu_act("ev", -1)
        elif k in (ord("f"), ord("F")):
            self._pending_action = "restore"

    def _draw_titlebar(self, frame):
        h, w = frame.shape[:2]
        BAR, BW = self._TB_H, self._TB_BW
        self._tb_drawn_at = time.monotonic()
        sub = frame[0:BAR, :]
        panel = np.full_like(sub, (26, 24, 22))
        cv2.addWeighted(panel, 0.94, sub, 0.06, 0, sub)
        cv2.line(frame, (0, BAR - 1), (w, BAR - 1), (85, 85, 85), 1)
        cv2.putText(frame, "LPR", (16, BAR - 16), cv2.FONT_HERSHEY_DUPLEX,
                    0.7, (235, 235, 235), 1, cv2.LINE_AA)
        mx, my = self._mouse_xy
        # przycisk MENU (hamburger) — obszar klikalny 58..130 (patrz _on_mouse)
        mb_hov = 58 <= mx < 130 and my < BAR
        if mb_hov or self._menu_open:
            cv2.rectangle(frame, (58, 0), (130, BAR - 2), (60, 56, 52), -1)
        for dy in (-6, 0, 6):
            cv2.line(frame, (70, BAR // 2 + dy), (88, BAR // 2 + dy),
                     (235, 235, 235), 2)
        cv2.putText(frame, "MENU", (94, BAR - 18), cv2.FONT_HERSHEY_SIMPLEX,
                    0.42, (200, 200, 200), 1, cv2.LINE_AA)
        x0 = w - 3 * BW
        potw = (self._menu_confirm == "close"
                and time.monotonic() < self._menu_confirm_until)
        for i, glyph in enumerate(("min", "restore", "close")):
            bx = x0 + i * BW
            hover = bx <= mx < bx + BW and my < BAR
            if hover or (potw and glyph == "close"):
                col = (52, 48, 180) if glyph == "close" else (60, 56, 52)
                cv2.rectangle(frame, (bx, 0), (bx + BW, BAR - 2), col, -1)
            gc = (235, 235, 235)
            cx, cy = bx + BW // 2, BAR // 2
            if glyph == "close" and potw:
                cv2.putText(frame, "NA PEWNO?", (bx + 6, BAR - 17),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.42, gc, 1, cv2.LINE_AA)
            elif glyph == "min":
                cv2.line(frame, (cx - 9, cy + 6), (cx + 9, cy + 6), gc, 2)
            elif glyph == "restore":
                cv2.rectangle(frame, (cx - 8, cy - 7), (cx + 8, cy + 7), gc, 2)
            else:
                cv2.line(frame, (cx - 8, cy - 8), (cx + 8, cy + 8), gc, 2)
                cv2.line(frame, (cx - 8, cy + 8), (cx + 8, cy - 8), gc, 2)

    def _draw_volume_overlay(self, frame) -> None:
        """Pasek głośności (jak na telefonie) — pojawia się przy zmianie
        głośności JBL i po ~2,5 s znika. Lekki: półprzezroczyste tło tylko
        na małym pasku (nie na całej klatce), więc nie obciąża 30 fps."""
        if time.monotonic() >= _vol_overlay["until"]:
            return
        h, w = frame.shape[:2]
        val = int(_vol_overlay["val"])
        muted = bool(_vol_overlay["muted"])
        bw = int(w * 0.42)
        bh = max(10, int(h * 0.016))
        bx = (w - bw) // 2
        by = int(h * 0.84)
        pad = 22
        x0 = max(0, bx - pad); y0 = max(0, by - 52)
        x1 = min(w, bx + bw + pad); y1 = min(h, by + bh + pad)
        roi = frame[y0:y1, x0:x1]
        if roi.size:
            dark = roi.copy(); dark[:] = (25, 25, 25)
            cv2.addWeighted(dark, 0.55, roi, 0.45, 0, dst=roi)
        # tekst przez PIL — cv2.putText nie ma polskich znaków (było „Glosnosc")
        etykieta = "WYCISZONE" if muted else ("Głośność JBL   %d%%" % val)
        if roi.size:
            _put_text_batch(roi, [(etykieta, (max(0, bx - x0),
                                              max(0, by - y0 - 34)),
                                   (245, 245, 245), 20)])
        cv2.rectangle(frame, (bx, by), (bx + bw, by + bh), (90, 90, 90), -1)
        fillw = 0 if muted else int(bw * max(0, min(100, val)) / 100.0)
        if fillw > 0:
            cv2.rectangle(frame, (bx, by), (bx + fillw, by + bh),
                          (120, 220, 255), -1)

    def _draw_alert(self, frame) -> None:
        """Dolny baner: trafienie z bazy albo komunikat z menu."""
        if time.monotonic() < _alert_until:
            # baner trafienia — kolor wg bazy
            fh2, fw2 = frame.shape[:2]
            col = ALERT_COLORS.get(_alert_color, ALERT_COLORS["red"])
            cv2.rectangle(frame, (0, fh2 - 74), (fw2, fh2), col, -1)
            cv2.putText(frame, "! " + _alert_text,
                        (18, fh2 - 26), cv2.FONT_HERSHEY_DUPLEX, 1.1,
                        (255, 255, 255), 2, cv2.LINE_AA)
            # ZDJĘCIE POJAZDU, na którym trafiliśmy tablicę
            if _alert_img is not None and _alert_img.size:
                ah, aw = _alert_img.shape[:2]
                max_w = int(fw2 * 0.38)
                max_h = int(fh2 * 0.46)
                sc = min(max_w / aw, max_h / ah)
                tw, th = max(40, int(aw * sc)), max(30, int(ah * sc))
                thumb = cv2.resize(_alert_img, (tw, th),
                                   interpolation=cv2.INTER_AREA)
                x0 = fw2 - tw - 18
                y0 = fh2 - 74 - th - 14
                if y0 > 40:
                    frame[y0:y0 + th, x0:x0 + tw] = thumb
                    cv2.rectangle(frame, (x0 - 3, y0 - 3),
                                  (x0 + tw + 3, y0 + th + 3), col, 3)

    # Rejestrator zacięć: mierzy PRZERWĘ między klatkami podglądu.
    # Pętla GLib obsługuje podgląd i magistralę GStreamera, więc gdy coś
    # ją zablokuje, widać to tutaj jako lukę. Do logu trafia tylko
    # zdarzenie ponad progiem — bez zaśmiecania przy normalnej pracy.
    _ZACIECIE_MS = 400.0
    # Ile wolno nie mieć nowej klatki, zanim zamiast obrazu pokażemy ekran
    # stanu. Zgrane z progiem [ZACIECIE-KAMERA] (1,5 s) — czyli ekran stanu
    # wchodzi dokładnie wtedy, gdy i tak uznajemy brak klatek za zdarzenie.
    _BRAK_OBRAZU_S = 1.5
    _last_tick = 0.0
    _zaciec = 0
    _ost_klatka = -1
    _ost_klatka_t = 0.0
    _brak_zgloszony = False
    _start_t = 0.0          # rozruch kamery trwa ~10 s — to nie zacięcie
    _ost_watchdog = 0.0     # ostatni sygnał życia wysłany do systemd

    def _display_tick(self):
        global _stat_disp_count
        _teraz = time.monotonic()
        if not self._start_t:
            self._start_t = _teraz
        if self._start_t and _teraz - self._start_t > 90.0:
            _splash_mpv_stop()
        if self._okno_tiki < 6:
            self._okno_tiki += 1
            if self._okno_tiki == 5:          # ~125 ms po zmapowaniu okna
                _splash_proces_stop()
        # Sygnał życia dla systemd. Wysyłany STĄD, bo to pętla rysowania
        # zamiera przy awarii — jeśli przestanie tykać na dłużej niż
        # WatchdogSec, systemd sam zrestartuje usługę. Co 10 s przy limicie
        # 60 s daje 6-krotny zapas: chwilowe zadławienie nie wywoła restartu
        # (restart kosztuje ~20 s ślepoty — gorzej niż sama czkawka).
        if _teraz - self._ost_watchdog >= 10.0:
            self._ost_watchdog = _teraz
            _sd_notify("WATCHDOG=1")
        self._ekran_tick(_teraz)
        rozruch = _teraz - self._start_t < 25.0
        if self._last_tick and not rozruch:
            luka = (_teraz - self._last_tick) * 1000.0
            if luka > self._ZACIECIE_MS:
                self._zaciec += 1
                print("[%s] [ZACIECIE-PETLA] rysowanie stalo %.0f ms "
                      "(menu=%s obraz=%s bt=%s cpu=%.0f%% temp=%.0f "
                      "hailo=%.0f) - lacznie %d"
                      % (datetime.now().strftime("%H:%M:%S"), luka,
                         self._menu_open, self._img_panel,
                         self._bt_panel is not None, _stat_cpu,
                         _stat_cpu_temp, _stat_hailo_temp,
                         self._zaciec), flush=True)
                _hailort_ogon_do_logu("ZACIECIE-PETLA")
        self._last_tick = _teraz
        # DRUGI, WAŻNIEJSZY objaw: pętla chodzi, ale KAMERA nie dostarcza
        # nowych klatek. Ekran zamarza z ostatnim obrazem, a licznik pętli
        # tego nie widzi — trzeba pilnować napływu klatek z potoku.
        if _stat_cb_count != self._ost_klatka:
            self._ost_klatka = _stat_cb_count
            self._ost_klatka_t = _teraz
            self._brak_zgloszony = False
        elif (self._ost_klatka_t and not rozruch
              and _teraz - self._ost_klatka_t > 1.5
              and not self._brak_zgloszony):
            self._brak_zgloszony = True
            self._zaciec += 1
            print("[%s] [ZACIECIE-KAMERA] brak nowych klatek od %.1f s "
                  "(cpu=%.0f%% temp=%.0f hailo=%.0f) - lacznie %d"
                  % (datetime.now().strftime("%H:%M:%S"),
                     _teraz - self._ost_klatka_t, _stat_cpu,
                     _stat_cpu_temp, _stat_hailo_temp, self._zaciec),
                  flush=True)
            _hailort_ogon_do_logu("ZACIECIE-KAMERA")
        # PRIORYTET: rozpoznawanie przed podglądem. Przy obciążonym
        # procesorze rysujemy co drugą klatkę, żeby OCR i detekcja dostały
        # oddech.
        # Próg podniesiony 88 → 93 (02.09.2026): przy 88 właczał się w
        # normalnej jeździe i zbijał podgląd do ~15 fps — użytkownik zgłosił
        # to jako „klatkuje". To ma być ratunek przed zapchaniem, a nie stan
        # roboczy; przy 93 zostaje zapas na prawdziwe szczyty.
        self._disp_skip = (self._disp_skip + 1) % 2
        if (_stat_cpu > 93.0 and self._disp_skip
                and not (self._menu_open or self._img_panel
                         or self._bt_panel is not None)):
            return True
        if self._pending_action:
            self._apply_action()
        if self._menu_click:
            idx, d = self._menu_click
            self._menu_click = None
            if idx < 0:                      # z ekranu „Obraz"/„Dźwięk"
                lista = (self._snd_def() if self._img_mode == "sound"
                         else self._img_def())
                self._menu_act(lista[min(self._img_sel,
                                         len(lista) - 1)][2], d)
            else:
                items = self._menu_def()
                if 0 <= idx < len(items):
                    self._menu_act(items[idx][2], d)
        frame = self._user_data.get_frame()
        if self._ekran_wyl:
            return True          # kolejkę opróżniamy, ale nie malujemy (CPU)
        if frame is None:
            # Pusta kolejka w POJEDYNCZYM tiku to NIE awaria: podgląd chodzi
            # 20 fps, kamera 30 fps, więc raz na kilkadziesiąt sekund trafia
            # się tik, w którym nowej klatki jeszcze nie ma.
            #
            # Dotąd rysowaliśmy wtedy od razu ekran startowy — a że jest on
            # DOKŁADNIE TĄ SAMĄ grafiką co tapeta pulpitu (patrz
            # _ensure_wallpaper), wyglądało to jak mignięcie pulpitu w środku
            # pracy. Zgłoszone przez użytkownika 30.07.2026, zmierzone serią
            # zrzutów: ~4 błyski na 3 minuty, każdy na jedną klatkę.
            #
            # Ekran stanu ma sens dopiero, gdy obrazu NAPRAWDĘ nie ma. Do tego
            # czasu na ekranie zostaje ostatnia klatka — nie odświeżamy okna,
            # więc kompozytor pokazuje poprzednią zawartość.
            if (self._boot_done
                    and _teraz - self._last_frame_t < self._BRAK_OBRAZU_S):
                return True
            # Brak obrazu z kamery: ekran ładowania (start) albo błędu
            # (kamera niepodłączona / utracona). Nigdy pusty ekran.
            self._draw_status_screen()
            return True
        if not self._boot_done and frame is not None:
            self._fade_boot_t0 = _teraz
            _splash_mpv_stop()
            self._fade_boot_img = (self._boot_cache[1]
                                   if self._boot_cache is not None else None)
        self._boot_done = True
        self._last_frame_t = _teraz
        if frame is not None:
            # Callback oddaje klatkę JUŻ zmniejszoną do rozmiaru ekranu, już po
            # zoomie i z narysowaną nakładką (patrz app_callback). Przekazujemy
            # mu tylko, jaki jest rozmiar i zoom — a tu zostaje samo
            # wyświetlenie. Skalowanie i zoom robimy jedynie awaryjnie, gdyby
            # klatka przyszła w innym rozmiarze (np. tuż po zmianie ekranu).
            globals()["_podglad_wh"] = (self._disp_sw or 800,
                                        self._disp_sh or 480)
            globals()["_podglad_zoom"] = self._disp_zoom
            if (self._disp_sw and self._disp_sh
                    and (frame.shape[1], frame.shape[0]) != (self._disp_sw, self._disp_sh)):
                if self._disp_zoom > 1:
                    fh, fw = frame.shape[:2]
                    zw, zh = fw // self._disp_zoom, fh // self._disp_zoom
                    frame = frame[(fh - zh) // 2:(fh - zh) // 2 + zh,
                                  (fw - zw) // 2:(fw - zw) // 2 + zw]
                frame = cv2.resize(frame, (self._disp_sw, self._disp_sh),
                                   interpolation=cv2.INTER_NEAREST)
            if self._fade_boot_img is not None:
                a = (_teraz - self._fade_boot_t0) / 0.8
                if a >= 1.0 or self._fade_boot_img.shape != frame.shape:
                    self._fade_boot_img = None
                else:
                    a = a * a * (3.0 - 2.0 * a)          # smoothstep
                    cv2.addWeighted(self._fade_boot_img, 1.0 - a, frame, a,
                                    0, dst=frame)
            if self._disp_zoom > 1:
                cv2.putText(frame, f"ZOOM {self._disp_zoom}x",
                            (frame.shape[1] - 150, frame.shape[0] - 16),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7,
                            (120, 220, 255), 2, cv2.LINE_AA)
            if time.monotonic() < _snap_flash_until:
                # blysk przy recznym zdjeciu (klawisz P / przycisk FOTO)
                fh2, fw2 = frame.shape[:2]
                cv2.rectangle(frame, (3, 3), (fw2 - 4, fh2 - 4),
                              (255, 255, 255), 6)
                cv2.putText(frame, "FOTO", (fw2 // 2 - 60, 90),
                            cv2.FONT_HERSHEY_DUPLEX, 1.4,
                            (255, 255, 255), 3, cv2.LINE_AA)
            # Nakładki są półprzezroczyste, więc pasek statystyk i pasek
            # okienny przebijały przez nagłówek ustawień. Przy otwartym
            # ekranie ustawień po prostu ich nie rysujemy.
            nakladka = (self._menu_open or self._bt_panel is not None
                        or self._img_panel)
            if HUD_ON and not nakladka:
                self._draw_stats_bar(frame)
                # ikona BT w pasku statusu (jak w telefonie) — lewy skraj jest
                # wolny, pierwsza kolumna tekstu zaczyna się dalej
                if _bt_live_get()[0]:
                    _ikona_rysuj(frame, "bt", 16, 20, 9,
                                 _TOAST_KOLORY["blue"])
            # 09.10 (kiosk): paska okiennego już nie ma — dotknięcie = menu
            # 09.10: alfa nakładek — 150 ms dojścia do celu w obie strony
            cel = 1.0 if nakladka else 0.0
            krok = min(1.0, ((_teraz - self._nakl_t) if self._nakl_t else 0.05) / 0.15)
            self._nakl_t = _teraz
            if self._nakl_a < cel:
                self._nakl_a = min(cel, self._nakl_a + krok)
            elif self._nakl_a > cel:
                self._nakl_a = max(cel, self._nakl_a - krok)
            a = self._nakl_a
            przejscie = 0.0 < a < 1.0
            if nakladka:
                baza = frame.copy() if przejscie else None
                if self._menu_open:
                    self._draw_menu(frame)
                if self._img_panel:
                    self._draw_image_panel(frame)
                if self._bt_panel is not None:
                    self._draw_bt_panel(frame)
                self._nakl_ostatnia = frame.copy()   # do wygaszania po zamknięciu
                if baza is not None:
                    cv2.addWeighted(baza, 1.0 - a, frame, a, 0, dst=frame)
            elif przejscie and self._nakl_ostatnia is not None \
                    and self._nakl_ostatnia.shape == frame.shape:
                cv2.addWeighted(frame, 1.0 - a, self._nakl_ostatnia, a, 0,
                                dst=frame)
            elif not przejscie:
                self._nakl_ostatnia = None
            # Baner NA KOŃCU, nad nakładkami. Wcześniej rysowany był przed
            # nimi, więc każdy komunikat („Kopia gotowa", „Rozłączono…")
            # przy otwartych ustawieniach ginął pod przyciemnieniem.
            self._draw_alert(frame)
            if time.monotonic() < _notif_podglad_do:
                # przykładowy dymek przy regulacji — rysowany po panelach,
                # więc nie jest przygaszony nakładką menu
                _draw_notifications(frame, frame.shape[1],
                                    [{"crop": _notif_demo(),
                                      "text": "WX 1234A",
                                      "t0": time.monotonic()}],
                                    time.monotonic())
            _draw_toasts(frame)          # po panelach → nieprzygaszone
            self._draw_volume_overlay(frame)
            if _ae_plate["akt"] and not nakladka:
                # nocą obraz mocno ciemnieje — bez tego wygląda jak awaria
                cv2.putText(frame, "AE: TABLICA", (12, 64),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (58, 155, 202), 1,
                            cv2.LINE_AA)
            cv2.imshow("LPR", frame)
            with _stat_lock:
                _stat_disp_count += 1
        return True  # GLib: kontynuuj timeout

    def _ekran_tick(self, teraz: float) -> None:
        """Wygaszanie ekranu po bezczynności i budzenie (05.10).
        Aktywność = dotyk / klawisz / mysz, tablica w kadrze (jazda), alert
        z watchlisty. Budzi: dotknięcie (okno albo evdev), klawisz, alert."""
        if self._ekran_wyl:
            budzik = (self._ekran_dotyk_t > self._ekran_wyl_t
                      or time.monotonic() < _alert_until)
            if budzik:
                self._ekran_ustaw(True)
            return
        # tablica w kadrze = jedziemy → nie gasić w trakcie jazdy
        tm = getattr(self._user_data, "track_mgr", None)
        if tm is not None:
            try:
                if any(not t.finalized for t in list(tm.tracks.values())):
                    self._ekran_aktywnosc = teraz
            except Exception:
                pass
            if tm.last_confirmed[1] > self._ekran_aktywnosc:
                self._ekran_aktywnosc = tm.last_confirmed[1]
        if (SCREEN_OFF_MIN > 0 and not self._menu_open
                and teraz - self._ekran_aktywnosc > SCREEN_OFF_MIN * 60.0):
            self._ekran_ustaw(False)

    def _ekran_ustaw(self, wlacz: bool) -> None:
        if wlacz == (not self._ekran_wyl):
            return
        if wlacz:
            ok = _ekran_zasilanie(True)
            self._ekran_wyl = False
            self._ekran_aktywnosc = time.monotonic()
            self._ekran_laska_do = time.monotonic() + 0.8
            self._mouse_xy = (-1, -1)
            print(f"[Ekran] obudzony ({'wlopm' if ok else 'bez wlopm'})")
        else:
            self._menu_open = False
            self._menu_edit = False
            ok = _ekran_zasilanie(False)
            if not ok:
                print("[Ekran] nie udało się zgasić (wlopm) — zostaje włączony")
                self._ekran_aktywnosc = time.monotonic()
                return
            self._ekran_wyl = True
            self._ekran_wyl_t = time.monotonic()
            print(f"[Ekran] zgaszony (bezczynność {SCREEN_OFF_MIN} min albo "
                  "z menu) — budzi dotyk / klawisz / alert")

    def _draw_status_screen(self) -> None:
        """Pełnoekranowy ekran ładowania / błędu, gdy nie ma obrazu z kamery.
        Pasek zamknięcia jest dostępny (mysz u góry), żeby dało się wyjść
        z aplikacji nawet gdy nic nie działa."""
        w = self._disp_sw or 800
        h = self._disp_sh or 480
        teraz = time.monotonic()
        boot = _get_boot()
        # kamera padła W TRAKCIE pracy (działała, klatki ustały > 4 s)
        if (self._boot_done and not boot["blad"]
                and teraz - self._last_frame_t > 4.0):
            _set_boot("utrata", "Utracono obraz z kamery",
                      "Kamera przestała przesyłać obraz.", blad=True,
                      kroki=["Sprawdź taśmę CSI (oba końce)",
                             "Jeśli nie pomoże — wyłącz i włącz zasilanie"])
            boot = _get_boot()
        # cache: błąd jest statyczny (rysuj raz), spinner animujemy ~10 fps
        krok = 0 if boot["blad"] else int(teraz * 10)
        klucz = (w, h, boot["tytul"], boot["opis"], boot["blad"],
                 tuple(boot["kroki"]), krok)
        if self._boot_cache is None or self._boot_cache[0] != klucz:
            self._boot_cache = (klucz, _boot_frame(w, h, boot, teraz))
        frame = self._boot_cache[1].copy()
        if boot["blad"]:
            _splash_mpv_stop()               # ekran błędu musi być widoczny
        cv2.imshow("LPR", frame)

    def _key_tick(self):
        """Odpytywanie klawiatury osobno od rysowania (co 15 ms zamiast 50).

        cv2.waitKey NIE kolejkuje klawiszy — pamięta tylko ostatni kod. Gdy
        pytaliśmy raz na klatkę podglądu, dwa naciśnięcia strzałki w tym
        samym oknie 50 ms dawały jeden ruch zaznaczenia. Skrócenie okna do
        15 ms kosztuje tyle co nic, a menu przestaje gubić naciśnięcia.
        Ta sama pętla GLib, więc bez wątków i bez blokad.
        """
        k = cv2.waitKeyEx(1)
        if k not in (-1, 255):
            self._handle_key(k)
        return True

    def _draw_stats_bar(self, frame: np.ndarray) -> None:
        """Rysuje pasek statystyk u góry ekranu (PIL — ten sam styl co dolny HUD)."""
        global _stats_strip_cache, _stats_strip_key
        with _stat_lock:
            fps   = _stat_fps
            dfps  = _stat_disp_fps
            cpu   = _stat_cpu
            ram   = _stat_ram
            cpu_t = _stat_cpu_temp
            hailo = _stat_hailo_temp
        h, w = frame.shape[:2]
        bar_h = 40
        fsize = 17
        ty    = (bar_h - fsize) // 2 - 2
        cache_key = (round(fps), round(dfps), round(cpu), round(ram, 1),
                     round(cpu_t), round(hailo), w)
        if cache_key != _stats_strip_key:
            _stats_strip_key = cache_key
            strip = np.full((bar_h, w, 3), (20, 20, 20), dtype=np.uint8)
            cv2.line(strip, (0, bar_h - 1), (w, bar_h - 1), (50, 50, 50), 1)
            GRAY = (160, 160, 160)
            RED  = (60, 60, 210)   # BGR czerwony
            col_w = w // 5
            items = [
                # kamera/ekran — druga liczba to realna płynność wyświetlania
                (f"FPS {fps:.0f}/{dfps:.0f}",
                 RED if 0 < min(fps, dfps) < 8 else GRAY),
                (f"CPU   {cpu:.0f}%",
                 RED if cpu > 85 else GRAY),
                (f"RAM {ram:.1f}GB",
                 RED if ram > 6.5 else GRAY),
                (f"PROC  {cpu_t:.0f}°C" if cpu_t > 0 else "PROC  --",
                 RED if cpu_t > 70 else GRAY),
                (f"HAILO {hailo:.0f}°C" if hailo > 0 else "HAILO  --",
                 RED if hailo > 70 else GRAY),
            ]
            texts: list = []
            for i, (text, color) in enumerate(items):
                tw, _ = _text_size(text, fsize)
                x = col_w * i + (col_w - tw) // 2
                texts.append((text, (max(0, x), ty), color, fsize))
            _put_text_batch(strip, texts)
            _stats_strip_cache = strip
        frame[0:bar_h, :] = _stats_strip_cache

    def _connect_callback(self):
        super()._connect_callback()
        raw = self.pipeline.get_by_name("lpr_raw")
        if raw is None:
            print("[Ramka] brak identity lpr_raw w potoku — ramki z trackera")
        else:
            raw.set_property("signal-handoffs", True)
            raw.connect("handoff", _raw_tap_cb, None)
            print("[Ramka] surowe detekcje sprzed trackera podpięte")

    def get_pipeline_string(self):
        global _dashcam_active
        pipeline = super().get_pipeline_string()
        # 09.10: identity PRZED hailotracker — surowe ramki (patrz _dets_surowe)
        pipeline = pipeline.replace(
            "hailotracker name=",
            "identity name=lpr_raw signal-handoffs=true ! hailotracker name=", 1)
        pipeline = re.sub(r"fpsdisplaysink\s+name=\S+.*", "fakesink sync=false", pipeline)
        # Kolejka OBEJSCIA w hailocropperze trzyma ORYGINALNA klatke,
        # zanim agregator zlaczy ja z wynikiem detekcji. Hailo ustawia
        # tam 20 buforow — przy 2304x1296x3 to 180 MB RAM i, co gorsza,
        # do 660 ms opoznienia obrazu (20 klatek / 30 fps). Uzytkownik
        # zglaszal to jako „jestem dużo sekund do tyłu".
        # 5 buforow = 45 MB i najwyzej 166 ms. Zapas na chwilowe
        # zadlawienie zostaje, a zaleglosc nie ma sie gdzie odkladac.
        pipeline = pipeline.replace(
            "queue name=inference_wrapper_bypass_q leaky=no "
            "max-size-buffers=20",
            "queue name=inference_wrapper_bypass_q leaky=no "
            "max-size-buffers=5")
        if CAM_SOURCE == "csi" and "app_source" not in pipeline:
            # --input rpi = appsrc karmiony przez Picamera2 (watek picamera_thread
            # w hailo-apps); zweryfikowane w gstreamer_helper_pipelines.py 2026-07-16
            print("[LPR] UWAGA: tryb csi, ale w pipeline nie ma appsrc "
                  "(app_source) — sprawdź czy lpr_run.sh przekazał --input rpi")
        # Wideorejestrator: tee na strumieniu JPEG PRZED dekodowaniem — zapis
        # bez re-kodowania (koszt ~0 CPU). Gałąź z leaky=downstream: gdy dysk
        # nie nadąża, gubi klatki NAGRANIA, nigdy nie blokuje LPR.
        # Tryb CSI: źródło libcamera nie ma strumienia JPEG — dashcam jedzie
        # wtedy osobnym pipeline z kamery USB (_start_usb_dashcam w main()).
        if (DASHCAM_ENABLED and CAM_SOURCE == "usb"
                and os.path.isdir(os.path.dirname(DASHCAM_DIR))):
            try:
                seg_dir = Path(DASHCAM_DIR) / datetime.now().strftime("%Y%m%d_%H%M%S")
                seg_dir.mkdir(parents=True, exist_ok=True)
                new_pipeline, n = re.subn(
                    r"(image/jpeg[^!]*!)", r"\1 tee name=dashtee !", pipeline, count=1)
                if n == 1:
                    pipeline = new_pipeline + (
                        f" dashtee. ! queue name=dashq max-size-buffers=120 "
                        f"max-size-bytes=0 max-size-time=0 leaky=downstream ! "
                        f"jpegparse ! splitmuxsink name=dashsink "
                        f"muxer-factory=matroskamux "
                        f"location={seg_dir}/jazda_%05d.mkv "
                        f"max-size-time={DASHCAM_SEG_SEC * 1_000_000_000}")
                    _dashcam_active = True
                    print(f"[Dashcam] nagrywam do {seg_dir} (segmenty {DASHCAM_SEG_SEC} s)")
            except OSError as e:
                print(f"[Dashcam] wyłączony: {e}")
        elif DASHCAM_ENABLED and CAM_SOURCE == "usb":
            print("[Dashcam] brak pendrive — nagrywanie wyłączone")
        return pipeline


class LprState(app_callback_class):
    def __init__(self, track_mgr: TrackManager,
                 web_server: Optional['WebServer'] = None,
                 web_ip: str = ""):
        super().__init__()
        # threading.SimpleQueue: display jest na tym samym procesie (GLib.timeout_add)
        # — brak forka, brak IPC overhead, brak "Frame queue is full" warnings
        self.frame_queue    = _queue.SimpleQueue()
        _diag_ref["stan"] = self         # do pomiaru glebokosci kolejek
        self.track_mgr      = track_mgr
        self.exit_requested = multiprocessing.Event()
        self.main_pid       = os.getpid()
        self.web_server     = web_server
        self.web_ip         = web_ip
        self.web_start_mono = time.monotonic()

    def set_frame(self, frame):
        # qsize() zamiast recznego licznika: licznik aktualizowany z dwoch
        # watkow potrafil sie rozjechac i NA STALE zablokowac wyswietlanie
        if self.frame_queue.qsize() < 2:
            self.frame_queue.put(frame)

    def get_frame(self):
        if not self.frame_queue.empty():
            return self.frame_queue.get()
        return None


# ── Głos (Piper TTS) + głośnik Bluetooth ─────────────────────────────────────

class SpeechWorker:
    """Wątek tła: kolejka tekstów → Piper (pl_PL-gosia) → pw-play (PipeWire,
    trafia w domyślne wyjście — po połączeniu głośnika BT gra przez niego).
    Model ładowany leniwie przy pierwszym komunikacie (~2 s, ~100 MB RAM)."""

    def __init__(self) -> None:
        self._q: _queue.Queue = _queue.Queue(maxsize=8)
        self._voice = None
        self._gra = False
        self._lock = threading.Lock()   # Piper nie jest wielowątkowy
        threading.Thread(target=self._loop, daemon=True, name="tts").start()

    def zajety(self) -> bool:
        """Czy trwa synteza/odtwarzanie — keep-alive ma wtedy ustąpić."""
        return self._gra or not self._q.empty()

    def say(self, text: str, kind: str = "system") -> None:
        """Odtwórz na głośniku malinki (auto/BT).
        kind: alert | plate | system — decyduje o głośności."""
        try:
            self._q.put_nowait((text, kind))
        except _queue.Full:
            pass

    def synth(self, text: str, path: str, kind: str = "system") -> bool:
        """Synteza do pliku WAV (współdzielona przez głośnik i podgląd www)."""
        import wave
        with self._lock:
            try:
                if self._voice is None:
                    from piper import PiperVoice
                    self._voice = PiperVoice.load(TTS_VOICE_PATH)
                    print("[Głos] Piper (gosia) załadowany")
                with wave.open(path, "wb") as w:
                    self._voice.synthesize_wav(text, w)
                self._normalize(path, kind)
                return True
            except Exception as e:
                print(f"[Głos] błąd syntezy: {e}")
                return False

    @staticmethod
    def _normalize(path: str, kind: str = "system",
                   target_rms: float = 0.22) -> None:
        """KOMPRESJA + normalizacja głośności. Piper daje szczyty ~100%, ale
        średnią (RMS) ~16% — krótkie samogłoski ("a", "e") są dużo cichsze
        od reszty i giną w hałasie auta. Wyrównujemy obwiednię, potem
        miękkie ograniczenie (tanh) zamiast twardego przesteru."""
        import wave
        try:
            with wave.open(path, "rb") as w:
                params = w.getparams()
                if params.sampwidth != 2 or params.nframes < 100:
                    return
                data = w.readframes(params.nframes)
            a = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
            if not a.size:
                return
            # obwiednia: średnia krocząca |sygnału| w oknie ~25 ms
            win = max(64, int(params.framerate * 0.025))
            env = np.convolve(np.abs(a), np.ones(win) / win, mode="same")
            floor = max(1e-4, float(np.percentile(env, 60)) * 0.12)
            env = np.maximum(env, floor)
            # 1) kompresja: podbij ciche fragmenty (wykładnik <1)
            g = np.clip((target_rms / env) ** 0.6, 0.4, 10.0)
            a = a * g
            # 2) wzmocnienie wyrównawcze — po kompresji poziom bywa niższy
            #    niż docelowy; bez tego całość była nadal cicha
            rms = float(np.sqrt(np.mean(a * a)))
            if rms > 1e-5:
                a *= min(6.0, target_rms / rms)
            a *= max(1.0, TTS_GAINS.get(kind, 1.6))
            # 3) miękki limiter zamiast twardego przesteru
            a = np.tanh(a * 1.15) * 0.98
            out = np.clip(a * 32767.0, -32768, 32767).astype(np.int16)
            with wave.open(path, "wb") as w:
                w.setparams(params)
                w.writeframes(out.tobytes())
        except Exception as e:
            print(f"[Głos] normalizacja pominięta: {e}")

    def synth_bytes(self, text: str, kind: str = "system"):
        """WAV jako bajty — do odtworzenia w PRZEGLĄDARCE użytkownika."""
        path = "/tmp/lpr_tts_preview.wav"
        if not self.synth(text, path, kind):
            return None
        try:
            with open(path, "rb") as f:
                return f.read()
        except OSError:
            return None

    def _loop(self) -> None:
        import subprocess as _sp
        _ustap_procesor(10)      # synteza nie może dławić rozpoznawania
        while True:
            zadanie = self._q.get()
            if zadanie is None:
                break
            text, kind = zadanie
            self._gra = True
            path = "/tmp/lpr_tts.wav"
            if not self.synth(text, path, kind):
                continue
            try:
                env = os.environ.copy()
                env.setdefault("XDG_RUNTIME_DIR", "/run/user/1000")
                _sp.run(["pw-play", path], env=env,
                        capture_output=True, timeout=30)
            except Exception as e:
                print(f"[Głos] błąd odtwarzania: {e}")
            finally:
                self._gra = False


SPEECH = SpeechWorker()


# Nazwy znaków po polsku — bez tego syntezator sklejał "WA" w sylabę
# zamiast przeliterować ("wu a"). Cyfry też słownie: pewniejsze niż licząc
# na to, że TTS nie połączy "1 2" w "dwanaście".
_SPELL = {
    "A": "a", "B": "be", "C": "ce", "D": "de", "E": "e", "F": "ef",
    "G": "gie", "H": "ha", "I": "i", "J": "jot", "K": "ka", "L": "el",
    "M": "em", "N": "en", "O": "o", "P": "pe", "Q": "ku", "R": "er",
    "S": "es", "T": "te", "U": "u", "V": "fał", "W": "wu", "X": "iks",
    "Y": "igrek", "Z": "zet",
    "0": "zero", "1": "jeden", "2": "dwa", "3": "trzy", "4": "cztery",
    "5": "pięć", "6": "sześć", "7": "siedem", "8": "osiem", "9": "dziewięć",
}


# Alfabet fonetyczny — pojedyncze samogłoski ("a", "e") syntezator wypowiada
# krótko i cicho, giną przy hałasie w aucie. Imiona są jednoznaczne.
_SPELL_PHON = {
    "A": "Adam", "B": "Barbara", "C": "Celina", "D": "Dorota", "E": "Ewa",
    "F": "Franciszek", "G": "Gustaw", "H": "Henryk", "I": "Irena",
    "J": "Jan", "K": "Karol", "L": "Leon", "M": "Maria", "N": "Natalia",
    "O": "Olga", "P": "Paweł", "Q": "Quebec", "R": "Roman", "S": "Stefan",
    "T": "Tadeusz", "U": "Urszula", "V": "Violetta", "W": "Wanda",
    "X": "Xawery", "Y": "Ypsylon", "Z": "Zygmunt",
}


def _spell_plate(plate: str) -> str:
    """'WA12345' → 'wu a, jeden dwa trzy cztery pięć' (tryb liter)
    albo 'Wanda Adam, jeden dwa...' (tryb fonetyczny — czytelniejszy
    w hałasie). Przecinek po literach powiatowych = naturalna pauza."""
    phon = TTS_SPELL_MODE == "phonetic"
    out, prev_alpha = [], None
    for ch in plate.upper():
        name = (_SPELL_PHON.get(ch) if phon else None) or _SPELL.get(ch)
        if not name:
            continue
        is_alpha = ch.isalpha()
        if prev_alpha is True and not is_alpha:
            out.append(",")          # przejście litery → cyfry: pauza
        out.append(name)
        prev_alpha = is_alpha
    return " ".join(out).replace(" ,", ",")


# notatki, które NIE są marką (zaślepki z importów) — nie ma czego czytać
_NIE_MARKA = {"radiowoz", "radiowóz", "brak", "brak marki", "nieznany",
              "nieznana", "auto", "pojazd", "samochod", "samochód", "-", "?"}
# cyfry generacji w nazwach modeli: "Superb IV" → "Superb cztery"
_RZYMSKIE = {"II": "dwa", "III": "trzy", "IV": "cztery", "V": "pięć",
             "VI": "sześć", "VII": "siedem", "VIII": "osiem"}


def _brand_speak(note: str) -> str:
    """Notatka z bazy → sama MARKA gotowa do czytania, albo "" gdy jej brak.

    Notatki z importu CSV mają postać "BMW 330i · Dolnośląskie" — do
    komunikatu bierzemy tylko pierwszy człon. Województwo zostaje na
    banerze; w aucie nikomu nie jest potrzebne w komunikacie głosowym.
    Skróty czyta się literami ("VZ" → "wu zet"), bo Piper czyta je jak
    wyrazy; cyfry generacji zamieniamy na słowa."""
    marka = str(note or "").split("·")[0].split(";")[0].strip(" ,.-")
    if (not marka or len(marka) > 40
            or marka.lower() in _NIE_MARKA
            or not any(c.isalpha() for c in marka)):
        return ""
    out = []
    for tok in marka.split():
        if tok.upper() in _RZYMSKIE:
            out.append(_RZYMSKIE[tok.upper()])
        elif len(tok) == 2 and tok.isalpha() and tok.isupper():
            out.append(" ".join(_SPELL.get(c, c) for c in tok))
        else:
            out.append(tok)
    return " ".join(out)


def _alert_speak(gid: str, plate: str, note: str = "",
                 pewne: bool = True) -> str:
    """Buduje komunikat z szablonu bazy ({tablica} → literowany numer).
    Marka pojazdu doczytywana tylko gdy włączona i gdy baza ją zna.
    Przy dopasowaniu po korekcie mylonych znaków komunikat MUSI zaczynać
    się od „niepotwierdzony" — inaczej brzmiałby jak pewne trafienie."""
    g = WATCH_GROUPS.get(gid) or _watch_default_group()
    tmpl = g.get("alert") or _watch_default_group()["alert"]
    if not pewne:
        tmpl = "Niepotwierdzony! " + tmpl
    msg = tmpl.replace("{tablica}", _spell_plate(plate))
    # Doczytywanie notatki musi być zgodne NA OBU poziomach: globalnym
    # (menu w aucie) i danej bazy. Bazy różnią się treścią notatki —
    # w jednej to marka auta, w innej miasto, a wtedy bywa zbędna.
    if TTS_BRAND and g.get("nota", True):
        marka = _brand_speak(note)
        if marka:
            msg += " " + marka + "."
    return msg


def _alert_example(gid: str) -> tuple:
    """(tablica, notatka) do podglądu komunikatu — pierwszy wpis z bazy,
    który ma markę. Gdy baza pusta albo bez marek: przykład zastępczy."""
    zapas = None
    for p, e in WATCHLIST.items():
        if e.get("group") != gid:
            continue
        if _brand_speak(e.get("note") or ""):
            return p, e.get("note") or ""
        if zapas is None:
            zapas = (p, e.get("note") or "")
    return zapas or ("WA12345", "")


# Znaki, które OCR notorycznie myli (kształt jest niemal ten sam).
# Sprowadzamy je do jednego reprezentanta, żeby „WXA31O11" i „WXA3IO11"
# dały tę samą postać kanoniczną co „WXA31011" z bazy.
_MYLONE_ZNAKI = {"I": "1", "S": "5", "D": "0", "O": "0",
                 "B": "8", "Z": "2", "G": "6", "Q": "0"}
_watch_kanon: dict = {}          # postać kanoniczna → [tablice z bazy]
_watch_kanon_zrodlo = -1         # rozmiar WATCHLIST, dla którego zbudowano


def _kanon_tablica(p: str) -> str:
    return "".join(_MYLONE_ZNAKI.get(c, c) for c in str(p).upper())


def _watch_kanon_odswiez() -> None:
    """Indeks kanoniczny bazy. Przebudowywany, gdy baza się zmieni."""
    global _watch_kanon, _watch_kanon_zrodlo
    if _watch_kanon_zrodlo == len(WATCHLIST):
        return
    idx: dict = {}
    for p in WATCHLIST:
        idx.setdefault(_kanon_tablica(p), []).append(p)
    _watch_kanon = idx
    _watch_kanon_zrodlo = len(WATCHLIST)


def _watch_dopasuj(plate: str):
    """(tablica_z_bazy, pewne) — czy odczyt pasuje do bazy.

    `pewne=True`  — zgadza się znak w znak.
    `pewne=False` — zgadza się PO ujednoliceniu mylonych znaków (I/1, S/5,
                    D/0/O …). Wtedy to tylko podejrzenie i komunikat musi
                    to jasno powiedzieć, żeby nie brać go za pewnik."""
    if plate in WATCHLIST:
        return plate, True
    _watch_kanon_odswiez()
    kand = _watch_kanon.get(_kanon_tablica(plate)) or []
    return (kand[0], False) if kand else (None, False)


def _alert_trigger(plate: str, vehicle_img=None, pewne: bool = True,
                   z_bazy: str = "") -> None:
    """Trafienie w bazie: baner + zdjęcie pojazdu na ekranie + głos.
    `pewne=False` → odczyt pasuje dopiero po korekcie mylonych znaków."""
    global _alert_text, _alert_until, _alert_color, _alert_img
    klucz = z_bazy or plate
    entry = WATCHLIST.get(klucz) or {}
    gid = entry.get("group") or "default"
    g = WATCH_GROUPS.get(gid) or _watch_default_group()
    if not g.get("enabled", True):
        return                       # baza wyłączona — bez alertu
    note = entry.get("note") or ""
    if pewne:
        _alert_text = (f"{plate}  [{g.get('name', '?')}]"
                       + (f"  {note}" if note else ""))
        _alert_color = g.get("color") or "red"
    else:
        # widać OBA numery: co odczytano i co jest w bazie
        _alert_text = (f"? {plate} = {klucz}  [{g.get('name', '?')}]"
                       + (f"  {note}" if note else ""))
        _alert_color = "orange"
    _alert_until = time.monotonic() + 8.0
    _alert_img = vehicle_img
    # Baner i zdjęcie pokazujemy zawsze — mowa tylko, gdy ta tablica nie
    # była czytana przed chwilą (jazda za tym samym autem).
    if _mowa_dozwolona(klucz):
        SPEECH.say(_alert_speak(gid, klucz, note, pewne), "alert")
        print(f"[Alert] {g.get('name')}: {plate}"
              + ("" if pewne else f" ~ {klucz} (niepotwierdzony)")
              + f" {note}")
    else:
        print(f"[Alert] {g.get('name')}: {plate} (bez mowy — powtórzenie)")


def _bt_run(args: list, timeout: int = 12) -> str:
    import subprocess as _sp
    try:
        r = _sp.run(["bluetoothctl"] + args, capture_output=True,
                    text=True, timeout=timeout)
        return r.stdout
    except Exception as e:
        return f"blad: {e}"


def _bt_connected_devices() -> list:
    """Lista `(mac, nazwa)` aktualnie połączonych urządzeń."""
    out = []
    for line in _bt_run(["devices", "Connected"]).splitlines():
        p = line.split(maxsplit=2)
        if len(p) >= 3 and p[0] == "Device":
            out.append((p[1], p[2]))
    return out


def _bt_connected() -> tuple:
    """(mac, nazwa) aktywnego urządzenia albo ('','').

    UWAGA: to jest fork+exec+D-Bus — 20–30 ms, a przy zajętym bluetoothd
    nawet kilka sekund. WOLNO wołać TYLKO z wątków tła. Wątek rysowania
    ma używać `_bt_live_get()`."""
    devs = _bt_connected_devices()
    cel = BT_DEVICE.upper()
    for mac, nazwa in devs:
        if cel and mac.upper() == cel:
            return mac, nazwa
    return devs[0] if devs else ("", "")


# Ostatni znany stan głośnika. Rysowanie czyta WYŁĄCZNIE stąd — inaczej
# każda klatka podglądu odpalała bluetoothctl i pętla GLib (podgląd +
# magistrala GStreamera) stawała na kilkadziesiąt ms.
_bt_live = {"mac": "", "name": "", "ts": 0.0}


def _bt_live_refresh() -> tuple:
    """Odświeża pamięć podręczną. Tylko z wątków tła!"""
    try:
        mac, nm = _bt_connected()
    except Exception:
        return _bt_live["mac"], _bt_live["name"]
    _bt_live.update(mac=mac, name=nm, ts=time.monotonic())
    return mac, nm


def _bt_live_get() -> tuple:
    """Bezpieczne dla wątku rysowania: zero podprocesów, zero blokad."""
    return _bt_live["mac"], _bt_live["name"]


# ── Malinka jako URZĄDZENIE: zestaw w aucie łączy się z NIĄ ──────────────────
# Dwa kierunki łączenia, oba potrzebne:
#   1. malinka → głośnik (JBL): to my inicjujemy, urządzenie musi być w pobliżu
#   2. auto → malinka: zestaw samochodowy szuka i łączy się sam — wtedy malinka
#      musi być WIDOCZNA i mieć agenta, który przyjmie parowanie
BT_ALIAS = "LPR"           # nazwa widoczna w menu Bluetooth samochodu
_bt_agent_proc = None      # długowieczny bluetoothctl trzymający agenta
_bt_vis_until = 0.0        # do kiedy malinka jest widoczna


def _bt_agent_start() -> bool:
    """Agent parowania — bez niego BlueZ ODRZUCA żądanie z auta, bo nie ma
    kto go potwierdzić (na ekranie w aucie i tak nie wpiszemy kodu).

    Agent żyje tylko dopóki działa `bluetoothctl`, więc trzymamy go jako
    długowieczny podproces. Tryb NoInputNoOutput = parowanie „Just Works",
    bez kodu PIN."""
    global _bt_agent_proc
    import subprocess as _sp
    if _bt_agent_proc is not None and _bt_agent_proc.poll() is None:
        return True
    try:
        _bt_agent_proc = _sp.Popen(["bluetoothctl"], stdin=_sp.PIPE,
                                   stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
        _bt_agent_proc.stdin.write(b"agent NoInputNoOutput\ndefault-agent\n")
        _bt_agent_proc.stdin.flush()      # stdin zostaje OTWARTE — inaczej padnie
        print("[BT] agent parowania gotowy (auto może się połączyć)")
        return True
    except Exception as e:
        print(f"[BT] agent parowania: {e}")
        return False


def _bt_vis_left() -> float:
    """Ile sekund jeszcze widoczna (0 = niewidoczna). Bez podprocesów."""
    return max(0.0, _bt_vis_until - time.monotonic())


def _bt_set_visible(secs: int = 300) -> bool:
    """Włącz widoczność, żeby zestaw w aucie mógł znaleźć malinkę."""
    global _bt_vis_until
    _bt_agent_start()
    _bt_run(["system-alias", BT_ALIAS], timeout=6)
    _bt_run(["pairable", "on"], timeout=6)
    _bt_run(["discoverable-timeout", str(secs)], timeout=6)
    out = _bt_run(["discoverable", "on"], timeout=8)
    ok = "succeed" in out.lower()
    _bt_vis_until = (time.monotonic() + secs) if ok else 0.0
    print("[BT] widoczność jako %s: %s" % (
        BT_ALIAS, f"{secs} s" if ok
        else f"NIE UDAŁO SIĘ ({out.strip()[:60]})"))
    return ok


def _bt_set_hidden() -> None:
    global _bt_vis_until
    _bt_vis_until = 0.0
    _bt_run(["discoverable", "off"], timeout=8)


def _audio_bt_node(mac: str):
    """(id, opis, nazwa węzła) dla głośnika BT albo (None, "", "").

    Węzeł nazywa się bluez_output.<MAC z podkreśleniami>[.N]."""
    import json as _json
    import subprocess as _sp
    # CAŁY klucz wielkimi literami — porównujemy z `nazwa.upper()`. Wcześniej
    # prefiks „bluez_output." zostawał mały, więc warunek NIGDY nie był
    # prawdziwy: węzeł głośnika nie był znajdowany, przez co nie działała
    # głośność (menu „—"), pasek głośności ani przełączanie wyjścia.
    key = ("bluez_output." + mac.replace(":", "_")).upper()
    try:
        dump = _sp.run(["pw-dump"], capture_output=True, text=True,
                       timeout=10).stdout
        for o in _json.loads(dump):
            props = (o.get("info") or {}).get("props") or {}
            nazwa = str(props.get("node.name", ""))
            if (nazwa.upper().startswith(key)
                    and "Audio/Sink" in str(props.get("media.class", ""))):
                return (o["id"], str(props.get("node.description") or nazwa),
                        nazwa)
    except Exception as e:
        print(f"[Dźwięk] odczyt węzłów: {e}")
    return None, "", ""


def _audio_bt_id(mac: str):
    """Zgodny wstecz wrapper zwracający tylko `(id, opis)`."""
    wid, opis, _target = _audio_bt_node(mac)
    return wid, opis


def _audio_domyslny_id():
    """Id aktualnie domyślnego wyjścia (gwiazdka w `wpctl status`)."""
    import re as _re
    import subprocess as _sp
    try:
        out = _sp.run(["wpctl", "status"], capture_output=True, text=True,
                      timeout=8).stdout
        blok = out.split("Sinks:")[1].split("Sources:")[0]
        m = _re.search(r"\*\s+(\d+)\.", blok)
        return int(m.group(1)) if m else None
    except Exception:
        return None


_bt_audio_lock = threading.Lock()
_bt_audio_live = {"mac": "", "id": None, "target": "",
                  "volume": None, "muted": False, "ts": 0.0}
# Overlay głośności na ekranie RP5 — pasek pojawia się przy zmianie głośności
# (np. przyciskiem na JBL) i po chwili znika, jak na telefonie.
_vol_overlay = {"until": 0.0, "val": 0, "muted": False}


def _audio_live_node(mac: str, wid, target: str) -> None:
    with _bt_audio_lock:
        if _bt_audio_live["mac"] != mac or _bt_audio_live["id"] != wid:
            _bt_audio_live.update(volume=None, muted=False)
        _bt_audio_live.update(mac=mac, id=wid, target=target,
                              ts=time.monotonic())


def _audio_live_clear() -> None:
    with _bt_audio_lock:
        _bt_audio_live.update(mac="", id=None, target="", volume=None,
                              muted=False, ts=time.monotonic())


def _audio_live_get() -> dict:
    """Stan master volume bez podprocesów — bezpieczny dla renderowania i API."""
    with _bt_audio_lock:
        return dict(_bt_audio_live)


def _audio_volume_refresh(mac: str, wid) -> bool:
    """Czyta wspólną głośność PipeWire/JBL (AVRCP absolute volume)."""
    import subprocess as _sp
    try:
        r = _sp.run(["wpctl", "get-volume", str(wid)], capture_output=True,
                    text=True, timeout=5)
        m = re.search(r"Volume:\s*([0-9.]+)", r.stdout)
        if r.returncode != 0 or not m:
            return False
        volume = max(0, min(100, int(round(float(m.group(1)) * 100))))
        muted = "[MUTED]" in r.stdout.upper()
        changed = False
        with _bt_audio_lock:
            if _bt_audio_live["mac"] == mac and _bt_audio_live["id"] == wid:
                prev = _bt_audio_live.get("volume")
                prev_m = _bt_audio_live.get("muted")
                _bt_audio_live.update(volume=volume, muted=muted,
                                      ts=time.monotonic())
                changed = (prev is not None
                           and (volume != prev or muted != prev_m))
        if changed:   # realna zmiana (przycisk JBL / menu / web) → pokaż pasek
            _vol_overlay.update(until=time.monotonic() + 2.5,
                                val=volume, muted=muted)
        return True
    except Exception:
        return False


def _audio_set_volume_percent(value) -> bool:
    """Ustawia master volume. Przy hw-volume zmiana trafia też fizycznie do JBL."""
    import subprocess as _sp
    try:
        volume = max(0, min(100, int(round(float(value)))))
    except (TypeError, ValueError):
        return False
    state = _audio_live_get()
    mac, _name = _bt_live_get()
    wid = state["id"] if state["mac"] == mac else None
    if mac and wid is None:
        wid, _opis, target = _audio_bt_node(mac)
        if wid is not None:
            _audio_live_node(mac, wid, target)
    if not mac or wid is None:
        return False
    try:
        r = _sp.run(["wpctl", "set-volume", str(wid),
                     f"{volume / 100.0:.2f}"],
                    capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            return False
        _audio_volume_refresh(mac, wid)
        return True
    except Exception:
        return False


def _audio_bt_sink(mac: str, proby: int = 12) -> bool:
    """Przełącz domyślne wyjście dźwięku na głośnik BT.

    PONAWIAMY, bo BlueZ zgłasza połączenie ZANIM PipeWire zdąży utworzyć
    węzeł audio — pojedyncza próba trafiała w pustkę i mowa dalej szła
    w HDMI (objaw: „głośnik połączony, a cisza")."""
    import subprocess as _sp
    for i in range(max(1, proby)):
        wid, opis, target = _audio_bt_node(mac)
        if wid is not None:
            try:
                _sp.run(["wpctl", "set-default", str(wid)], timeout=8,
                        capture_output=True)
                # Nie ustawiaj 100% przy każdym połączeniu. PipeWire pamięta
                # ostatnią wspólną głośność, a hw-volume synchronizuje ją z JBL.
                _audio_live_node(mac, wid, target)
                _audio_volume_refresh(mac, wid)
                _keepalive_start(target)
                print(f"[Dźwięk] wyjście → {opis}")
                return True
            except Exception as e:
                print(f"[Dźwięk] nie przełączyłem wyjścia: {e}")
                return False
        time.sleep(1.0)
    print("[Dźwięk] węzeł głośnika BT nie pojawił się — zostaje HDMI")
    return False


# Bardzo cichy sygnał podtrzymujący. JBL (i wiele głośników BT) wyłącza się
# po chwili bezczynności — a PipeWire przy ciszy zawiesza strumień A2DP.
# Odtwarzanie ledwie słyszalnego dźwięku co pół minuty utrzymuje połączenie.
# UWAGA: świadomie NIE zmieniamy konfiguracji WirePlumbera
# (`session.suspend-timeout-seconds`) — próba takiej zmiany ustawiła
# urządzeniu profil „off" i całkiem odcięła dźwięk.
KEEPALIVE_PLIK = "/tmp/lpr_keepalive.wav"
_keepalive_proc = None
_keepalive_target = ""


def _keepalive_utworz() -> bool:
    """Plik podtrzymujący: ciągły ton 40 Hz. JBL Go 3 (pasmo od ~110 Hz)
    fizycznie go NIE odtworzy, więc jest niesłyszalny, ale to realny sygnał
    A2DP — głośnik nie uznaje bezczynności i się nie wyłącza.

    Poprzednia wersja miała dwie wady: (1) grała IMPULS co 30 s, a między
    impulsami głośnik zasypiał; (2) amplituda ~0,1% była cyfrowo tak niska,
    że głośnik traktował to jak ciszę. Dlatego JBL dalej się rozłączał."""
    import wave
    try:
        sr = 48000
        n = sr * 20                       # 20 s, odtwarzane w pętli
        t = np.arange(n, dtype=np.float32) / sr
        # ~9% skali: niesłyszalne przy 40 Hz na małym głośniku, ale to dla
        # protokołu wyraźny, aktywny strumień
        fala = (np.sin(2 * np.pi * 40.0 * t) * 3000.0).astype(np.int16)
        stereo = np.repeat(fala[:, None], 2, axis=1)      # 2 kanały (A2DP)
        with wave.open(KEEPALIVE_PLIK, "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes(stereo.tobytes())
        return True
    except Exception as e:
        print(f"[Dźwięk] keep-alive plik: {e}")
        return False


def _keepalive_gra(target: str = "") -> bool:
    global _keepalive_proc
    gra = _keepalive_proc is not None and _keepalive_proc.poll() is None
    return gra and (not target or target == _keepalive_target)


def _keepalive_start(target: str) -> None:
    """Ciągły strumień przypięty do konkretnego węzła JBL.

    Użycie wyjścia `auto` było zawodne: proces mógł dalej działać, ale po zmianie
    domyślnego wyjścia wysyłał podtrzymanie do HDMI. Jawny `--target` gwarantuje,
    że aktywny strumień A2DP faktycznie dochodzi do wybranego głośnika."""
    global _keepalive_proc, _keepalive_target
    import subprocess as _sp
    if not target:
        return
    if _keepalive_gra(target):
        return
    if _keepalive_gra():
        _keepalive_stop()
    if not os.path.exists(KEEPALIVE_PLIK) and not _keepalive_utworz():
        return
    env = os.environ.copy()
    env.setdefault("XDG_RUNTIME_DIR", "/run/user/1000")
    try:
        _keepalive_proc = _sp.Popen(
            ["bash", "-c",
             'while :; do pw-play --target "$1" --volume=0.35 "$2" '
             '|| sleep 2; done',
             "lpr-keepalive", target, KEEPALIVE_PLIK],
            env=env, stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
        _keepalive_target = target
        print(f"[Dźwięk] keep-alive JBL → {target} (ciągły ton 40 Hz)")
    except Exception as e:
        _keepalive_target = ""
        print(f"[Dźwięk] keep-alive start: {e}")


def _keepalive_stop() -> None:
    global _keepalive_proc, _keepalive_target
    if _keepalive_proc is not None:
        try:
            _keepalive_proc.terminate()
            import subprocess as _sp
            _sp.run(["pkill", "-f", KEEPALIVE_PLIK], capture_output=True,
                    timeout=5)
        except Exception as e:
            _zglos("keepalive_stop", e)
        _keepalive_proc = None
    _keepalive_target = ""


def _audio_straznik() -> None:
    """Pilnuje trasy, keep-alive i wspólnego master volume JBL/PipeWire."""
    import subprocess as _sp
    _ustap_procesor(12)
    _keepalive_utworz()
    node_mac, wid, target = "", None, ""
    ostatnie_utrzymanie = 0.0
    ostatnie_volume = 0.0
    while True:
        time.sleep(1.0)
        try:
            mac, _nm = _bt_live_get()
            if not mac:
                node_mac, wid, target = "", None, ""
                _audio_live_clear()
                if _keepalive_gra():
                    _keepalive_stop()
                continue
            if mac != node_mac or wid is None:
                wid, _opis, target = _audio_bt_node(mac)
                node_mac = mac
                if wid is None:
                    _audio_live_clear()
                    if _keepalive_gra():
                        _keepalive_stop()
                    continue
                _audio_live_node(mac, wid, target)
            teraz = time.monotonic()
            # Odczyt głośności NIE służy już do pokazywania jej na ekranie —
            # od tego jest `_audio_zdarzenia` (pw-mon, reakcja < 0,3 s). Tutaj
            # został wyłącznie jako TEST ŻYCIA węzła audio, a do tego w zupeł-
            # ności wystarczy co 10 s. Wcześniej `wpctl get-volume` (46 ms)
            # startował CO SEKUNDĘ — a każdy podproces to rozwidlenie procesu
            # z ~1 GB pamięci i 61 wątkami, czyli realne przestoje.
            if teraz - ostatnie_volume >= 10.0:
                ostatnie_volume = teraz
                if not _audio_volume_refresh(mac, wid):
                    # Transport zniknął wcześniej niż odświeżył się bluetoothctl.
                    wid, target = None, ""
                    _audio_live_clear()
                    if _keepalive_gra():
                        _keepalive_stop()
                    continue
            if teraz - ostatnie_utrzymanie < 15.0:   # było 5 s — też podproces
                continue
            ostatnie_utrzymanie = teraz
            if _audio_domyslny_id() != wid:      # coś przestawiło wyjście
                _sp.run(["wpctl", "set-default", str(wid)], timeout=8,
                        capture_output=True)
                print("[Dźwięk] przywrócono wyjście na głośnik")
            if not _keepalive_gra(target):
                _keepalive_start(target)
        except Exception as e:
            print(f"[Dźwięk] strażnik: {e}")


def _bt_known() -> list:
    out = []
    for line in _bt_run(["devices"]).splitlines():
        p = line.split(maxsplit=2)
        if len(p) >= 3 and p[0] == "Device":
            out.append((p[1], p[2]))
    return out


def _bt_connect(mac: str) -> bool:
    global BT_DEVICE
    out = _bt_run(["connect", mac], timeout=20)
    ok = "successful" in out.lower()
    if ok:
        BT_DEVICE = mac
        _save_runtime_config()
        _bt_live_refresh()
        _audio_bt_sink(mac)
    return ok


def _audio_zdarzenia() -> None:
    """Natychmiastowa reakcja na zmianę głośności (przyciski JBL / AVRCP).

    Strażnik odpytuje co 1 s, więc pasek głośności pojawiał się z widocznym
    opóźnieniem. `wpctl get-volume` kosztuje ~46 ms, więc szybkie odpytywanie
    odpada (5×/s ≈ 25% rdzenia). `pw-mon` zgłasza zmianę węzła OD RAZU, a w
    spoczynku nasz węzeł „rusza się" ~2 razy na 18 s — czyli odczyt robimy
    tylko wtedy, gdy naprawdę coś się zmieniło (koszt pomijalny)."""
    import subprocess as _sp
    _ustap_procesor(12)
    while True:
        proc = None
        try:
            env = os.environ.copy()
            env.setdefault("XDG_RUNTIME_DIR", "/run/user/1000")
            proc = _sp.Popen(["pw-mon"], stdout=_sp.PIPE, stderr=_sp.DEVNULL,
                             text=True, bufsize=1, env=env)
            czekaj_id = False
            ostatnie = 0.0
            for line in proc.stdout:
                s = line.strip()
                if s == "changed:":       # blok zmiany; w NASTĘPNEJ linii jest id
                    czekaj_id = True
                    continue
                if not czekaj_id:
                    continue
                czekaj_id = False
                if not s.startswith("id:"):
                    continue
                state = _audio_live_get()
                wid = state.get("id")
                if wid is None:
                    continue
                try:
                    if int(s.split(":", 1)[1].strip()) != int(wid):
                        continue
                except (ValueError, IndexError):
                    continue
                teraz = time.monotonic()
                if teraz - ostatnie < 0.10:   # zbij serię zdarzeń w jedno
                    continue
                ostatnie = teraz
                _audio_volume_refresh(state["mac"], wid)
        except FileNotFoundError:
            print("[Dźwięk] brak pw-mon — pasek głośności bez natychmiastowej "
                  "reakcji (zostaje odpytywanie strażnika co 1 s)")
            return
        except Exception as e:
            print(f"[Dźwięk] monitor zdarzeń: {e}")
        finally:
            if proc is not None:
                try:
                    proc.kill()
                except Exception as e:
                    _zglos("pw_mon_kill", e)
        time.sleep(3.0)          # pw-mon padł → spróbuj ponownie


def _bt_autoconnect() -> None:
    """Pilnuje zapamiętanego głośnika: łączy przy starcie i PO ZERWANIU.

    Wcześniej były tylko trzy próby przy starcie — gdy głośnik wyszedł poza
    zasięg albo został wyłączony i włączony, nic go już nie podnosiło i alerty
    leciały w HDMI. Teraz wątek żyje cały czas: po aktywnym połączeniu sprawdza
    je co 5 s, a po nieudanej próbie odczekuje coraz dłużej (maks. 60 s), żeby
    szybko wrócić po zerwaniu, ale nie zasypywać
    bluetoothctl przy głośniku wyłączonym na stałe.
    """
    def work():
        # SPRAWDZANIE stanu (tanie) i PRÓBY łączenia (drogie, timeout 20 s) mają
        # osobne tempo. Wcześniej wspólna `przerwa` rosła do 60 s po nieudanych
        # próbach — a że była pauzą na początku pętli, po włączeniu głośnika
        # malinka zauważała go nawet minutę później (brak powiadomienia i ikony
        # mimo faktycznie działającego połączenia).
        odstep = 5.0              # backoff wyłącznie między PRÓBAMI łączenia
        nastepna_proba = 0.0
        bylo_polaczone = False
        _bt_agent_start()           # auto może się połączyć w każdej chwili
        _bt_run(["system-alias", BT_ALIAS], timeout=6)
        _bt_run(["pairable", "on"], timeout=6)
        _bt_live_refresh()          # żeby kafel nie kłamał przez pierwsze 20 s
        while True:
            time.sleep(3.0)              # stan sprawdzamy ZAWSZE co 3 s
            cel = BT_DEVICE
            if not cel:
                # sparowany, ale nigdy nie zapamiętany (np. parowanie z www)
                znane = [m for m, _n in _bt_known()]
                cel = znane[0] if znane else ""
            if not cel:
                time.sleep(20.0)         # nic sparowanego — nie ma czego pilnować
                continue
            _lista_bt = _bt_connected_devices()
            polaczone = {m.upper() for m, _n in _lista_bt}
            nazwy = {m.upper(): n for m, n in _lista_bt}
            _bt_live_refresh()
            if cel.upper() in polaczone:
                nazwa_bt = nazwy.get(cel.upper(), "głośnik")
                if not bylo_polaczone:
                    toast(f"Połączono: {nazwa_bt}", "bt", "green")
                    # Zadziała też dla połączenia PRZYCHODZĄCEGO (auto samo
                    # połączyło się z malinką) — wtedy trzeba je zapamiętać
                    # i przerzucić dźwięk, bo inaczej mowa leci w HDMI.
                    print(f"[BT] połączony: {cel}")
                    _bt_run(["trust", cel], timeout=8)
                    globals()["BT_DEVICE"] = cel
                    _save_runtime_config()
                    _audio_bt_sink(cel)
                bylo_polaczone = True
                odstep, nastepna_proba = 5.0, 0.0
                continue
            if bylo_polaczone:
                print("[BT] połączenie zerwane — próbuję wrócić")
                toast("Rozłączono głośnik", "x", "red")
                bylo_polaczone = False
                nastepna_proba = 0.0          # po zerwaniu próbuj natychmiast
            if time.monotonic() < nastepna_proba:
                continue                      # czekamy z kolejną PRÓBĄ, ale
                                              # stan i tak sprawdzamy co 3 s
            if _bt_connect(cel):
                print(f"[BT] głośnik odzyskany ({cel})")
                toast("Głośnik z powrotem", "bt", "green")
                bylo_polaczone = True
                odstep, nastepna_proba = 5.0, 0.0
            else:
                odstep = min(60.0, max(5.0, odstep * 1.6))
                nastepna_proba = time.monotonic() + odstep
    threading.Thread(target=work, daemon=True, name="bt-auto").start()


# ── Bluetooth: bogaty panel www (jak w telefonie) ────────────────────────────

_bt_scan_until = 0.0
_bt_status_cache = (0.0, None)


def _bt_powered() -> bool:
    return "Powered: yes" in _bt_run(["show"], timeout=8)


def _bt_info(mac: str) -> dict:
    """Szczegóły urządzenia z `bluetoothctl info`: nazwa, typ, stan, bateria."""
    d = {"mac": mac, "name": mac, "icon": "generic", "paired": False,
         "connected": False, "trusted": False, "battery": None}
    for line in _bt_run(["info", mac], timeout=5).splitlines():
        s = line.strip()
        if s.startswith("Name:"):
            d["name"] = s[5:].strip()
        elif s.startswith("Alias:") and (d["name"] == mac or not d["name"]):
            d["name"] = s[6:].strip()
        elif s.startswith("Icon:"):
            ic = s[5:].strip()
            d["icon"] = ("headphones" if "head" in ic
                         else "speaker" if "audio" in ic
                         else "phone" if "phone" in ic
                         else "car" if ("car" in ic or "multimedia" in ic)
                         else "generic")
        elif s.startswith("Paired:"):
            d["paired"] = "yes" in s
        elif s.startswith("Connected:"):
            d["connected"] = "yes" in s
        elif s.startswith("Trusted:"):
            d["trusted"] = "yes" in s
        elif "Battery Percentage" in s:
            m = re.search(r"\((\d+)\)", s)
            if m:
                d["battery"] = int(m.group(1))
    return d


def _bt_start_scan(seconds: int = 12) -> None:
    global _bt_scan_until
    _bt_scan_until = time.monotonic() + seconds
    threading.Thread(
        target=lambda: _bt_run(["--timeout", str(seconds), "scan", "on"],
                               timeout=seconds + 6),
        daemon=True, name="bt-scan").start()


def _bt_devlist(arg: str = "") -> dict:
    """{mac: nazwa} z `bluetoothctl devices [Paired|Connected]` — szybkie."""
    out = {}
    args = ["devices", arg] if arg else ["devices"]
    for line in _bt_run(args, timeout=8).splitlines():
        p = line.split(maxsplit=2)
        if len(p) >= 2 and p[0] == "Device":
            out[p[1]] = p[2].strip() if len(p) >= 3 else p[1]
    return out


_MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}[:\-]){5}[0-9A-Fa-f]{2}$")


def _bt_status(force: bool = False) -> dict:
    """Stan dla panelu www. SZYBKO: nazwy/stan z listowania (kilka wywołań),
    a kosztowne `info` (ikona/bateria) TYLKO dla sparowanych (jest ich mało).
    Bezimienne beacony BLE pomijamy — jak w telefonie."""
    global _bt_status_cache
    ts, cached = _bt_status_cache
    if cached and not force and time.monotonic() - ts < 2.0:
        cached = dict(cached)
        cached["scanning"] = time.monotonic() < _bt_scan_until
        return cached
    names = _bt_devlist()
    paired = set(_bt_devlist("Paired"))
    connected = set(_bt_devlist("Connected"))
    devs = []
    for mac, name in names.items():
        is_paired = mac in paired
        named = name and name != mac and not _MAC_RE.match(name.replace("_", ":"))
        if not is_paired and not named:
            continue                      # nienazwany beacon — pomiń
        d = {"mac": mac, "name": name, "icon": "generic",
             "paired": is_paired, "connected": mac in connected,
             "battery": None}
        if is_paired:                     # tylko sparowane → mało wywołań info
            info = _bt_info(mac)
            d["icon"] = info["icon"]
            d["battery"] = info["battery"]
            if info["name"] and info["name"] != mac:
                d["name"] = info["name"]
        devs.append(d)
    devs.sort(key=lambda x: (not x["connected"], not x["paired"],
                             x["name"].lower()))
    st = {"powered": _bt_powered(), "default": BT_DEVICE, "devices": devs}
    _bt_status_cache = (time.monotonic(), st)
    st = dict(st)
    st["scanning"] = time.monotonic() < _bt_scan_until
    return st


# ── Ręczne zdjęcie (klawisz P na ekranie / przycisk FOTO w www) ──────────────
# Użytkownik widzi pojazd, którego system nie złapał → 3 czyste klatki w
# odstępach ~0.3 s zapisują się jako wpis "FOTO-HHMMSS": w edytorze widać
# piramidę (klatka wcześniej / środkowa / później), można dorysować ramkę
# i wpisać numer — pełnoprawne dane treningowe.

# ── RADAR (tryb fotoradaru) ───────────────────────────────────────────────────
# 25.09.2026. Moduł HLK-LD2415H (24 GHz Doppler CW — mierzy TYLKO prędkość,
# odległości nie zna) na UART0 (GPIO14/15 → /dev/ttyAMA0, 9600 8N1,
# `dtparam=uart0=on`). Wysyła tylko gdy coś się rusza, do ~22 linii/s, tekst
# `V+012.3\r\n` — znak to kierunek (+ / −), liczba w km/h. Wiązka ±20° w
# poziomie, ±8° w pionie, zasięg ≥180 m (auto), dokładność ±1 km/h przy 0°.
#
# Tryb RADAR (kafel w menu, przełącznik w www) to „osobna aplikacja" w tej
# samej usłudze: rozpoznawanie tablic działa jak zwykle (bez niego nie ma
# tablicy na zdjęciu), ale zapisywane są tylko ZDARZENIA — przekroczenie
# progu prędkości. Zdarzenie dostaje: zdjęcie „fotoradarowe" (pełna klatka
# 2304×1296, kolor, ramka pojazdu + tablicy, powiększony wycinek tablicy,
# pasek danych na dole: data, godzina, rejestracja, DUŻA prędkość), klip
# z dashcamu z ramką ŚLEDZĄCĄ pojazd i bieżącą prędkością nad nim, wpis w
# tabeli `radar_zdarzenia` w plates.db.
#
# Radar mierzy z daleka (do 180 m), tablica czyta się z bliska (tele 25 mm:
# ~15° pola, 26 px wysokości tablicy przy ~40 m) — dlatego zdarzenie CZEKA
# `okno_s` na zakończenie tracku. Żeby nie przypiąć cudzej tablicy (drugie
# auto, pas obok), każdy track dostaje PRĘDKOŚĆ Z KAMERY: szerokość tablicy
# w pikselach → odległość (d = K / px) → regresja d(t) → km/h. Kandydat,
# którego prędkość z kamery zgadza się z radarem, wygrywa; brak zgodności
# = zdarzenie oznaczone „niepewne".
RADAR_DIR = "/media/lpr/radar"   # domyślnie; realnie `_radar_dir()`
RADAR_CFG = {
    "tryb": False,          # tryb fotoradaru włączony (kafel / www)
    "port": "/dev/ttyAMA0",
    "prog_kmh": 50.0,       # próg zdarzenia
    "kierunek": "oba",      # "oba" | "+" | "-"  (znak z radaru)
    "min_probek": 3,        # ile kolejnych próbek ≥ prog otwiera zdarzenie
    "okno_s": 15.0,         # ile czekać na tablicę po ostatniej próbce
    "klip_przed_s": 4.0,    # klip z dashcamu: ile sekund przed zdarzeniem
    "klip_po_s": 6.0,       # …i po ostatniej próbce ≥ prog
    "zapis_zwykly": False,  # w trybie radar zapisywać też zwykłe przejazdy?
    "glos": True,           # czytać prędkość na głos
    # prędkość z kamery: d[m] = K / szerokość_tablicy[px] (obiektyw 25 mm,
    # IMX708 2304 px na 6,45 mm → ~2,8 µm/px, tablica 520 mm → K ≈ 4640).
    # Korygować po porównaniu z radarem (ratio średnie w [Radar] logu).
    "kam_K": 4640.0,
    "kam_tol": 0.35,        # zgodność radar/kamera: |Δv|/v_radar ≤ tol
}
_radar_lock = threading.Lock()
_radar_stan = {"ok": False, "blad": "", "v": 0.0, "t": 0.0, "probek": 0,
               "zdarzen": 0, "ostatnie": None, "otwarte": False}
_radar_zdarzenie: Optional[dict] = None     # bieżące (czeka na tablicę)
_dash_sesja: Optional[dict] = None          # {"dir": Path, "start": datetime}
_RADAR_RE = re.compile(rb"V([+-])(\d{1,3}\.\d)")
# Historie do klipu z ramką: detekcje z każdej klatki (tylko w trybie radar;
# ~30 s) i próbki radaru (czas ścienny, v ze znakiem).
_radar_dets_hist: deque = deque(maxlen=1000)
_radar_v_hist: deque = deque(maxlen=1200)
# Dzienne pliki CSV na pendrive (radar/<data>/): WSZYSTKIE próbki radaru
# (nie tylko wokół zdarzeń — ruch, rozkład prędkości, „prawie zdarzenia")
# i pary do kalibracji prędkości z kamery (każdy zakończony track w trybie
# radar: kamera vs maksimum radaru z ostatnich 3 s). Bufor, zapis co ~5 s.
_radar_csv_buf: list = []
_radar_csv_lock = threading.Lock()


def _radar_csv(nazwa: str, wiersz: str) -> None:
    with _radar_csv_lock:
        _radar_csv_buf.append((nazwa, wiersz))


def _radar_csv_flush() -> None:
    with _radar_csv_lock:
        if not _radar_csv_buf:
            return
        paczka, _radar_csv_buf[:] = list(_radar_csv_buf), []
    if not _usb_ready.is_set():
        return
    try:
        dzien = datetime.now().strftime("%Y-%m-%d")
        katalog = _radar_dir() / dzien
        katalog.mkdir(parents=True, exist_ok=True)
        pliki: dict = {}
        for nazwa, w in paczka:
            pliki.setdefault(nazwa, []).append(w)
        for nazwa, wiersze in pliki.items():
            plik = katalog / f"{nazwa}_{dzien}.csv"
            nowy = not plik.exists()
            with open(plik, "a", encoding="utf-8") as fh:
                if nowy:
                    fh.write({"probki": "czas;v_kmh_ze_znakiem\n",
                              "kalibracja": "czas;tablica;track;v_kamera_kmh;v_radar_max3s_kmh;"
                                            "prog;tryb_radar\n"}.get(nazwa, ""))
                fh.write("".join(wiersze))
    except Exception as e:
        _zglos("radar_csv", e)


def _radar_dir() -> Path:
    """Katalog zdarzeń radaru OBOK bazy, do której aplikacja faktycznie pisze:
    pendrive → /media/lpr/radar, brak pendrive'a (fallback na SD,
    logs/lpr/plates.db) → logs/radar. 01.10.2026: bez pendrive'a zapis do
    /media/lpr (pusty automount) kończył się błędem i zdarzenia ginęły."""
    if SAVE_DB_PATH:
        return Path(SAVE_DB_PATH).parent.parent / "radar"
    return Path(RADAR_DIR)


def _radar_kierunek_ok(znak: str) -> bool:
    k = RADAR_CFG.get("kierunek", "oba")
    return k == "oba" or k == znak


def _radar_db_init(db_path: str) -> None:
    con = sqlite3.connect(db_path, timeout=5)
    try:
        con.execute("""CREATE TABLE IF NOT EXISTS radar_zdarzenia (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            czas TEXT NOT NULL, predkosc REAL NOT NULL, kierunek TEXT,
            probek INTEGER, tablica TEXT, ocr_conf REAL,
            zdjecie TEXT, crop TEXT, klip TEXT, uwagi TEXT)""")
        for kol in ("v_kamera REAL", "pewne INTEGER", "debug TEXT", "pojazd INTEGER"):
            try:
                con.execute(f"ALTER TABLE radar_zdarzenia ADD COLUMN {kol}")
            except sqlite3.OperationalError:
                pass
        con.commit()
    finally:
        con.close()


def _kamera_predkosc(hist) -> Optional[float]:
    """km/h z historii (t_mono, szerokość_tablicy_znormalizowana) tracku.
    Regresja liniowa odległości d = K / px po czasie; dodatnie = zbliża się.
    None, gdy za mało próbek (<8) albo za krótko (<0,4 s)."""
    pr = [(t, LPR_MAIN_W * wn) for t, wn in hist if wn > 0]
    pr = [(t, px) for t, px in pr if px >= 18]
    if len(pr) < 8:
        return None
    K = float(RADAR_CFG["kam_K"])
    ts = [t for t, _ in pr]
    ds = [K / px for _, px in pr]
    t0 = ts[0]
    span = ts[-1] - t0
    if span < 0.4:
        return None
    n = len(ts)
    mt = sum(ts) / n
    md = sum(ds) / n
    sxx = sum((t - mt) ** 2 for t in ts)
    if sxx <= 0:
        return None
    slope = sum((t - mt) * (d - md) for t, d in zip(ts, ds)) / sxx   # m/s, ujemne = zbliża
    return -slope * 3.6


def _pojazd_dla_tablicy(dets, pb) -> Optional[tuple]:
    """Ramka POJAZDU (znormalizowana), w której leży środek tablicy `pb`;
    najciaśniejsza pasująca. Brak → ramka wokół tablicy z zapasem."""
    if not pb or len(pb) != 4:
        return None
    px = (pb[0] + pb[2]) / 2.0
    py = (pb[1] + pb[3]) / 2.0
    best = None
    for d in dets or ():
        label, x1, y1, x2, y2 = d[1], d[3], d[4], d[5], d[6]
        if label not in VEHICLE_LABELS:
            continue
        if x1 <= px <= x2 and y1 <= py <= y2:
            area = (x2 - x1) * (y2 - y1)
            if best is None or area < best[0]:
                best = (area, x1, y1, x2, y2)
    if best:
        return (best[1], best[2], best[3], best[4])
    pw, ph = pb[2] - pb[0], pb[3] - pb[1]
    return (max(0.0, px - pw * 1.8), max(0.0, py - ph * 4.5),
            min(1.0, px + pw * 1.8), min(1.0, py + ph * 2.0))


class RadarWorker:
    """Wątek: czyta port radaru, buduje zdarzenia, pilnuje okna na tablicę."""

    def __init__(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="radar")
        self._ser = None
        self._ost_prog = []          # czasy ostatnich próbek ≥ prog (mono)
        self._ost_v: list = []       # (mono, v ze znakiem) z ostatniej sekundy — filtr szpilek
        self._cmd_q: _queue.Queue = _queue.Queue()   # komendy do modułu (02.10)
        self._cmd: Optional[dict] = None             # komenda w toku

    def komenda(self, bajty: bytes, czekaj: float = 1.5) -> dict:
        """Komenda konfiguracyjna LD2415H (karta, rozdz. 6.3): wysyła ją WĄTEK
        radaru (jeden właściciel portu) i przez `czekaj` s zbiera odpowiedź
        modułu (tekst bez linii `V±…`). Tu tylko czekamy na wynik."""
        if self._ser is None:
            return {"ok": False, "blad": "brak portu radaru"}
        zad = {"bajty": bytes(bajty), "czekaj": float(czekaj), "odp": b"",
               "gotowe": threading.Event(), "blad": ""}
        self._cmd_q.put(zad)
        if not zad["gotowe"].wait(czekaj + 3.0):
            return {"ok": False, "blad": "wątek radaru nie odpowiada"}
        if zad["blad"]:
            return {"ok": False, "blad": zad["blad"]}
        odp = re.sub(rb"V[+-]\d{1,3}\.\d\r?\n?", b"", zad["odp"])
        return {"ok": True, "odp": odp.decode("ascii", errors="replace").strip(),
                "hex": zad["odp"].hex(" "), "wyslano": zad["bajty"].hex(" ")}

    def start(self) -> None:
        self._thread.start()

    def _otworz(self) -> bool:
        try:
            import serial
            self._ser = serial.Serial(RADAR_CFG["port"], 9600, timeout=1.0)
            with _radar_lock:
                _radar_stan["ok"], _radar_stan["blad"] = True, ""
            print(f"[Radar] port {RADAR_CFG['port']} otwarty", flush=True)
            return True
        except Exception as e:
            with _radar_lock:
                _radar_stan["ok"], _radar_stan["blad"] = False, str(e)[:80]
            return False

    def _loop(self) -> None:
        _ustap_procesor(2)
        bufor = b""
        ost_otwarcie = 0.0
        while True:
            if self._ser is None:
                if time.monotonic() - ost_otwarcie < 10.0:
                    time.sleep(1.0)
                    self._tik(time.monotonic())
                    continue
                ost_otwarcie = time.monotonic()
                if not self._otworz():
                    continue
            try:
                dane = self._ser.read(64)
            except Exception as e:
                _zglos("radar_odczyt", e)
                try:
                    self._ser.close()
                except Exception:
                    pass
                self._ser = None
                with _radar_lock:
                    _radar_stan["ok"] = False
                continue
            teraz = time.monotonic()
            # komendy konfiguracyjne (02.10): zapis do portu i zbieranie
            # odpowiedzi z TEGO wątku — próbki V± w tym czasie idą normalnie
            if self._cmd is None and not self._cmd_q.empty():
                self._cmd = self._cmd_q.get()
                try:
                    self._ser.write(self._cmd["bajty"])
                    self._ser.flush()
                    self._cmd["do"] = teraz + self._cmd["czekaj"]
                except Exception as e:
                    self._cmd["blad"] = str(e)[:120]
                    self._cmd["gotowe"].set()
                    self._cmd = None
            if dane and self._cmd is not None:
                self._cmd["odp"] += dane
            if self._cmd is not None and teraz >= self._cmd["do"]:
                self._cmd["gotowe"].set()
                self._cmd = None
            if dane:
                bufor = (bufor + dane)[-256:]
                while b"\n" in bufor:
                    linia, bufor = bufor.split(b"\n", 1)
                    m = _RADAR_RE.search(linia)
                    if m:
                        self._probka(m.group(1).decode(), float(m.group(2)), teraz)
            self._tik(teraz)

    def _probka(self, znak: str, v: float, teraz: float) -> None:
        global _radar_zdarzenie
        vz = v if znak == "+" else -v
        with _radar_lock:
            _radar_stan["v"] = vz
            _radar_stan["t"] = teraz
            _radar_stan["probek"] += 1
        tw = time.time()
        _radar_v_hist.append((tw, vz))
        _radar_csv("probki", f"{datetime.fromtimestamp(tw).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]};{vz:.1f}\n")
        if not RADAR_CFG["tryb"] or not _radar_kierunek_ok(znak):
            return
        if v < RADAR_CFG["prog_kmh"]:
            return
        # 02.10: SZPILKA = artefakt modułu (harmoniczna Dopplera przy słabnącym
        # echu, gdy auto wyjeżdża z wiązki): 01:03:48 ciąg 58,53,**101**,-3 —
        # 101 = 2×50,3. Auto nie przyspiesza o 40 km/h w 0,1 s, więc próbkę
        # tego samego znaku, która jest > 1,5× i > +20 km/h ponad medianę
        # ostatnich 6 próbek z 0,6 s (mediana ≥ 15 km/h = w wiązce jest auto),
        # odrzucamy: nie liczy się do progu, vmax ani zapisu. Nowe auto
        # wjeżdżające w wiązkę (otoczenie = szum ±2) przechodzi normalnie.
        hist = [(t, vv) for t, vv in self._ost_v if teraz - t < 0.6 and (vv > 0) == (vz > 0)][-6:]
        self._ost_v = [(t, vv) for t, vv in self._ost_v if teraz - t < 1.0] + [(teraz, vz)]
        if len(hist) >= 3:
            m = sorted(abs(vv) for _, vv in hist)[len(hist) // 2]
            if m >= 15.0 and v > 1.5 * m and v - m > 20.0:
                with _radar_lock:
                    _radar_stan["szpilek"] = _radar_stan.get("szpilek", 0) + 1
                print(f"[Radar] odrzucona szpilka {v:.1f} km/h (otoczenie {m:.0f} km/h, "
                      f"{len(hist)} próbek)", flush=True)
                return
        self._ost_prog = [t for t in self._ost_prog if teraz - t < 0.6] + [teraz]
        ev = _radar_zdarzenie
        if ev is not None and not ev["zapisane"] and ev["zamkniete"]:
            # 02.10 (analiza nocy): radar ucichł > 1,5 s, czekamy na tablicę,
            # a tu NOWE auto ≥ prog — dotąd wchłaniane do starego zdarzenia
            # (zawyżało vmax, mieszało kandydatów; 126 ciągów ≥ prog vs 84
            # zapisy w noc). Stare zdarzenie zapisujemy z tym, co ma, nowe
            # auto dostaje własne.
            _radar_zapisz(ev, _radar_wybierz(ev))
            ev = None
        if ev is not None and not ev["zapisane"]:
            # trwające zdarzenie (drgania odczytu, to samo auto)
            ev["vmax"] = max(ev["vmax"], v)       # na żywo (ekran/dymek); przy zamknięciu → odporny
            ev["v_lista"].append(v)
            ev["t_ost"] = teraz
            ev["probek"] += 1
            return
        if len(self._ost_prog) < int(RADAR_CFG["min_probek"]):
            return
        _radar_zdarzenie = {
            "t0": teraz, "t_ost": teraz, "czas": datetime.now(),
            "t0_wall": time.time(),
            "vmax": v, "znak": znak, "probek": len(self._ost_prog),
            "v_lista": [v], "v_szczyt": v,
            "zamkniete": False, "zapisane": False, "kandydaci": [],
        }
        with _radar_lock:
            _radar_stan["zdarzen"] += 1
            _radar_stan["otwarte"] = True
        print(f"[Radar] ZDARZENIE {v:.0f} km/h ({znak}) — czekam na tablicę",
              flush=True)
        toast(f"RADAR {v:.0f} km/h", "info", "red", 5.0)
        if RADAR_CFG.get("glos"):
            try:
                SPEECH.say(f"{int(round(v))} kilometrów na godzinę.", "system")
            except Exception as e:
                _zglos("radar_glos", e)

    def _tik(self, teraz: float) -> None:
        """Domykanie zdarzenia: radar ucichł → czekamy okno na tablicę →
        zapis. Kandydat z pewną tablicą I zgodną prędkością z kamery wygrywa
        od razu; po upływie okna bierzemy najlepszego, jaki jest."""
        global _radar_zdarzenie
        if teraz - getattr(self, "_ost_flush", 0.0) > 5.0:
            self._ost_flush = teraz
            _radar_csv_flush()
        ev = _radar_zdarzenie
        if ev is None or ev["zapisane"]:
            return
        if not ev["zamkniete"] and teraz - ev["t_ost"] > 1.5:
            ev["zamkniete"] = True
            # 02.10 (analiza nocy): pojedyncza próbka-szpilka zawyżała zapis
            # (01:03:48: zapisane 100,6 km/h, a pięć najwyższych próbek ~61).
            # Prędkość zdarzenia = MEDIANA z 5 najwyższych próbek (przy < 5
            # próbkach — druga najwyższa); surowy szczyt zostaje w `v_szczyt`
            # (log + debug), żeby dało się to sprawdzić.
            v_sort = sorted(ev.get("v_lista") or [ev["vmax"]], reverse=True)
            ev["v_szczyt"] = v_sort[0]
            if len(v_sort) >= 5:
                ev["vmax"] = float(sorted(v_sort[:5])[2])
            elif len(v_sort) >= 2:
                ev["vmax"] = float(v_sort[1])
            if ev["v_szczyt"] - ev["vmax"] > 3.0:
                print(f"[Radar] szczyt {ev['v_szczyt']:.1f} km/h → zapis {ev['vmax']:.1f} "
                      f"(mediana top-5, próbek {len(v_sort)})", flush=True)
            with _radar_lock:
                _radar_stan["otwarte"] = False
        if not ev["zamkniete"]:
            return
        kand = _radar_wybierz(ev)
        if kand is not None and kand["tablica"] != "UNKNOWN" and kand.get("zgodny"):
            _radar_zapisz(ev, kand)
        elif teraz - ev["t_ost"] > float(RADAR_CFG["okno_s"]):
            _radar_zapisz(ev, kand)


def _radar_wybierz(ev: dict) -> Optional[dict]:
    """Najlepszy kandydat: pewna tablica > UNKNOWN; wśród równych — najmniejsza
    różnica prędkości z kamery względem radaru. Ustawia `zgodny`."""
    tol = float(RADAR_CFG["kam_tol"])
    best, best_k = None, None
    for k in ev["kandydaci"]:
        vk = k.get("v_kam")
        if vk is not None and ev["vmax"] > 0:
            # 01.10.2026: porównujemy |v_kam| — znak v_kam to kierunek
            # (ujemne = oddala się), a vmax radaru jest zawsze dodatni.
            roz = abs(abs(vk) - ev["vmax"]) / ev["vmax"]
            k["zgodny"] = roz <= tol
        else:
            roz = 9.9
            k["zgodny"] = False
        klucz = (0 if k["tablica"] != "UNKNOWN" else 1, 0 if k["zgodny"] else 1, roz)
        if best_k is None or klucz < best_k:
            best, best_k = k, klucz
    return best


def _scal_polowki_tablic(dets: list) -> list:
    """05.10.2026: z bliska detektor dzieli jedną tablicę na DWIE ramki
    (prefiks | numer — przerwa między nimi), np. „WGM" + „9396J", „WY" + „8796K";
    każda połówka dostaje własny track i OCR czyta pół numeru („niepełna" =
    95 z 2163 ocen, 2,2 % zapisów ma ≤ 4 znaki). Dwie ramki tablic leżące
    obok siebie na tej samej wysokości, o zbliżonej wysokości, z małą przerwą
    i łącznej proporcji ≤ 6,5:1 łączymy w jedną (unia; track_id i pewność
    z szerszej). Pary jedna NAD drugą (dwurzędowe) nie są dotykane."""
    pl = [d for d in dets if d[1] == "license_plate"]
    if len(pl) < 2:
        return dets
    inne = [d for d in dets if d[1] != "license_plate"]
    pl.sort(key=lambda d: d[3])
    wynik = []
    while pl:
        a = pl.pop(0)
        zmiana = True
        while zmiana and pl:
            zmiana = False
            for i, b in enumerate(pl):
                ha, hb = a[6] - a[4], b[6] - b[4]
                if ha <= 0 or hb <= 0:
                    continue
                nakl = min(a[6], b[6]) - max(a[4], b[4])          # wspólna wysokość
                if nakl < 0.5 * min(ha, hb) or max(ha, hb) / min(ha, hb) > 1.7:
                    continue
                przerwa = max(a[3], b[3]) - min(a[5], b[5])       # <0 = nachodzą
                if przerwa > 0.8 * max(ha, hb):
                    continue
                x1, y1 = min(a[3], b[3]), min(a[4], b[4])
                x2, y2 = max(a[5], b[5]), max(a[6], b[6])
                if (x2 - x1) / (y2 - y1) > 6.5:
                    continue
                szersza = a if (a[5] - a[3]) >= (b[5] - b[3]) else b
                a = (szersza[0], "license_plate", max(a[2], b[2]), x1, y1, x2, y2)
                pl.pop(i)
                zmiana = True
                break
        wynik.append(a)
    return inne + wynik


def _radar_track_zakonczony(tablica: str, frame_rgb, bbox, crop_bgr,
                            ocr_conf: float, tid: int = -1,
                            v_kam: Optional[float] = None) -> None:
    """Wołane z TrackManager._finalize (wątek potoku) w trybie radar:
    zakończony track staje się KANDYDATEM dla czekającego zdarzenia."""
    ev = _radar_zdarzenie
    if ev is None or ev["zapisane"] or frame_rgb is None:
        return
    # Ramka pojazdu z TEJ SAMEJ klatki, z której jest zdjęcie (najlepsza klatka
    # tracku, `bbox` = tablica w niej). 02.10: braliśmy ostatnią klatkę z tą
    # tablicą — auto zdążyło odjechać i zmaleć, ramka lądowała obok (na zdjęciu
    # WX707AB „zaznaczała lusterko"). Szukamy w historii detekcji klatki, w której
    # tablica tego tracku leży tam, gdzie `bbox` (IoU ≥ 0,7); gdy jej nie ma,
    # ramka z innej klatki tylko, jeśli obejmuje środek `bbox`; inaczej zapas
    # wokół tablicy.
    pojazd = None
    bb = tuple(bbox) if bbox and len(bbox) == 4 else ()
    px = py = None
    if bb:
        px, py = (bb[0] + bb[2]) / 2.0, (bb[1] + bb[3]) / 2.0

    def _iou(a, b):
        ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
        iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
        i = ix * iy
        u = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - i
        return i / u if u > 0 else 0.0

    najlepszy, inny = None, None
    for _t, dets in reversed(_radar_dets_hist):
        pb = next((d[3:7] for d in dets if d[0] == tid and d[1] == "license_plate"), None)
        if not pb:
            continue
        if bb and _iou(pb, bb) >= 0.7:
            najlepszy = _pojazd_dla_tablicy(dets, pb)
            break
        if inny is None:
            inny = _pojazd_dla_tablicy(dets, pb)
    if najlepszy is not None:
        pojazd = najlepszy
    elif (inny is not None and px is not None
          and inny[0] <= px <= inny[2] and inny[1] <= py <= inny[3]):
        pojazd = inny
    if pojazd is None:
        pojazd = _pojazd_dla_tablicy((), bb)
    ev["kandydaci"].append({"tablica": tablica or "UNKNOWN", "frame": frame_rgb,
                            "bbox": tuple(bbox) if bbox else (), "crop": crop_bgr,
                            "ocr": float(ocr_conf or 0.0), "t": time.monotonic(),
                            "tid": tid, "v_kam": v_kam, "pojazd": pojazd})
    print(f"[Radar] kandydat {tablica} (track {tid}, kamera "
          f"{'%.0f' % v_kam if v_kam is not None else '?'} km/h, radar {ev['vmax']:.0f})",
          flush=True)


RADAR_RUCH_MIN = 0.06   # min. przesunięcie środka ramki (ułamek kadru), by uznać pojazd za jadący


def _radar_klatka_zdarzenia(ev: dict, full_rgb, dets, now_mono: float) -> None:
    """Wołane z callbacka (tryb radar, zdarzenie otwarte/czekające): wybór
    klatki do zdjęcia zdarzenia BEZ tablicy.
    02.10 rano: „największy pojazd" trafiał w ZAPARKOWANE auta przy drodze
    (ramka na stojącym samochodzie, sprawca poza nią). Teraz śledzimy każdy
    pojazd (track_id) w trakcie zdarzenia: jak daleko przesunął się jego
    środek od pierwszego zobaczenia (`ruch`) i klatkę, w której był
    największy. Do zdjęcia idzie pojazd JADĄCY (ruch ≥ RADAR_RUCH_MIN)
    o największym polu; stojące tylko w ostateczności. Trzymamy REFERENCJE
    do klatek pierścienia (bez kopii), najwyżej 8 pojazdów."""
    if now_mono - ev["t_ost"] > 2.0:
        return
    ruch = ev.setdefault("kl_ruch", {})
    for d in dets:
        if d[1] not in VEHICLE_LABELS:
            continue
        pole = (d[5] - d[3]) * (d[6] - d[4])
        cx, cy = (d[3] + d[5]) / 2.0, (d[4] + d[6]) / 2.0
        tid = d[0]
        if tid is None or tid < 0:
            # bez track_id: dopasuj do najbliższego znanego środka
            tid = next((k for k, r in ruch.items()
                        if abs(r["cx"] - cx) + abs(r["cy"] - cy) < 0.05), None)
            if tid is None:
                tid = f"p{len(ruch)}"
        r = ruch.get(tid)
        if r is None:
            if len(ruch) >= 8:
                # pełno (parking): wyrzuć stojący pojazd o najmniejszym polu
                k_min = min(ruch, key=lambda k: (ruch[k]["ruch"] >= RADAR_RUCH_MIN, ruch[k]["pole"]))
                if ruch[k_min]["ruch"] >= RADAR_RUCH_MIN:
                    continue
                del ruch[k_min]
            r = ruch[tid] = {"cx0": cx, "cy0": cy, "cx": cx, "cy": cy, "ruch": 0.0,
                             "pole": 0.0, "frame": None, "bbox": None, "n": 0}
        r["n"] += 1
        r["cx"], r["cy"] = cx, cy
        r["ruch"] = max(r["ruch"], abs(cx - r["cx0"]) + abs(cy - r["cy0"]))
        if pole > r["pole"]:
            r["pole"], r["frame"], r["bbox"] = pole, full_rgb, (d[3], d[4], d[5], d[6])
    if not ruch and ev.get("kl_vmax") != ev["vmax"]:
        # żadnego pojazdu nie widać — klatka z chwili ostatniego maksimum prędkości
        ev["kl_vmax"], ev["kl_frame0"] = ev["vmax"], full_rgb


def _radar_wybierz_klatke(ev: dict):
    """(frame, bbox, opis) dla zdjęcia bez tablicy: pojazd jadący o największym
    polu → stojący (ostrzeżenie w logu) → klatka z chwili vmax → ostatnia."""
    ruch = ev.get("kl_ruch") or {}
    jadace = [r for r in ruch.values() if r["frame"] is not None and r["ruch"] >= RADAR_RUCH_MIN]
    stojace = [r for r in ruch.values() if r["frame"] is not None and r["ruch"] < RADAR_RUCH_MIN]
    ev["kl_info"] = sorted(((round(r["ruch"], 3), round(r["pole"], 4), r["n"]) for r in ruch.values()),
                           reverse=True)[:8]
    if jadace:
        r = max(jadace, key=lambda r: r["pole"])
        return r["frame"], r["bbox"], f"pojazd w ruchu (przesunięcie {r['ruch']:.2f}, pole {r['pole']:.3f}, {len(stojace)} stojących)"
    if stojace:
        r = max(stojace, key=lambda r: r["pole"])
        return r["frame"], None, f"UWAGA tylko stojące pojazdy ({len(stojace)}) — bez ramki"
    if ev.get("kl_frame0") is not None:
        return ev["kl_frame0"], None, "chwila vmax (bez pojazdu w kadrze)"
    with _pierscien_lock:
        frame = next(reversed(_pierscien.values())) if _pierscien else None
    return frame, None, "ostatnia klatka"


def _radar_zapisz(ev: dict, kand: Optional[dict]) -> None:
    """Zapis zdarzenia w tle (zdjęcie + baza), potem klip z dashcamu."""
    ev["zapisane"] = True
    if kand is None:
        # brak tracku w oknie — zdjęcie bez tablicy: klatka z największym
        # pojazdem w trakcie zdarzenia (02.10), inaczej klatka z chwili vmax,
        # w ostateczności ostatnia klatka pierścienia; klip pokaże resztę
        frame, pojazd, skad = _radar_wybierz_klatke(ev)
        print(f"[Radar] zdjęcie bez tablicy: {skad}; pojazdy (ruch, pole, klatek): "
              f"{ev.get('kl_info')}", flush=True)
        kand = {"tablica": "", "frame": frame, "bbox": (), "crop": None,
                "ocr": 0.0, "tid": -1, "v_kam": None, "pojazd": pojazd, "zgodny": False}
    ev_kopia = dict(ev)
    ev_kopia.pop("kl_ruch", None)          # referencje do klatek zostają tylko w `kand`
    ev.pop("kl_ruch", None)
    threading.Thread(target=_radar_zapisz_praca, args=(ev_kopia, kand),
                     daemon=True, name="radar-zapis").start()


def _radar_render_zdjecie(img_bgr, ev: dict, kand: dict) -> np.ndarray:
    """Zdjęcie jak z fotoradaru, ale w kolorze: ramka pojazdu (czerwona) i
    tablicy (żółta), powiększony wycinek tablicy w rogu, na dole pasek:
    data + godzina | rejestracja | DUŻA prędkość; niżej drobne: kierunek,
    próg, prędkość z kamery, radar."""
    img = img_bgr.copy()
    h, w = img.shape[:2]
    czas: datetime = ev["czas"]
    tabl = kand.get("tablica") or ""
    pb = kand.get("bbox") or ()
    vb = kand.get("pojazd")
    if vb and len(vb) == 4:
        cv2.rectangle(img, (int(vb[0] * w), int(vb[1] * h)),
                      (int(vb[2] * w), int(vb[3] * h)), (0, 0, 230), 4)
    if len(pb) == 4:
        cv2.rectangle(img, (int(pb[0] * w), int(pb[1] * h)),
                      (int(pb[2] * w), int(pb[3] * h)), (0, 220, 255), 3)
    # powiększony wycinek tablicy (prawy górny róg)
    crop = kand.get("crop")
    if crop is not None and crop.size:
        ch, cw = crop.shape[:2]
        tw = min(w // 4, 640)
        th = int(tw * ch / max(1, cw))
        if th < h // 3:
            big = cv2.resize(crop, (tw, th), interpolation=cv2.INTER_CUBIC)
            x0, y0 = w - tw - 24, 24
            img[y0:y0 + th, x0:x0 + tw] = big
            cv2.rectangle(img, (x0 - 3, y0 - 3), (x0 + tw + 2, y0 + th + 2),
                          (255, 255, 255), 3)
    # Dane BEZ paska: biały tekst z czarnym obrysem wprost na zdjęciu (jak
    # w policyjnym Videorapidzie), zgrupowany w lewym dolnym rogu, żeby nie
    # zasłaniać kadru. Prędkość największa, w białej ramce; niżej rejestracja,
    # data i godzina, na końcu drobne szczegóły pomiaru. Czcionka mono.
    pil = _PILImage.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
    dr = _PILDraw.Draw(pil)
    mono_b = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"
    def _f(sz):
        for path in (mono_b, _UI_FONT_BLD):
            try:
                return _PILFont.truetype(path, int(sz))
            except Exception:
                continue
        return _PILFont.load_default()
    f_big, f_pl, f_dt, f_sm = _f(h / 7.5), _f(h / 16), _f(h / 20), _f(h / 34)
    biel, czern = (255, 255, 255), (0, 0, 0)
    def _t(x, y, txt, f, kolor=biel):
        dr.text((x, y), txt, font=f, fill=kolor, stroke_width=max(2, int(f.size / 11)),
                stroke_fill=czern)
    x0 = int(w * 0.02)
    y = h - int(h * 0.03)
    # lewy dolny róg: data i godzina, rejestracja. Szczegóły pomiaru NIE
    # na zdjęciu — są w bazie (kolumna debug), w logu i w panelu DEBUG www.
    y -= f_dt.size + 10
    _t(x0, y, czas.strftime("%Y-%m-%d   %H:%M:%S"), f_dt)
    y -= f_pl.size + 12
    _t(x0, y, tabl if tabl and tabl != "UNKNOWN" else "brak odczytu tablicy", f_pl,
       biel if tabl and tabl != "UNKNOWN" else (200, 200, 200))
    # prawy dolny róg: sama wartość (biała, większa) w białej ramce + km/h
    vtxt = f"{ev['vmax']:.0f}"
    vw = int(dr.textlength(vtxt, font=f_big))
    pad = int(f_big.size * 0.18)
    kw = int(dr.textlength(" km/h", font=f_pl))
    xk = w - int(w * 0.02) - kw            # km/h przy prawej krawędzi
    xv = xk - pad - vw                      # wartość
    yv = h - int(h * 0.03) - f_big.size - pad
    dr.rectangle([xv - pad, yv - pad // 2, xv + vw + pad, yv + f_big.size + pad // 2],
                 outline=biel, width=max(3, int(f_big.size / 20)))
    dr.rectangle([xv - pad + 3, yv - pad // 2 + 3, xv + vw + pad - 3,
                  yv + f_big.size + pad // 2 - 3],
                 outline=czern, width=2)
    _t(xv, yv, vtxt, f_big, biel)
    _t(xk, yv + int(f_big.size * 0.36), " km/h", f_pl)
    return cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)


def _radar_zapisz_praca(ev: dict, kand: dict) -> None:
    _ustap_procesor(5)
    try:
        if not _usb_ready.is_set():
            print("[Radar] pendrive niedostępny — zdarzenie niezapisane", flush=True)
            return
        czas: datetime = ev["czas"]
        day = _radar_dir() / czas.strftime("%Y-%m-%d")
        day.mkdir(parents=True, exist_ok=True)
        tabl = kand.get("tablica") or ""
        baza = f"RADAR_{czas.strftime('%H%M%S')}_{int(round(ev['vmax']))}kmh" \
               + (f"_{tabl}" if tabl and tabl != "UNKNOWN" else "")
        zdj_name = crop_name = None
        frame = kand.get("frame")
        if frame is not None:
            img = _radar_render_zdjecie(cv2.cvtColor(frame, cv2.COLOR_RGB2BGR), ev, kand)
            zdj_name = baza + ".jpg"
            safe_write_image(day / zdj_name, img)
            if kand.get("crop") is not None:
                crop_name = baza + "_crop.jpg"
                safe_write_image(day / crop_name, kand["crop"])
        pewne = int(bool(kand.get("zgodny"))) if tabl and tabl != "UNKNOWN" else 0
        # 02.10: zdarzenie BEZ tablicy zapisujemy zawsze, ale oznaczamy, czy
        # kamera w ogóle widziała JADĄCY pojazd (kl_info: (ruch, pole, klatek));
        # brak pojazdu = prawdopodobny błąd pomiaru (radar widzi szerzej niż kamera)
        if tabl and tabl != "UNKNOWN":
            pojazd = 1
        else:
            pojazd = int(any(r[0] >= RADAR_RUCH_MIN for r in (ev.get("kl_info") or [])))
        uwagi = ("" if not ev.get("kandydaci") else
                 f"kandydatów {len(ev['kandydaci'])}")
        if not pojazd:
            uwagi = (uwagi + "; " if uwagi else "") + "brak pojazdu w kadrze — możliwy błąd pomiaru"
        # DEBUG do bazy (panel DEBUG w www): ustawienia w chwili zdarzenia,
        # wszyscy kandydaci, przebieg próbek radaru wokół zdarzenia
        import json as _json
        t0w = float(ev.get("t0_wall") or time.time())
        t1w = t0w + (ev["t_ost"] - ev["t0"])
        probki = [[round(t - t0w, 2), round(v, 1)] for t, v in list(_radar_v_hist)
                  if t0w - 3.0 <= t <= t1w + 3.0][-400:]
        debug = _json.dumps({
            "prog": RADAR_CFG["prog_kmh"], "kierunek_cfg": RADAR_CFG["kierunek"],
            "min_probek": RADAR_CFG["min_probek"], "okno_s": RADAR_CFG["okno_s"],
            "kam_K": RADAR_CFG["kam_K"], "kam_tol": RADAR_CFG["kam_tol"],
            "trwanie_s": round(ev["t_ost"] - ev["t0"], 2),
            "v_szczyt": round(float(ev.get("v_szczyt", ev["vmax"])), 1),   # surowy max próbki
            "wybrany_tid": kand.get("tid"),
            "pojazdy_ruch": ev.get("kl_info"),      # (przesunięcie, pole, klatek) per pojazd
            "kandydaci": [{"tablica": k.get("tablica"), "tid": k.get("tid"),
                           "v_kam": (round(k["v_kam"], 1) if k.get("v_kam") is not None else None),
                           "zgodny": bool(k.get("zgodny")), "ocr": round(float(k.get("ocr") or 0), 2),
                           "po_s": round(k.get("t", ev["t_ost"]) - ev["t_ost"], 1)}
                          for k in ev.get("kandydaci", [])],
            "probki": probki}, ensure_ascii=False)
        db_path = str(SAVE_DB_PATH)
        _radar_db_init(db_path)
        con = sqlite3.connect(db_path, timeout=5)
        try:
            cur = con.execute("""INSERT INTO radar_zdarzenia
                (czas, predkosc, kierunek, probek, tablica, ocr_conf, zdjecie, crop,
                 klip, uwagi, v_kamera, pewne, debug, pojazd)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (czas.isoformat(sep=" ", timespec="seconds"), float(ev["vmax"]),
                 ev["znak"], int(ev["probek"]), tabl or None,
                 float(kand.get("ocr") or 0.0), zdj_name, crop_name, None, uwagi,
                 kand.get("v_kam"), pewne, debug, pojazd))
            rid = cur.lastrowid
            con.commit()
        finally:
            con.close()
        with _radar_lock:
            _radar_stan["ostatnie"] = {"czas": czas.strftime("%H:%M:%S"),
                                       "v": float(ev["vmax"]), "tablica": tabl,
                                       "zdjecie": zdj_name,
                                       "dzien": czas.strftime("%Y-%m-%d")}
        print(f"[Radar] zapisane: {baza} radar {ev['vmax']:.1f} km/h ({ev['znak']}), "
              f"próbek {ev['probek']}, trwanie {ev['t_ost'] - ev['t0']:.1f} s, próg "
              f"{RADAR_CFG['prog_kmh']:.0f}, kamera "
              f"{('%.1f' % kand['v_kam']) if kand.get('v_kam') is not None else '-'} km/h, "
              f"kandydatów {len(ev.get('kandydaci', []))}, pewne={pewne}", flush=True)
        # klip — dopiero, gdy dashcam zdąży dopisać klatki PO zdarzeniu
        opoznienie = max(0.0, float(RADAR_CFG["klip_po_s"]) + 3.0
                         - (time.monotonic() - ev["t_ost"]))
        time.sleep(opoznienie)
        klip = _radar_klip(ev, kand, day, baza)
        if klip:
            con = sqlite3.connect(db_path, timeout=5)
            try:
                con.execute("UPDATE radar_zdarzenia SET klip=? WHERE id=?", (klip, rid))
                con.commit()
            finally:
                con.close()
    except Exception as e:
        _zglos("radar_zapis", e)


def _dash_segmenty_z_czasem() -> list:
    """[(sciezka, start_wall, koniec_wall)] segmentów bieżącej sesji dashcamu.
    Start sesji = chwila startu potoku; długości z ffprobe (zamknięte segmenty
    w cache), otwarty segment = do teraz."""
    ses = _dash_sesja
    if not ses:
        return []
    pliki = sorted(Path(ses["dir"]).glob("jazda_*.mkv"))
    starty = ses.get("pliki") or {}
    if pliki and all(p.name in starty for p in pliki):
        # 02.10.2026: czas otwarcia z sygnału splitmuxsink — bez dryfu
        out = []
        for i, p in enumerate(pliki):
            s0 = starty[p.name]
            s1 = starty[pliki[i + 1].name] if i + 1 < len(pliki) else datetime.now()
            out.append((p, s0, s1))
        return out
    out, t = [], ses["start"]
    cache = ses.setdefault("dlug", {})
    for i, p in enumerate(pliki):
        ostatni = (i == len(pliki) - 1)
        if not ostatni and p.name not in cache:
            try:
                r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                                    "format=duration", "-of", "csv=p=0", str(p)],
                                   capture_output=True, text=True, timeout=20)
                cache[p.name] = float(r.stdout.strip() or DASHCAM_SEG_SEC)
            except Exception:
                cache[p.name] = float(DASHCAM_SEG_SEC)
        dl = cache.get(p.name)
        koniec = datetime.now() if ostatni else t + timedelta(seconds=dl)
        out.append((p, t, koniec))
        t = koniec
    return out


def _radar_klip(ev: dict, kand: dict, day: Path, baza: str) -> Optional[str]:
    """Klip [t0 − przed, t_ost + po] z segmentów dashcamu, dekodowany
    OpenCV (pozycja w ms = czas ścienny), z nałożoną ramką ŚLEDZĄCĄ pojazd
    (z historii detekcji) i bieżącą prędkością z radaru, kodowany x264
    ultrafast przez ffmpeg (stdin). Zwraca nazwę pliku albo None."""
    try:
        if not _dashcam_active or _dash_sesja is None:
            return None
        czas: datetime = ev["czas"]
        od = czas - timedelta(seconds=float(RADAR_CFG["klip_przed_s"]))
        do = czas + timedelta(seconds=(ev["t_ost"] - ev["t0"])
                              + float(RADAR_CFG["klip_po_s"]))
        segs = [(p, s0, s1) for p, s0, s1 in _dash_segmenty_z_czasem()
                if not (s1 <= od or s0 >= do)]
        if not segs:
            print(f"[Radar] klip: brak pasujących segmentów dashcamu dla "
                  f"{od.strftime('%H:%M:%S')}–{do.strftime('%H:%M:%S')}", flush=True)
            return None
        # kopie historii: inne wątki dopisują — deque potrafi rzucić
        # RuntimeError w trakcie iteracji, więc kilka prób
        def _kopia(d):
            for _ in range(5):
                try:
                    return list(d)
                except RuntimeError:
                    time.sleep(0.01)
            return []
        dets_hist = _kopia(_radar_dets_hist)        # (wall, dets)
        v_hist = _kopia(_radar_v_hist)              # (wall, v)
        d_ts = [t for t, _ in dets_hist]
        v_ts = [t for t, _ in v_hist]
        tid = kand.get("tid", -1)
        tabl = kand.get("tablica") or ""
        out = day / (baza + ".mp4")
        proc = None
        n_kl = 0
        import bisect
        krok = 1.0 / float(DASHCAM_FPS)
        nast = None              # czas ścienny następnej klatki wyjściowej
        ost_raw = None           # ostatnia wypisana klatka (powielanie luk)
        n_dup = 0
        for p, s0, s1 in segs:
            cap = cv2.VideoCapture(str(p))
            if not cap.isOpened():
                continue
            start_ms = max(0.0, (od - s0).total_seconds() * 1000.0)
            if start_ms > 0:
                cap.set(cv2.CAP_PROP_POS_MSEC, start_ms)
            while True:
                pos = cap.get(cv2.CAP_PROP_POS_MSEC)
                ok, fr = cap.read()
                if not ok:
                    break
                wall = s0 + timedelta(milliseconds=pos)
                if wall > do:
                    break
                if wall < od:
                    continue
                if proc is None:
                    fh, fw = fr.shape[:2]
                    proc = subprocess.Popen(
                        ["nice", "-n", "19", "ffmpeg", "-v", "error", "-y",
                         "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{fw}x{fh}",
                         "-r", str(DASHCAM_FPS), "-i", "-",
                         "-c:v", "libx264", "-preset", "ultrafast", "-crf", "24",
                         "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)],
                        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE)
                fh, fw = fr.shape[:2]
                wt = wall.timestamp()
                # bieżąca prędkość z radaru (najbliższa próbka ≤ 0,5 s)
                vtxt = None
                i = bisect.bisect_left(v_ts, wt)
                for j in (i - 1, i):
                    if 0 <= j < len(v_hist) and abs(v_hist[j][0] - wt) <= 0.5:
                        vtxt = f"{abs(v_hist[j][1]):.0f} km/h"
                        break
                # ramka pojazdu: najbliższa klatka detekcji ≤ 0,12 s
                box = None
                i = bisect.bisect_left(d_ts, wt)
                for j in (i - 1, i):
                    if 0 <= j < len(dets_hist) and abs(dets_hist[j][0] - wt) <= 0.12:
                        dets = dets_hist[j][1]
                        pb = next((d[3:7] for d in dets
                                   if d[0] == tid and d[1] == "license_plate"), None)
                        if pb:
                            box = _pojazd_dla_tablicy(dets, pb)
                        break
                if box:
                    x1, y1 = int(box[0] * fw), int(box[1] * fh)
                    x2, y2 = int(box[2] * fw), int(box[3] * fh)
                    cv2.rectangle(fr, (x1, y1), (x2, y2), (0, 0, 230), 3)
                    lbl = (vtxt or f"{ev['vmax']:.0f} km/h") + (f"  {tabl}" if tabl and tabl != "UNKNOWN" else "")
                    (tw, th), _ = cv2.getTextSize(lbl, cv2.FONT_HERSHEY_DUPLEX, 0.9, 2)
                    ly = max(th + 8, y1 - 8)
                    cv2.rectangle(fr, (x1, ly - th - 8), (x1 + tw + 12, ly + 4), (0, 0, 0), -1)
                    cv2.putText(fr, lbl, (x1 + 6, ly), cv2.FONT_HERSHEY_DUPLEX, 0.9,
                                (255, 255, 255), 2, cv2.LINE_AA)
                # HUD w rogu: czas, radar teraz, max
                hud = (wall.strftime("%H:%M:%S") + "   radar: " + (vtxt or "--")
                       + f"   max {ev['vmax']:.0f} km/h")
                cv2.rectangle(fr, (0, 0), (fw, 34), (0, 0, 0), -1)
                cv2.putText(fr, hud, (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.7,
                            (255, 255, 255), 1, cv2.LINE_AA)
                raw = fr.tobytes()
                # 02.10: stały rytm wyjścia (DASHCAM_FPS) — luki w nagraniu
                # (kamera < 30 fps, klatki zrzucone przez koder) wypełniamy
                # poprzednią klatką, żeby klip trwał tyle, co naprawdę;
                # luki > 1 s przeskakujemy bez powielania
                if nast is None or wt - nast >= 1.0:
                    nast = wt
                while ost_raw is not None and wt - nast >= krok:
                    proc.stdin.write(ost_raw)
                    n_kl += 1
                    n_dup += 1
                    nast += krok
                proc.stdin.write(raw)
                n_kl += 1
                ost_raw = raw
                nast += krok
            cap.release()
        if proc is None:
            print("[Radar] klip: brak klatek w oknie "
                  f"{od.strftime('%H:%M:%S')}–{do.strftime('%H:%M:%S')}; segmenty: "
                  + ", ".join(f"{p.name} {s0.strftime('%H:%M:%S')}–{s1.strftime('%H:%M:%S')}"
                              for p, s0, s1 in segs), flush=True)
            return None
        proc.stdin.close()
        err = proc.stderr.read().decode(errors="replace")
        proc.wait(timeout=120)
        if proc.returncode != 0 or not out.exists():
            print(f"[Radar] klip: ffmpeg {proc.returncode}: {err[:160]}", flush=True)
            return None
        print(f"[Radar] klip {out.name} ({n_kl} kl, w tym {n_dup} powielonych, "
              f"{out.stat().st_size // 1024} kB)", flush=True)
        return out.name
    except Exception as e:
        _zglos("radar_klip", e)
        return None


def _radar_sql_filtr(f: Optional[dict]) -> tuple:
    """(WHERE, params, ORDER BY) z filtra www: od/do (YYYY-MM-DD), v_min/v_max,
    tablica (fragment), tylko_tablica, tylko_pojazd (bez „brak pojazdu"),
    kierunek (+/-), sort (czas_desc|czas_asc|v_desc|v_asc). 02.10."""
    f = f or {}
    w, par = [], []
    if f.get("od"):
        w.append("czas >= ?"); par.append(str(f["od"])[:10] + " 00:00:00")
    if f.get("do"):
        w.append("czas <= ?"); par.append(str(f["do"])[:10] + " 23:59:59")
    for k, op in (("v_min", ">="), ("v_max", "<=")):
        try:
            if f.get(k) not in (None, ""):
                w.append(f"predkosc {op} ?"); par.append(float(f[k]))
        except (TypeError, ValueError):
            pass
    if f.get("tablica"):
        w.append("UPPER(tablica) LIKE ?"); par.append("%" + str(f["tablica"]).upper() + "%")
    if f.get("tylko_tablica"):
        w.append("tablica IS NOT NULL AND tablica != '' AND tablica != 'UNKNOWN'")
    if f.get("tylko_pojazd"):
        w.append("(pojazd IS NULL OR pojazd = 1)")
    if f.get("kierunek") in ("+", "-"):
        w.append("kierunek = ?"); par.append(f["kierunek"])
    order = {"czas_asc": "czas ASC, id ASC", "v_desc": "predkosc DESC, id DESC",
             "v_asc": "predkosc ASC, id ASC"}.get(f.get("sort"), "id DESC")
    return (("WHERE " + " AND ".join(w)) if w else ""), par, order


def _radar_lista(limit: int = 60, f: Optional[dict] = None) -> list:
    db_path = str(SAVE_DB_PATH)
    if not db_path:
        return []
    try:
        _radar_db_init(db_path)
        con = sqlite3.connect(db_path, timeout=5)
        con.row_factory = sqlite3.Row
        try:
            where, par, order = _radar_sql_filtr(f)
            rows = con.execute(f"SELECT * FROM radar_zdarzenia {where} ORDER BY {order} LIMIT ?",
                               (*par, int(limit))).fetchall()
        finally:
            con.close()
        out = []
        for r in rows:
            d = r["czas"][:10]
            k = r.keys()
            out.append({"id": r["id"], "czas": r["czas"], "v": r["predkosc"],
                        "kierunek": r["kierunek"], "probek": r["probek"],
                        "tablica": (r["tablica"] if r["tablica"] not in (None, "", "UNKNOWN") else None),
                        "ocr": r["ocr_conf"],
                        "v_kam": r["v_kamera"] if "v_kamera" in k else None,
                        "pewne": r["pewne"] if "pewne" in k else None,
                        "pojazd": r["pojazd"] if "pojazd" in k else None,
                        "uwagi": r["uwagi"],
                        "debug": (r["debug"] if "debug" in k else None),
                        "zdjecie": f"/api/radar/plik/{d}/{r['zdjecie']}" if r["zdjecie"] else None,
                        "mini": f"/api/radar/plik/{d}/{r['zdjecie']}?mini=1" if r["zdjecie"] else None,
                        "crop": f"/api/radar/plik/{d}/{r['crop']}" if r["crop"] else None,
                        "klip": f"/api/radar/plik/{d}/{r['klip']}" if r["klip"] else None})
        return out
    except Exception as e:
        _zglos("radar_lista", e)
        return []


def _radar_test(v: float = 88.0, sighting_id: Optional[int] = None) -> None:
    """Symulacja zdarzenia (przycisk TEST w www) — sprawdza cały tor bez
    auta: zdjęcie z pierścienia (albo z zapisanej klatki `sighting_id` —
    wtedy z tablicą i ramkami jak na prawdziwym zdarzeniu), wpis w bazie,
    klip z dashcamu."""
    global _radar_zdarzenie
    teraz = time.monotonic()
    ev = {"t0": teraz - 1.0, "t_ost": teraz, "czas": datetime.now(),
          "t0_wall": time.time(), "vmax": float(v), "znak": "+", "probek": 5,
          "zamkniete": True, "zapisane": False, "kandydaci": []}
    if sighting_id:
        try:
            con = sqlite3.connect(str(SAVE_DB_PATH), timeout=5)
            r = con.execute("SELECT seen_at, full_image, crop_image, bbox, plate_text "
                            "FROM sightings WHERE id=?", (int(sighting_id),)).fetchone()
            con.close()
            if r:
                dd = Path(SAVE_DB_PATH).parent / r[0][:10]
                full = cv2.imread(str(dd / r[1]))
                crop = cv2.imread(str(dd / r[2])) if r[2] else None
                bb = tuple(float(x) for x in (r[3] or "").split(";")) if r[3] else ()
                if full is not None:
                    ev["kandydaci"].append({
                        "tablica": r[4] or "UNKNOWN",
                        "frame": cv2.cvtColor(full, cv2.COLOR_BGR2RGB),
                        "bbox": bb, "crop": crop, "ocr": 0.97, "t": teraz, "tid": -1,
                        "v_kam": float(v) * 0.94, "pojazd": _pojazd_dla_tablicy((), bb),
                        "zgodny": True})
        except Exception as e:
            _zglos("radar_test", e)
    _radar_zdarzenie = ev
    with _radar_lock:
        _radar_stan["zdarzen"] += 1
    toast(f"RADAR TEST {v:.0f} km/h", "info", "red", 4.0)
    threading.Timer(2.0, lambda: (not ev["zapisane"]
                                  and _radar_zapisz(ev, _radar_wybierz(ev)))).start()


_RADAR_FONT_CACHE: dict = {}


def _radar_font(size: int):
    """DejaVu Sans Mono Bold (ten sam krój co na zdjęciu), z cache."""
    f = _RADAR_FONT_CACHE.get(size)
    if f is None:
        for path in ("/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
                     _UI_FONT_BLD):
            try:
                f = _PILFont.truetype(path, size)
                break
            except Exception:
                continue
        if f is None:
            f = _PILFont.load_default()
        _RADAR_FONT_CACHE[size] = f
    return f


_radar_ov_cache: dict = {"klucz": None, "rgb": None, "alfa": None}


def _draw_radar_overlay(frame, w: int, h: int, now: float) -> None:
    """Ekran trybu RADAR w stylu zdjęcia fotoradarowego: biały tekst z czarnym
    obrysem wprost na obrazie, bez tła. Po prawej bieżąca prędkość w białej
    ramce (czerwona, gdy trwa zdarzenie), po lewej ostatnie zdarzenie
    (rejestracja, data i godzina) i drobna linia stanu.

    01.10.2026: rysowanie PIL-em z obrysem na KAŻDEJ klatce kosztowało 37 ms
    w ruchu (podgląd gubił klatki). Teraz warstwa tekstu (RGB + alfa) jest
    renderowana tylko, gdy zmieni się treść (prędkość, stan, ostatnie
    zdarzenie), a na klatkę przypada jedno mieszanie alfa w numpy (~1 ms)."""
    with _radar_lock:
        st = dict(_radar_stan)
    swieza = (now - st["t"]) < 1.5 and st["t"] > 0
    v = abs(st["v"]) if swieza else None
    hud_h = 40 if HUD_ON else 0
    pas_h = min(150, h // 3)
    y0 = h - hud_h - pas_h
    if y0 < 0:
        return
    vtxt = f"{v:.0f}" if v is not None else "--"
    ost = st.get("ostatnie") or {}
    stan = (f"RADAR  próg {RADAR_CFG['prog_kmh']:.0f} km/h  zdarzeń {st['zdarzen']}"
            if st["ok"] else "RADAR  BRAK PORTU  " + (st.get("blad") or "")[:30])
    klucz = (vtxt, bool(st["otwarte"]), bool(st["ok"]), stan, w, pas_h,
             ost.get("czas"), ost.get("tablica"), ost.get("v"), ost.get("dzien"))
    if _radar_ov_cache["klucz"] != klucz:
        pil = _PILImage.new("RGBA", (w, pas_h), (0, 0, 0, 0))
        dr = _PILDraw.Draw(pil)
        biel, czern = (255, 255, 255, 255), (0, 0, 0, 255)
        f_v, f_pl, f_dt, f_sm = (_radar_font(int(pas_h * 0.5)), _radar_font(int(pas_h * 0.2)),
                                 _radar_font(int(pas_h * 0.15)), _radar_font(int(pas_h * 0.11)))

        def _t(x, y, txt, f, kolor=biel):
            dr.text((x, y), txt, font=f, fill=kolor,
                    stroke_width=max(2, int(f.size / 11)), stroke_fill=czern)

        vw = int(dr.textlength(vtxt, font=f_v))
        pad = int(f_v.size * 0.18)
        kw = int(dr.textlength(" km/h", font=f_pl))
        xk = w - int(w * 0.02) - kw
        xv = xk - pad - vw
        yv = pas_h - 12 - f_v.size - pad
        ramka = (255, 60, 60, 255) if st["otwarte"] else biel
        dr.rectangle([xv - pad, yv - pad // 2, xv + vw + pad, yv + f_v.size + pad // 2],
                     outline=ramka, width=max(3, int(f_v.size / 20)))
        dr.rectangle([xv - pad + 3, yv - pad // 2 + 3, xv + vw + pad - 3,
                      yv + f_v.size + pad // 2 - 3], outline=czern, width=2)
        _t(xv, yv, vtxt, f_v, biel if st["ok"] else (255, 80, 80, 255))
        _t(xk, yv + int(f_v.size * 0.36), " km/h", f_pl)
        x0 = int(w * 0.02)
        y = pas_h - 12
        y -= f_sm.size + 4
        _t(x0, y, stan, f_sm, (220, 220, 220, 255) if st["ok"] else (255, 80, 80, 255))
        if ost:
            y -= f_dt.size + 6
            _t(x0, y, f"{ost.get('dzien', '')}   {ost['czas']}", f_dt)
            y -= f_pl.size + 8
            _t(x0, y, (ost["tablica"] or "brak odczytu tablicy") + f"   {ost['v']:.0f} km/h",
               f_pl, biel if ost["tablica"] else (200, 200, 200, 255))
        arr = np.array(pil)
        rgb = cv2.cvtColor(arr[:, :, :3], cv2.COLOR_RGB2BGR).astype(np.float32)
        alfa = (arr[:, :, 3:4].astype(np.float32) / 255.0)
        # 02.10: prostokąt, w którym warstwa coś ma, i gotowe iloczyny — raz
        # przy zmianie treści, nie na każdą klatkę (np.where na 150×800 px)
        maska = alfa[:, :, 0] > 0.01
        if maska.any():
            ys, xs = np.where(maska)
            y1, y2, x1, x2 = int(ys.min()), int(ys.max()) + 1, int(xs.min()), int(xs.max()) + 1
            # cv2.blendLinear (wagi per piksel w C++): 1,0 ms zamiast 4,4 ms
            # dla mieszania w float32 numpy (zmierzone na malince 02.10)
            a2 = np.ascontiguousarray(alfa[y1:y2, x1:x2, 0])
            bbox = (y1, y2, x1, x2, np.ascontiguousarray(1.0 - a2), a2,
                    np.ascontiguousarray(rgb[y1:y2, x1:x2]).astype(np.uint8))
        else:
            bbox = None
        _radar_ov_cache.update(klucz=klucz, rgb=rgb, alfa=alfa, bbox=bbox)
    bbox = _radar_ov_cache.get("bbox")
    if bbox is not None:
        y1, y2, x1, x2, w_tlo, w_tekst, rgb8 = bbox
        band = frame[y0:h - hud_h]
        band[y1:y2, x1:x2] = cv2.blendLinear(band[y1:y2, x1:x2], rgb8, w_tlo, w_tekst)


RADAR: Optional[RadarWorker] = None


SAVE_WORKER: Optional["SaveWorker"] = None   # ustawiane w main()
SAVE_DB_PATH = ""                            # ścieżka bazy (menu na ekranie)
_snap_lock = threading.Lock()
_snap_frames: list = []
_snap_want = 0
_snap_last_fc = -999
_snap_flash_until = 0.0


def request_manual_snap() -> bool:
    global _snap_want
    with _snap_lock:
        if _snap_want > 0 or _snap_frames:
            return False              # poprzednia seria jeszcze trwa
        _snap_want = 3
    return True


def _manual_capture_tick(frame_rgb, fc: int) -> None:
    """Wołane z app_callback na CZYSTEJ klatce (przed nakładkami).
    Klatka jest RGB (24.09) — trzymamy ją tak i konwertujemy dopiero przy
    zapisie (SaveWorker.submit z extra_rgb=True), żeby callback nie robił
    trzech pełnoklatkowych konwersji naraz."""
    global _snap_want, _snap_last_fc, _snap_flash_until
    with _snap_lock:
        if _snap_want <= 0:
            return
        if _snap_frames and fc - _snap_last_fc < 8:   # ~0.27 s @ 30 fps
            return
        _snap_frames.append(frame_rgb.copy())
        _snap_last_fc = fc
        _snap_want -= 1
        _snap_flash_until = time.monotonic() + 0.3
        if _snap_want > 0:
            return
        frames = _snap_frames[:]
        _snap_frames.clear()
    if SAVE_WORKER is None:
        return
    now_dt = datetime.now()
    plate = "FOTO-" + now_dt.strftime("%H%M%S")
    mid = frames[1] if len(frames) > 1 else frames[0]
    h, w = mid.shape[:2]
    thumb = cv2.cvtColor(cv2.resize(mid, (max(2, w // 4), max(2, h // 4)),
                                    interpolation=cv2.INTER_AREA),
                         cv2.COLOR_RGB2BGR)
    extra = []
    if len(frames) > 1:
        extra.append(("km1", frames[0]))
    if len(frames) > 2:
        extra.append(("kp1", frames[2]))
    SAVE_WORKER.submit(plate_text=plate, full_rgb=mid, crop_bgr=thumb,
                       seen_at=now_dt, det_conf=0.0, ocr_conf=0.0,
                       ocr_reads=0, track_duration=0.0, bbox=(),
                       extra_crops=extra, extra_rgb=True)
    print(f"[Foto] ręczne zdjęcie {plate} zapisane (3 klatki)")


# ── Callback ──────────────────────────────────────────────────────────────────

_zone_cache = {"key": None, "segs": [], "pts": []}


def _zone_segments(w: int, h: int):
    """Segmenty przerywanej ramki strefy w px — przeliczane tylko przy zmianie."""
    with _zone_lock:
        zone = tuple(tuple(p) for p in DETECT_ZONE)
    key = (w, h, zone)
    if _zone_cache["key"] != key:
        pts = [(int(x * w), int(y * h)) for x, y in zone]
        segs = []
        n = len(pts)
        for i in range(n):
            p1, p2 = pts[i], pts[(i + 1) % n]
            dx = p2[0] - p1[0]; dy = p2[1] - p1[1]
            length = max(1, int((dx * dx + dy * dy) ** 0.5))
            for d in range(0, length, 20):
                if (d // 20) % 2 == 0:
                    t0 = d / length; t1 = min(d + 10, length) / length
                    segs.append(np.array(
                        [[int(p1[0] + dx * t0), int(p1[1] + dy * t0)],
                         [int(p1[0] + dx * t1), int(p1[1] + dy * t1)]],
                        dtype=np.int32))
        _zone_cache["key"], _zone_cache["segs"], _zone_cache["pts"] = key, segs, pts
    return _zone_cache["segs"], _zone_cache["pts"]

def _klatka_rgb_z_bufora(buffer, fmt: str, w: int, h: int):
    """GstBuffer → klatka RGB (widok tylko-do-odczytu) bez żadnej konwersji.

    Dlaczego nie `get_numpy_from_buffer` z hailo-apps: tamta robi
    `np.ndarray(..., buffer=map_info.data).copy()`, a `map_info.data`
    w PyGObject **samo jest już kopią** (zwraca `bytes`). Do 24.09 do tego
    dochodził jeszcze pełnoklatkowy `cvtColor` RGB→BGR (zmierzone 28.07:
    pobranie + cvtColor 10,04 ms = 30 % rdzenia @30 fps).

    Niżej się nie da: `memoryview(MapInfo)` nie działa (PyGObject nie
    wystawia protokołu bufora), wskaźnik przez ctypes też nie (`bytes`
    jest niemodyfikowalne), a `extract_dup` jest wolniejsze (14.4 ms).

    24.09.2026 — BEZ pełnoklatkowego `cvtColor`. Zwracamy widok RGB
    (tylko-do-odczytu) i NIC więcej. Odkąd nakładka rysuje się na kopii
    800×480 (05.08), pełnej klatki w BGR nie potrzebuje nikt: wycinki tablic
    konwertuje `_crop`/`_vehicle_crop` (kilka KB), zapisy — `SaveWorker.submit`
    (raz na zdarzenie, zamiast dotychczasowej kopii 9 MB), a jasność/ostrość
    liczą się z szarości (RGB2GRAY). Zmierzone na malince: cvtColor 2304×1296
    = 5,1 ms/klatkę = 15 % rdzenia przy 30 fps — oddane za darmo.
    Konwencja: zmienna `frame_rgb` = pełna klatka, `*_bgr` = wycinki/podgląd.
    """
    if fmt != "RGB":                     # inne formaty — jak dotąd (RGB)
        return get_numpy_from_buffer(buffer, fmt, w, h)
    from gi.repository import Gst
    ok, mi = buffer.map(Gst.MapFlags.READ)
    if not ok:
        return None
    try:
        potrzeba = w * h * 3
        if len(mi.data) < potrzeba:      # nietypowy stride/padding — nie ryzykuj
            return np.ndarray(shape=(h, w, 3), dtype=np.uint8,
                              buffer=mi.data).copy()
        # `mi.data` to obiekt bytes (kopia zrobiona przez PyGObject) — żyje
        # tak długo, jak trzymamy widok, także po unmap.
        return np.frombuffer(mi.data, dtype=np.uint8,
                             count=potrzeba).reshape(h, w, 3)
    finally:
        buffer.unmap(mi)


# ── Surowe ramki PRZED trackerem (09.10.2026) ─────────────────────────────────
# hailotracker podmienia ramkę detekcji na STAN FILTRU KALMANA (strack.hpp:
# update_tlwh bierze m_mean), więc przy nagłym ruchu kamery (dziura w asfalcie)
# ramka goni tablicę przez kilka klatek — użytkownik widział to w zdjęciach
# jako „ramka dobra, ale dla 3 klatek wstecz". Dodatkowy `identity lpr_raw`
# przed trackerem łapie ramki wprost z YOLO dla tej samej klatki (numer z PTS);
# w callbacku ramki tablic są podmieniane na surowe (IoU ≥ 0,3), id tracka
# zostaje z trackera. Różnice zliczane do logu „[Ramka]".
_raw_dets: OrderedDict = OrderedDict()
_raw_lock = threading.Lock()
_raw_stat = {"n": 0, "suma": 0.0, "maks": 0.0, "t": 0.0, "brak": 0}


def _raw_tap_cb(_el, buffer, _ud=None):
    try:
        fc = int(buffer.pts // _PTS_KLATKA_NS) if buffer.pts is not None else -1
        roi = hailo.get_roi_from_buffer(buffer)
        lst = []
        for d in roi.get_objects_typed(hailo.HAILO_DETECTION):
            b = d.get_bbox()
            lst.append((d.get_label(), d.get_confidence(), b.xmin(), b.ymin(),
                        b.xmin() + b.width(), b.ymin() + b.height()))
        with _raw_lock:
            _raw_dets[fc] = lst
            while len(_raw_dets) > 12:
                _raw_dets.popitem(last=False)
    except Exception as e:
        _zglos("raw_tap", e)


def _iou_xyxy(a, b) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    i = ix * iy
    u = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - i
    return i / u if u > 0 else 0.0


def _dets_surowe(dets: list, fc: int, fw: int, fh: int, now: float) -> list:
    """Ramki tablic → surowe z tej samej klatki. Wywoływać PRZED scalaniem
    połówek (surowe połówki też się scalą)."""
    with _raw_lock:
        raw = _raw_dets.get(fc)
    if raw is None:
        _raw_stat["brak"] += 1
        return dets
    out = []
    for tid, label, conf, x1, y1, x2, y2 in dets:
        if label == "license_plate":
            best, bi = 0.3, None
            for r in raw:
                if r[0] != label:
                    continue
                v = _iou_xyxy((x1, y1, x2, y2), r[2:])
                if v > best:
                    best, bi = v, r
            if bi is not None:
                dx = ((bi[2] + bi[4]) - (x1 + x2)) * 0.5 * fw
                dy = ((bi[3] + bi[5]) - (y1 + y2)) * 0.5 * fh
                d = (dx * dx + dy * dy) ** 0.5
                _raw_stat["n"] += 1
                _raw_stat["suma"] += d
                if d > _raw_stat["maks"]:
                    _raw_stat["maks"] = d
                x1, y1, x2, y2 = bi[2], bi[3], bi[4], bi[5]
        out.append((tid, label, conf, x1, y1, x2, y2))
    if now - _raw_stat["t"] > 60.0:
        if _raw_stat["n"]:
            print("[Ramka] %d ramek tablic: tracker vs surowa średnio %.1f px, "
                  "maks %.1f px (brak surowych dla %d klatek)"
                  % (_raw_stat["n"], _raw_stat["suma"] / _raw_stat["n"],
                     _raw_stat["maks"], _raw_stat["brak"]), flush=True)
        _raw_stat.update(n=0, suma=0.0, maks=0.0, t=now, brak=0)
    return out


_mpv_zatrzymany = [False]


def _splash_mpv_stop() -> None:
    """Animacja startowa (mpv z autostartu labwc, okno NAD wszystkim) kończy
    się, gdy jest co pokazać: pierwsza klatka z kamery, ekran błędu albo
    po 90 s. Raz na proces."""
    if _mpv_zatrzymany[0]:
        return
    _mpv_zatrzymany[0] = True
    try:
        import subprocess as _sp
        _sp.Popen(["pkill", "-x", "mpv"], stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
    except Exception as e:
        _zglos("mpv_stop", e)


def app_callback(element, buffer, user_data: LprState) -> None:
    global _stat_cb_count
    if buffer is None:
        return
    _cb_start = time.monotonic()

    pad = element.get_static_pad("src")
    fmt, w, h = get_caps_from_pad(pad)
    if not all([fmt, w, h]):
        return
    _stat_cb_count += 1

    # Ile klatek utknęło w potoku między kamerą a tym miejscem (patrz _diag).
    # Koszt: jedno dzielenie całkowite — wolno to robić przy 30 fps.
    if buffer.pts:
        _diag_zapisz_locie(_kamera_klatka - int(buffer.pts // _PTS_KLATKA_NS))

    frame_rgb = None
    if user_data.use_frame:
        frame_rgb = _klatka_rgb_z_bufora(buffer, fmt, w, h)
    if frame_rgb is None:
        return

    # 24.09: bufor z potoku to `lores` (1280×720) — do detekcji i podglądu.
    # Do wycinków, OCR, zdjęć, FOTO i ekspozycji bierzemy PEŁNĄ klatkę main
    # z pierścienia (ten sam numer klatki = to samo ujęcie sensora). Gdy jej
    # nie ma (zaległość potoku > LPR_PIERSCIEN klatek albo tryb bez pierścienia),
    # pracujemy na lores i liczymy braki do [Diag] — nigdy nie gubimy klatki.
    global _pierscien_brak
    fc_kam = int(buffer.pts // _PTS_KLATKA_NS) if buffer.pts is not None else -1
    full_rgb = _pierscien_wez(fc_kam)
    if full_rgb is None:
        full_rgb, fw, fh = frame_rgb, w, h
        if _pierscien:
            _pierscien_brak += 1
    else:
        fh, fw = full_rgb.shape[:2]

    roi        = hailo.get_roi_from_buffer(buffer)
    detections = roi.get_objects_typed(hailo.HAILO_DETECTION)
    now        = time.monotonic()
    fc         = user_data.get_count()

    _manual_capture_tick(full_rgb, fc)   # ręczne FOTO: czysta PEŁNA klatka

    # Wyciągnij track_id z każdej detekcji (ByteTracker HAILO_UNIQUE_ID)
    dets = []
    for det in detections:
        label = det.get_label()
        conf  = det.get_confidence()
        b     = det.get_bbox()
        xmin  = b.xmin()
        ymin  = b.ymin()
        xmax  = xmin + b.width()
        ymax  = ymin + b.height()
        tid_list = det.get_objects_typed(hailo.HAILO_UNIQUE_ID)
        tid = tid_list[0].get_id() if tid_list else -1
        dets.append((tid, label, conf, xmin, ymin, xmax, ymax))
    dets = _dets_surowe(dets, fc_kam, fw, fh, now)   # 09.10: ramka z TEJ klatki
    dets = _scal_polowki_tablic(dets)

    _ae_plate_tick(full_rgb, dets, fw, fh, fc, now)   # ekspozycja pod tablicę
    if RADAR_CFG["tryb"]:
        # historia detekcji do klipu z ramką śledzącą (tylko w trybie radar)
        _radar_dets_hist.append((time.time(), tuple(dets)))
        _ev = _radar_zdarzenie
        if _ev is not None and not _ev["zapisane"]:
            _radar_klatka_zdarzenia(_ev, full_rgb, dets, now)
    _cb_t1 = time.monotonic()          # koniec: pobranie klatki + detekcje

    # współrzędne detekcji są znormalizowane, więc wycinki z pełnej klatki
    # (fw×fh) wychodzą tak samo jak dawniej; progi w px (OCR_MIN_PLATE_PX)
    # liczą się względem pełnej klatki — bez zmian
    user_data.track_mgr.update(dets, full_rgb, fw, fh, fc, now)
    _cb_t2 = time.monotonic()          # koniec: sledzenie + wycinki + OCR

    # ── PODGLĄD W OSOBNYM WĄTKU (24.09.2026) ─────────────────────────────────
    # Do 24.09 ten callback — czyli WĄTEK POTOKU GStreamera — zmniejszał
    # klatkę, rysował strefę, narożniki, etykiety, dymki (PIL), HUD, pchał
    # klatkę do www i do okna. Log z jazd: przy 5 detekcjach callback trwał
    # 60-86 ms, z czego „nakładka" 27-48 ms, a potok miał 40-70 klatek
    # zaległości (1,3-2,3 s). Każda milisekunda rysowania opóźniała DETEKCJĘ
    # następnej klatki. Teraz callback zostawia tu tylko to, co musi widzieć
    # pełną klatkę (wycinki, OCR, AE) i oddaje robotę wątkowi `_nakladka`
    # przez skrzynkę na JEDNĄ klatkę: gdy tamten nie nadąża, gubi się klatka
    # PODGLĄDU, nigdy klatka rozpoznawania. Rysowanie czyta stan tracków
    # przez migawkę zrobioną tu (pod GIL-em, w tym samym wątku co update),
    # więc nie ogląda słownika w trakcie zmian.
    tracks = user_data.track_mgr.tracks
    rys = []
    for tid, label, conf, xmin, ymin, xmax, ymax in dets:
        if label == "license_plate" and conf >= PLATE_MIN_CONF:
            track = tracks.get(tid)
            ct    = track.confident_text if track else None
            if ct:
                label_text = ct
            elif track:
                label_text = f"{track.matching_count}/{OCR_MIN_MATCHING}"
            else:
                label_text = f"{conf:.0%}"
            rys.append(("plate", xmin, ymin, xmax, ymax, bool(ct), label_text))
        elif label != "license_plate":
            rys.append(("vehicle", xmin, ymin, xmax, ymax, False, ""))
    _nakladka_podaj({
        "frame": frame_rgb, "w": w, "h": h, "now": now, "rys": rys,
        "full": full_rgb,          # do zoomu podglądu (z pełnej rozdzielczości)
        "notifs": list(user_data.track_mgr.notifications),
        "hud": _hud_migawka(user_data.track_mgr, now),
        "web_ip": user_data.web_ip, "web_start": user_data.web_start_mono,
        "user_data": user_data,
    })

    # Ile trwał cały callback i który etap zawinił. Loguje się TYLKO gdy jest
    # źle (>60 ms), więc w normalnej pracy nie kosztuje nic poza kilkoma
    # odczytami zegara. Rozpoznawanie musi wyrobić się w 33 ms przy 30 fps —
    # przekroczenie oznacza zapychanie potoku. „Nakładka" to od 24.09 tylko
    # koszt migawki i oddania klatki (powinno być ~0).
    _cb_zapisz_czas(time.monotonic() - _cb_start, _cb_start, _cb_t1, _cb_t2,
                    len(dets), pominieto=False)


# ── Wątek nakładki i podglądu ─────────────────────────────────────────────────
# Skrzynka na jedną klatkę: callback podmienia zawartość, wątek bierze
# najnowszą. Stare klatki podglądu giną (licznik do [Diag]), rozpoznawanie
# nigdy nie czeka. Priorytet nice 4: niżej niż potok (-5) i niż OCR (3),
# bo OCR to rozpoznawanie i ma pierwszeństwo; wyżej niż zapis (5). Podgląd
# ma być płynny, gdy procesor ma zapas, a ustępować, gdy trwa czytanie tablic.
_nakl_lock = threading.Lock()
_nakl_cv = threading.Condition(_nakl_lock)
_nakl_item = None
_nakl_thread = None
_nakl_pominiete = 0          # klatki podglądu porzucone (wątek nie nadążał)
_nakl_ms = 0.0               # ostatni czas rysowania (do [Diag])
# 02.10: sumy czasów etapów rysowania od ostatniego [Diag] (ms) + czas CPU
# wątku — gdy „cpu" ≪ „razem", wątek nie pracuje, tylko CZEKA na procesor
_nakl_etapy = {"n": 0, "razem": 0.0, "cpu": 0.0, "maks": 0.0, "skala": 0.0,
               "ramki": 0.0, "dymki": 0.0, "hud": 0.0, "radar": 0.0,
               "www": 0.0, "ekran": 0.0}


def _nakladka_podaj(item: dict) -> None:
    """Callback → skrzynka. Startuje wątek przy pierwszym użyciu."""
    global _nakl_item, _nakl_thread, _nakl_pominiete
    with _nakl_cv:
        if _nakl_item is not None:
            _nakl_pominiete += 1
        _nakl_item = item
        if _nakl_thread is None:
            _nakl_thread = threading.Thread(target=_nakladka_watek,
                                            daemon=True, name="nakladka")
            _nakl_thread.start()
        _nakl_cv.notify()


def _hud_migawka(track_mgr, now: float) -> tuple:
    """Dane do HUD-u zebrane na wątku, który je zmienia (bez wyścigu)."""
    active = sum(1 for t in track_mgr.tracks.values() if not t.finalized)
    return (active, track_mgr.total_saved, track_mgr.last_confirmed)


def _nakladka_watek() -> None:
    global _nakl_item, _nakl_ms
    _ustap_procesor(4)
    while True:
        with _nakl_cv:
            while _nakl_item is None:
                _nakl_cv.wait()
            item, _nakl_item = _nakl_item, None
        t0 = time.monotonic()
        c0 = time.thread_time()
        try:
            _nakladka_rysuj(item)
        except Exception as e:
            _zglos("nakladka", e)
        _nakl_ms = (time.monotonic() - t0) * 1000.0
        e = _nakl_etapy
        e["n"] += 1
        e["razem"] += _nakl_ms
        e["cpu"] += (time.thread_time() - c0) * 1000.0
        if _nakl_ms > e["maks"]:
            e["maks"] = _nakl_ms


def _nakladka_rysuj(item: dict) -> None:
    frame_rgb, w, h, now = item["frame"], item["w"], item["h"], item["now"]
    user_data = item["user_data"]
    # etapy liczone czasem CPU WĄTKU (thread_time): czas ścienny zawierał
    # czekanie na procesor (nice 4 przy 80 % CPU) i mylił — np. „skala 11 ms"
    # to realnie 3,6 ms liczenia (zmierzone 02.10)
    _tt = [time.thread_time()]

    def _etap(k: str) -> None:
        t = time.thread_time()
        _nakl_etapy[k] += (t - _tt[0]) * 1000.0
        _tt[0] = t

    # ── NAKŁADKĘ RYSUJEMY NA MAŁEJ KLATCE, NIE NA PEŁNEJ ───────────────────
    # Pomiar z jazdy 05.08 pokazał, że sama nakładka zjadała 43-55 ms PRZY
    # ZEROWEJ liczbie detekcji ([CB-WOLNY] 316 razy w trakcie jazdy), przy
    # budżecie 33 ms na klatkę. Powód: rysowaliśmy na 2304×1296, żeby zaraz
    # zmniejszyć wszystko do 800×480 — malowaliśmy osiem razy więcej pikseli,
    # niż widać, plus kopia 9 MB pod nakładkę i druga 9 MB dla serwera www.
    # Wszystkie współrzędne rysowania są ZNORMALIZOWANE (×w, ×h), więc
    # wystarczy zmniejszyć klatkę i podać wymiary podglądu.
    dw, dh = _podglad_wh
    if _podglad_zoom > 1:
        # zoom wycinamy z PEŁNEJ rozdzielczości (main z pierścienia) —
        # inaczej powiększalibyśmy już zmniejszony obraz i podgląd byłby rozmyty
        full = item.get("full")
        if full is not None:
            frame_rgb = full
            h, w = full.shape[:2]
        zw, zh = w // _podglad_zoom, h // _podglad_zoom
        x0, y0 = (w - zw) // 2, (h - zh) // 2
        frame_rgb = frame_rgb[y0:y0 + zh, x0:x0 + zw]
    # najpierw zmniejszenie, DOPIERO potem RGB→BGR (0,4 ms zamiast 5 ms)
    frame_bgr = cv2.cvtColor(cv2.resize(frame_rgb, (dw, dh),
                                        interpolation=cv2.INTER_NEAREST),
                             cv2.COLOR_RGB2BGR)
    w, h = dw, dh
    _etap("skala")

    # Strefa detekcji — przerywany czworokąt (segmenty z cache: liczone tylko
    # przy zmianie strefy/rozdzielczości; wcześniej ~100 wywołań cv2.line
    # w pythonowej pętli NA KAŻDĄ klatkę)
    segs, zone_pts = _zone_segments(w, h)
    if segs:
        cv2.polylines(frame_bgr, segs, False, COLOR_ROI, 1)
    for px, py in zone_pts:
        cv2.circle(frame_bgr, (px, py), 4, COLOR_ROI, -1)

    for rodzaj, xmin, ymin, xmax, ymax, pewna, label_text in item["rys"]:
        x1 = int(xmin * w)
        y1 = int(ymin * h)
        x2 = int(xmax * w)
        y2 = int(ymax * h)
        if rodzaj == "plate":
            color = COLOR_CONFIDENT if pewna else COLOR_TRACKING
            # Grubości i czcionki dobrane pod EKRAN (800×480). Wcześniej
            # rysowaliśmy na 2304 px i zmniejszali 2,88×, więc te same liczby
            # dawały na ekranie cienkie, rozmyte kreski — teraz są ostre.
            thick = 2 if pewna else 1
            _draw_corners(frame_bgr, x1, y1, x2, y2, color, thick)
            # potwierdzony numer — duża czcionka; licznik głosów — dyskretna
            _label_box(frame_bgr, label_text, x1, y1, color,
                       font_scale=0.6 if pewna else 0.4,
                       font_thick=2 if pewna else 1)
        else:
            # Pojazd — bardzo dyskretne narożniki (nie odwracają uwagi od tablic).
            # Tablice PONIŻEJ progu detekcji nie są rysowane wcale — wcześniej
            # dostawały narożniki pojazdu i wyglądały na "wykryte" mimo progu.
            _draw_corners(frame_bgr, x1, y1, x2, y2, COLOR_VEHICLE, 1, ratio=0.18)

    _etap("ramki")
    # Dymki powiadomień: crop tablicy + numer (jak powiadomienia na telefonie)
    _draw_notifications(frame_bgr, w, item["notifs"], now)
    _etap("dymki")

    # HUD pasek statusu (dane z migawki zrobionej w callbacku)
    _draw_hud_migawka(frame_bgr, w, h, item["hud"], now)
    _etap("hud")

    # tryb RADAR: prędkość z radaru, próg i zdarzenia u góry ekranu
    if RADAR_CFG["tryb"]:
        _draw_radar_overlay(frame_bgr, w, h, now)
    _etap("radar")

    # Adres webowy przez pierwsze 15 sekund (przed HUDem żeby nie zasłaniał)
    if item["web_ip"] and (now - item["web_start"]) < 15.0:
        url   = f"http://{item['web_ip']}:{WebServer.PORT}"
        label = f"WEB:  {url}"
        font  = cv2.FONT_HERSHEY_SIMPLEX
        scale, thick = 0.65, 1
        (tw, th), _ = cv2.getTextSize(label, font, scale, thick)
        pad   = 8
        bx1, by1 = 12, 12
        bx2, by2 = bx1 + tw + pad * 2, by1 + th + pad * 2
        cv2.rectangle(frame_bgr, (bx1, by1), (bx2, by2), (10, 10, 10), -1)
        cv2.rectangle(frame_bgr, (bx1, by1), (bx2, by2), (88, 166, 255), 1)
        cv2.putText(frame_bgr, label, (bx1 + pad, by2 - pad),
                    font, scale, (88, 166, 255), thick, cv2.LINE_AA)

    # Push do web streamu (osobny wątek kodera, nie blokuje)
    if user_data.web_server:
        user_data.web_server.push_frame(frame_bgr)
    _etap("www")

    # Klatka jest już w rozmiarze ekranu i z narysowaną nakładką — wątek
    # podglądu nie musi nic skalować ani rysować, tylko wyświetlić.
    user_data.set_frame(frame_bgr)
    _etap("ekran")


# ── Web interface ────────────────────────────────────────────────────────────

_WEB_ICON_DIR = Path("/home/admin/hailo-apps/local_resources/web")


def _make_icon(path: Path, size: int) -> None:
    """Generuje ikonę aplikacji (PWA): granatowe tło + biała tablica z pasem PL."""
    img = np.full((size, size, 3), (16, 9, 14), np.uint8)      # tło #0e0910 (BGR)
    m = size // 7
    y1, y2 = size // 2 - size // 6, size // 2 + size // 6
    cv2.rectangle(img, (m, y1), (size - m, y2), (245, 245, 245), -1)
    cv2.rectangle(img, (m, y1), (m + size // 11, y2), (153, 51, 0), -1)  # pas PL
    cv2.rectangle(img, (m, y1), (size - m, y2), (40, 40, 40), max(2, size // 90))
    fs = size / 160.0
    (tw, th), _ = cv2.getTextSize("LPR", cv2.FONT_HERSHEY_DUPLEX, fs, max(2, size // 60))
    cv2.putText(img, "LPR", (m + size // 11 + ((size - 2 * m - size // 11) - tw) // 2,
                (y1 + y2 + th) // 2),
                cv2.FONT_HERSHEY_DUPLEX, fs, (30, 30, 30), max(2, size // 60), cv2.LINE_AA)
    path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(path), img)

def _get_local_ip() -> str:
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


_HTML_PAGE = r"""<!DOCTYPE html>
<html lang="pl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<link rel="manifest" href="/manifest.json">
<meta name="theme-color" content="#0e0910">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<link rel="apple-touch-icon" href="/icon-192.png">
<title>LPR/ANPR — System Rozpoznawania Tablic</title>
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

/* Motyw PiLPR (VHS) — kolory pobrane wprost z grafiki logo:
   tło #0e0910, tablica/tekst #c2b4bd, niebieski UE #202c71, złoto gwiazdek
   #ca9b3a. Kolory ocen (zielony/czerwony) zostają wyraziste — muszą być
   czytelne na pierwszy rzut oka podczas weryfikacji. */
:root {
  --bg:      #0e0910;
  --bg-row:  #150e18;
  --bg-row2: #0e0910;
  --bg-head: #090509;
  --bg-th:   #1a1222;
  --border:  #2b2034;
  --text:    #c2b4bd;
  --dim:     #6d5f74;
  --blue:    #6f7fd8;
  --green:   #4a9a5e;
  --yellow:  #ca9b3a;
  --red:     #d14b52;
  --new-row: rgba(111, 127, 216, 0.14);
}

/* ── PIXEL ART: czcionki serwowane LOKALNIE (dzialaja bez internetu) ──
   Dwie role, zeby bylo klimatycznie ALE czytelnie w jezdzie:
   • PixelHead (Press Start 2P) — naglowki, przyciski, etykiety. Blokowa,
     bardzo „8-bit", ale szeroka — tylko krotkie napisy.
   • PixelData (VT323) — dane, tabela, liczby. Terminal CRT: wask i czytelny
     nawet przy 15-16 px, wiec tablice daja sie skanowac wzrokiem. */
@font-face {
  font-family: 'PixelHead';
  src: url('/font/PressStart2P-Regular.ttf') format('truetype');
  font-display: swap;
}
@font-face {
  font-family: 'PixelData';
  src: url('/font/VT323-Regular.ttf') format('truetype');
  font-display: swap;
}

html, body {
  height: 100%;
  background: var(--bg);
  color: var(--text);
  font-family: 'PixelData', 'Courier New', monospace;
  font-size: 17px;              /* VT323 rysuje drobno — stad wieksza podstawa */
  line-height: 1.25;
  overflow: hidden;
  -webkit-font-smoothing: none; /* bez wygladzania = ostre piksele */
  font-smooth: never;
}

/* Pixel art nie ma zaokraglen — wszystko na ostro */
button, input, select, .rvb, .pg-btn, .cam-btn, #save-btn,
#refresh-btn, #reviewer-btn, .vbtn, .hdr-btn {
  border-radius: 0 !important;
  font-family: 'PixelData', 'Courier New', monospace;
  letter-spacing: 0.5px;
  text-transform: uppercase;
}

/* PixelHead (Press Start 2P) NIE MA polskich znakow — przy „OSTROŚĆ"
   przegladarka podstawiala inny krój i napis sie rozjezdzal. Dlatego czcionka
   blokowa TYLKO tam, gdzie napisy sa bez ogonkow (naglowek, logo, stronicowanie),
   a wszedzie indziej VT323 — tez pikselowy, ale z pelna polszczyzna. */
.brand-name, #header button, .pg-btn, #save-btn, #refresh-btn, #reviewer-btn {
  font-family: 'PixelHead', 'PixelData', monospace;
  font-size: 11px;
  letter-spacing: 1px;
}
.brand-name { font-size: 13px; }
.brand-sub, .stat { font-family: 'PixelData', monospace; letter-spacing: 0.5px; }
/* ZDJEC NIE RUSZAMY. Pierwotnie bylo tu `image-rendering: pixelated` na
   miniaturach i podgladzie — wygladalo „w stylu", ale to narzedzie do CZYTANIA
   tablic: pikseloza pogarszala czytelnosc, czyli szkodzila glownej funkcji.
   Pixel-art dotyczy INTERFEJSU (czcionki, ikony, krawedzie), nie materialu
   dowodowego. Z tego samego powodu nie ma tu nakladki „kineskopowej" (scanlines)
   — przyciemniala caly obraz z kamery. */

/* ── HEADER ─────────────────────────────────────────────── */
#header {
  display: flex;
  align-items: center;
  height: 38px;
  background: var(--bg-head);
  border-bottom: 1px solid var(--border);
  padding: 0 12px;
  gap: 0;
  flex-shrink: 0;
  white-space: nowrap;
}

#header .brand {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-right: 20px;
}

#header .brand-icon {
  width: 22px;
  height: 22px;
  background: var(--blue);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 11px;
  font-weight: 700;
  color: #fff;
  letter-spacing: -0.5px;
}

#header .brand-name {
  font-size: 13px;
  font-weight: 600;
  color: #fff;
  letter-spacing: 0.5px;
}

#header .brand-sub {
  font-size: 11px;
  color: var(--dim);
  letter-spacing: 0.3px;
}

#header .sep {
  width: 1px;
  height: 20px;
  background: var(--border);
  margin: 0 14px;
}

#header .stat {
  font-size: 12px;
  color: var(--dim);
}

#header .stat span {
  color: var(--text);
  font-weight: 600;
}

#header .spacer {
  flex: 1;
}

#header .status-online {
  font-size: 12px;
  font-weight: 600;
  color: var(--green);
  letter-spacing: 0.3px;
}

#header .status-offline {
  font-size: 12px;
  font-weight: 600;
  color: var(--red);
  letter-spacing: 0.3px;
}

#header .clock {
  font-size: 13px;
  font-family: 'PixelData', 'Courier New', monospace;
  color: var(--text);
  margin-left: 14px;
  letter-spacing: 0.5px;
}

/* ── MAIN LAYOUT ─────────────────────────────────────────── */
#main {
  display: flex;
  height: calc(100vh - 38px - 46px);
  overflow: hidden;
}

/* ── STREAM PANEL ────────────────────────────────────────── */
#stream-panel {
  width: 52%;   /* przy 60% tabela nie miescila wszystkich kolumn na 1280px */
  min-width: 320px;
  display: flex;
  flex-direction: column;
  background: #000;
  flex-shrink: 0;
}

#splitter {
  width: 8px;
  cursor: col-resize;
  background: var(--bg-head);
  border-left: 1px solid var(--border);
  border-right: 1px solid var(--border);
  flex-shrink: 0;
  touch-action: none;
}

#splitter:hover, #splitter.drag {
  background: var(--blue);
}

#stream-label {
  height: 28px;
  display: flex;
  align-items: center;
  padding: 0 10px;
  background: var(--bg-th);
  border-bottom: 1px solid var(--border);
  font-size: 11px;
  color: var(--dim);
  letter-spacing: 0.5px;
  text-transform: uppercase;
  gap: 8px;
  flex-shrink: 0;
}

#stream-label .rec-dot {
  width: 7px;
  height: 7px;
  background: var(--green);         /* zielona = strumień OK, czerwona = problem */
  border-radius: 50%;
  animation: recpulse 2s ease-in-out infinite;
}

#stream-label .rec-dot.err {
  background: var(--red);
  animation: none;
}

@keyframes recpulse {
  0%, 100% { opacity: 1; }
  50%      { opacity: 0.35; }
}

#stream-wrap {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  overflow: hidden;
  position: relative;
}

#stream-wrap img {
  max-width: 100%;
  max-height: 100%;
  object-fit: contain;
  display: block;
}

#stream-wrap .no-signal {
  color: var(--dim);
  font-size: 12px;
  letter-spacing: 0.5px;
  text-align: center;
}

#stream-stats {
  position: absolute;
  bottom: 0;
  left: 0;
  right: 0;
  height: 26px;
  background: rgba(8, 12, 18, 0.82);
  border-top: 1px solid rgba(30,45,64,.6);
  display: flex;
  align-items: center;
  padding: 0 10px;
  gap: 0;
  pointer-events: none;
}
.ss-item {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 5px;
  font-size: 11px;
  font-family: 'PixelData', 'Courier New', monospace;
  letter-spacing: 0.2px;
}
.ss-lbl {
  color: #48586a;
  font-size: 10px;
  letter-spacing: 0.4px;
}
.ss-val {
  color: #8ea8c0;
}
.ss-val.alert { color: #cc4444; }
.ss-sep {
  width: 1px;
  height: 14px;
  background: rgba(30,45,64,.8);
}

/* ── ZONE EDITOR ─────────────────────────────────────────── */
#zone-btn {
  margin-left: auto;
  padding: 2px 8px;
  background: none;
  border: 1px solid #2a3a4a;
  color: #48586a;
  font-size: 10px;
  font-family: inherit;
  letter-spacing: 0.5px;
  text-transform: uppercase;
  cursor: pointer;
  border-radius: 2px;
}
#zone-btn.active {
  border-color: #3a7bd5;
  color: #3a7bd5;
}
#zone-save-row {
  display: none;
  gap: 4px;
}
#zone-save-row.show { display: flex; }
#zone-save-row button {
  padding: 2px 8px;
  border-radius: 2px;
  font-size: 10px;
  font-family: inherit;
  letter-spacing: 0.5px;
  text-transform: uppercase;
  cursor: pointer;
  border: 1px solid;
}
#zone-confirm-btn { background: none; border-color: #3a7bd5; color: #3a7bd5; }
#zone-cancel-btn  { background: none; border-color: #2a3a4a; color: #48586a; }
#zone-svg {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  display: none;
  touch-action: none;
}
#zone-svg.show { display: block; }
#zone-poly {
  fill: rgba(58, 123, 213, 0.08);
  stroke: #3a7bd5;
  stroke-width: 1.5;
  stroke-dasharray: 6 6;
}
.zone-handle {
  fill: #3a7bd5;
  stroke: #0e0910;
  stroke-width: 1.5;
  cursor: grab;
  r: 7;
}
.zone-handle:active { cursor: grabbing; }

/* ── EVENTS PANEL ────────────────────────────────────────── */
#events-panel {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--bg);
}

/* pasek podkrecania obrazu w edytorze */
#rv-tune {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 5px 8px;
  flex-wrap: wrap;
  background: var(--bg-th);
  border-top: 1px solid var(--border);
}
.tn-btn {
  background: var(--bg-head);
  color: var(--text);
  border: 1px solid var(--border);
  padding: 5px 9px;
  font-size: 12px;
  cursor: pointer;
  min-height: 30px;
}
.tn-btn:hover { border-color: var(--blue); }
.tn-btn:active { background: var(--blue); color: #fff; }
.tn-reset { color: var(--dim); }
#rv-tune-info { font-size: 12px; color: var(--yellow); margin-left: 4px; }

#tbl-pager {
  display: none;
  flex-shrink: 0;
  align-items: center;
  justify-content: center;
  gap: 12px;
  padding: 8px;
  background: var(--bg-th);
  border-top: 1px solid var(--border);
}
.pg-btn {
  background: var(--bg-head);
  color: var(--text);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 6px 14px;
  font-size: 13px;
  cursor: pointer;
}
.pg-btn:disabled { opacity: 0.4; cursor: default; }
.pg-btn:not(:disabled):hover { border-color: var(--blue); }
.pg-info { font-size: 12px; color: var(--dim); }

#events-toolbar {
  height: 36px;
  display: flex;
  align-items: center;
  padding: 0 8px;
  background: var(--bg-th);
  border-bottom: 1px solid var(--border);
  gap: 6px;
  flex-shrink: 0;
}

#events-toolbar label {
  font-size: 11px;
  color: var(--dim);
  text-transform: uppercase;
  letter-spacing: 0.4px;
}

#search-input {
  flex: 1;
  min-width: 0;   /* input ma domyslna min-szerokosc ~170px — na telefonie wypychal pasek */
  height: 24px;
  background: var(--bg);
  border: 1px solid var(--border);
  color: var(--text);
  font-size: 12px;
  font-family: 'PixelData', 'Courier New', monospace;
  padding: 0 7px;
  outline: none;
}

#search-input:focus {
  border-color: var(--blue);
}

#refresh-btn, #reviewer-btn {
  height: 24px;
  padding: 0 8px;
  background: var(--bg-th);
  border: 1px solid var(--border);
  color: var(--text);
  font-size: 12px;
  cursor: pointer;
  letter-spacing: 0.3px;
}

#refresh-btn:hover, #reviewer-btn:hover {
  background: var(--border);
}

#reviewer-btn { border-color: var(--blue); color: var(--blue); }

/* ── EDYTOR WERYFIKACJI (pelny ekran) ── */
#reviewer {
  display: none;
  position: fixed;
  inset: 0;
  background: var(--bg);
  z-index: 1200;
  flex-direction: column;
}

#reviewer.open { display: flex; }

#rv-head {
  display: flex;
  align-items: center;
  height: 44px;
  padding: 0 14px;
  background: var(--bg-head);
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}

#rv-count {
  font-size: 14px;
  font-weight: 700;
  color: var(--blue);
  min-width: 140px;
}

#rv-title { flex: 1; text-align: center; font-size: 13px; color: var(--dim); letter-spacing: 1px; }

#rv-close { font-size: 22px; color: var(--dim); cursor: pointer; padding: 4px 8px; }
#rv-close:hover { color: var(--text); }

#rv-body {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: flex-start;
  gap: 6px;
  overflow: hidden;
  padding: 6px;
}

/* odczyt OCR wycentrowany pod zdjęciem, id dyskretnie obok */
#rv-meta {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 14px;
  flex-shrink: 0;
}

#rv-head #rv-boxfix { margin-right: 14px; }

#rv-imgwrap { position: relative; line-height: 0; flex-shrink: 1; min-height: 0; }
#rv-full { max-width: 96vw; max-height: 30vh; object-fit: contain; border: 1px solid var(--border); }
#rv-box  { position: absolute; border: 2px solid var(--green);
           box-shadow: 0 0 8px rgba(61,154,61,0.9); display: none; pointer-events: none; }

/* kolumny kandydatów na całą szerokość: starsza | NAJLEPSZA | nowsza;
   w każdej kolumnie ta sama klatka w trzech malejących rozmiarach
   (mniejsza skala bywa czytelniejsza przy rozmyciu) */
#rv-cands {
  display: flex;
  width: 100%;
  gap: 12px;
  align-items: flex-start;
  justify-content: center;
}

.rv-col {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
}

.rv-col-cap { font-size: 11px; color: var(--dim); letter-spacing: 0.5px; }

.rv-col img {
  object-fit: contain;
  border: 1px solid var(--border);
  background: #000;
  max-width: 100%;
  width: auto;
}

/* rozmiary od WYSOKOŚCI ekranu — całość zawsze mieści się bez scrollowania */
.rv-col img.s1 { height: 15vh; }
.rv-col img.s2 { height: 9.5vh; }
.rv-col img.s3 { height: 5.5vh; }
.rv-col-best img.s1 { border: 2px solid var(--green); }
/* Klatka wskazana recznie jako lepsza od zapisanej — to ona pojdzie
   do uczenia. Zolty, zeby nie mylic sie z zielonym „zapisana". */
.rv-col.rv-wybrana img { border-color: var(--yellow) !important; }
.rv-col.rv-wybrana .rv-col-cap::after { content: '  ★ DO UCZENIA'; color: var(--yellow); }
#rv-bonus img.wybrana { border: 2px solid var(--yellow); }
/* Suwaki regulacji obrazu w edytorze */
.tn-sl { display: inline-flex; align-items: center; gap: 6px; font-size: 10px;
         color: var(--dim); letter-spacing: .06em; margin-right: 10px; }
.tn-sl input[type=range] { width: 90px; accent-color: var(--blue); }
.tn-sl span { min-width: 30px; text-align: right; color: var(--text);
              font-family: PixelData, monospace; }

#rv-bonus { display: flex; gap: 6px; justify-content: center; flex-wrap: nowrap;
            overflow: hidden; flex-shrink: 0; }
#rv-bonus img { height: 4.5vh; border: 1px solid var(--border); background: #000; }

#rv-cands-toggle {
  height: 26px;
  padding: 0 10px;
  margin-right: 14px;
  background: var(--bg-th);
  border: 1px solid var(--border);
  color: var(--text);
  font-size: 11px;
  cursor: pointer;
  border-radius: 3px;
}
#rv-cands-toggle:hover { background: var(--border); }

/* numer jak na prawdziwej tablicy: biała, niebieski pas PL, czcionka techniczna */
#rv-plate {
  display: inline-flex;
  align-items: stretch;
  background: #f5f5f5;
  border: 2px solid #222;
  border-radius: 7px;
  overflow: hidden;
  box-shadow: 0 2px 10px rgba(0,0,0,0.5);
}

#rv-plate .pl-strip {
  background: #003399;
  color: #fff;
  font-family: 'PixelHead', monospace;
  font-size: 13px;
  font-weight: 700;
  display: flex;
  align-items: flex-end;
  padding: 4px 7px;
}

#rv-plate #rv-plate-num {
  font-family: 'PixelData', 'Arial Narrow', sans-serif;
  font-size: 32px;
  line-height: 1.2;             /* bez tego dolne części cyfr były ucinane */
  font-weight: 700;
  color: #111;
  letter-spacing: 4px;
  padding: 4px 10px 4px 12px;   /* letter-spacing dodaje odstęp za ostatnim znakiem */
  display: flex;
  align-items: center;
  font-stretch: condensed;
  white-space: nowrap;
}

#rv-sid { font-size: 12px; color: var(--dim); }

/* ── reczna korekta ramki ── */
#rv-boxfix, #lb-boxfix {
  height: 26px;
  padding: 0 10px;
  background: var(--bg-th);
  border: 1px solid rgba(255, 157, 46, 0.5);
  color: #ff9d2e;
  font-size: 11px;
  letter-spacing: 0.4px;
  cursor: pointer;
  border-radius: 3px;
}

#rv-boxfix:hover, #lb-boxfix:hover { background: var(--border); }
#rv-boxfix.armed, #lb-boxfix.armed {
  background: #ff9d2e;
  color: #111;
  border-color: #ff9d2e;
}

#lb-boxfix {
  position: absolute;
  top: 16px;
  left: 20px;
  z-index: 1;
}

/* poprawiona (ludzka) ramka = pomaranczowa */
#rv-box.fixed, #lightbox-box.fixed {
  border-color: #ff9d2e;
  box-shadow: 0 0 8px rgba(255, 157, 46, 0.9);
}

#rv-wait {
  flex: 1;
  display: none;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 14px;
  font-size: 24px;
  color: var(--green);
  text-align: center;
}

#rv-wait .rv-sub { font-size: 14px; color: var(--dim); }
#rv-stream { max-width: 80vw; max-height: 45vh; border: 1px solid var(--border); }

#rv-btns {
  display: grid;
  grid-template-columns: repeat(6, 1fr);
  gap: 8px;
  padding: 10px;
  flex-shrink: 0;
  background: var(--bg-head);
  border-top: 1px solid var(--border);
}

.rvb {
  min-height: 64px;
  font-size: 16px;
  font-weight: 700;
  letter-spacing: 0.5px;
  border-radius: 4px;
  cursor: pointer;
  border: 1px solid var(--border);
  background: var(--bg-th);
}

.rvb.rv-ok   { color: #fff; background: var(--green);  border-color: var(--green); }
.rvb.rv-fix  { color: #fff; background: var(--yellow); border-color: var(--yellow); }
.rvb.rv-part { color: #fff; background: var(--blue);   border-color: var(--blue); }
.rvb.rv-unr  { color: #fff; background: #5f6368;       border-color: #5f6368; }
.rvb.rv-not  { color: #fff; background: var(--red);    border-color: var(--red); }
.rvb:active  { filter: brightness(1.3); }

/* tryb rysowania ramki: klatka na caly ekran, reszta schowana */
#reviewer.boxfix #rv-meta,
#reviewer.boxfix #rv-cands,
#reviewer.boxfix #rv-bonus,
#reviewer.boxfix #rv-btns { display: none !important; }
#reviewer.boxfix #rv-full { max-height: 84vh; }
#lightbox.boxfix #lightbox-crop,
#lightbox.boxfix #lightbox-plate { display: none !important; }
#lightbox.boxfix #lightbox-img { max-height: 86vh; }
/* pinch-zoom w trybie rysowania: powiekszony obraz nie moze rozpychac strony,
   a przegladarka nie moze przechwycic gestow (scroll/pinch strony) */
#reviewer.boxfix, #lightbox.boxfix { overflow: hidden; }
#reviewer.boxfix #rv-imgwrap,
#lightbox.boxfix #lightbox-imgwrap { touch-action: none; }

#refresh-btn:active {
  background: #241a2e;
}

#table-wrap {
  flex: 1;
  overflow-y: auto;
  overflow-x: auto;   /* szeroka tabela przewija się WEWNĄTRZ panelu, nie strona */
}

#table-wrap::-webkit-scrollbar {
  width: 6px;
}

#table-wrap::-webkit-scrollbar-track {
  background: var(--bg);
}

#table-wrap::-webkit-scrollbar-thumb {
  background: var(--border);
}

#table-wrap::-webkit-scrollbar-thumb:hover {
  background: var(--dim);
}

table#events-table {
  width: 100%;
  border-collapse: collapse;
  table-layout: fixed;
}

table#events-table thead {
  position: sticky;
  top: 0;
  z-index: 10;
}

table#events-table th {
  background: var(--bg-th);
  color: var(--dim);
  font-size: 10px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  padding: 0 6px;
  height: 26px;
  text-align: left;
  border-bottom: 1px solid var(--border);
  border-right: 1px solid var(--border);
  white-space: nowrap;
  overflow: hidden;
}

table#events-table th:last-child {
  border-right: none;
}

/* col widths */
table#events-table col.c-time   { width: 50px; }
/* NIE "auto": przy waskim panelu auto-kolumna dostaje 0 px i tablice znikaja */
table#events-table col.c-plate  { width: 110px; }
table#events-table col.c-det    { width: 42px; }
table#events-table col.c-ocr    { width: 42px; }
table#events-table col.c-dur    { width: 36px; }
table#events-table col.c-img    { width: 100px; }

/* --tscale: skala tabeli rosnie, gdy poszerzasz panel suwakiem (JS) */
#events-panel { --tscale: 1; }

table#events-table {
  font-size: calc(12px * var(--tscale));
}

table#events-table td {
  height: calc(38px * var(--tscale));
  padding: 0 6px;
  border-bottom: 1px solid var(--border);
  border-right: 1px solid var(--border);
  vertical-align: middle;
  overflow: hidden;
}

table#events-table td:last-child {
  border-right: none;
}

table#events-table tbody tr:nth-child(odd) {
  background: var(--bg-row);
}

table#events-table tbody tr:nth-child(even) {
  background: var(--bg-row2);
}

table#events-table tbody tr:hover {
  background: #15202e;
}

table#events-table tbody tr.new-row {
  background: var(--new-row);
  opacity: 1;
  transition: background 1.5s ease;
}

.td-time {
  font-size: 11px;
  color: var(--dim);
  font-family: 'PixelData', 'Courier New', monospace;
  white-space: nowrap;
}

.td-plate {
  font-family: 'PixelData', Consolas, 'DejaVu Sans Mono', monospace;
  font-size: calc(14px * var(--tscale, 1));
  font-weight: 700;
  color: #fff;
  letter-spacing: 0.5px;
  white-space: nowrap;
}

.td-plate.unknown {
  color: var(--yellow);
}

tr.unknown-row td:nth-child(2) {
  border-left: 3px solid var(--yellow);
  padding-left: 4px;
}

.conf-green { color: var(--green); font-size: 11px; font-family: 'Courier New', monospace; }
.conf-yellow { color: var(--yellow); font-size: 11px; font-family: 'Courier New', monospace; }
.conf-red { color: var(--red); font-size: 11px; font-family: 'Courier New', monospace; }

.td-dur {
  font-size: 11px;
  color: var(--dim);
  text-align: right;
  font-family: 'PixelData', 'Courier New', monospace;
}

.td-img {
  text-align: right;
  padding: 3px 6px;
}

.td-img img {
  width: calc(96px * var(--tscale, 1));
  height: calc(32px * var(--tscale, 1));
  object-fit: cover;
  display: block;
  margin-left: auto;
  cursor: pointer;
  border: 1px solid var(--border);
}

.td-img img:hover {
  border-color: var(--blue);
}

.td-img .no-img {
  font-size: 10px;
  color: var(--border);
  text-align: right;
}

/* ── FOOTER / CONFIG ─────────────────────────────────────── */
#footer {
  height: 46px;
  background: var(--bg-head);
  border-top: 1px solid var(--border);
  display: flex;
  align-items: center;
  padding: 0 12px;
  gap: 0;
  flex-shrink: 0;
  overflow: hidden;
}

.cfg-label {
  font-size: 11px;
  color: var(--dim);
  white-space: nowrap;
  margin-right: 6px;
}

.cfg-group {
  display: flex;
  align-items: center;
  gap: 5px;
  margin-right: 16px;
}

.cfg-group input[type=range] {
  width: 90px;
  height: 4px;
  accent-color: var(--blue);
  cursor: pointer;
  background: var(--border);
}

.cfg-val {
  font-size: 11px;
  font-family: 'PixelData', 'Courier New', monospace;
  color: var(--text);
  min-width: 30px;
  text-align: left;
}

#footer .sep {
  width: 1px;
  height: 20px;
  background: var(--border);
  margin: 0 12px;
}

#save-btn {
  height: 26px;
  padding: 0 14px;
  background: var(--blue);
  border: none;
  color: #fff;
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
  letter-spacing: 0.3px;
}

#save-btn:hover {
  background: #1560d4;
}

#save-btn:active {
  background: #0f4eaa;
}

#save-status {
  font-size: 11px;
  margin-left: 10px;
  min-width: 80px;
}

#save-status.ok { color: var(--green); }
#save-status.err { color: var(--red); }

/* ── FOCUS MODAL ─────────────────────────────────────────── */
#focus-modal {
  display: none;
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.85);
  z-index: 950;
  align-items: center;
  justify-content: center;
}

#focus-panel {
  width: min(1500px, 97vw);
  background: var(--bg-row);
  border: 1px solid var(--border);
  display: flex;
  flex-direction: column;
}

#focus-img { width: 100%; max-height: 86vh; object-fit: contain; background: #000; }

#focus-btn, #cam-btn, #snap-btn {
  height: 22px;
  padding: 0 8px;
  margin-right: 6px;
  background: var(--bg-th);
  border: 1px solid var(--border);
  color: var(--text);
  font-size: 11px;
  cursor: pointer;
}
#focus-btn:hover, #cam-btn:hover, #snap-btn:hover { background: var(--border); }
#snap-btn.done { border-color: var(--green); color: #5c5; }

/* ── RECORDINGS MODAL ────────────────────────────────────── */
#rec-modal {
  display: none;
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.75);
  z-index: 900;
  align-items: center;
  justify-content: center;
}

#rec-panel {
  width: min(720px, 94vw);
  max-height: 88vh;
  background: var(--bg-row);
  border: 1px solid var(--border);
  display: flex;
  flex-direction: column;
}

#rec-hdr {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 10px 14px;
  border-bottom: 1px solid var(--border);
  font-size: 13px;
  letter-spacing: 0.5px;
}

#rec-hdr button { background: none; border: none; color: var(--dim);
                  font-size: 18px; cursor: pointer; }

#rec-player { display: none; padding: 10px; text-align: center; }
#rec-player video {
  max-width: 100%;
  max-height: 44vh;
  border: 1px solid var(--border);
  background: #000;
}
#rec-pstat { color: var(--dim); font-size: 12px; padding: 4px 0 6px; min-height: 16px; }
.rec-pbar {
  height: 7px;
  max-width: 300px;
  margin: 7px auto 0;
  background: var(--bg-th);
  border: 1px solid var(--border);
  border-radius: 4px;
  overflow: hidden;
}
.rec-pbar div {
  height: 100%;
  width: 0;
  background: var(--blue);
  transition: width 0.7s linear;
}
#rec-ctl {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  margin-top: 8px;
  flex-wrap: wrap;
}
.rec-spd {
  display: inline-block;
  min-width: 34px;
  padding: 5px 6px;
  background: var(--bg-th);
  border: 1px solid var(--border);
  border-radius: 3px;
  font-size: 12px;
  cursor: pointer;
  user-select: none;
}
.rec-spd:hover { border-color: var(--blue); }
.rec-spd.act { border-color: var(--blue); color: var(--blue); font-weight: 600; }

#rec-body { overflow-y: auto; padding: 8px 0; font-size: 12px; }
#rec-body table { width: 100%; border-collapse: collapse; }
#rec-body td { padding: 6px 12px; border-bottom: 1px solid var(--border); }
#rec-body tr:hover { background: var(--bg-th); }

/* lista nagrań: miniaturka + czas + rozmiar, filtry dzień/jazda */
#rec-filter {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px 4px;
  flex-wrap: wrap;
}
#rec-filter select {
  height: 28px;
  background: var(--bg);
  color: var(--text);
  border: 1px solid var(--border);
  font-size: 12px;
  padding: 0 6px;
  max-width: 44vw;
}
#rec-fcount { font-size: 11px; color: var(--dim); margin-left: auto; }
#rec-list { padding: 4px 10px 10px; }
.rec-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 5px 6px;
  margin-bottom: 6px;
  background: var(--bg-th);
  border: 1px solid var(--border);
  border-radius: 3px;
  cursor: pointer;
}
.rec-row:hover { border-color: var(--blue); }
.rec-row img, .rec-row .rec-noimg {
  width: 96px;
  height: 54px;
  object-fit: cover;
  background: #000;
  flex-shrink: 0;
  border-radius: 2px;
}
.rec-noimg {
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--border);
  font-size: 9px;
  text-align: center;
}
.rec-row .rec-info { flex: 1; min-width: 0; }
.rec-row .rec-time { font-size: 13px; font-weight: 600; color: var(--text); }
.rec-row .rec-sub { font-size: 11px; color: var(--dim); }
.rec-act {
  display: inline-block; width: 34px; height: 30px; line-height: 30px;
  text-align: center; margin-left: 6px; background: var(--bg-th);
  border: 1px solid var(--border); border-radius: 3px; color: var(--text);
  cursor: pointer; text-decoration: none; font-size: 13px;
}
.rec-act:hover { border-color: var(--blue); color: var(--blue); }

#rec-btn, #log-btn, #wl-btn, #bt-btn, #usb-btn {
  height: 24px; padding: 0 10px; margin-right: 6px;
  background: var(--bg-th); border: 1px solid var(--border);
  color: var(--text); font-size: 12px; cursor: pointer; letter-spacing: 0.3px;
  display: inline-flex; align-items: center; gap: 3px;
}
#rec-btn { margin-right: 12px; }
#rec-btn:hover, #log-btn:hover, #wl-btn:hover, #bt-btn:hover,
#usb-btn:hover { background: var(--border); }
#bt-btn.on { border-color: var(--blue); color: var(--blue); }
#usb-btn.warn { border-color: var(--yellow); color: var(--yellow); }
#usb-btn.err { border-color: var(--red); color: var(--red); }

/* ── RADAR (fotoradar) ────────────────────────────────────── */
#radar-btn { height: 24px; padding: 0 10px; margin-right: 6px; background: var(--bg-th);
  border: 1px solid var(--border); color: var(--text); font-size: 12px; cursor: pointer;
  letter-spacing: 0.3px; display: inline-flex; align-items: center; gap: 3px; }
#radar-btn:hover { background: var(--border); }
#radar-btn.on { border-color: var(--red); color: var(--red); }
#radar-modal { display: none; position: fixed; inset: 0; background: rgba(0,0,0,.75);
  z-index: 950; align-items: center; justify-content: center; }
#radar-panel { width: min(640px, 94vw); max-height: 90vh; background: var(--bg-row);
  border: 1px solid var(--border); border-radius: 10px; display: flex; flex-direction: column; }
#radar-body { padding: 12px 16px; overflow-y: auto; }
.rd-row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 10px; font-size: 13px; }
.rd-row label { color: var(--dim); font-size: 11px; text-transform: uppercase; }
.rd-row input[type=number], .rd-row select { height: 28px; background: var(--bg); color: var(--text);
  border: 1px solid var(--border); font-size: 13px; padding: 0 6px; }
.rd-row input[type=number] { width: 76px; }
.rd-row input[type=date], .rd-row input[type=text] { height: 28px; background: var(--bg); color: var(--text);
  border: 1px solid var(--border); font-size: 13px; padding: 0 6px; }
.rd-row .rd-chk { display: flex; align-items: center; gap: 4px; cursor: pointer; text-transform: none; font-size: 12px; color: var(--text); }
.rd-big { font-family: 'PixelData', monospace; font-size: 44px; font-weight: 700; color: var(--green); }
.rd-big.ev { color: var(--red); }
.rd-ev { display: flex; gap: 10px; align-items: center; padding: 8px; border-bottom: 1px solid var(--border); }
.rd-ev img { width: 150px; height: 84px; object-fit: cover; border: 1px solid var(--border); cursor: pointer; }
.rd-ev .v { font-family: 'PixelData', monospace; font-size: 26px; font-weight: 700; color: var(--red); min-width: 90px; }
.rd-ev .t { font-size: 12px; color: var(--dim); }
.rd-ev .pl { font-family: 'PixelData', monospace; font-size: 18px; color: #fff; }
.rd-ev a { color: var(--blue); font-size: 12px; }

/* ── PENDRIVE / KOPIE ────────────────────────────────────── */
#usb-modal { display: none; position: fixed; inset: 0;
  background: rgba(0,0,0,.75); z-index: 950; align-items: center;
  justify-content: center; }
#usb-panel { width: min(520px, 94vw); max-height: 88vh;
  background: var(--bg-row); border: 1px solid var(--border);
  border-radius: 10px; display: flex; flex-direction: column; }
#usb-body { padding: 14px 16px; overflow-y: auto; }
.usb-stat { display: flex; align-items: center; gap: 10px; padding: 12px;
  border-radius: 8px; margin-bottom: 12px; font-size: 14px; }
.usb-stat.ok { background: rgba(61,154,61,.16); color: #7d7; }
.usb-stat.warn { background: rgba(184,134,11,.18); color: #dc8; }
.usb-stat.err { background: rgba(204,51,51,.18); color: #e88; }
.usb-stat .big { font-size: 22px; }
.usb-info { font-size: 12px; color: var(--dim); margin-bottom: 12px;
  line-height: 1.6; }
.usb-acts { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 14px; }
.usb-acts button { flex: 1; min-width: 130px; height: 42px; border-radius: 8px;
  border: 1px solid var(--border); background: var(--bg-th);
  color: var(--text); font-size: 13px; cursor: pointer; }
.usb-acts button.primary { background: var(--yellow); border-color: var(--yellow);
  color: #201800; font-weight: 600; }
.usb-acts button.go { background: var(--green); border-color: var(--green);
  color: #fff; font-weight: 600; }
#usb-baks { font-size: 11px; }
#usb-baks div { display: flex; justify-content: space-between;
  padding: 5px 2px; border-bottom: 1px solid rgba(255,255,255,.05);
  color: var(--dim); }
#usb-baks b { color: var(--text); font-weight: 400; }

/* ── BLUETOOTH MODAL (styl telefonu) ─────────────────────── */
#bt-modal {
  display: none; position: fixed; inset: 0; background: rgba(0,0,0,.75);
  z-index: 950; align-items: center; justify-content: center;
}
#bt-panel {
  width: min(460px, 94vw); max-height: 88vh; background: var(--bg-row);
  border: 1px solid var(--border); border-radius: 12px; overflow: hidden;
  display: flex; flex-direction: column;
}
#bt-head {
  display: flex; align-items: center; gap: 10px; padding: 14px 16px;
  border-bottom: 1px solid var(--border);
}
#bt-head .t { font-size: 16px; font-weight: 600; color: #fff; flex: 1; }
#bt-head .x { color: var(--dim); font-size: 20px; cursor: pointer;
  background: none; border: none; }
/* przełącznik iOS */
.sw { position: relative; width: 44px; height: 26px; flex-shrink: 0; }
.sw input { display: none; }
.sw .tr { position: absolute; inset: 0; background: #3a3a3a;
  border-radius: 13px; transition: .2s; cursor: pointer; }
.sw .tr::before { content: ''; position: absolute; width: 22px; height: 22px;
  left: 2px; top: 2px; background: #fff; border-radius: 50%; transition: .2s; }
.sw input:checked + .tr { background: var(--blue); }
.sw input:checked + .tr::before { transform: translateX(18px); }
#bt-body { overflow-y: auto; padding: 4px 0 8px; }
.bt-sec { font-size: 11px; color: var(--dim); text-transform: uppercase;
  letter-spacing: .5px; padding: 14px 16px 6px; display: flex;
  align-items: center; gap: 8px; }
.bt-spin { width: 13px; height: 13px; border: 2px solid var(--border);
  border-top-color: var(--blue); border-radius: 50%;
  animation: btspin .8s linear infinite; }
@keyframes btspin { to { transform: rotate(360deg); } }
.bt-row {
  display: flex; align-items: center; gap: 12px; padding: 11px 16px;
  cursor: pointer; border-bottom: 1px solid rgba(255,255,255,.04);
}
.bt-row:hover { background: var(--bg-th); }
.bt-ico { width: 34px; height: 34px; flex-shrink: 0; border-radius: 9px;
  background: var(--bg-th); display: flex; align-items: center;
  justify-content: center; font-size: 17px; }
.bt-row.conn .bt-ico { background: rgba(22,119,255,.18); }
.bt-info { flex: 1; min-width: 0; }
.bt-name { font-size: 14px; color: var(--text); white-space: nowrap;
  overflow: hidden; text-overflow: ellipsis; }
.bt-row.conn .bt-name { color: #fff; font-weight: 600; }
.bt-sub { font-size: 11px; color: var(--dim); display: flex;
  align-items: center; gap: 6px; margin-top: 1px; }
.bt-star { color: var(--yellow); }
.bt-batt { display: inline-flex; align-items: center; gap: 3px; }
.bt-batt b { display: inline-block; width: 20px; height: 10px;
  border: 1px solid var(--dim); border-radius: 2px; position: relative; }
.bt-batt b::after { content: ''; position: absolute; right: -3px; top: 2px;
  width: 2px; height: 4px; background: var(--dim); }
.bt-batt b i { display: block; height: 100%; background: var(--green); }
.bt-act { position: relative; }
.bt-dots { color: var(--dim); font-size: 18px; padding: 4px 8px;
  background: none; border: none; cursor: pointer; }
.bt-menu { display: none; position: absolute; right: 8px; top: 30px;
  background: var(--bg-th); border: 1px solid var(--border);
  border-radius: 8px; z-index: 5; min-width: 150px; overflow: hidden; }
.bt-menu.open { display: block; }
.bt-menu div { padding: 10px 14px; font-size: 13px; cursor: pointer;
  white-space: nowrap; }
.bt-menu div:hover { background: var(--bg-row); }
.bt-empty { padding: 16px; color: var(--dim); font-size: 12px;
  text-align: center; }
#bt-foot { padding: 12px 16px; border-top: 1px solid var(--border); }
#bt-volume-row { display: none; align-items: center; gap: 10px;
  padding: 0 0 12px; color: var(--dim); }
#bt-volume-row.show { display: flex; }
#bt-volume-row svg { flex: 0 0 auto; }
#bt-volume { flex: 1; min-width: 0; accent-color: var(--blue); }
#bt-volume-value { width: 38px; color: var(--text); text-align: right;
  font: 12px Consolas, 'DejaVu Sans Mono', monospace; }
#bt-scan-btn { width: 100%; height: 40px; background: var(--bg-th);
  border: 1px solid var(--blue); color: var(--blue); border-radius: 9px;
  font-size: 13px; cursor: pointer; display: flex; align-items: center;
  justify-content: center; gap: 8px; }
#bt-scan-btn:disabled { opacity: .6; cursor: default; }

/* ── WATCHLIST MODAL ─────────────────────────────────────── */
#wl-modal {
  display: none; position: fixed; inset: 0; background: rgba(0,0,0,.75);
  z-index: 900; align-items: center; justify-content: center;
}
#wl-panel {
  width: min(620px, 96vw); max-height: 88vh; background: var(--bg-row);
  border: 1px solid var(--border); display: flex; flex-direction: column;
}
/* zakładki baz */
#wl-tabs { display: flex; gap: 6px; padding: 8px 12px 0; overflow-x: auto;
  border-bottom: 1px solid var(--border); flex-shrink: 0; }
/* przy tysiącach wpisów lista musi się kurczyć, a nie reszta panelu */
#wl-gset, #wl-add, #wl-bulk, #wl-foot, #rec-hdr { flex-shrink: 0; }
#wl-list { flex: 1 1 auto; min-height: 60px; }
.wl-tab { padding: 7px 12px; background: var(--bg-th); border: 1px solid var(--border);
  border-bottom: none; border-radius: 6px 6px 0 0; font-size: 12px;
  cursor: pointer; white-space: nowrap; color: var(--dim); }
.wl-tab.act { color: #fff; border-color: var(--blue); background: var(--bg-row); }
.wl-tab .c { opacity: .7; font-size: 11px; }
.wl-tab.off { text-decoration: line-through; opacity: .55; }
.wl-tab.new { color: var(--blue); }
/* ustawienia bazy */
#wl-gset { padding: 10px 12px; border-bottom: 1px solid var(--border);
  display: flex; flex-direction: column; gap: 7px; }
.wl-line { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
.wl-line label { font-size: 11px; color: var(--dim); min-width: 82px; }
#wl-gname { width: 150px; }
#wl-galert { flex: 1; min-width: 220px; }
#wl-gset input[type=text] { height: 30px; background: var(--bg);
  color: var(--text); border: 1px solid var(--border); padding: 0 8px;
  font-size: 13px; }
#wl-gcolor { height: 30px; background: var(--bg); color: var(--text);
  border: 1px solid var(--border); font-size: 12px; }
.wl-hint { font-size: 10px; color: var(--dim); }
/* import wsadowy */
#wl-bulk { display: none; padding: 10px 12px; gap: 6px;
  border-bottom: 1px solid var(--border); flex-direction: column; }
#wl-bulk.open { display: flex; }
#wl-bulk textarea { width: 100%; height: 110px; background: var(--bg);
  color: var(--text); border: 1px solid var(--border); padding: 7px;
  font-family: Consolas, monospace; font-size: 12px; resize: vertical; }
#wl-bulk-res { font-size: 12px; }
#wl-add { display: flex; gap: 6px; padding: 10px 12px;
  border-bottom: 1px solid var(--border); }
#wl-add input { height: 30px; background: var(--bg); color: var(--text);
  border: 1px solid var(--border); padding: 0 8px; font-size: 13px; }
#wl-plate { width: 120px; font-family: Consolas, monospace;
  text-transform: uppercase; }
#wl-note { flex: 1; min-width: 0; }
#wl-list { overflow-y: auto; padding: 4px 0; }
.wl-row { display: flex; align-items: center; gap: 10px;
  padding: 7px 14px; border-bottom: 1px solid var(--border); }
.wl-row .p { font-family: Consolas, monospace; font-weight: 700;
  font-size: 15px; min-width: 110px; }
.wl-row .n { flex: 1; color: var(--dim); font-size: 12px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.wl-row .x { color: var(--red); cursor: pointer; font-size: 16px;
  padding: 0 6px; }
#wl-foot { display: flex; align-items: center; gap: 10px;
  padding: 10px 12px; border-top: 1px solid var(--border);
  font-size: 12px; color: var(--dim); flex-wrap: wrap; }

/* ── LOG MODAL ───────────────────────────────────────────── */
#log-modal {
  display: none; position: fixed; inset: 0; background: rgba(0,0,0,.75);
  z-index: 900; align-items: center; justify-content: center;
}
#log-panel {
  width: min(1000px, 96vw); height: 84vh; background: var(--bg-row);
  border: 1px solid var(--border); display: flex; flex-direction: column;
}
#log-pre {
  flex: 1; overflow: auto; margin: 0; padding: 10px 12px;
  font: 11px/1.45 Consolas, 'DejaVu Sans Mono', monospace;
  color: var(--text); white-space: pre-wrap; word-break: break-all;
}

/* ── CAMERA MODAL ────────────────────────────────────────── */
#cam-modal {
  display: none;
  position: fixed;
  inset: 0;
  background: rgba(0,0,0,.65);
  z-index: 200;
  align-items: center;
  justify-content: center;
}
#cam-panel {
  background: #0e1620;
  border: 1px solid var(--border);
  width: min(420px, 94vw);
  max-height: 80vh;
  display: flex;
  flex-direction: column;
}
#cam-hdr {
  display: flex;
  align-items: center;
  justify-content: space-between;
  height: 38px;
  padding: 0 14px;
  background: var(--bg-head);
  border-bottom: 1px solid var(--border);
  font-size: 12px;
  font-weight: 600;
  letter-spacing: .5px;
  color: var(--dim);
  flex-shrink: 0;
}
#cam-hdr button {
  background: none;
  border: none;
  color: var(--dim);
  font-size: 15px;
  cursor: pointer;
  padding: 0;
  line-height: 1;
}
#cam-hdr button:hover { color: var(--text); }
#cam-body {
  overflow-y: auto;
  flex: 1;
  padding: 6px 0;
}
.cam-row {
  display: flex;
  align-items: center;
  padding: 5px 14px;
  gap: 10px;
  border-bottom: 1px solid rgba(30,45,64,.5);
}
.cam-row.inactive { opacity: .4; }
.cam-label {
  font-size: 11px;
  color: var(--dim);
  width: 155px;
  flex-shrink: 0;
  white-space: nowrap;
}
.cam-slider-wrap {
  display: flex;
  align-items: center;
  gap: 7px;
  flex: 1;
}
.cam-slider-wrap input[type=range] {
  flex: 1;
  height: 4px;
  accent-color: var(--blue);
  background: var(--border);
  cursor: pointer;
}
.cam-val {
  font-size: 11px;
  font-family: 'PixelData', 'Courier New', monospace;
  color: var(--text);
  min-width: 36px;
  text-align: right;
}
.cam-row select {
  flex: 1;
  height: 24px;
  background: var(--bg);
  border: 1px solid var(--border);
  color: var(--text);
  font-size: 11px;
  padding: 0 4px;
  cursor: pointer;
}
.cam-toggle { position: relative; display: inline-block; width: 36px; height: 18px; }
.cam-toggle input { opacity: 0; width: 0; height: 0; }
.cam-toggle-track {
  position: absolute; inset: 0;
  background: var(--border);
  border-radius: 9px;
  cursor: pointer;
  transition: background .2s;
}
.cam-toggle input:checked + .cam-toggle-track { background: var(--blue); }
.cam-toggle-track::after {
  content: '';
  position: absolute;
  width: 12px; height: 12px;
  left: 3px; top: 3px;
  background: #fff;
  border-radius: 50%;
  transition: left .2s;
}
.cam-toggle input:checked + .cam-toggle-track::after { left: 21px; }
#cam-ftr {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 14px;
  border-top: 1px solid var(--border);
  background: var(--bg-head);
  flex-shrink: 0;
}
#cam-status {
  flex: 1;
  font-size: 11px;
}
#cam-status.ok  { color: var(--green); }
#cam-status.err { color: var(--red); }
#cam-status.warn { color: var(--yellow); }
.cam-btn {
  height: 26px;
  padding: 0 12px;
  border: none;
  font-size: 11px;
  font-weight: 600;
  cursor: pointer;
  letter-spacing: .3px;
}
.cam-btn-secondary {
  background: var(--bg);
  border: 1px solid var(--border);
  color: var(--dim);
}
.cam-btn-secondary:hover { color: var(--text); }
.cam-btn-primary {
  background: var(--blue);
  color: #fff;
}
.cam-btn-primary:hover { background: #1560d4; }
#cam-modal-open {
  height: 26px;
  padding: 0 12px;
  background: var(--bg);
  border: 1px solid var(--border);
  color: var(--dim);
  font-size: 11px;
  font-weight: 600;
  cursor: pointer;
  letter-spacing: .3px;
  margin-left: 8px;
}
#cam-modal-open:hover { color: var(--text); }

/* ── LIGHTBOX ────────────────────────────────────────────── */
/* ── przyciski weryfikacji odczytów ── */
.td-verify {
  text-align: right;
  white-space: nowrap;
}

.vbtn {
  width: calc(30px * var(--tscale, 1));
  height: calc(30px * var(--tscale, 1));
  margin-left: 3px;
  background: var(--bg-th);
  color: var(--dim);
  border: 1px solid var(--border);
  border-radius: 3px;
  cursor: pointer;
  font-size: calc(14px * var(--tscale, 1));
  line-height: 1;
}

/* delikatne kolory w stanie spoczynku — intuicyjnie: zielony/żółty/czerwony */
.vbtn.v-ok  { color: #5cb85c; border-color: rgba(92, 184, 92, 0.4); }
.vbtn.v-fix { color: #d4a017; border-color: rgba(212, 160, 23, 0.4); }
.vbtn.v-part { color: #4d94d4; border-color: rgba(77, 148, 212, 0.4); }
.vbtn.v-unr { color: #9aa0a6; border-color: rgba(154, 160, 166, 0.4); }
.vbtn.v-not { color: #e05252; border-color: rgba(224, 82, 82, 0.4); }
.vbtn:hover { filter: brightness(1.4); border-color: var(--dim); }
.vbtn.v-ok.act   { background: var(--green);  color: #fff; border-color: var(--green); }
.vbtn.v-fix.act  { background: var(--yellow); color: #fff; border-color: var(--yellow); }
.vbtn.v-part.act { background: var(--blue);   color: #fff; border-color: var(--blue); }
.vbtn.v-unr.act  { background: #5f6368;       color: #fff; border-color: #5f6368; }
.vbtn.v-not.act  { background: var(--red);    color: #fff; border-color: var(--red); }

.v-text {
  font-family: 'PixelData', 'Courier New', monospace;
  font-size: 12px;
  font-weight: 700;
  color: var(--green);
  margin-top: 3px;
}

#lightbox {
  display: none;
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.88);
  z-index: 1000;
  align-items: center;
  justify-content: center;
}

#lightbox.open {
  display: flex;
}

#lightbox-wrap {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 12px;
  max-width: 92vw;
}

#lightbox-imgwrap {
  position: relative;
  line-height: 0;
}

#lightbox-img {
  max-width: 92vw;
  max-height: 50vh;
  object-fit: contain;
  border: 1px solid var(--border);
}

#lightbox-box {
  position: absolute;
  border: 2px solid var(--green);
  box-shadow: 0 0 8px rgba(61, 154, 61, 0.9);
  display: none;
  pointer-events: none;
}

#lightbox-crop {
  max-width: 88vw;
  max-height: 34vh;
  object-fit: contain;
  border: 1px solid var(--border);
}

#lightbox-close {
  position: absolute;
  top: 16px;
  right: 20px;
  color: var(--dim);
  font-size: 22px;
  cursor: pointer;
  font-family: monospace;
  line-height: 1;
}

#lightbox-close:hover {
  color: var(--text);
}

#lightbox-plate {
  position: absolute;
  bottom: 24px;
  left: 50%;
  transform: translateX(-50%);
  font-family: 'PixelData', Consolas, 'DejaVu Sans Mono', monospace;
  font-size: 22px;
  font-weight: 700;
  color: #fff;
  letter-spacing: 2px;
  background: rgba(0,0,0,0.6);
  padding: 4px 18px;
}

/* ── SCROLLBAR global ────────────────────────────────────── */
* {
  scrollbar-width: thin;
  scrollbar-color: var(--border) var(--bg);
}

/* ── RESPONSIVE ──────────────────────────────────────────── */
@media (max-width: 820px) {
  html, body { overflow-y: auto; overflow-x: hidden; }

  /* telefon: tylko kluczowe kolumny (CZAS, TABLICA, MINIATURA, OCENA) —
     to reszta kolumn wypychała stronę w bok */
  table#events-table td:nth-child(1), table#events-table th:nth-child(1),
  table#events-table td:nth-child(4), table#events-table th:nth-child(4),
  table#events-table td:nth-child(5), table#events-table th:nth-child(5),
  table#events-table td:nth-child(6), table#events-table th:nth-child(6) {
    display: none;
  }
  /* przy fixed ukryte komorki przesuwaja pozostale do zlych slotow <col> —
     na telefonie tabela uklada sie wg tresci */
  table#events-table { table-layout: auto; }
  table#events-table col.c-id,
  table#events-table col.c-det,
  table#events-table col.c-ocr,
  table#events-table col.c-dur { width: 0 !important; }
  table#events-table col.c-verify { width: auto !important; }
  #header .brand-sub { display: none; }
  #header .stat { font-size: 10px; }
  #header .sep { margin: 0 6px; }
  #header .brand { margin-right: 8px; }
  #rec-btn { padding: 0 6px; font-size: 10px; margin-right: 6px; }

  #main {
    flex-direction: column;
    height: auto;
    overflow: visible;
  }

  #stream-panel {
    width: 100%;
    min-width: 0;
    border-right: none;
    border-bottom: 1px solid var(--border);
    height: 240px;
  }

  #events-panel {
    height: 400px;
    flex-shrink: 0;
  }

  #footer {
    flex-wrap: wrap;
    height: auto;
    padding: 8px 12px;
    gap: 6px;
  }

  #footer .sep { display: none; }

  table#events-table col.c-img { width: 72px; }
  .td-img img { width: 66px; height: 22px; }

  /* telefon: bez suwaka podziału, edytor w kompaktowym układzie */
  #splitter { display: none; }
  #events-panel { --tscale: 1 !important; }
  #rv-title { display: none; }
  #rv-btns { grid-template-columns: repeat(2, 1fr); }
  .rvb { min-height: 52px; font-size: 14px; }
  #rv-plate #rv-plate-num { font-size: 24px; letter-spacing: 3px; }
  #rv-full { max-height: 24vh; }
  .rv-col img.s1 { height: 10vh; }
  .rv-col img.s2 { height: 6.5vh; }
  .rv-col img.s3 { display: none; }
  #rv-bonus { display: none; }
  .td-verify { white-space: normal; }
  .vbtn { margin: 2px; }
}

@media (max-width: 480px) {
  #stream-panel { height: 200px; }
  #events-panel { height: 420px; }
  /* najwezsze ekrany: zeby naglowek nie wypychal strony w bok */
  #header { padding: 0 6px; }
  #header .sep, #header #stat-disk, #header .clock { display: none; }
  #header .stat { margin-right: 8px; }
  /* OCENA znika z tabeli (5 przyciskow w slupku = wiersz 170px wysoki);
     na telefonie do weryfikacji sluzy EDYTOR */
  table#events-table td:nth-child(8),
  table#events-table th:nth-child(8) { display: none; }
  /* miniatura: wąska kolumna przy prawej, luz idzie do TABLICY */
  table#events-table .td-img { width: 78px; text-align: center; }
  table#events-table .td-plate { width: 99%; }
}

@media (max-width: 480px) {
  /* na pasku podgladu musi zmiescic sie LIVE + FOTO + OSTROSC + KAMERA + STREFA */
  #cam01 { display: none; }
  #snap-btn .snap-txt { display: none; }
  .hdr-txt { display: none; }   /* BAZA/LOGI/NAGRANIA jako same ikony */
}

@media (max-width: 400px) {
  /* najciasniejsze telefony: napis LPR/ANPR i tak jest w ikonie LP */
  #header .brand-name { display: none; }
  #header .brand { margin-right: 4px; }
  /* pasek edytora: wszystko musi sie zmiescic z krzyzykiem zamkniecia */
  #rv-head { padding: 0 6px; }
  #rv-count { min-width: 0; font-size: 12px; }
  #rv-head #rv-boxfix { margin-right: 6px; }
  #rv-cands-toggle { padding: 2px 5px; font-size: 9px; }
}
</style>
</head>
<body>

<!-- HEADER -->
<div id="header">
  <div class="brand">
    <div class="brand-icon">LP</div>
    <div>
      <div class="brand-name">LPR / ANPR</div>
    </div>
  </div>
  <div class="brand-sub">SYSTEM ROZPOZNAWANIA TABLIC REJESTRACYJNYCH</div>
  <div class="sep"></div>
  <div class="stat"><span id="h-today">—</span> dzisiaj</div>
  <div class="sep"></div>
  <div class="stat"><span id="h-total">—</span> lacznie</div>
  <div class="sep"></div>
  <div class="stat" id="stat-disk"><span id="h-disk">—</span> wolne</div>
  <div class="spacer"></div>
  <button id="radar-btn" onclick="openRadarModal()" title="Tryb fotoradaru (LD2415H)">&#9673;<span class="hdr-txt"> RADAR</span></button>
  <button id="usb-btn" onclick="openUsbModal()" title="Pendrive i kopie zapasowe"><span data-ico="usb"></span><span class="hdr-txt"> PENDRIVE</span></button>
  <button id="bt-btn" onclick="openBtModal()" title="Bluetooth / g&#322;o&#347;nik"><span data-ico="bt"></span><span class="hdr-txt"> BLUETOOTH</span></button>
  <button id="wl-btn" onclick="openWlModal()"><span data-ico="star"></span><span class="hdr-txt"> BAZA</span></button>
  <button id="log-btn" onclick="openLogModal()"><span data-ico="list"></span><span class="hdr-txt"> LOGI</span></button>
  <button id="rec-btn" onclick="openRecModal()"><span data-ico="play"></span><span class="hdr-txt"> NAGRANIA</span></button>
  <div id="h-status" class="status-offline">&#x25CF; OFFLINE</div>
  <div class="clock" id="h-clock">--:--:--</div>
</div>

<!-- MAIN -->
<div id="main">

  <!-- STREAM PANEL -->
  <div id="stream-panel">
    <div id="stream-label">
      <div class="rec-dot"></div>
      LIVE<span id="cam01"> &#8212; KAMERA 01</span>
      <div id="zone-save-row">
        <button id="zone-confirm-btn" onclick="zoneConfirm()">ZAPISZ</button>
        <button id="zone-cancel-btn"  onclick="zoneCancel()">ANULUJ</button>
      </div>
      <button id="snap-btn" onclick="manualSnap()" title="R&#281;czne zdj&#281;cie (3 klatki)"><span data-ico="cam"></span><span class="snap-txt"> FOTO</span></button>
      <button id="focus-btn" onclick="openFocusModal()">OSTRO&#346;&#262;</button>
      <button id="cam-btn" onclick="openCameraModal()">KAMERA</button>
      <button id="zone-btn" onclick="zoneToggle()">STREFA</button>
    </div>
    <div id="stream-wrap">
      <img id="stream-img" src="" alt="" onerror="this.style.display='none'; document.getElementById('no-signal').style.display='block'">
      <div id="no-signal" class="no-signal" style="display:none">BRAK SYGNALU<br><span style="font-size:10px;color:var(--border)">sprawdz polaczenie</span></div>
      <svg id="zone-svg">
        <polygon id="zone-poly" points=""/>
        <circle id="zone-h0" class="zone-handle" cx="0" cy="0"/>
        <circle id="zone-h1" class="zone-handle" cx="0" cy="0"/>
        <circle id="zone-h2" class="zone-handle" cx="0" cy="0"/>
        <circle id="zone-h3" class="zone-handle" cx="0" cy="0"/>
      </svg>
      <div id="stream-stats">
        <div class="ss-item"><span class="ss-lbl">FPS</span><span class="ss-val" id="ss-fps">--</span></div>
        <div class="ss-sep"></div>
        <div class="ss-item"><span class="ss-lbl">CPU</span><span class="ss-val" id="ss-cpu">--</span></div>
        <div class="ss-sep"></div>
        <div class="ss-item"><span class="ss-lbl">PROC</span><span class="ss-val" id="ss-cput">--</span></div>
        <div class="ss-sep"></div>
        <div class="ss-item"><span class="ss-lbl">HAILO</span><span class="ss-val" id="ss-hailo">--</span></div>
        <div class="ss-sep"></div>
        <div class="ss-item" id="ss-zas-item" title="vcgencmd get_throttled"><span class="ss-lbl">ZASIL.</span><span class="ss-val" id="ss-zas">--</span></div>
      </div>
    </div>
  </div>

  <!-- SPLITTER: przeciagnij, aby zmienic podzial podglad/tabela -->
  <div id="splitter" title="Przeciagnij, aby zmienic podzial ekranu"></div>

  <!-- EVENTS PANEL -->
  <div id="events-panel">
    <div id="events-toolbar">
      <label>SZUKAJ:</label>
      <input type="text" id="search-input" placeholder="np. WA1234A" autocomplete="off" spellcheck="false">
      <button id="refresh-btn" onclick="fetchPlates(true)"><span data-ico="ref"></span> ODSWIEZ</button>
      <button id="reviewer-btn" onclick="reviewerOpen()"><span data-ico="list"></span> EDYTOR</button>
    </div>
    <div id="table-wrap">
      <table id="events-table">
        <colgroup>
          <col class="c-id" style="width:44px">
          <col class="c-time">
          <col class="c-plate">
          <col class="c-det">
          <col class="c-ocr">
          <col class="c-dur">
          <col class="c-img">
          <col class="c-verify" style="width:176px">
        </colgroup>
        <thead>
          <tr>
            <th>#</th>
            <th>CZAS</th>
            <th>TABLICA</th>
            <th>DET%</th>
            <th>OCR%</th>
            <th>CZAS</th>
            <th style="text-align:right">MINIATURA</th>
            <th style="text-align:right">OCENA</th>
          </tr>
        </thead>
        <tbody id="events-tbody">
          <tr><td colspan="8" style="text-align:center;color:var(--dim);padding:20px;font-size:11px">Ladowanie danych...</td></tr>
        </tbody>
      </table>
    </div>
    <div id="tbl-pager"></div>
  </div>

</div><!-- /main -->

<!-- FOOTER / CONFIG -->
<div id="footer">
  <span class="cfg-label">PEWNOSC DET:</span>
  <div class="cfg-group">
    <input type="range" id="cfg-det" min="0" max="1" step="0.01" value="0.60" oninput="document.getElementById('val-det').textContent=parseFloat(this.value).toFixed(2)">
    <span class="cfg-val" id="val-det">0.60</span>
  </div>
  <div class="sep"></div>
  <span class="cfg-label">PEWNOSC OCR:</span>
  <div class="cfg-group">
    <input type="range" id="cfg-ocr" min="0" max="1" step="0.01" value="0.55" oninput="document.getElementById('val-ocr').textContent=parseFloat(this.value).toFixed(2)">
    <span class="cfg-val" id="val-ocr">0.55</span>
  </div>
  <div class="sep"></div>
  <span class="cfg-label">MIN ODCZYTOW:</span>
  <div class="cfg-group">
    <input type="range" id="cfg-reads" min="1" max="20" step="1" value="5" oninput="document.getElementById('val-reads').textContent=this.value">
    <span class="cfg-val" id="val-reads">5</span>
  </div>
  <div class="sep"></div>
  <button id="save-btn" onclick="saveConfig()">ZAPISZ</button>
  <span id="save-status"></span>
  <button id="cam-modal-open" onclick="openCameraModal()">&#9654; KAMERA</button>
</div>

<!-- LIGHTBOX -->
<!-- EDYTOR WERYFIKACJI -->
<div id="reviewer">
  <div id="rv-head">
    <span id="rv-count">W kolejce: 0</span>
    <span id="rv-title">EDYTOR WERYFIKACJI — nowe odczyty od uruchomienia</span>
    <button id="rv-boxfix" onclick="bboxStart('rv')">&#x25A2; POPRAW RAMK&#0280;</button>
    <button id="rv-cands-toggle" onclick="rvToggleCands()">KANDYDACI: WL</button>
    <span id="rv-close" onclick="reviewerClose()">&#x2715;</span>
  </div>
  <div id="rv-body">
    <div id="rv-imgwrap">
      <img id="rv-full" src="" alt="">
      <div id="rv-box"></div>
    </div>
    <!-- Podkrecanie obrazu: czasem tablica jest „prawie" czytelna i wystarczy
         podbic jasnosc albo kontrast, zeby odczytac numer. Dziala tylko na
         PODGLAD — plik na dysku zostaje nietkniety. -->
    <div id="rv-tune">
      <label class="tn-sl">EV
        <input type="range" id="tn-ev" min="-4" max="4" step="0.25" value="0"
               oninput="rvTuneSuwak()">
        <span id="tn-ev-v">0</span>
      </label>
      <label class="tn-sl">KONTRAST
        <input type="range" id="tn-c" min="0" max="3" step="0.1" value="0"
               oninput="rvTuneSuwak()">
        <span id="tn-c-v">0</span>
      </label>
      <label class="tn-sl" title="Sciaga same swiatla — ratuje przeswietlone tablice">PRZESWIETLENIA
        <input type="range" id="tn-h" min="0" max="1" step="0.05" value="0"
               oninput="rvTuneSuwak()">
        <span id="tn-h-v">0</span>
      </label>
      <button class="tn-btn tn-reset" onclick="rvTune('reset')" title="Powrot do oryginalu">RESET</button>
    </div>
    <!-- Redukcja przeswietlen: krzywa tonalna sciagajaca GORNY zakres jasnosci.
         Filtry CSS tego nie potrafia (brightness/contrast ruszaja calosc), wiec
         uzywamy filtra SVG z tablica przejscia. Wartosci podmienia rvTuneSuwak. -->
    <svg width="0" height="0" style="position:absolute" aria-hidden="true">
      <filter id="fltSwiatla">
        <feComponentTransfer>
          <feFuncR id="fnR" type="table" tableValues="0 0.25 0.5 0.75 1"/>
          <feFuncG id="fnG" type="table" tableValues="0 0.25 0.5 0.75 1"/>
          <feFuncB id="fnB" type="table" tableValues="0 0.25 0.5 0.75 1"/>
        </feComponentTransfer>
      </filter>
    </svg>
    <div id="rv-meta">
      <div id="rv-plate"><span class="pl-strip">PL</span><span id="rv-plate-num">&mdash;</span></div>
      <div id="rv-sid"></div>
    </div>
    <div id="rv-cands">
      <div class="rv-col" id="rv-col-prev"><div class="rv-col-cap">&#x2190; klatka wcze&#347;niej</div></div>
      <div class="rv-col rv-col-best" id="rv-col-best"><div class="rv-col-cap">najlepsza (zapisana)</div></div>
      <div class="rv-col" id="rv-col-next"><div class="rv-col-cap">klatka p&#243;&#378;niej &#x2192;</div></div>
    </div>
    <div id="rv-bonus"></div>
  </div>
  <div id="rv-wait">
    Wszystko ocenione &#x2713;
    <span class="rv-sub">czekam na nowe odczyty... (podgl&#261;d na &#380;ywo poni&#380;ej)</span>
    <img id="rv-stream" src="" alt="">
  </div>
  <div id="rv-btns">
    <button class="rvb rv-prev" onclick="rvPrev()" style="background:var(--bg-head);border:1px solid var(--border)">&#x25C0; POPRZEDNIE</button>
    <button class="rvb rv-ok"   onclick="rvVerdict('ok')">&#x2713; DOBRZE</button>
    <button class="rvb rv-fix"  onclick="rvVerdict('fix')">&#x270E; POPRAW</button>
    <button class="rvb rv-part" onclick="rvVerdict('partial')">&#xBD; NIEPELNA</button>
    <button class="rvb rv-unr"  onclick="rvVerdict('unreadable')">? NIECZYTELNA</button>
    <button class="rvb rv-not"  onclick="rvVerdict('notplate')">&#x2298; NIE TABLICA</button>
  </div>
</div>

<div id="lightbox" onclick="closeLightbox(event)">
  <span id="lightbox-close" onclick="closeLightbox(null, true)">&#x2715;</span>
  <button id="lb-boxfix" onclick="event.stopPropagation(); bboxStart('lb')">&#x25A2; POPRAW RAMK&#280;</button>
  <div id="lightbox-wrap">
    <div id="lightbox-imgwrap">
      <img id="lightbox-img" src="" alt="">
      <div id="lightbox-box"></div>
    </div>
    <img id="lightbox-crop" src="" alt="">
  </div>
  <div id="lightbox-plate"></div>
</div>

<!-- FOCUS MODAL -->
<div id="focus-modal" onclick="if(event.target===this)closeFocusModal()">
  <div id="focus-panel">
    <div id="rec-hdr">
      <span>ASYSTENT OSTRO&#346;CI &mdash; czerwona zebra = ostre kraw&#281;dzie, maksymalizuj wynik</span>
      <button onclick="closeFocusModal()">&#x2715;</button>
    </div>
    <img id="focus-img" src="" alt="">
  </div>
</div>

<!-- RECORDINGS MODAL -->
<div id="rec-modal" onclick="if(event.target===this)closeRecModal()">
  <div id="rec-panel">
    <div id="rec-hdr">
      <span>NAGRANIA Z JAZDY (dashcam)</span>
      <button onclick="closeRecModal()">&#x2715;</button>
    </div>
    <div id="rec-player">
      <div id="rec-pstat"></div>
      <video id="rec-video" controls playsinline preload="metadata"></video>
      <div id="rec-ctl">
        <span class="rec-spd" data-x="0.25" onclick="setRecSpeed(0.25)">&#188;&#215;</span>
        <span class="rec-spd" data-x="0.5"  onclick="setRecSpeed(0.5)">&#189;&#215;</span>
        <span class="rec-spd act" data-x="1" onclick="setRecSpeed(1)">1&#215;</span>
        <span class="rec-spd" data-x="2"    onclick="setRecSpeed(2)">2&#215;</span>
        <span class="rec-spd" data-x="4"    onclick="setRecSpeed(4)">4&#215;</span>
        <button class="cam-btn cam-btn-secondary" onclick="stopRecPlay()">&#x25A0; ZAMKNIJ</button>
      </div>
    </div>
    <div id="rec-body">Ladowanie...</div>
  </div>
</div>

<!-- PENDRIVE / KOPIE MODAL -->
<div id="usb-modal" onclick="if(event.target===this)closeUsbModal()">
  <div id="usb-panel">
    <div id="rec-hdr">
      <span>&#128190; PENDRIVE I KOPIE ZAPASOWE</span>
      <button onclick="closeUsbModal()" style="background:none;border:none;color:var(--dim);font-size:18px;cursor:pointer">&#10005;</button>
    </div>
    <div id="usb-body">Ladowanie...</div>
  </div>
</div>

<!-- RADAR MODAL -->
<div id="radar-modal" onclick="if(event.target===this)closeRadarModal()">
  <div id="radar-panel">
    <div id="rec-hdr">
      <span>&#9673; RADAR &mdash; FOTORADAR (LD2415H)</span>
      <button onclick="closeRadarModal()" style="background:none;border:none;color:var(--dim);font-size:18px;cursor:pointer">&#10005;</button>
    </div>
    <div id="radar-body">Ladowanie...</div>
    <div id="radar-modul"></div>
  </div>
</div>

<!-- BLUETOOTH MODAL -->
<div id="bt-modal" onclick="if(event.target===this)closeBtModal()">
  <div id="bt-panel">
    <div id="bt-head">
      <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="var(--blue)" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"><path d="M7 7.5 17 16.5 12 21V3l5 4.5L7 16.5"/></svg>
      <span class="t">Bluetooth</span>
      <label class="sw"><input type="checkbox" id="bt-power" onchange="btPower(this.checked)"><span class="tr"></span></label>
      <button class="x" onclick="closeBtModal()">&#10005;</button>
    </div>
    <div id="bt-body"><div class="bt-empty">Ladowanie...</div></div>
    <div id="bt-foot">
      <div id="bt-volume-row">
        <svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 5 6 9H2v6h4l5 4z"/><path d="M15 9a4 4 0 0 1 0 6M18 6a8 8 0 0 1 0 12"/></svg>
        <input id="bt-volume" type="range" min="0" max="100" step="1"
               oninput="btVolumePreview(this.value)"
               onchange="btVolumeCommit(this.value)">
        <span id="bt-volume-value">--%</span>
      </div>
      <button id="bt-scan-btn" onclick="btScan()">
        <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M21 21l-4.3-4.3M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16z"/></svg>
        Szukaj urz&#261;dze&#324;
      </button>
    </div>
  </div>
</div>

<!-- WATCHLIST MODAL -->
<div id="wl-modal" onclick="if(event.target===this)closeWlModal()">
  <div id="wl-panel">
    <div id="rec-hdr">
      <span>&#9733; BAZY OBSERWOWANYCH</span>
      <button onclick="closeWlModal()" style="background:none;border:none;color:var(--dim);font-size:18px;cursor:pointer">&#10005;</button>
    </div>
    <div id="wl-tabs"></div>
    <div id="wl-gset">
      <div class="wl-line">
        <label>Nazwa bazy</label>
        <input type="text" id="wl-gname" maxlength="40" placeholder="np. Rodzina">
        <select id="wl-gcolor" title="kolor banera na ekranie">
          <option value="red">czerwony</option>
          <option value="orange">pomara&#324;czowy</option>
          <option value="blue">niebieski</option>
          <option value="green">zielony</option>
        </select>
        <label style="min-width:auto;display:flex;align-items:center;gap:5px;cursor:pointer">
          <input type="checkbox" id="wl-genabled" checked> aktywna
        </label>
        <label style="min-width:auto;display:flex;align-items:center;gap:5px;cursor:pointer"
               title="czytaj tez notatke (marke auta / miasto) po numerze">
          <input type="checkbox" id="wl-gnota" checked> czytaj notatk&#281;
        </label>
      </div>
      <div class="wl-line">
        <label>Komunikat</label>
        <input type="text" id="wl-galert" maxlength="200"
               placeholder="Uwaga! Tablica {tablica} z listy obserwowanych.">
      </div>
      <div class="wl-line">
        <span class="wl-hint">&#123;tablica&#125; = numer czytany litera po literze &middot; malinka powie to na g&#322;os</span>
        <span style="flex:1"></span>
        <button class="cam-btn cam-btn-secondary" id="wl-play" onclick="wlAlertTest()" title="odtworzy na TYM komputerze">&#9654; POS&#321;UCHAJ</button>
        <button class="cam-btn cam-btn-secondary" onclick="wlAlertTestCar()" title="odtworzy na g&#322;o&#347;niku w aucie">&#128266; W AUCIE</button>
        <button class="cam-btn cam-btn-primary" onclick="wlGroupSave()">ZAPISZ BAZ&#280;</button>
        <button class="cam-btn cam-btn-secondary" onclick="wlGroupRemove()" style="color:var(--red)">USU&#323;</button>
      </div>
    </div>
    <div id="wl-add">
      <input id="wl-plate" placeholder="WA12345" maxlength="10">
      <input id="wl-note" placeholder="notatka (opcjonalna)" maxlength="60">
      <button class="cam-btn cam-btn-primary" onclick="wlAdd()">DODAJ</button>
      <button class="cam-btn cam-btn-secondary" onclick="wlBulkToggle()">&#8595; HURTEM</button>
    </div>
    <div id="wl-add" style="border-top:none;padding-top:0">
      <input id="wl-search" placeholder="&#128269; szukaj w tej bazie (tablica lub notatka)"
             style="flex:1;min-width:0" oninput="wlReloadList()">
      <span id="wl-count" style="font-size:11px;color:var(--dim);white-space:nowrap"></span>
    </div>
    <div id="wl-bulk">
      <textarea id="wl-bulk-text" placeholder="Wklej list&#281; — jedna tablica na lini&#281;:&#10;WA12345&#10;WB1234A;auto szefa&#10;KR9876X, do sprawdzenia"></textarea>
      <div class="wl-line">
        <span class="wl-hint">separator notatki: &#160;;&#160; lub &#160;,&#160; lub spacja &middot; duplikaty pomijane</span>
        <span style="flex:1"></span>
        <span id="wl-bulk-res"></span>
        <button class="cam-btn cam-btn-primary" onclick="wlBulkImport()">IMPORTUJ DO TEJ BAZY</button>
      </div>
    </div>
    <div id="wl-list">Ladowanie...</div>
    <div id="wl-foot">
      <label style="display:flex;align-items:center;gap:6px;cursor:pointer">
        <input type="checkbox" id="wl-ttsall" onchange="wlTtsAll(this.checked)">
        czytaj na g&#322;os ka&#380;d&#261; rozpoznan&#261; tablic&#281;
      </label>
      <label style="min-width:auto">literowanie</label>
      <select id="wl-spell" onchange="wlSpellMode(this.value)"
              style="height:28px;background:var(--bg);color:var(--text);border:1px solid var(--border);font-size:12px">
        <option value="letters">litery (wu, a&#8230;)</option>
        <option value="phonetic">fonetycznie (Wanda, Adam&#8230;)</option>
      </select>
      <label style="min-width:auto">g&#322;o&#347;no&#347;&#263;</label>
      <select id="wl-gain" onchange="wlGain(this.value)"
              style="height:28px;background:var(--bg);color:var(--text);border:1px solid var(--border);font-size:12px">
        <option value="1">normalna</option>
        <option value="1.6">g&#322;o&#347;niej</option>
        <option value="2.4">maks.</option>
      </select>
      <label style="display:flex;align-items:center;gap:6px;cursor:pointer;flex-basis:100%">
        <input type="checkbox" id="wl-ttsbrand" onchange="wlTtsBrand(this.checked)">
        doczytuj mark&#281; pojazdu (je&#347;li baza j&#261; zna)
      </label>
      <span id="wl-spell-ex" class="wl-hint" style="flex-basis:100%"></span>
      <span id="wl-brand-ex" class="wl-hint" style="flex-basis:100%"></span>
      <button class="cam-btn cam-btn-secondary" id="wl-ttsp" onclick="wlTtsTest()">&#9654; TEST G&#321;OSU (tu)</button>
      <button class="cam-btn cam-btn-secondary" onclick="wlTtsTestCar()">&#128266; w aucie</button>
    </div>
  </div>
</div>

<!-- LOG MODAL -->
<div id="log-modal" onclick="if(event.target===this)closeLogModal()">
  <div id="log-panel">
    <div id="rec-hdr">
      <span>LOG US&#321;UGI (ostatnie linie)</span>
      <span>
        <button class="cam-btn cam-btn-secondary" onclick="openLogModal()">&#8635; OD&#346;WIE&#379;</button>
        <button onclick="closeLogModal()" style="background:none;border:none;color:var(--dim);font-size:18px;cursor:pointer">&#10005;</button>
      </span>
    </div>
    <pre id="log-pre">Ladowanie...</pre>
  </div>
</div>

<!-- CAMERA MODAL -->
<div id="cam-modal" onclick="if(event.target===this)closeCameraModal()">
  <div id="cam-panel">
    <div id="cam-hdr">
      <span>USTAWIENIA KAMERY</span>
      <button onclick="closeCameraModal()">&#x2715;</button>
    </div>
    <div id="cam-body"></div>
    <div id="cam-ftr">
      <span id="cam-status"></span>
      <button class="cam-btn cam-btn-secondary" onclick="resetCameraAuto()">TRYB AUTO</button>
      <button class="cam-btn cam-btn-secondary" onclick="openCameraModal()">&#8635; ODSWIEZ</button>
      <button class="cam-btn cam-btn-primary" onclick="saveCameraSettings()">ZAPISZ</button>
    </div>
  </div>
</div>

<script>
(function() {
  'use strict';

  /* ── STATE ── */
  var knownPlates = {};
  var allRows = [];
  var searchTerm = '';
  var isIOS = /iPad|iPhone|iPod/.test(navigator.userAgent) && !window.MSStream;
  var snapshotInterval = null;
  var pollInterval = null;
  var statsInterval = null;

  /* ── CLOCK ── */
  function updateClock() {
    var now = new Date();
    var h = String(now.getHours()).padStart(2, '0');
    var m = String(now.getMinutes()).padStart(2, '0');
    var s = String(now.getSeconds()).padStart(2, '0');
    document.getElementById('h-clock').textContent = h + ':' + m + ':' + s;
  }
  setInterval(updateClock, 1000);
  updateClock();

  /* ── TIME AGO ── */
  function timeAgo(isoStr) {
    if (!isoStr) return '—';
    var then = new Date(isoStr);
    var diff = Math.floor((Date.now() - then.getTime()) / 1000);
    if (isNaN(diff) || diff < 0) return formatTime(then);
    if (diff < 60) return diff + ' s temu';
    if (diff < 3600) return Math.floor(diff / 60) + ' min temu';
    if (diff < 86400) return Math.floor(diff / 3600) + ' godz temu';
    return Math.floor(diff / 86400) + ' dni temu';
  }

  function formatTime(d) {
    if (!d || isNaN(d)) return '—';
    return String(d.getHours()).padStart(2, '0') + ':' +
           String(d.getMinutes()).padStart(2, '0') + ':' +
           String(d.getSeconds()).padStart(2, '0');
  }

  function formatTimeShort(isoStr) {
    if (!isoStr) return '—';
    var d = new Date(isoStr);
    if (isNaN(d)) return '—';
    return String(d.getHours()).padStart(2, '0') + ':' +
           String(d.getMinutes()).padStart(2, '0');
  }

  /* ── CONF COLOR ── */
  function confClass(val) {
    if (val === null || val === undefined) return 'conf-red';
    var v = parseFloat(val);
    if (v >= 0.80) return 'conf-green';
    if (v >= 0.60) return 'conf-yellow';
    return 'conf-red';
  }

  function confText(val) {
    if (val === null || val === undefined) return '—';
    return Math.round(parseFloat(val) * 100) + '%';
  }

  /* ── BUILD ROW ── */
  function buildRow(item, isNew) {
    var plate = (item.plate || 'UNKNOWN').trim().toUpperCase();
    var isUnknown = plate === 'UNKNOWN' || plate === '' || plate === '?';

    var tr = document.createElement('tr');
    if (isUnknown) tr.classList.add('unknown-row');
    if (isNew) tr.classList.add('new-row');

    tr.dataset.plate = plate;
    tr.dataset.seen = item.seen_at || '';

    /* klik w wiersz = podglad: cala klatka + zblizenie */
    tr.style.cursor = 'pointer';
    tr.onclick = function() { openLightbox(item); };

    /* nr porzadkowy (id z bazy — staly, mozna sie nim poslugiwac) */
    var tdId = document.createElement('td');
    tdId.className = 'td-id';
    tdId.style.color = 'var(--dim)';
    tdId.textContent = item.id !== undefined ? item.id : '—';
    tr.appendChild(tdId);

    /* time */
    var tdTime = document.createElement('td');
    tdTime.className = 'td-time';
    tdTime.title = timeAgo(item.seen_at);
    tdTime.textContent = formatTimeShort(item.seen_at);
    tr.appendChild(tdTime);

    /* plate */
    var tdPlate = document.createElement('td');
    tdPlate.className = 'td-plate' + (isUnknown ? ' unknown' : '');
    tdPlate.textContent = plate;
    tr.appendChild(tdPlate);

    /* det */
    var tdDet = document.createElement('td');
    tdDet.className = confClass(item.det_conf);
    tdDet.textContent = confText(item.det_conf);
    tr.appendChild(tdDet);

    /* ocr */
    var tdOcr = document.createElement('td');
    tdOcr.className = confClass(item.ocr_conf);
    tdOcr.textContent = confText(item.ocr_conf);
    tr.appendChild(tdOcr);

    /* duration */
    var tdDur = document.createElement('td');
    tdDur.className = 'td-dur';
    if (item.duration !== null && item.duration !== undefined) {
      tdDur.textContent = parseFloat(item.duration).toFixed(1) + 's';
    } else {
      tdDur.textContent = '—';
      tdDur.style.color = 'var(--border)';
    }
    tr.appendChild(tdDur);

    /* image */
    var tdImg = document.createElement('td');
    tdImg.className = 'td-img';
    if (item.crop_url) {
      var img = document.createElement('img');
      img.src = item.crop_url;
      img.alt = plate;
      img.title = plate;
      img.loading = 'lazy';
      img.onerror = function() {
        this.parentNode.innerHTML = '<span class="no-img">BRAK</span>';
      };
      img.onclick = function(e) {
        e.stopPropagation();
        openLightbox(item);
      };
      tdImg.appendChild(img);
    } else {
      tdImg.innerHTML = '<span class="no-img">—</span>';
    }
    tr.appendChild(tdImg);

    /* weryfikacja: ptaszek / popraw / to-nie-tablica */
    var tdVer = document.createElement('td');
    tdVer.className = 'td-verify';
    renderVerify(tdVer, item);
    tr.appendChild(tdVer);

    return tr;
  }

  /* ── WERYFIKACJA ODCZYTÓW ── */
  function sendVerify(item, verdict, td) {
    if (item.verify === verdict && verdict !== 'fix') verdict = 'clear';  /* drugi klik = cofnij */
    var payload = { id: item.id, verdict: verdict };
    if (verdict === 'fix') {
      var t = prompt('Poprawny numer dla #' + item.id + ':', item.vtext || item.plate || '');
      if (t === null) return;
      t = t.toUpperCase().replace(/[^A-Z0-9]/g, '');
      if (t.length < 2) return;
      payload.text = t;
    }
    fetch('/api/verify', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    })
      .then(function(r) { return r.json(); })
      .then(function(d) {
        if (!d.ok) return;
        item.verify = (verdict === 'clear') ? null : verdict;
        item.vtext = payload.text || null;
        renderVerify(td, item);
      })
      .catch(function() {});
  }

  /* ── PODKRECANIE OBRAZU W EDYTORZE ────────────────────────────────────
     Nie zmienia pliku — tylko filtr na podgladzie. Sluzy odzyskiwaniu
     „prawie czytelnych" tablic. Trzy suwaki (reszta byla nieuzywana):
       EV             — jasnosc, krok 0.25 dzialki
       KONTRAST       — rozciagniecie tonow
       PRZESWIETLENIA — sciaga SAME swiatla krzywa tonalna (filtr SVG),
                        bo brightness/contrast ruszaja caly obraz i
                        przepalonej tablicy nie da sie tym uratowac. */
  var rvT = { ev: 0, c: 0, h: 0 };

  function rvTuneApply() {
    var jasn  = Math.pow(2, rvT.ev * 0.5);      // 1 dzialka EV = x1.41
    var kontr = 1 + rvT.c * 0.35;
    var f = 'brightness(' + jasn.toFixed(3) + ') contrast(' + kontr.toFixed(2) + ')';
    if (rvT.h > 0) {
      // Krzywa: dol bez zmian, gora sciagnieta. Przy h=1 biel schodzi do 0.55,
      // wiec detale w przepaleniu wracaja zamiast zlewac sie w plame.
      var t = [0, 0.25, 0.5,
               (0.75 - 0.10 * rvT.h).toFixed(3),
               (1 - 0.45 * rvT.h).toFixed(3)].join(' ');
      ['fnR', 'fnG', 'fnB'].forEach(function(id) {
        var el = document.getElementById(id);
        if (el) el.setAttribute('tableValues', t);
      });
      f += ' url(#fltSwiatla)';
    }
    // Ten sam filtr na GLOWNYM zdjeciu I na wszystkich klatkach pod nim
    // (kandydaci + miniatury bonusowe). Inaczej porownywaloby sie rozjasnione
    // zdjecie z ciemnymi miniaturami — czyli jablka z gruszkami.
    var cele = document.querySelectorAll(
      '#rv-full, #rv-cands img, #rv-bonus img');
    for (var i = 0; i < cele.length; i++) cele[i].style.filter = f;
    var p = function(id, v) {
      var el = document.getElementById(id);
      if (el) el.textContent = v;
    };
    p('tn-ev-v', (rvT.ev > 0 ? '+' : '') + rvT.ev);
    p('tn-c-v', rvT.c.toFixed(1));
    p('tn-h-v', rvT.h.toFixed(2));
  }

  window.rvTuneOdswiez = rvTuneApply;   // po dociagnieciu nowych miniatur

  window.rvTuneSuwak = function() {
    var v = function(id) {
      var el = document.getElementById(id);
      return el ? parseFloat(el.value) : 0;
    };
    rvT.ev = v('tn-ev'); rvT.c = v('tn-c'); rvT.h = v('tn-h');
    rvTuneApply();
  };

  window.rvTune = function(co) {
    if (co === 'reset') {
      rvT = { ev: 0, c: 0, h: 0 };
      [['tn-ev', 0], ['tn-c', 0], ['tn-h', 0]].forEach(function(p) {
        var el = document.getElementById(p[0]);
        if (el) el.value = p[1];
      });
    }
    rvTuneApply();
  };

  /* ── IKONY PIXEL-ART ──────────────────────────────────────────────────
     Symboli ✓ ✎ ½ ⊘ ★ ▶ NIE MA w czcionkach pikselowych, wiec leca z zapasowej
     i psuja spojnosc. Rysujemy je sami: siatka 8x8, kazdy piksel to <rect>,
     `shape-rendering: crispEdges` = zero wygladzania.
     `fill: currentColor` sprawia, ze ikona dziedziczy kolor przycisku
     (zielony/zolty/czerwony) — bez dublowania stylow. */
  var PXI = {
    ok:   [[1,3],[2,4],[3,5],[4,4],[5,3],[6,2],[7,1]],
    fix:  [[7,0],[6,1],[5,2],[4,3],[3,4],[2,5],[1,6],[0,7],[1,7],[0,6]],
    part: [[1,1],[2,1],[3,1],[1,2],[2,2],[3,2],[1,3],[2,3],[3,3],
           [1,4],[2,4],[3,4],[1,5],[2,5],[3,5],[1,6],[2,6],[3,6],
           [4,1],[5,1],[6,1],[6,2],[6,3],[6,4],[6,5],[4,6],[5,6],[6,6]],
    unr:  [[2,1],[3,1],[4,1],[5,2],[4,3],[3,4],[3,5],[3,7]],
    not:  [[2,1],[3,1],[4,1],[5,1],[1,2],[6,2],[1,3],[6,3],[1,4],[6,4],
           [1,5],[6,5],[2,6],[3,6],[4,6],[5,6],[2,5],[3,4],[4,3],[5,2]],
    usb:  [[3,0],[4,0],[3,1],[4,1],[2,2],[3,2],[4,2],[5,2],[2,3],[5,3],
           [2,4],[5,4],[2,5],[3,5],[4,5],[5,5],[3,6],[4,6],[3,7],[4,7]],
    bt:   [[3,0],[4,1],[5,2],[3,1],[3,2],[3,3],[3,4],[3,5],[3,6],[3,7],
           [4,6],[5,5],[2,2],[2,5]],
    star: [[3,0],[3,1],[2,2],[3,2],[4,2],[0,3],[1,3],[2,3],[3,3],[4,3],
           [5,3],[6,3],[1,4],[2,4],[3,4],[4,4],[5,4],[2,5],[3,5],[4,5],
           [1,6],[5,6],[0,7],[6,7]],
    list: [[1,1],[2,1],[3,1],[4,1],[5,1],[6,1],[1,3],[2,3],[3,3],[4,3],
           [5,3],[6,3],[1,5],[2,5],[3,5],[4,5],[5,5],[6,5]],
    play: [[2,1],[2,2],[3,2],[2,3],[3,3],[4,3],[2,4],[3,4],[4,4],[5,4],
           [2,5],[3,5],[4,5],[2,6],[3,6],[2,7]],
    cam:  [[2,1],[3,1],[1,2],[2,2],[3,2],[4,2],[5,2],[6,2],[1,3],[6,3],
           [1,4],[3,4],[4,4],[6,4],[1,5],[3,5],[4,5],[6,5],[1,6],[2,6],
           [3,6],[4,6],[5,6],[6,6]],
    ref:  [[2,1],[3,1],[4,1],[1,2],[5,2],[1,3],[6,3],[5,4],[6,4],[1,5],
           [2,5],[3,5],[4,5],[5,5],[6,2]]
  };
  /* Statyczne przyciski (naglowek, pasek) maja <span data-ico="...">.
     Wypelniamy je po zaladowaniu — jedno miejsce zamiast wklejania SVG
     w kazdym przycisku. */
  window.pxIconyWstaw = function(gdzie) {
    var el = (gdzie || document).querySelectorAll('[data-ico]');
    for (var i = 0; i < el.length; i++) {
      if (el[i].firstChild) continue;              // juz wstawione
      el[i].innerHTML = window.pxIcon(el[i].getAttribute('data-ico'), 14);
    }
  };
  window.pxIcon = function(nazwa, rozmiar) {
    var p = PXI[nazwa]; if (!p) return '';
    var r = '';
    for (var i = 0; i < p.length; i++)
      r += '<rect x="' + p[i][0] + '" y="' + p[i][1] + '" width="1" height="1"/>';
    var s = rozmiar || 13;
    return '<svg viewBox="0 0 8 8" width="' + s + '" height="' + s +
           '" fill="currentColor" shape-rendering="crispEdges" ' +
           'style="vertical-align:-1px">' + r + '</svg>';
  };

  function renderVerify(td, item) {
    td.innerHTML = '';
    if (item.id === undefined) return;
    var mk = function(sym, cls, verdict, title) {
      var b = document.createElement('button');
      b.className = 'vbtn ' + cls + (item.verify === verdict ? ' act' : '');
      b.innerHTML = sym;
      b.title = title;
      b.onclick = function(e) { e.stopPropagation(); sendVerify(item, verdict, td); };
      td.appendChild(b);
    };
    mk(pxIcon('ok'),   'v-ok',   'ok',         'Odczyt poprawny');
    mk(pxIcon('fix'),  'v-fix',  'fix',        'Popraw numer (wpiszesz wlasciwy)');
    mk(pxIcon('part'), 'v-part', 'partial',    'Tablica niepelna / ucieta kadrem');
    mk(pxIcon('unr'),  'v-unr',  'unreadable', 'Nieczytelna nawet dla czlowieka');
    mk(pxIcon('not'),  'v-not',  'notplate',   'To nie jest tablica');
    if (item.verify === 'fix' && item.vtext) {
      var s = document.createElement('div');
      s.className = 'v-text';
      s.textContent = '→ ' + item.vtext;
      td.appendChild(s);
    }
  }

  /* ── FETCH PLATES (ze stronami) ── */
  var tablePage = 0, tableTotal = 0, PAGE_SIZE = 100;
  function fetchPlates(forceRefresh) {
    var offset = tablePage * PAGE_SIZE, totalHdr = 0;
    fetch('/api/plates?limit=' + PAGE_SIZE + '&offset=' + offset)
      .then(function(r) {
        setOnline(true);
        totalHdr = parseInt(r.headers.get('X-Total-Count') || '0', 10) || 0;
        return r.json();
      })
      .then(function(data) {
        if (!Array.isArray(data)) return;
        tableTotal = totalHdr;
        allRows = data;
        renderTable(data, forceRefresh);
        renderPager();
        if (window.rvRefresh) rvRefresh();   /* edytor: odswiez kolejke */
      })
      .catch(function() {
        setOnline(false);
      });
  }

  window.tableGoto = function(page) {
    var maxPage = Math.max(0, Math.ceil(tableTotal / PAGE_SIZE) - 1);
    tablePage = Math.max(0, Math.min(maxPage, page));
    fetchPlates(false);
    var ep = document.getElementById('events-panel');
    if (ep) ep.scrollTop = 0;              /* nowa strona od gory */
  };

  function renderPager() {
    var pager = document.getElementById('tbl-pager');
    if (!pager) return;
    var pages = Math.max(1, Math.ceil(tableTotal / PAGE_SIZE));
    if (pages <= 1) { pager.innerHTML = ''; pager.style.display = 'none'; return; }
    pager.style.display = 'flex';
    var cur = tablePage;
    pager.innerHTML =
      '<button class="pg-btn"' + (cur <= 0 ? ' disabled' : '') +
        ' onclick="tableGoto(' + (cur - 1) + ')">&#x25C0; nowsze</button>' +
      '<span class="pg-info">strona ' + (cur + 1) + ' / ' + pages +
        ' &middot; ' + tableTotal + ' szt.</span>' +
      '<button class="pg-btn"' + (cur >= pages - 1 ? ' disabled' : '') +
        ' onclick="tableGoto(' + (cur + 1) + ')">starsze &#x25B6;</button>';
  }

  function renderTable(data, detectNew) {
    var tbody = document.getElementById('events-tbody');
    var filter = searchTerm.toLowerCase();

    /* find new keys */
    var newKeys = {};
    if (detectNew) {
      data.forEach(function(item) {
        var key = (item.plate || '') + '|' + (item.seen_at || '');
        if (!knownPlates[key]) {
          newKeys[key] = true;
        }
        knownPlates[key] = true;
      });
    } else {
      data.forEach(function(item) {
        var key = (item.plate || '') + '|' + (item.seen_at || '');
        knownPlates[key] = true;
      });
    }

    /* filter */
    var filtered = data.filter(function(item) {
      if (!filter) return true;
      var plate = (item.plate || '').toUpperCase();
      return plate.indexOf(filter.toUpperCase()) !== -1;
    });

    if (filtered.length === 0) {
      tbody.innerHTML = '<tr><td colspan="8" style="text-align:center;color:var(--dim);padding:20px;font-size:11px">Brak wynikow' + (filter ? ' dla: ' + filter : '') + '</td></tr>';
      return;
    }

    var frag = document.createDocumentFragment();
    filtered.forEach(function(item) {
      var key = (item.plate || '') + '|' + (item.seen_at || '');
      var isNew = detectNew && newKeys[key];
      var tr = buildRow(item, isNew);
      frag.appendChild(tr);

      if (isNew) {
        setTimeout(function() {
          tr.classList.remove('new-row');
          tr.style.background = '';
        }, 1500);
      }
    });

    tbody.innerHTML = '';
    tbody.appendChild(frag);
  }

  /* ── SEARCH ── */
  document.getElementById('search-input').addEventListener('input', function() {
    searchTerm = this.value;
    renderTable(allRows, false);
  });

  /* ── STATS ── */
  function setStatVal(id, text, alert) {
    var el = document.getElementById(id);
    if (!el) return;
    el.textContent = text;
    el.className = 'ss-val' + (alert ? ' alert' : '');
  }

  function fetchStats() {
    fetch('/api/stats')
      .then(function(r) { return r.json(); })
      .then(function(d) {
        setOnline(true);
        if (d.today_plates !== undefined)
          document.getElementById('h-today').textContent = d.today_plates;
        if (d.total_plates !== undefined)
          document.getElementById('h-total').textContent = d.total_plates;
        if (d.free_disk_mb !== undefined) {
          var mb = parseFloat(d.free_disk_mb);
          document.getElementById('h-disk').textContent =
            mb >= 1024 ? (mb / 1024).toFixed(1) + ' GB' : mb.toFixed(0) + ' MB';
        }
        if (d.fps !== undefined)
          setStatVal('ss-fps',   d.fps.toFixed(1),            d.fps > 0 && d.fps < 8);
        if (d.cpu !== undefined)
          setStatVal('ss-cpu',   d.cpu.toFixed(0) + '%',      d.cpu > 85);
        if (d.cpu_temp !== undefined)
          setStatVal('ss-cput',  d.cpu_temp > 0 ? d.cpu_temp.toFixed(0) + '°C' : '--',
                     d.cpu_temp > 70);
        if (d.hailo_temp !== undefined)
          setStatVal('ss-hailo', d.hailo_temp > 0 ? d.hailo_temp.toFixed(0) + '°C' : '--',
                     d.hailo_temp > 70);
        if (d.zasilanie) {
          var z = d.zasilanie;
          var zt = z.spadek ? 'SPADEK!' : (z.dlawienie ? 'DŁAWI' :
                   (z.spadek_bylo ? 'było ' + z.n_spadkow + '×' : 'OK'));
          setStatVal('ss-zas', zt, z.spadek || z.dlawienie);
          var zi = document.getElementById('ss-zas-item');
          if (zi) zi.title = 'throttled=0x' + z.hex.toString(16) +
            (z.ostatni ? ', ostatni spadek ' + z.ostatni : '') +
            (z.laczny_s ? ', łącznie ' + Math.round(z.laczny_s) + ' s' : '');
        }
      })
      .catch(function() { setOnline(false); });
  }

  /* ── SPLITTER: przeciąganie podziału podgląd/tabela ── */
  (function() {
    var sp = document.getElementById('splitter');
    var panel = document.getElementById('stream-panel');
    if (!sp || !panel) return;
    var saved = localStorage.getItem('lpr_split');
    /* na telefonie panel ma byc pelnej szerokosci (CSS) — inline styl by go nadpisal */
    if (saved && window.innerWidth > 820) panel.style.width = saved;
    /* skala tabeli: im szerszy panel, tym wieksze zdjecia i napisy */
    var ep = document.getElementById('events-panel');
    function updateTScale() {
      var s = Math.max(1, Math.min(2.4, ep.clientWidth / 640));
      ep.style.setProperty('--tscale', s.toFixed(2));
    }
    updateTScale();
    window.addEventListener('resize', updateTScale);
    var drag = false;
    function move(x) {
      var r = document.getElementById('main').getBoundingClientRect();
      var pct = (x - r.left) / r.width * 100;
      pct = Math.max(18, Math.min(82, pct));
      panel.style.width = pct + '%';
      updateTScale();
    }
    function stop() {
      if (!drag) return;
      drag = false;
      sp.classList.remove('drag');
      localStorage.setItem('lpr_split', panel.style.width);
    }
    sp.addEventListener('mousedown', function(e) {
      drag = true; sp.classList.add('drag'); e.preventDefault();
    });
    document.addEventListener('mousemove', function(e) { if (drag) move(e.clientX); });
    document.addEventListener('mouseup', stop);
    sp.addEventListener('touchstart', function(e) {
      drag = true; sp.classList.add('drag'); e.preventDefault();
    }, { passive: false });
    document.addEventListener('touchmove', function(e) {
      if (drag) move(e.touches[0].clientX);
    }, { passive: true });
    document.addEventListener('touchend', stop);
  })();

  /* ── ONLINE STATUS ── */
  var _online = null;
  function setOnline(val) {
    if (_online === val) return;
    _online = val;
    /* kropka przy LIVE: zielona pulsująca = OK, czerwona = brak łączności */
    var dot = document.querySelector('#stream-label .rec-dot');
    if (dot) dot.className = 'rec-dot' + (val ? '' : ' err');
    var el = document.getElementById('h-status');
    if (val) {
      el.className = 'status-online';
      el.textContent = '● ONLINE';
    } else {
      el.className = 'status-offline';
      el.textContent = '✕ OFFLINE';
    }
  }

  /* ── CONFIG ── */
  function fetchConfig() {
    fetch('/api/config')
      .then(function(r) { return r.json(); })
      .then(function(d) {
        if (d.plate_min_conf !== undefined) {
          document.getElementById('cfg-det').value = d.plate_min_conf;
          document.getElementById('val-det').textContent = parseFloat(d.plate_min_conf).toFixed(2);
        }
        if (d.ocr_min_conf !== undefined) {
          document.getElementById('cfg-ocr').value = d.ocr_min_conf;
          document.getElementById('val-ocr').textContent = parseFloat(d.ocr_min_conf).toFixed(2);
        }
        if (d.ocr_min_matching !== undefined) {
          document.getElementById('cfg-reads').value = d.ocr_min_matching;
          document.getElementById('val-reads').textContent = d.ocr_min_matching;
        }
      })
      .catch(function() {});
  }

  window.saveConfig = function() {
    var payload = {
      plate_min_conf: parseFloat(document.getElementById('cfg-det').value),
      ocr_min_conf: parseFloat(document.getElementById('cfg-ocr').value),
      ocr_min_matching: parseInt(document.getElementById('cfg-reads').value, 10)
    };
    var statusEl = document.getElementById('save-status');
    statusEl.textContent = 'Zapisywanie...';
    statusEl.className = '';

    fetch('/api/config', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload)
    })
    .then(function(r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      statusEl.textContent = 'Zapisano.';
      statusEl.className = 'ok';
      setTimeout(function() { statusEl.textContent = ''; statusEl.className = ''; }, 3000);
    })
    .catch(function(e) {
      statusEl.textContent = 'Blad: ' + e.message;
      statusEl.className = 'err';
      setTimeout(function() { statusEl.textContent = ''; statusEl.className = ''; }, 5000);
    });
  };

  /* ── RADAR: tryb fotoradaru ── */
  var _radarTimer = null, _radarCfgOpen = false;
  function radarRender(d) {
    var btn = document.getElementById('radar-btn');
    btn.classList.toggle('on', !!d.cfg.tryb);
    var st = d.stan, c = d.cfg;
    var v = (st.swieza ? Math.abs(st.v).toFixed(0) : '--');
    var h = '<div class="rd-row"><span class="rd-big' + (st.otwarte ? ' ev' : '') + '">' + v +
      '</span><span style="color:var(--dim)">km/h</span>' +
      '<span style="flex:1"></span>' +
      '<span style="color:' + (st.ok ? 'var(--green)' : 'var(--red)') + '">' +
      (st.ok ? '&#9679; port OK' : '&#9679; BRAK PORTU ' + (st.blad || '')) + '</span>' +
      '<span style="color:var(--dim)">pr&oacute;bek: ' + st.probek + ' &middot; zdarze&#324;: ' + st.zdarzen + '</span></div>';
    if (!_radarCfgOpen) {
      h += '<div class="rd-row">' +
        '<label style="display:flex;align-items:center;gap:6px;cursor:pointer;color:var(--text);font-size:13px;text-transform:none">' +
        '<input type="checkbox" id="rd-tryb"' + (c.tryb ? ' checked' : '') + ' onchange="radarSet({tryb:this.checked})"> TRYB RADAR (zapisuj tylko przekroczenia)</label>' +
        '<label>pr&oacute;g</label><input type="number" id="rd-prog" min="5" max="200" step="5" value="' + c.prog_kmh + '" onchange="radarSet({prog_kmh:parseFloat(this.value)})"> km/h' +
        '<label>kierunek</label><select onchange="radarSet({kierunek:this.value})">' +
        ['oba', '+', '-'].map(function(k) { return '<option value="' + k + '"' + (c.kierunek === k ? ' selected' : '') + '>' +
          (k === 'oba' ? 'oba' : k === '+' ? 'znak + ' : 'znak &minus; ') + '</option>'; }).join('') + '</select>' +
        '<label style="display:flex;align-items:center;gap:6px;cursor:pointer;color:var(--text);font-size:12px;text-transform:none">' +
        '<input type="checkbox"' + (c.zapis_zwykly ? ' checked' : '') + ' onchange="radarSet({zapis_zwykly:this.checked})"> zapisuj te&#380; zwyk&#322;e przejazdy</label>' +
        '<label style="display:flex;align-items:center;gap:6px;cursor:pointer;color:var(--text);font-size:12px;text-transform:none">' +
        '<input type="checkbox"' + (c.glos ? ' checked' : '') + ' onchange="radarSet({glos:this.checked})"> czytaj pr&#281;dko&#347;&#263;</label>' +
        '<button class="cam-btn cam-btn-secondary" onclick="radarTest(false)" title="Symuluje zdarzenie 88 km/h: zdjecie z pierscienia + klip z dashcamu">&#9654; TEST</button>' +
        '<button class="cam-btn cam-btn-secondary" onclick="radarTest(true)" title="Symulacja z ostatnia zapisana tablica (zdjecie z ramkami)">&#9654; TEST Z TABLIC&#260;</button>' +
        '</div>';
    }
    var dbg = localStorage.getItem('lpr_radar_debug') === '1';
    h += '<div style="font-size:11px;color:var(--dim);margin:6px 0 4px;display:flex;align-items:center;gap:10px">ZDARZENIA' +
      '<label style="margin-left:auto;display:flex;align-items:center;gap:4px;cursor:pointer;text-transform:none"><input type="checkbox"' +
      (dbg ? ' checked' : '') + ' onchange="radarDebug(this.checked)"> DEBUG</label></div>' +
      rdFiltrHtml((d.zdarzenia || []).length);
    h += (d.zdarzenia || []).map(function(e) {
      var dd = null;
      if (dbg && e.debug) { try { dd = JSON.parse(e.debug); } catch (x) {} }
      var dbgHtml = '';
      if (dbg) {
        dbgHtml = '<div class="t" style="margin-top:4px;color:var(--yellow)">kierunek ' + (e.kierunek || '?') +
          ' &middot; pr&oacute;bek ' + e.probek + ' &middot; kamera ' + (e.v_kam != null ? Math.round(e.v_kam) + ' km/h' : 'brak') +
          ' &middot; ' + (e.pewne ? 'zgodne' : 'niepewne') + (e.uwagi ? ' &middot; ' + e.uwagi : '') + '</div>';
        if (dd) {
          dbgHtml += '<div class="t">pr&oacute;g ' + dd.prog + ' &middot; kierunek cfg ' + dd.kierunek_cfg + ' &middot; min pr&oacute;bek ' + dd.min_probek +
            ' &middot; okno ' + dd.okno_s + ' s &middot; K=' + dd.kam_K + ' tol=' + dd.kam_tol + ' &middot; trwanie ' + dd.trwanie_s + ' s</div>';
          dbgHtml += '<div class="t">kandydaci: ' + ((dd.kandydaci || []).map(function(k) {
            return (k.tablica || '?') + ' (track ' + k.tid + ', kamera ' + (k.v_kam != null ? k.v_kam : '-') + ', ocr ' + k.ocr + ', ' +
              (k.zgodny ? 'zgodny' : 'niezgodny') + ', +' + k.po_s + ' s)'; }).join('; ') || 'brak') +
            (dd.wybrany_tid != null ? ' &middot; wybrany track ' + dd.wybrany_tid : '') + '</div>';
          if (dd.probki && dd.probki.length) {
            dbgHtml += '<div class="t" style="font-family:monospace;white-space:normal;word-break:break-all">radar (t;v): ' +
              dd.probki.map(function(p) { return p[0] + ';' + p[1]; }).join('  ') + '</div>';
          }
        }
      }
      return '<div class="rd-ev">' +
        (e.zdjecie ? '<img src="' + (e.mini || e.zdjecie) + '" loading="lazy" onclick="window.open(\'' + e.zdjecie + '\')">' : '<div style="width:150px;color:var(--dim)">bez zdj&#281;cia</div>') +
        '<div class="v">' + Math.round(e.v) + '<span style="font-size:11px;color:var(--dim)"> km/h ' + (e.kierunek || '') + '</span></div>' +
        '<div><div class="pl">' + (e.tablica || '&mdash;') + rdPojazdHtml(e) +
        (e.tablica ? (e.pewne ? ' <span style="color:var(--green);font-size:11px">&#10003; zgodne z kamer&#261;</span>' : ' <span style="color:var(--yellow);font-size:11px">? niepewne</span>') : '') +
        '</div><div class="t">' + e.czas + '</div>' +
        (e.klip ? '<a href="' + e.klip + '" target="_blank">&#9654; klip</a>' : '<span class="t">klip: czekam / brak</span>') +
        dbgHtml + '</div></div>';
    }).join('') || '<div style="color:var(--dim);padding:8px">brak zdarze&#324;</div>';
    if (dbg) {
      h += '<div style="font-size:11px;color:var(--dim);margin:10px 0 4px">LOG RADARU (ostatnie linie)</div>' +
        '<pre id="rd-log" style="font-size:11px;max-height:220px;overflow:auto;background:var(--bg);padding:6px;border:1px solid var(--border)">...</pre>';
    }
    document.getElementById('radar-body').innerHTML = h;
    if (dbg) {
      fetch('/api/log?n=1500').then(function(r) { return r.text(); }).then(function(t) {
        var el = document.getElementById('rd-log');
        if (!el) return;
        var lines = t.split('\n').filter(function(l) { return l.indexOf('[Radar]') >= 0; });
        el.textContent = lines.slice(-60).join('\n') || 'brak wpisow [Radar]';
        el.scrollTop = el.scrollHeight;
      }).catch(function() {});
    }
  }
  window.radarDebug = function(on) {
    localStorage.setItem('lpr_radar_debug', on ? '1' : '0');
    radarRefresh();
  };
  function radarRefresh(force) {
    /* odświeżanie z zegara nie przerywa wpisywania w filtrze */
    if (!force && document.activeElement && document.activeElement.closest &&
        document.activeElement.closest('#rd-filtr')) return;
    fetch('/api/radar' + rdQuery()).then(function(r) { return r.json(); }).then(radarRender).catch(function() {});
  }
  /* ── RADAR: filtr / sortowanie listy zdarzeń (02.10) — ten sam kod w www i w przeglądarce na pendrive ── */
  var _rdF = (function() { try { return JSON.parse(localStorage.getItem('lpr_rd_filtr') || '{}'); } catch (e) { return {}; } })();
  function rdQuery() {
    var f = _rdF, q = [];
    if (f.od) q.push('od=' + encodeURIComponent(f.od));
    if (f.do) q.push('do=' + encodeURIComponent(f.do));
    if (f.v_min) q.push('v_min=' + encodeURIComponent(f.v_min));
    if (f.v_max) q.push('v_max=' + encodeURIComponent(f.v_max));
    if (f.tablica) q.push('tablica=' + encodeURIComponent(f.tablica));
    if (f.tylko_tablica) q.push('tylko_tablica=1');
    if (f.tylko_pojazd) q.push('tylko_pojazd=1');
    if (f.kierunek) q.push('kierunek=' + encodeURIComponent(f.kierunek));
    q.push('sort=' + (f.sort || 'czas_desc'));
    q.push('limit=' + (f.limit || 60));
    return '?' + q.join('&');
  }
  window.rdFiltrZmien = function(k, v) {
    _rdF[k] = v; localStorage.setItem('lpr_rd_filtr', JSON.stringify(_rdF)); radarRefresh(true);
  };
  window.rdFiltrReset = function() { _rdF = {}; localStorage.setItem('lpr_rd_filtr', '{}'); radarRefresh(true); };
  function rdFiltrHtml(n) {
    var f = _rdF;
    function sel(id, opts, cur) {
      return '<select onchange="rdFiltrZmien(\'' + id + '\', this.value)">' + opts.map(function(o) {
        return '<option value="' + o[0] + '"' + ((cur || '') === o[0] ? ' selected' : '') + '>' + o[1] + '</option>'; }).join('') + '</select>';
    }
    return '<div class="rd-row" id="rd-filtr" style="font-size:12px">' +
      '<label>od</label><input type="date" value="' + (f.od || '') + '" onchange="rdFiltrZmien(\'od\', this.value)">' +
      '<label>do</label><input type="date" value="' + (f.do || '') + '" onchange="rdFiltrZmien(\'do\', this.value)">' +
      '<label>km/h od</label><input type="number" min="0" max="300" value="' + (f.v_min || '') + '" onchange="rdFiltrZmien(\'v_min\', this.value)">' +
      '<label>do</label><input type="number" min="0" max="300" value="' + (f.v_max || '') + '" onchange="rdFiltrZmien(\'v_max\', this.value)">' +
      '<label>tablica</label><input type="text" style="width:96px" value="' + (f.tablica || '') + '" placeholder="np. WY7" onchange="rdFiltrZmien(\'tablica\', this.value.toUpperCase())">' +
      '<label>kier.</label>' + sel('kierunek', [['', 'oba'], ['+', '+ nadje&#380;d&#380;a'], ['-', '&minus; oddala']], f.kierunek) +
      '<label>sortuj</label>' + sel('sort', [['czas_desc', 'najnowsze'], ['czas_asc', 'najstarsze'], ['v_desc', 'najszybsze'], ['v_asc', 'najwolniejsze']], f.sort || 'czas_desc') +
      '<label>ile</label>' + sel('limit', [['60', '60'], ['200', '200'], ['500', '500']], String(f.limit || 60)) +
      '<label class="rd-chk"><input type="checkbox"' + (f.tylko_tablica ? ' checked' : '') + ' onchange="rdFiltrZmien(\'tylko_tablica\', this.checked)"> tylko z tablic&#261;</label>' +
      '<label class="rd-chk"><input type="checkbox"' + (f.tylko_pojazd ? ' checked' : '') + ' onchange="rdFiltrZmien(\'tylko_pojazd\', this.checked)"> bez b&#322;&#281;d&oacute;w pomiaru</label>' +
      '<button class="cam-btn cam-btn-secondary" onclick="rdFiltrReset()">WYCZY&#346;&#262;</button>' +
      '<span style="color:var(--dim)">' + (n != null ? n + ' zdarze&#324;' : '') + '</span></div>';
  }
  function rdPojazdHtml(e) {
    if (e.tablica) return '';
    if (e.pojazd === 1) return ' <span style="color:var(--green);font-size:11px">&#10003; pojazd widoczny w kadrze</span>';
    if (e.pojazd === 0) return ' <span style="color:var(--red);font-size:11px">&#9888; brak pojazdu w kadrze &mdash; b&#322;&#261;d pomiaru?</span>';
    return '';
  }

  /* 02.10: co 2 s tylko lekki stan (prędkość, port), pełna lista co 10 s —
     pełna odpowiedź z DEBUG-iem to kilkadziesiąt kB, a Pi jest na tetheringu */
  var _radarLekkiTimer = null, _radarTick = 0;
  function radarRefreshLekki() {
    fetch('/api/radar?lekki=1').then(function(r) { return r.json(); }).then(function(d) {
      var st = d.stan, el = document.querySelector('#radar-body .rd-big');
      if (!el) return;
      el.textContent = st.swieza ? Math.abs(st.v).toFixed(0) : '--';
      el.classList.toggle('ev', !!st.otwarte);
    }).catch(function() {});
  }
  window.openRadarModal = function() {
    document.getElementById('radar-modal').style.display = 'flex';
    radarRefresh(true);
    clearInterval(_radarTimer); clearInterval(_radarLekkiTimer);
    _radarTimer = setInterval(function() { radarRefresh(false); }, 10000);
    _radarLekkiTimer = setInterval(radarRefreshLekki, 2000);
    radarModulInit();
  };
  window.closeRadarModal = function() {
    clearInterval(_radarTimer); _radarTimer = null;
    clearInterval(_radarLekkiTimer); _radarLekkiTimer = null;
    document.getElementById('radar-modal').style.display = 'none';
  };
  /* ── MODUŁ LD2415H: parametry wewnętrzne (karta, rozdz. 6.3) ── */
  var _radarModulGotowy = false;
  function radarModulInit() {
    if (_radarModulGotowy) return;
    _radarModulGotowy = true;
    var box = document.getElementById('radar-modul');
    if (!box) return;
    box.innerHTML =
      '<details' + (localStorage.getItem('lpr_radar_modul') === '1' ? ' open' : '') + ' ontoggle="localStorage.setItem(\'lpr_radar_modul\', this.open ? \'1\' : \'0\')">' +
      '<summary style="cursor:pointer;font-size:11px;color:var(--dim);margin:6px 0">MODU&#321; LD2415H &mdash; parametry wewn&#281;trzne (zapisywane W MODULE)</summary>' +
      '<div class="rd-row"><button class="cam-btn cam-btn-secondary" onclick="radarKomenda({typ:\'odczyt\'})">&#8635; ODCZYTAJ Z MODU&#321;U</button>' +
      '<span id="rdm-info" style="font-size:12px;color:var(--dim)">nie odczytano</span></div>' +
      '<div class="rd-row"><label>min. pr&#281;dko&#347;&#263;</label><input type="number" id="rdm-min" min="0" max="255" value="1"> km/h' +
      '<label>k&#261;t</label><input type="number" id="rdm-kat" min="0" max="90" value="0">&deg;' +
      '<label>czu&#322;o&#347;&#263;</label><input type="number" id="rdm-czul" min="1" max="15" value="5"> <span style="font-size:11px;color:var(--dim)">(1 = najdalej, 15 = najbli&#380;ej/odporniej)</span>' +
      '<button class="cam-btn cam-btn-secondary" onclick="radarKomenda({typ:\'param\',min_kmh:v(\'rdm-min\'),kat:v(\'rdm-kat\'),czulosc:v(\'rdm-czul\')})">ZAPISZ</button></div>' +
      '<div class="rd-row"><label>kierunek</label><select id="rdm-kier"><option value="0">oba</option><option value="1">tylko nadje&#380;d&#380;aj&#261;ce (+)</option><option value="2">tylko oddalaj&#261;ce (&minus;)</option></select>' +
      '<label>pr&oacute;bkowanie</label><select id="rdm-prob"><option value="0">~22/s</option><option value="1">~11/s</option><option value="2">~5,5/s</option><option value="3">~2,75/s</option></select>' +
      '<button class="cam-btn cam-btn-secondary" onclick="radarKomenda({typ:\'wyjscie\',kierunek:v(\'rdm-kier\'),probkowanie:v(\'rdm-prob\')})">ZAPISZ</button></div>' +
      '<div class="rd-row"><label>antywibracja</label><input type="number" id="rdm-anty" min="0" max="112" value="0"> <span style="font-size:11px;color:var(--dim)">(0&ndash;112; wi&#281;cej = odporniej na drgania, gorzej na kr&oacute;tkie ruchy)</span>' +
      '<button class="cam-btn cam-btn-secondary" onclick="radarKomenda({typ:\'antywibracja\',wartosc:v(\'rdm-anty\')})">ZAPISZ</button></div>' +
      '<pre id="rdm-odp" style="font-size:11px;max-height:120px;overflow:auto;background:var(--bg);padding:6px;border:1px solid var(--border);white-space:pre-wrap"></pre>' +
      '</details>';
  }
  window.v = function(id) { return parseInt(document.getElementById(id).value, 10) || 0; };
  function radarModulPokaz(w) {
    var p = (w.odczyt && w.odczyt.param) || w.param, info = document.getElementById('rdm-info');
    var odp = document.getElementById('rdm-odp');
    if (odp) odp.textContent = (w.ok ? '' : 'B\u0141\u0104D: ' + (w.blad || '') + '\n') +
      'wys\u0142ano: ' + (w.wyslano || '') + '\nodpowied\u017a: ' + (w.odp || '(brak)') +
      (w.odczyt ? '\nodczyt kontrolny: ' + (w.odczyt.odp || '(brak)') : '');
    if (!p) { if (info) info.textContent = w.ok ? 'modu\u0142 nie odpowiedzia\u0142 tekstem (tryb protoko\u0142u?)' : (w.blad || 'b\u0142\u0105d'); return; }
    var set = function(id, val) { var e = document.getElementById(id); if (e && val !== undefined) e.value = val; };
    set('rdm-min', p['1']); set('rdm-kat', p['2']); set('rdm-czul', p['3']);
    set('rdm-kier', p['4']); set('rdm-prob', p['5']); set('rdm-anty', p['7']);
    if (info) info.textContent = (w.firmware || (w.odczyt && w.odczyt.firmware) || '') +
      ' \u00b7 min ' + p['1'] + ' km/h, k\u0105t ' + p['2'] + '\u00b0, czu\u0142o\u015b\u0107 ' + p['3'] +
      ', kierunek ' + p['4'] + ', pr\u00f3bkowanie ' + p['5'] + ', jednostka ' + p['6'] + ', antywibracja ' + p['7'] + ', protok\u00f3\u0142 ' + p['0'];
  }
  window.radarKomenda = function(o) {
    var info = document.getElementById('rdm-info');
    if (info) info.textContent = 'wysy\u0142am\u2026';
    fetch('/api/radar/komenda', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(o) }).then(function(r) { return r.json(); }).then(radarModulPokaz)
      .catch(function() { if (info) info.textContent = 'b\u0142\u0105d po\u0142\u0105czenia'; });
  };
  window.radarSet = function(o) {
    fetch('/api/radar', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(o) }).then(radarRefresh).catch(function() {});
  };
  window.radarTest = function(zTablica) {
    fetch('/api/radar/test', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(zTablica ? { v: 72, sighting_id: 'auto' } : { v: 88 }) })
      .then(radarRefresh).catch(function() {});
  };
  /* stan przycisku w nagłówku także bez otwartego panelu */
  setInterval(function() {
    fetch('/api/radar?lekki=1').then(function(r) { return r.json(); })
      .then(function(d) { document.getElementById('radar-btn').classList.toggle('on', !!d.cfg.tryb); })
      .catch(function() {});
  }, 15000);

  /* ── PENDRIVE: bezpieczne wyjmowanie + kopie zapasowe ── */
  var _usbTimer = null;

  function usbRender(d) {
    var btn = document.getElementById('usb-btn');
    btn.classList.toggle('warn', d.state === 'eject');
    btn.classList.toggle('err', !d.present);
    var box, txt;
    if (!d.present) {
      box = 'err'; txt = '<b>Pendrive wyj&#281;ty</b><br>Zapis wstrzymany. ' +
        'W&#322;&#243;&#380; pendrive — system wznowi si&#281; sam.';
    } else if (d.state === 'eject') {
      box = 'warn'; txt = '<b>Mo&#380;esz bezpiecznie wyj&#261;&#263;</b><br>' +
        'Zapis wstrzymany, dane zapisane na dysk.';
    } else {
      box = 'ok'; txt = '<b>Pendrive pod&#322;&#261;czony</b><br>Zapis dzia&#322;a normalnie.';
    }
    var h = '<div class="usb-stat ' + box + '"><span class="big">' +
      (box === 'ok' ? '&#9989;' : box === 'warn' ? '&#9208;' : '&#9888;') +
      '</span><span>' + txt + '</span></div>';
    h += '<div class="usb-acts">';
    if (d.state === 'eject' || !d.writing) {
      h += '<button class="go" onclick="usbAct(\'resume\')">&#9654; WZN&#211;W ZAPIS</button>';
    } else {
      h += '<button class="primary" onclick="usbAct(\'eject\')">&#9208; BEZPIECZNIE WYJMIJ</button>';
    }
    h += '<button onclick="usbAct(\'backup\')">&#128190; KOPIA TERAZ</button></div>';
    var ph = d.photos || {};
    h += '<div class="usb-info">Na kart&#281; SD (inny no&#347;nik ni&#380; pendrive) ' +
      'kopiowane s&#261; <b>baza</b> (rozpoznania + Twoje oceny) i <b>zdj&#281;cia</b>, ' +
      'automatycznie co 30 min oraz przed ka&#380;dym wyj&#281;ciem.<br>' +
      'Baza: <b>' + d.db_kb + ' kB</b> &middot; zdj&#281;cia w kopii: <b>' +
      (ph.files || 0) + ' plik&#243;w / ' + (ph.mb || 0) + ' MB</b>' +
      (ph.when ? ' (' + ph.when + ')' : ' — jeszcze nie kopiowane') +
      (ph.busy ? ' <span style="color:var(--yellow)">kopiuj&#281;&#8230;</span>' : '') +
      '<br>Wolne na SD: <b>' + Math.round(d.free_sd_mb / 1024 * 10) / 10 +
      ' GB</b>. Nagrania z jazdy (dashcam) NIE s&#261; kopiowane — nadpisuj&#261; ' +
      'si&#281; z za&#322;o&#380;enia.</div>';
    h += '<div style="font-size:11px;color:var(--dim);margin-bottom:4px">' +
      'OSTATNIE KOPIE</div><div id="usb-baks">';
    h += (d.backups || []).map(function(b) {
      return '<div><b>' + b.when + '</b><span>' + b.name.replace(/^plates_\d+_\d+_/, '') +
        ' &middot; ' + b.kb + ' kB</span></div>';
    }).join('') || '<div>brak kopii</div>';
    h += '</div>';
    document.getElementById('usb-body').innerHTML = h;
  }

  function usbRefresh() {
    fetch('/api/usb').then(function(r) { return r.json(); })
      .then(usbRender).catch(function() {});
  }
  window.openUsbModal = function() {
    document.getElementById('usb-modal').style.display = 'flex';
    usbRefresh();
    clearInterval(_usbTimer);
    _usbTimer = setInterval(usbRefresh, 3000);
  };
  window.closeUsbModal = function() {
    clearInterval(_usbTimer); _usbTimer = null;
    document.getElementById('usb-modal').style.display = 'none';
  };
  window.usbAct = function(a) {
    var body = document.getElementById('usb-body');
    if (a === 'eject') body.innerHTML =
      '<div class="usb-stat warn"><span class="big">&#8987;</span>' +
      '<span>Ko&#324;cz&#281; zapisy i robi&#281; kopi&#281;&#8230;</span></div>';
    fetch('/api/usb', { method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: a }) })
      .then(function(r) { return r.json(); })
      .then(function() { usbRefresh(); })
      .catch(function() { usbRefresh(); });
  };
  /* stan pendrive'a widoczny w nagłówku nawet bez otwierania panelu */
  setInterval(function() {
    fetch('/api/usb').then(function(r) { return r.json(); })
      .then(function(d) {
        var b = document.getElementById('usb-btn');
        b.classList.toggle('warn', d.state === 'eject');
        b.classList.toggle('err', !d.present);
      }).catch(function() {});
  }, 15000);

  /* ── BLUETOOTH (panel jak w telefonie) ── */
  var _btTimer = null, _btVolTimer = null, _btMenuMac = null;
  var _btVolEditing = false;
  var BT_ICON = { speaker: '\u{1F50A}', headphones: '\u{1F3A7}',
                  phone: '\u{1F4F1}', car: '\u{1F697}', generic: '\u{1F535}' };

  function btPost(payload) {
    return fetch('/api/bluetooth', { method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload) })
      .then(function() { setTimeout(btRefresh, 400); });
  }

  window.btVolumePreview = function(v) {
    _btVolEditing = true;
    document.getElementById('bt-volume-value').textContent = v + '%';
  };

  window.btVolumeCommit = function(v) {
    _btVolEditing = false;
    fetch('/api/bluetooth/volume', { method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ volume: Number(v) }) })
      .then(function() { setTimeout(btVolumeRefresh, 300); });
  };

  function btVolumeRefresh() {
    fetch('/api/bluetooth/volume').then(function(r) { return r.json(); })
      .then(function(s) {
        var row = document.getElementById('bt-volume-row');
        row.classList.toggle('show', !!s.connected);
        if (!s.connected || s.volume == null || _btVolEditing) return;
        document.getElementById('bt-volume').value = s.volume;
        document.getElementById('bt-volume-value').textContent =
          s.volume + '%' + (s.muted ? ' M' : '');
      }).catch(function() {});
  }

  function btBattery(p) {
    if (p == null) return '';
    var col = p <= 20 ? 'var(--red)' : p <= 40 ? 'var(--yellow)' : 'var(--green)';
    return '<span class="bt-batt"><b><i style="width:' + p +
      '%;background:' + col + '"></i></b>' + p + '%</span>';
  }

  function btRow(d) {
    var sub, cls = '';
    if (d.connected) { cls = 'conn'; sub = 'Po&#322;&#261;czono'; }
    else if (d.paired) sub = 'Sparowane';
    else sub = 'Dost&#281;pne — dotknij, aby sparowa&#263;';
    var star = (d.mac === _btDefault) ?
      '<span class="bt-star" title="&#322;&#261;czy si&#281; automatycznie">&#9733;</span> ' : '';
    var batt = d.connected ? btBattery(d.battery) : '';
    var tap = d.paired ? "btAct('connect','" + d.mac + "')"
                       : "btAct('pair','" + d.mac + "')";
    if (d.connected) tap = "btAct('disconnect','" + d.mac + "')";
    var menu = d.paired ?
      '<div class="bt-act"><button class="bt-dots" onclick="event.stopPropagation();btMenu(\'' +
      d.mac + '\')">&#8942;</button><div class="bt-menu" id="btm-' + d.mac.replace(/:/g,'') + '">' +
      '<div onclick="btAct(\'default\',\'' + d.mac + '\')">' +
      (d.mac === _btDefault ? 'Nie &#322;&#261;cz automatycznie' : '&#322;&#261;cz automatycznie') + '</div>' +
      '<div onclick="btAct(\'' + (d.connected ? 'disconnect' : 'connect') + '\',\'' + d.mac + '\')">' +
      (d.connected ? 'Roz&#322;&#261;cz' : 'Po&#322;&#261;cz') + '</div>' +
      '<div style="color:var(--red)" onclick="btAct(\'remove\',\'' + d.mac + '\')">Zapomnij urz&#261;dzenie</div>' +
      '</div></div>' : '';
    return '<div class="bt-row ' + cls + '" onclick="' + tap + '">' +
      '<div class="bt-ico">' + (BT_ICON[d.icon] || BT_ICON.generic) + '</div>' +
      '<div class="bt-info"><div class="bt-name">' + star +
      (d.name || d.mac) + '</div><div class="bt-sub">' + sub +
      (batt ? ' &middot; ' : '') + batt + '</div></div>' + menu + '</div>';
  }

  var _btDefault = '';
  function btRender(s) {
    document.getElementById('bt-btn').classList.toggle('on',
      (s.devices || []).some(function(d) { return d.connected; }));
    document.getElementById('bt-power').checked = !!s.powered;
    _btDefault = s.default || '';
    var sb = document.getElementById('bt-scan-btn');
    sb.disabled = !!s.scanning;
    sb.innerHTML = s.scanning
      ? '<span class="bt-spin"></span> Szukam&#8230;'
      : '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M21 21l-4.3-4.3M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16z"/></svg> Szukaj urz&#261;dze&#324;';
    if (!s.powered) {
      document.getElementById('bt-body').innerHTML =
        '<div class="bt-empty">Bluetooth wy&#322;&#261;czony</div>';
      return;
    }
    var conn = (s.devices || []).filter(function(d) { return d.connected; });
    var paired = (s.devices || []).filter(function(d) { return d.paired && !d.connected; });
    var avail = (s.devices || []).filter(function(d) { return !d.paired; });
    var h = '';
    if (conn.length) h += '<div class="bt-sec">Po&#322;&#261;czone</div>' + conn.map(btRow).join('');
    h += '<div class="bt-sec">Moje urz&#261;dzenia</div>' +
      (paired.length ? paired.map(btRow).join('')
        : '<div class="bt-empty">Brak sparowanych. U&#380;yj „Szukaj".</div>');
    h += '<div class="bt-sec">Dost&#281;pne' +
      (s.scanning ? ' <span class="bt-spin"></span>' : '') + '</div>' +
      (avail.length ? avail.map(btRow).join('')
        : '<div class="bt-empty">' + (s.scanning ? 'Szukam&#8230;'
          : 'Naci&#347;nij „Szukaj". Urz&#261;dzenie musi by&#263; w trybie parowania.') + '</div>');
    document.getElementById('bt-body').innerHTML = h;
  }

  function btRefresh() {
    /* nie przerysowuj, gdy użytkownik ma otwarte menu urządzenia — inaczej
       auto-odświeżanie zamykałoby je pod palcem */
    if (document.querySelector('.bt-menu.open')) return;
    fetch('/api/bluetooth').then(function(r) { return r.json(); })
      .then(btRender).catch(function() {});
  }

  window.openBtModal = function() {
    document.getElementById('bt-modal').style.display = 'flex';
    btRefresh(); btVolumeRefresh();
    clearInterval(_btTimer);
    clearInterval(_btVolTimer);
    _btTimer = setInterval(btRefresh, 3000);
    _btVolTimer = setInterval(btVolumeRefresh, 1000);
  };
  window.closeBtModal = function() {
    clearInterval(_btTimer); _btTimer = null;
    clearInterval(_btVolTimer); _btVolTimer = null;
    document.getElementById('bt-modal').style.display = 'none';
  };
  window.btPower = function(on) { btPost({ action: 'power', on: on }); };
  window.btScan = function() { btPost({ action: 'scan' }); };
  window.btAct = function(a, mac) { _btMenuMac = null; btPost({ action: a, mac: mac }); };
  window.btMenu = function(mac) {
    var id = 'btm-' + mac.replace(/:/g, '');
    var el = document.getElementById(id);
    var open = el.classList.contains('open');
    document.querySelectorAll('.bt-menu').forEach(function(m) { m.classList.remove('open'); });
    if (!open) el.classList.add('open');
  };

  /* ── BAZY OBSERWOWANYCH (wiele baz, własny komunikat na bazę) ── */
  var _wlGroups = [], _wlSel = null, _wlNew = false, _wlBrandEx = [];

  /* podgląd tekstem: dokładnie to, co usłyszysz po naciśnięciu ▶ */
  function wlBrandHint() {
    var el = document.getElementById('wl-brand-ex');
    if (!el) return;
    var ex = _wlBrandEx.filter(function(x) { return x.gid === _wlSel; })[0]
             || _wlBrandEx[0];
    if (!ex) { el.textContent = ''; return; }
    el.innerHTML = 'przyk&#322;ad (' + ex.plate + (ex.note ? ' &middot; ' + ex.note : '') +
      '): <span style="color:var(--text)">„' + ex.text + '"</span>';
  }

  function wlRender(d) {
    document.getElementById('wl-ttsall').checked = !!d.tts_all;
    if (d.spell_mode) document.getElementById('wl-spell').value = d.spell_mode;
    if (d.tts_gain) document.getElementById('wl-gain').value =
      String(d.tts_gain == 1 ? 1 : (d.tts_gain < 2 ? 1.6 : 2.4));
    if (d.spell_example) document.getElementById('wl-spell-ex').textContent =
      'WA12345 → „' + d.spell_example + '"';
    document.getElementById('wl-ttsbrand').checked = !!d.tts_brand;
    _wlBrandEx = d.brand_examples || [];
    wlBrandHint();
    _wlGroups = d.groups || [];
    if (_wlNew) { /* tryb tworzenia — nie nadpisuj formularza */ }
    else {
      if (!_wlSel || !_wlGroups.some(function(g) { return g.gid === _wlSel; }))
        _wlSel = _wlGroups.length ? _wlGroups[0].gid : null;
      var g = _wlGroups.filter(function(x) { return x.gid === _wlSel; })[0];
      if (g) {
        document.getElementById('wl-gname').value = g.name || '';
        document.getElementById('wl-galert').value = g.alert || '';
        document.getElementById('wl-gcolor').value = g.color || 'red';
        document.getElementById('wl-genabled').checked = g.enabled !== false;
        document.getElementById('wl-gnota').checked = g.nota !== false;
      }
    }
    /* zakładki */
    var t = _wlGroups.map(function(g) {
      return '<div class="wl-tab' + (g.gid === _wlSel && !_wlNew ? ' act' : '') +
        (g.enabled === false ? ' off' : '') + '" onclick="wlSelect(\'' + g.gid +
        '\')">' + g.name + ' <span class="c">' + g.count + '</span></div>';
    }).join('');
    t += '<div class="wl-tab new' + (_wlNew ? ' act' : '') +
         '" onclick="wlNewGroup()">+ nowa baza</div>';
    document.getElementById('wl-tabs').innerHTML = t;
    _wlPlates = d.plates || [];
    wlReloadList();
  }

  var _wlPlates = [], WL_MAX_ROWS = 250;

  /* lista tablic wybranej bazy: filtr + limit renderowania (baza policji ma
     3200 wpisów — wrzucenie wszystkiego do DOM zamula przeglądarkę) */
  window.wlReloadList = function() {
    var q = (document.getElementById('wl-search').value || '')
              .trim().toUpperCase();
    var all = _wlPlates.filter(function(p) {
      return !_wlNew && p.group === _wlSel; });
    var rows = q ? all.filter(function(p) {
      return p.plate.indexOf(q) >= 0 ||
             (p.note || '').toUpperCase().indexOf(q) >= 0; }) : all;
    var shown = rows.slice(0, WL_MAX_ROWS);
    document.getElementById('wl-count').textContent =
      all.length ? (rows.length === all.length
        ? all.length + ' szt.'
        : rows.length + ' z ' + all.length) : '';
    var h = shown.map(function(it) {
      return '<div class="wl-row"><span class="p">' + it.plate + '</span>' +
        '<span class="n">' + (it.note || '') + '</span>' +
        '<span class="x" onclick="wlRemove(\'' + it.plate +
        '\')" title="Usu&#324;">&#10005;</span></div>';
    }).join('');
    if (rows.length > shown.length) {
      h += '<div style="padding:12px 16px;color:var(--dim);font-size:11px">' +
        'pokazano ' + shown.length + ' z ' + rows.length +
        ' — u&#380;yj wyszukiwarki, aby zaw&#281;zi&#263;</div>';
    }
    document.getElementById('wl-list').innerHTML = h ||
      '<div style="padding:16px;color:var(--dim);font-size:12px">' +
      (_wlNew ? 'Nazwij baz&#281; i zapisz, potem dodaj tablice.'
        : q ? 'Brak wynik&#243;w dla „' + q + '".'
            : 'Pusta baza — dodaj numer powy&#380;ej albo wklej list&#281; ' +
              'przyciskiem HURTEM.') + '</div>';
  };

  function wlPost(payload) {
    return fetch('/api/watchlist', { method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload) })
      .then(function(r) { return r.json(); })
      .then(function(d) { if (d.ok && d.groups) wlRender(d); return d; });
  }
  function wlReload() {
    return fetch('/api/watchlist').then(function(r) { return r.json(); })
      .then(wlRender).catch(function() {});
  }

  window.openWlModal = function() {
    document.getElementById('wl-modal').style.display = 'flex';
    _wlNew = false;
    wlReload();
  };
  window.closeWlModal = function() {
    document.getElementById('wl-modal').style.display = 'none';
  };
  window.wlSelect = function(gid) {
    _wlSel = gid; _wlNew = false;
    document.getElementById('wl-bulk').classList.remove('open');
    wlBrandHint();
    wlReload();
  };
  window.wlNewGroup = function() {
    _wlNew = true;
    document.getElementById('wl-gname').value = '';
    document.getElementById('wl-galert').value =
      'Uwaga! Tablica {tablica} z listy obserwowanych.';
    document.getElementById('wl-gcolor').value = 'orange';
    document.getElementById('wl-genabled').checked = true;
    document.getElementById('wl-gnota').checked = true;
    wlReload();
    setTimeout(function() { document.getElementById('wl-gname').focus(); }, 60);
  };
  window.wlGroupSave = function() {
    var name = document.getElementById('wl-gname').value.trim();
    if (!name) { alert('Podaj nazwę bazy'); return; }
    wlPost({ group_save: {
      gid: _wlNew ? '' : _wlSel, name: name,
      alert: document.getElementById('wl-galert').value.trim(),
      color: document.getElementById('wl-gcolor').value,
      nota: document.getElementById('wl-gnota').checked,
      enabled: document.getElementById('wl-genabled').checked } })
      .then(function() {
        if (_wlNew) {   /* wybierz nowo utworzoną */
          _wlNew = false;
          fetch('/api/watchlist').then(function(r) { return r.json(); })
            .then(function(d) {
              var g = (d.groups || []).filter(function(x) {
                return x.name === name; }).pop();
              if (g) _wlSel = g.gid;
              wlRender(d);
            });
        }
      });
  };
  window.wlGroupRemove = function() {
    if (_wlNew) { _wlNew = false; wlReload(); return; }
    var g = _wlGroups.filter(function(x) { return x.gid === _wlSel; })[0];
    if (!g) return;
    if (!confirm('Usunąć bazę "' + g.name + '"?\n' +
                 'Tablice (' + g.count + ') zostaną przeniesione do pierwszej bazy.'))
      return;
    wlPost({ group_remove: _wlSel, keep_plates: true }).then(function(d) {
      if (d && !d.ok) alert(d.error || 'Błąd');
      _wlSel = null; wlReload();
    });
  };
  function wlSaveCurrent() {
    return wlPost({ group_save: {
      gid: _wlSel, name: document.getElementById('wl-gname').value.trim(),
      alert: document.getElementById('wl-galert').value.trim(),
      color: document.getElementById('wl-gcolor').value,
      nota: document.getElementById('wl-gnota').checked,
      enabled: document.getElementById('wl-genabled').checked } });
  }

  /* odtwarza W PRZEGLĄDARCE (na komputerze/telefonie, z którego konfigurujesz) */
  window.wlPlayHere = function(url, btn) {
    var b = btn && document.getElementById(btn);
    var old = b ? b.innerHTML : '';
    if (b) { b.innerHTML = '&#8987; generuj&#281;&#8230;'; b.disabled = true; }
    var a = new Audio(url + (url.indexOf('?') < 0 ? '?' : '&') + 't=' + Date.now());
    a.onended = a.onerror = function() {
      if (b) { b.innerHTML = old; b.disabled = false; }
    };
    a.play().catch(function() {
      if (b) { b.innerHTML = old; b.disabled = false; }
      alert('Przeglądarka zablokowała dźwięk — kliknij jeszcze raz.');
    });
  };

  window.wlAlertTest = function() {
    /* POSŁUCHAJ = na tym komputerze; zapisz najpierw, by brzmiało jak wpisane */
    if (_wlNew) { alert('Zapisz bazę, potem posłuchaj'); return; }
    wlSaveCurrent().then(function() {
      wlPlayHere('/api/tts-preview?gid=' + encodeURIComponent(_wlSel), 'wl-play');
    });
  };
  window.wlAlertTestCar = function() {
    if (_wlNew) { alert('Zapisz bazę, potem przetestuj'); return; }
    wlSaveCurrent().then(function() { wlPost({ alert_test: _wlSel }); });
  };
  window.wlBulkToggle = function() {
    document.getElementById('wl-bulk').classList.toggle('open');
  };
  window.wlBulkImport = function() {
    var txt = document.getElementById('wl-bulk-text').value;
    if (!txt.trim()) return;
    var res = document.getElementById('wl-bulk-res');
    res.textContent = 'Importuję…'; res.style.color = 'var(--dim)';
    wlPost({ bulk: { text: txt, group: _wlSel } }).then(function(d) {
      if (!d || !d.ok) { res.textContent = 'Błąd importu';
                         res.style.color = 'var(--red)'; return; }
      res.innerHTML = '<span style="color:var(--green)">dodano ' + d.added +
        '</span>' + (d.dup ? ' · duplikaty ' + d.dup : '') +
        (d.bad ? ' · <span style="color:var(--red)">błędne ' + d.bad + '</span>' : '');
      document.getElementById('wl-bulk-text').value = '';
      wlReload();
    });
  };
  window.wlAdd = function() {
    var p = document.getElementById('wl-plate').value.trim();
    if (!p) return;
    if (_wlNew) { alert('Najpierw zapisz bazę'); return; }
    wlPost({ add: { plate: p, group: _wlSel,
                    note: document.getElementById('wl-note').value.trim() } })
      .then(function(d) {
        if (d && d.ok) {
          document.getElementById('wl-plate').value = '';
          document.getElementById('wl-note').value = '';
        } else if (d) { alert(d.error || 'Błąd'); }
      });
  };
  window.wlRemove = function(p) { wlPost({ remove: p }); };
  window.wlTtsAll = function(v) { wlPost({ tts_all: v }); };
  window.wlTtsBrand = function(v) { wlPost({ tts_brand: v }); };
  window.wlSpellMode = function(v) { wlPost({ spell_mode: v }); };
  window.wlGain = function(v) { wlPost({ tts_gain: parseFloat(v) }); };
  window.wlTtsTest = function() {
    wlPlayHere('/api/tts-preview?text=' + encodeURIComponent(
      'Cześć! Tu system rozpoznawania tablic. Test głosu.'), 'wl-ttsp');
  };
  window.wlTtsTestCar = function() { wlPost({ tts_test: 1 }); };
  document.getElementById('wl-plate').addEventListener('keydown',
    function(e) { if (e.key === 'Enter') wlAdd(); });

  /* ── LOGI ── */
  window.openLogModal = function() {
    document.getElementById('log-modal').style.display = 'flex';
    var pre = document.getElementById('log-pre');
    pre.textContent = 'Ladowanie...';
    fetch('/api/log?n=500')
      .then(function(r) { return r.text(); })
      .then(function(t) {
        pre.textContent = t || '(pusty log)';
        pre.scrollTop = pre.scrollHeight;   /* najnowsze na koncu */
      })
      .catch(function() { pre.textContent = 'Blad pobierania logu'; });
  };
  window.closeLogModal = function() {
    document.getElementById('log-modal').style.display = 'none';
  };

  /* ── RĘCZNE ZDJĘCIE ── */
  window.manualSnap = function() {
    var b = document.getElementById('snap-btn');
    fetch('/api/snap', { method: 'POST' })
      .then(function(r) { return r.json(); })
      .then(function(d) {
        b.classList.add('done');
        b.textContent = d.ok ? '✓ ZAPISANE' : '⏳ TRWA...';
        setTimeout(function() {
          b.classList.remove('done');
          b.innerHTML = '📷<span class="snap-txt"> FOTO</span>';
        }, 1500);
      })
      .catch(function() {});
  };

  /* ── ASYSTENT OSTROŚCI ── */
  var focusTimer = null;
  window.openFocusModal = function() {
    document.getElementById('focus-modal').style.display = 'flex';
    var img = document.getElementById('focus-img');
    function tick() { img.src = '/api/focus-view?t=' + Date.now(); }
    tick();
    focusTimer = setInterval(tick, 700);
  };
  window.closeFocusModal = function() {
    clearInterval(focusTimer);
    focusTimer = null;
    document.getElementById('focus-img').src = '';
    document.getElementById('focus-modal').style.display = 'none';
  };

  /* ── NAGRANIA (dashcam) ── */
  var _recData = [];

  function _sesLabel(s, withDate) {
    /* "20260716_094146" -> "09:41" lub "16.07 09:41" */
    var d = s.slice(6, 8) + '.' + s.slice(4, 6);
    var t = s.slice(9, 11) + ':' + s.slice(11, 13);
    return withDate ? d + ' ' + t : t;
  }

  window.openRecModal = function() {
    document.getElementById('rec-modal').style.display = 'flex';
    var body = document.getElementById('rec-body');
    body.innerHTML = '<div style="padding:14px;color:var(--dim)">Ladowanie...</div>';
    fetch('/api/recordings')
      .then(function(r) { return r.json(); })
      .then(function(list) {
        _recData = list;
        if (!list.length) {
          body.innerHTML = '<div style="padding:14px;color:var(--dim)">Brak nagran</div>';
          return;
        }
        body.innerHTML =
          '<div id="rec-filter">' +
          '<select id="rec-fday" onchange="recDayChanged()"></select>' +
          '<select id="rec-fses" onchange="renderRecList()"></select>' +
          '<span id="rec-fcount"></span></div>' +
          '<div id="rec-list"></div>';
        var days = [];
        list.forEach(function(it) {
          var d = it.mtime.slice(0, 10);
          if (days.indexOf(d) < 0) days.push(d);
        });
        days.sort().reverse();
        var fd = document.getElementById('rec-fday');
        fd.innerHTML = '<option value="">Wszystkie dni</option>' +
          days.map(function(d) {
            return '<option value="' + d + '">' +
                   d.slice(8) + '.' + d.slice(5, 7) + '.' + d.slice(0, 4) +
                   '</option>';
          }).join('');
        recDayChanged();
      })
      .catch(function() {
        body.innerHTML = '<div style="padding:14px;color:var(--red)">Blad pobierania listy</div>';
      });
  };

  window.recDayChanged = function() {
    var day = document.getElementById('rec-fday').value;
    var fs = document.getElementById('rec-fses');
    var ses = [];
    _recData.forEach(function(it) {
      if (day && it.mtime.slice(0, 10) !== day) return;
      if (ses.indexOf(it.sesja) < 0) ses.push(it.sesja);
    });
    ses.sort().reverse();
    fs.innerHTML = '<option value="">Wszystkie jazdy</option>' +
      ses.map(function(s) {
        return '<option value="' + s + '">jazda ' +
               _sesLabel(s, !day) + '</option>';
      }).join('');
    renderRecList();
  };

  window.renderRecList = function() {
    var day = document.getElementById('rec-fday').value;
    var ses = document.getElementById('rec-fses').value;
    var rows = _recData.filter(function(it) {
      if (day && it.mtime.slice(0, 10) !== day) return false;
      if (ses && it.sesja !== ses) return false;
      return true;
    });
    document.getElementById('rec-fcount').textContent =
      rows.length + ' segm.';
    var html = '';
    rows.forEach(function(it) {
      html += '<div class="rec-row" onclick="playRec(\'' + it.sesja +
        '\',\'' + it.plik + '\')">' +
        (it.thumb
          ? '<img loading="lazy" src="' + it.thumb + '" alt="">'
          : '<div class="rec-noimg">za chwil&#281;<br>podgl&#261;d&#8230;</div>') +
        '<div class="rec-info">' +
        '<div class="rec-time">' + it.mtime.slice(11, 16) +
        '<span style="color:var(--dim);font-weight:400"> &middot; ' +
        it.mtime.slice(8, 10) + '.' + it.mtime.slice(5, 7) + '</span></div>' +
        '<div class="rec-sub">jazda ' + _sesLabel(it.sesja, true) +
        ' &middot; ' + Math.round(it.size_mb) + ' MB</div></div>' +
        '<a class="rec-act" href="/api/rec/' + it.sesja + '/' + it.plik +
        '" onclick="event.stopPropagation()" title="Pobierz">&#x2B07;</a>' +
        '</div>';
    });
    document.getElementById('rec-list').innerHTML = html ||
      '<div style="padding:14px;color:var(--dim)">Brak nagran dla filtra</div>';
  };

  /* Odtwarzanie: gotowy maly MP4 (H.264) per segment — natywny <video>
     z paskiem przewijania; janitor robi je z gory, brakujace przygotowuja
     sie na klik (~30-60 s). Zadnego strumieniowania na zywo = zero lagow. */
  var _prepTimer = null;

  window.playRec = function(sesja, plik) {
    var pl = document.getElementById('rec-player');
    var v  = document.getElementById('rec-video');
    var st = document.getElementById('rec-pstat');
    pl.style.display = 'block';
    clearInterval(_prepTimer);
    v.pause(); v.removeAttribute('src'); v.load();
    v.style.display = 'none';
    st.textContent = 'Sprawdzam podglad...';

    function tick() {
      fetch('/api/rec-prep/' + sesja + '/' + plik, { method: 'POST' })
        .then(function(r) {
          if (r.status === 404) {
            clearInterval(_prepTimer);
            st.textContent = 'Nagranie zostało nadpisane przez rotację — odśwież listę';
            return null;
          }
          return r.json();
        })
        .then(function(d) {
          if (!d) return;
          if (d.ready) {
            clearInterval(_prepTimer);
            st.textContent = '';
            v.style.display = '';
            v.src = '/api/rec-prev/' + sesja + '/' +
                    plik.replace('.mkv', '_prev.mp4');
            v.playbackRate = 1;
            setRecSpeed(1);
            v.play().catch(function() {});
          } else {
            var pct = (typeof d.pct === 'number') ? d.pct : null;
            if (!document.getElementById('rec-pbar-fill')) {
              st.innerHTML = '<span id="rec-ptext"></span>' +
                '<div class="rec-pbar"><div id="rec-pbar-fill"></div></div>';
            }
            document.getElementById('rec-ptext').textContent =
              'Przygotowuję podgląd… ' +
              (pct !== null ? pct + '%' : (d.msg || ''));
            document.getElementById('rec-pbar-fill').style.width =
              (pct !== null ? pct : 2) + '%';
          }
        })
        .catch(function() { st.textContent = 'Błąd połączenia'; });
    }
    tick();
    _prepTimer = setInterval(tick, 2000);
    pl.scrollIntoView({ block: 'nearest' });
  };

  window.setRecSpeed = function(x) {
    var v = document.getElementById('rec-video');
    v.playbackRate = x;
    Array.prototype.forEach.call(document.querySelectorAll('.rec-spd'),
      function(b) { b.classList.toggle('act', parseFloat(b.dataset.x) === x); });
  };

  window.stopRecPlay = function() {
    clearInterval(_prepTimer);
    var v = document.getElementById('rec-video');
    v.pause(); v.removeAttribute('src'); v.load();
    document.getElementById('rec-pstat').textContent = '';
    document.getElementById('rec-player').style.display = 'none';
  };

  window.closeRecModal = function() {
    stopRecPlay();
    document.getElementById('rec-modal').style.display = 'none';
  };

  /* ── CAMERA SETTINGS ── */
  var CAM_LABELS = {
    brightness: 'Jasnosc',
    contrast: 'Kontrast',
    saturation: 'Nasycenie',
    hue: 'Barwa',
    white_balance_automatic: 'Auto balans bieli',
    gamma: 'Gamma',
    gain: 'Wzmocnienie',
    power_line_frequency: 'Czest. sieci (migot.)',
    white_balance_temperature: 'Temperatura bieli',
    sharpness: 'Ostrosc',
    backlight_compensation: 'Komp. podswietlenia',
    auto_exposure: 'Tryb ekspozycji',
    exposure_time_absolute: 'Czas ekspozycji',
    exposure_dynamic_framerate: 'Dyn. FPS ekspozycji'
  };

  window.openCameraModal = function() {
    clearInterval(window._camTimer);
    document.getElementById('cam-modal').style.display = 'flex';
    document.getElementById('cam-body').innerHTML =
      '<div style="padding:20px 14px;color:var(--dim);font-size:12px">Ladowanie...</div>';
    document.getElementById('cam-status').textContent = '';
    document.getElementById('cam-status').className = '';
    fetch('/api/camera')
      .then(function(r) { return r.json(); })
      .then(function(d) {
        if (!d.ok) throw new Error('Blad API');
        window._camCsi = !!d.csi;
        document.getElementById('cam-ftr').style.display = '';
        if (d.csi) {
          renderCsiControls(d.settings || {}, d.meta || {});
          /* odczyt "Aktualnie" odswieza sie sam, dopoki panel otwarty
             (tylko ten wiersz — suwaki zostaja nietkniete) */
          window._camTimer = setInterval(function() {
            fetch('/api/camera')
              .then(function(r) { return r.json(); })
              .then(function(dd) {
                var el = document.getElementById('csi-now');
                if (el && dd.meta) el.textContent = _csiNowText(dd.meta);
              })
              .catch(function() {});
          }, 1500);
          return;
        }
        renderCamControls(d.controls);
      })
      .catch(function(e) {
        document.getElementById('cam-body').innerHTML =
          '<div style="padding:14px;color:var(--red)">Blad: ' + e.message + '</div>';
      });
  };

  window.closeCameraModal = function() {
    clearInterval(window._camTimer);
    document.getElementById('cam-modal').style.display = 'none';
  };

  function renderCamControls(ctrls) {
    var html = '';
    var order = [
      'auto_exposure','exposure_time_absolute','exposure_dynamic_framerate',
      'brightness','contrast','saturation','hue','sharpness','gamma','gain',
      'backlight_compensation','power_line_frequency',
      'white_balance_automatic','white_balance_temperature'
    ];
    var names = order.filter(function(n) { return ctrls[n]; });
    Object.keys(ctrls).forEach(function(n) {
      if (names.indexOf(n) < 0) names.push(n);
    });
    names.forEach(function(name) {
      var c = ctrls[name];
      if (!c) return;
      var label = CAM_LABELS[name] || name;
      var inact = c.inactive ? ' cam-row inactive' : '';
      html += '<div class="cam-row' + inact + '">';
      html += '<span class="cam-label">' + label + '</span>';
      if (c.type === 'bool') {
        html += '<label class="cam-toggle"><input type="checkbox" data-ctrl="' + name +
                '" ' + (c.value ? 'checked' : '') + '>' +
                '<span class="cam-toggle-track"></span></label>';
      } else if (c.type === 'menu' && c.options) {
        html += '<select data-ctrl="' + name + '">';
        Object.keys(c.options).sort(function(a,b){return a-b;}).forEach(function(k) {
          html += '<option value="' + k + '"' +
                  (parseInt(k) === c.value ? ' selected' : '') + '>' +
                  c.options[k] + '</option>';
        });
        html += '</select>';
      } else if (c.type === 'int') {
        html += '<div class="cam-slider-wrap">' +
                '<input type="range" data-ctrl="' + name + '" min="' + c.min +
                '" max="' + c.max + '" step="' + c.step + '" value="' + c.value +
                '" oninput="this.nextElementSibling.textContent=this.value">' +
                '<span class="cam-val">' + c.value + '</span></div>';
      }
      html += '</div>';
    });
    document.getElementById('cam-body').innerHTML = html;
  }

  window.resetCameraAuto = function() {
    var st = document.getElementById('cam-status');
    st.textContent = 'Przywracanie auto...';
    st.className = '';
    fetch('/api/camera', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ reset_auto: 1 })
    })
    .then(function(r) { return r.json(); })
    .then(function(d) {
      st.textContent = d.ok ? '✓ Tryb auto przywrócony (zapisane)' : 'Błąd';
      st.className = d.ok ? 'ok' : 'err';
      openCameraModal();   /* odśwież wartości */
    })
    .catch(function() { st.textContent = 'Błąd'; st.className = 'err'; });
  };

  /* panel kamery CSI: automatyka libcamera z realnym sterowaniem —
     tryb AE, kompensacja EV jak w aparatach, pomiar, pelny manual */
  function _csiShutter(us) {
    if (!us) return '—';
    return us >= 1000000 ? (us / 1000000).toFixed(1) + ' s'
                         : '1/' + Math.round(1000000 / us) + ' s';
  }

  function _csiNowText(m) {
    return _csiShutter(m.exposure_us) +
      ' · gain ' + (m.gain ? m.gain.toFixed(1) : '—') +
      (m.lux ? ' · ~' + Math.round(m.lux) + ' lx' : '');
  }

  function renderCsiControls(s, m) {
    var html =
      '<div class="cam-row" style="color:var(--dim)">' +
      '<span class="cam-label">Aktualnie</span>' +
      '<span style="font-size:11px" id="csi-now">' + _csiNowText(m) +
      '</span></div>' +

      '<div class="cam-row"><span class="cam-label">Tryb</span>' +
      '<select id="csi-mode">' +
      '<option value="short">Auto — kr&#243;tkie czasy (zalecane)</option>' +
      '<option value="normal">Auto — normalny</option>' +
      '<option value="long">Auto — d&#322;ugie czasy</option>' +
      '<option value="manual">R&#281;czny</option>' +
      '</select></div>' +

      '<div class="cam-row csi-auto"><span class="cam-label">Ekspozycja (EV)</span>' +
      '<div class="cam-slider-wrap">' +
      '<input type="range" id="csi-ev" min="-4" max="4" step="0.5" ' +
      'oninput="this.nextElementSibling.textContent=(this.value>0?\'+\':\'\')+this.value">' +
      '<span class="cam-val"></span></div></div>' +

      '<div class="cam-row csi-auto"><span class="cam-label">Pomiar &#347;wiat&#322;a</span>' +
      '<select id="csi-met">' +
      '<option value="centre">centralny (&#347;rodek kadru)</option>' +
      '<option value="spot">punktowy</option>' +
      '<option value="matrix">matrycowy (ca&#322;y kadr)</option>' +
      '</select></div>' +

      '<div class="cam-row csi-auto"><span class="cam-label">Migawka max ' +
      '<small style="color:var(--dim)">(od restartu)</small></span>' +
      '<select id="csi-maxsh">' +
      '<option value="0">bez limitu (do 1/30)</option>' +
      '<option value="16666">1/60 s</option>' +
      '<option value="8333">1/120 s</option>' +
      '<option value="4000">1/250 s</option>' +
      '</select></div>' +

      '<div class="cam-row csi-man"><span class="cam-label">Migawka</span>' +
      '<div class="cam-slider-wrap">' +
      '<input type="range" id="csi-exp" min="100" max="33000" step="100" ' +
      'oninput="this.nextElementSibling.textContent=csiShutterLbl(this.value)">' +
      '<span class="cam-val"></span></div></div>' +

      '<div class="cam-row csi-man"><span class="cam-label">Wzmocnienie</span>' +
      '<div class="cam-slider-wrap">' +
      '<input type="range" id="csi-gain" min="1" max="16" step="0.5" ' +
      'oninput="this.nextElementSibling.textContent=\'x\'+this.value">' +
      '<span class="cam-val"></span></div></div>';

    document.getElementById('cam-body').innerHTML = html;
    document.getElementById('csi-mode').value = s.manual ? 'manual'
                                                         : (s.ae_mode || 'short');
    var ev = document.getElementById('csi-ev');
    ev.value = s.ev || 0;
    ev.nextElementSibling.textContent = (ev.value > 0 ? '+' : '') + ev.value;
    document.getElementById('csi-met').value = s.metering || 'centre';
    document.getElementById('csi-maxsh').value =
      String(s.max_shutter_us !== undefined ? s.max_shutter_us : 16666);
    var ex = document.getElementById('csi-exp');
    ex.value = s.exposure_us || 4000;
    ex.nextElementSibling.textContent = csiShutterLbl(ex.value);
    var gn = document.getElementById('csi-gain');
    gn.value = s.gain || 4;
    gn.nextElementSibling.textContent = 'x' + gn.value;
    document.getElementById('csi-mode').onchange = function() {
      csiModeChanged(); saveCameraSettings();
    };
    csiModeChanged();
    /* AUTO-ZAPIS: zmiana suwaka/selecta od razu trafia na kamere (bez "ZAPISZ").
       'change' na suwaku odpala po puszczeniu, wiec nie spamuje w trakcie ruchu. */
    ['csi-ev','csi-exp','csi-gain','csi-met','csi-maxsh'].forEach(function(id){
      var el = document.getElementById(id);
      if (el) el.addEventListener('change', saveCameraSettings);
    });
  }

  window.csiShutterLbl = function(us) { return _csiShutter(parseInt(us, 10)); };

  window.csiModeChanged = function() {
    var manual = document.getElementById('csi-mode').value === 'manual';
    document.querySelectorAll('.csi-auto').forEach(function(r) {
      r.style.display = manual ? 'none' : '';
    });
    document.querySelectorAll('.csi-man').forEach(function(r) {
      r.style.display = manual ? '' : 'none';
    });
  };

  window.saveCameraSettings = function() {
    var payload = {};
    if (window._camCsi) {
      var mode = document.getElementById('csi-mode').value;
      payload = {
        manual: mode === 'manual',
        ae_mode: mode === 'manual' ? 'short' : mode,
        ev: parseFloat(document.getElementById('csi-ev').value),
        metering: document.getElementById('csi-met').value,
        max_shutter_us: parseInt(document.getElementById('csi-maxsh').value, 10),
        exposure_us: parseInt(document.getElementById('csi-exp').value, 10),
        gain: parseFloat(document.getElementById('csi-gain').value)
      };
    } else {
    document.querySelectorAll('#cam-body [data-ctrl]').forEach(function(el) {
      var name = el.getAttribute('data-ctrl');
      if (el.type === 'checkbox') payload[name] = el.checked ? 1 : 0;
      else payload[name] = parseInt(el.value, 10);
    });
    }
    var st = document.getElementById('cam-status');
    st.textContent = 'Zapisywanie...';
    st.className = '';
    fetch('/api/camera', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload)
    })
    .then(function(r) { return r.json(); })
    .then(function(d) {
      if (d.ok) {
        st.textContent = '✓ Zapisano';
        st.className = 'ok';
      } else {
        st.textContent = (d.errors || ['Blad']).join(' | ').slice(0, 80);
        st.className = 'err';
      }
      setTimeout(function() { st.textContent = ''; st.className = ''; }, 4000);
    })
    .catch(function(e) {
      st.textContent = 'Blad: ' + e.message;
      st.className = 'err';
    });
  };

  /* ── STREAM ── */
  function initStream() {
    var img = document.getElementById('stream-img');
    var noSignal = document.getElementById('no-signal');

    if (isIOS) {
      img.src = '/api/snapshot?' + Date.now();
      img.style.display = 'block';
      noSignal.style.display = 'none';
      snapshotInterval = setInterval(function() {
        var newImg = new Image();
        newImg.onload = function() {
          img.src = this.src;
          img.style.display = 'block';
          noSignal.style.display = 'none';
        };
        newImg.onerror = function() {
          img.style.display = 'none';
          noSignal.style.display = 'block';
        };
        newImg.src = '/api/snapshot?' + Date.now();
      }, 400);
    } else {
      img.src = '/stream';
      img.style.display = 'block';
      noSignal.style.display = 'none';
      img.onerror = function() {
        this.style.display = 'none';
        noSignal.style.display = 'block';
        setTimeout(function() {
          img.src = '/stream?' + Date.now();
          img.style.display = 'block';
        }, 5000);
      };
    }
  }

  /* ── LIGHTBOX ── */
  var _lbId = null;   /* id aktualnie otwartego podglądu (chroni przed spóźnionym locate) */

  function _placeBox(imgId, boxId, b) {
    var img = document.getElementById(imgId);
    var box = document.getElementById(boxId);
    var place = function() {
      var W = img.offsetWidth, H = img.offsetHeight;
      if (!W || !H) return;
      box.style.left   = (b[0] * W) + 'px';
      box.style.top    = (b[1] * H) + 'px';
      box.style.width  = ((b[2] - b[0]) * W) + 'px';
      box.style.height = ((b[3] - b[1]) * H) + 'px';
      box.style.display = 'block';
    };
    if (img.complete && img.offsetWidth) place();
    else img.onload = place;
  }

  function _lbShowBox(b) { _placeBox('lightbox-img', 'lightbox-box', b); }

  /* ── RĘCZNA KOREKTA RAMKI (rysowanie myszką/dotykiem) ── */
  var bbx = { mode: null, start: null, tmp: null };
  var _lbItem = null;

  function _bbxCfg(mode) {
    return mode === 'rv'
      ? { img: 'rv-full', box: 'rv-box', btn: 'rv-boxfix', wrap: 'reviewer',
          imgwrap: 'rv-imgwrap', item: function() { return rvCurrent; } }
      : { img: 'lightbox-img', box: 'lightbox-box', btn: 'lb-boxfix', wrap: 'lightbox',
          imgwrap: 'lightbox-imgwrap', item: function() { return _lbItem; } };
  }

  /* zoom/pan w trybie rysowania ramki: pinch (2 palce) i kółko/gest touchpada.
     Transform na wrapperze — ramka skaluje się razem z obrazem, a przeliczanie
     współrzędnych działa bez zmian (getBoundingClientRect uwzględnia transform). */
  var _zoom = { s: 1, tx: 0, ty: 0, base: null, wrapEl: null };

  function _zoomApply() {
    if (_zoom.wrapEl) {
      _zoom.wrapEl.style.transformOrigin = '0 0';
      _zoom.wrapEl.style.transform =
        'translate(' + _zoom.tx + 'px,' + _zoom.ty + 'px) scale(' + _zoom.s + ')';
    }
  }

  function _zoomReset(wrapEl) {
    if (_zoom.wrapEl && _zoom.wrapEl !== wrapEl) _zoom.wrapEl.style.transform = '';
    _zoom.s = 1; _zoom.tx = 0; _zoom.ty = 0; _zoom.base = null;
    _zoom.wrapEl = wrapEl || null;
    if (wrapEl) wrapEl.style.transform = '';
  }

  function _zoomAt(cx, cy, factor) {
    if (!_zoom.wrapEl) return;
    if (!_zoom.base) {
      var t = _zoom.wrapEl.style.transform;
      _zoom.wrapEl.style.transform = '';
      _zoom.base = _zoom.wrapEl.getBoundingClientRect();
      _zoom.wrapEl.style.transform = t;
    }
    var ns = Math.min(8, Math.max(1, _zoom.s * factor));
    var k = ns / _zoom.s;
    _zoom.tx = (cx - _zoom.base.left) - k * ((cx - _zoom.base.left) - _zoom.tx);
    _zoom.ty = (cy - _zoom.base.top) - k * ((cy - _zoom.base.top) - _zoom.ty);
    _zoom.s = ns;
    if (_zoom.s === 1) { _zoom.tx = 0; _zoom.ty = 0; }
    _zoomApply();
  }

  function _cropDataUrl(imgEl, b) {
    var iw = imgEl.naturalWidth, ih = imgEl.naturalHeight;
    if (!iw || !ih) return null;
    var c = document.createElement('canvas');
    c.width = Math.max(2, Math.round((b[2] - b[0]) * iw));
    c.height = Math.max(2, Math.round((b[3] - b[1]) * ih));
    c.getContext('2d').drawImage(imgEl, b[0] * iw, b[1] * ih,
                                 (b[2] - b[0]) * iw, (b[3] - b[1]) * ih,
                                 0, 0, c.width, c.height);
    return c.toDataURL('image/jpeg', 0.92);
  }

  window.bboxStart = function(mode) {
    if (bbx.mode === mode) {           /* drugi klik = anuluj tryb */
      bbx.mode = null; bbx.start = null;
      var c0 = _bbxCfg(mode);
      document.getElementById(c0.img).style.cursor = '';
      document.getElementById(c0.btn).classList.remove('armed');
      document.getElementById(c0.wrap).classList.remove('boxfix');
      _zoomReset(null);
      return;
    }
    bbx.mode = mode; bbx.start = null; bbx.tmp = null;
    var c = _bbxCfg(mode);
    document.getElementById(c.img).style.cursor = 'crosshair';
    document.getElementById(c.btn).classList.add('armed');
    /* tryb rysowania = klatka na pelnym ekranie */
    document.getElementById(c.wrap).classList.add('boxfix');
    _zoomReset(document.getElementById(c.imgwrap));
  };

  function _bbxPoint(e, img) {
    var r = img.getBoundingClientRect();
    var cx = (e.touches && e.touches.length) ? e.touches[0].clientX : e.clientX;
    var cy = (e.touches && e.touches.length) ? e.touches[0].clientY : e.clientY;
    return [Math.max(0, Math.min(1, (cx - r.left) / r.width)),
            Math.max(0, Math.min(1, (cy - r.top) / r.height))];
  }

  function _bbxAttach(mode) {
    var c = _bbxCfg(mode);
    var img = document.getElementById(c.img);
    var box = document.getElementById(c.box);
    var pinch = null;   /* {d: odleglosc palcow, mx, my: srodek} */
    function down(e) {
      if (bbx.mode !== mode) return;
      e.preventDefault(); e.stopPropagation();
      if (e.touches && e.touches.length >= 2) {
        /* drugi palec = koniec rysowania, zaczyna sie pinch-zoom */
        bbx.start = null; bbx.tmp = null;
        var dx = e.touches[0].clientX - e.touches[1].clientX;
        var dy = e.touches[0].clientY - e.touches[1].clientY;
        pinch = { d: Math.sqrt(dx * dx + dy * dy) || 1,
                  mx: (e.touches[0].clientX + e.touches[1].clientX) / 2,
                  my: (e.touches[0].clientY + e.touches[1].clientY) / 2 };
        return;
      }
      bbx.start = _bbxPoint(e, img);
    }
    function mv(e) {
      if (bbx.mode !== mode) return;
      if (e.touches && e.touches.length >= 2 && pinch) {
        e.preventDefault();
        var dx = e.touches[0].clientX - e.touches[1].clientX;
        var dy = e.touches[0].clientY - e.touches[1].clientY;
        var d = Math.sqrt(dx * dx + dy * dy) || 1;
        var mx = (e.touches[0].clientX + e.touches[1].clientX) / 2;
        var my = (e.touches[0].clientY + e.touches[1].clientY) / 2;
        _zoomAt(pinch.mx, pinch.my, d / pinch.d);
        _zoom.tx += mx - pinch.mx;   /* przesuwanie srodkiem gestu */
        _zoom.ty += my - pinch.my;
        _zoomApply();
        pinch = { d: d, mx: mx, my: my };
        return;
      }
      if (!bbx.start) return;
      e.preventDefault();
      var p = _bbxPoint(e, img);
      bbx.tmp = [Math.min(bbx.start[0], p[0]), Math.min(bbx.start[1], p[1]),
                 Math.max(bbx.start[0], p[0]), Math.max(bbx.start[1], p[1])];
      box.className = 'fixed';
      _placeBox(c.img, c.box, bbx.tmp);
    }
    function wheel(e) {
      if (bbx.mode !== mode) return;
      e.preventDefault();
      /* touchpad-pinch przychodzi jako ctrl+wheel; zwykle kolko tez zoomuje */
      var f = Math.exp(-e.deltaY * (e.ctrlKey ? 0.01 : 0.002));
      _zoomAt(e.clientX, e.clientY, f);
    }
    function up(e) {
      if (bbx.mode !== mode) return;
      if (e.touches && e.touches.length) {  /* zostal palec po pinchu */
        pinch = null;
        return;
      }
      pinch = null;
      if (!bbx.start) return;
      var b = bbx.tmp;
      bbx.start = null; bbx.mode = null; bbx.tmp = null;
      img.style.cursor = '';
      document.getElementById(c.btn).classList.remove('armed');
      document.getElementById(c.wrap).classList.remove('boxfix');
      _zoomReset(null);
      var it = c.item();
      if (!it || !b || (b[2] - b[0]) < 0.01 || (b[3] - b[1]) < 0.008) {
        box.className = '';
        /* przy anulowaniu przywroc te ramke, ktora byla */
        if (it && (it.vbbox || it.bbox)) {
          box.className = it.vbbox ? 'fixed' : '';
          _placeBox(c.img, c.box, it.vbbox || it.bbox);
        }
        return;
      }
      it.vbbox = b;
      box.className = 'fixed';
      _placeBox(c.img, c.box, b);
      /* zblizenie = dokladnie zaznaczony przeze mnie obszar */
      var du = null;
      try { du = _cropDataUrl(img, b); } catch (err) {}
      if (du) {
        if (mode === 'rv') {
          Array.prototype.slice.call(
            document.querySelectorAll('#rv-col-best img'))
            .forEach(function(n) { n.src = du; });
        } else {
          document.getElementById('lightbox-crop').src = du;
        }
      }
      fetch('/api/verify', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: it.id, verdict: 'bbox', bbox: b })
      }).catch(function() {});
    }
    img.addEventListener('mousedown', down);
    document.addEventListener('mousemove', mv);
    document.addEventListener('mouseup', up);
    img.addEventListener('touchstart', down, { passive: false });
    document.addEventListener('touchmove', mv, { passive: false });
    document.addEventListener('touchend', up);
    document.getElementById(c.wrap)
      .addEventListener('wheel', wheel, { passive: false });
  }
  _bbxAttach('rv');
  _bbxAttach('lb');

  window.openLightbox = function(item) {
    var full = document.getElementById('lightbox-img');
    var crop = document.getElementById('lightbox-crop');
    var lbBox = document.getElementById('lightbox-box');
    lbBox.style.display = 'none';
    lbBox.className = '';
    _lbId = (item.id !== undefined) ? item.id : null;
    _lbItem = item;
    full.src = item.full_url || item.crop_url || '';
    full.style.display = full.src ? '' : 'none';
    crop.src = (item.full_url && item.crop_url) ? item.crop_url : '';
    crop.style.display = crop.src ? '' : 'none';
    /* ramka: ręczna korekta (pomarańczowa) > baza > serwerowe odnalezienie */
    if (item.full_url && item.crop_url) {
      if (item.vbbox) {
        lbBox.className = 'fixed';
        _lbShowBox(item.vbbox);
      } else if (item.bbox) {
        _lbShowBox(item.bbox);
      } else if (item.id !== undefined) {
        (function(reqId) {
          fetch('/api/locate/' + reqId)
            .then(function(r) { return r.json(); })
            .then(function(d) {
              if (d.ok && d.bbox && _lbId === reqId) { item.bbox = d.bbox; _lbShowBox(d.bbox); }
            })
            .catch(function() {});
        })(item.id);
      }
    }
    var plate = (item.plate || 'UNKNOWN').trim().toUpperCase();
    var num = item.id !== undefined ? ('#' + item.id + '   ') : '';
    document.getElementById('lightbox-plate').textContent = num + plate;
    document.getElementById('lightbox').classList.add('open');
  };

  window.closeLightbox = function(e, force) {
    if (force || !e || e.target === document.getElementById('lightbox')) {
      document.getElementById('lightbox').classList.remove('open');
      document.getElementById('lightbox-img').src = '';
      document.getElementById('lightbox-crop').src = '';
      document.getElementById('lightbox-box').style.display = 'none';
      _lbId = null;
    }
  };

  /* ── EDYTOR WERYFIKACJI ── */
  var rvActive = false, rvStartId = 0, rvCurrent = null, rvSeen = {};
  var rvHistory = [];   /* stos ocenionych — „POPRZEDNIE" cofa do ostatniego */

  window.reviewerOpen = function() {
    rvActive = true;
    rvStartId = 0;
    for (var i = 0; i < allRows.length; i++)
      if (allRows[i].id !== undefined) rvStartId = Math.max(rvStartId, allRows[i].id);
    rvSeen = {};
    rvCurrent = null;
    rvHistory = [];
    rvBacklog = [];
    rvBacklogTotal = 0;
    document.getElementById('rv-cands-toggle').textContent =
      'KANDYDACI: ' + (localStorage.getItem('lpr_cands') !== '0' ? 'WL' : 'WYL');
    document.getElementById('reviewer').classList.add('open');
    rvRefresh();
  };

  window.reviewerClose = function() {
    rvActive = false;
    document.getElementById('reviewer').classList.remove('open');
    document.getElementById('rv-stream').src = '';
    document.getElementById('rv-full').src = '';
    ['rv-col-prev', 'rv-col-best', 'rv-col-next'].forEach(function(id) {
      Array.prototype.slice.call(
        document.getElementById(id).querySelectorAll('img'))
        .forEach(function(n) { n.remove(); });
    });
    document.getElementById('rv-bonus').innerHTML = '';
  };

  window.rvToggleCands = function() {
    var on = localStorage.getItem('lpr_cands') !== '0';
    localStorage.setItem('lpr_cands', on ? '0' : '1');
    document.getElementById('rv-cands-toggle').textContent =
      'KANDYDACI: ' + (on ? 'WYL' : 'WL');
    if (rvCurrent) { var it = rvCurrent; rvCurrent = null; rvShow(it); }
  };

  function rvQueue() {
    var q = [];
    for (var i = allRows.length - 1; i >= 0; i--) {   /* od najstarszego z nowych */
      var it = allRows[i];
      if (it.id !== undefined && it.id > rvStartId && !it.verify && !rvSeen[it.id]) q.push(it);
    }
    return q;
  }

  /* Klatka wskazana recznie jako lepsza od zapisanej — nazwa pliku z bazy.
     Trzymamy URL, zeby latwo porownac z tym, co wisi w kolumnach. */
  var rvLepszaUrl = null;

  window.rvWybierzKlatke = function(url) {
    if (!rvCurrent || rvCurrent.id === undefined) return;
    var nowa = (rvLepszaUrl === url) ? '' : url;   /* ponowny klik = cofniecie */
    var plik = nowa ? nowa.split('/').pop() : '';
    fetch('/api/verify', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: rvCurrent.id, verdict: 'lepsza', plik: plik })
    }).then(function(r) { return r.json(); })
      .then(function(d) {
        if (!d || d.ok === false) return;
        rvLepszaUrl = nowa || null;
        rvCurrent.lepsza = plik || null;
        var it = rvCurrent; rvCurrent = null; rvShow(it);   /* przerysuj */
      })
      .catch(function() {});
  };

  function rvShow(it) {
    rvCurrent = it;
    document.getElementById('rv-wait').style.display = 'none';
    document.getElementById('rv-body').style.display = 'flex';
    document.getElementById('rv-btns').style.display = 'grid';
    var st = document.getElementById('rv-stream');
    if (st.getAttribute('src')) st.src = '';
    document.getElementById('rv-full').src = it.full_url || it.crop_url || '';
    rvTune('reset');       /* kazde zdjecie zaczyna od oryginalu */
    document.getElementById('rv-plate-num').textContent =
      (it.plate || 'UNKNOWN').trim().toUpperCase();
    document.getElementById('rv-sid').textContent =
      '#' + it.id + (it.seen_at ? ('   ' + it.seen_at) : '');
    /* kolumny kandydatów: starsza | najlepsza | nowsza (każda w 3 rozmiarach);
       km1/km2 = klatki przed najlepszą, kp1/kp2 = po niej */
    var kv = {}, legacy = [];
    (it.crops || []).forEach(function(u) {
      var m = u.match(/_(km2|km1|kp1|kp2)\.jpg$/);
      if (m) kv[m[1]] = u; else legacy.push(u);
    });
    function fillCol(colId, url) {
      var col = document.getElementById(colId);
      Array.prototype.slice.call(col.querySelectorAll('img'))
        .forEach(function(n) { n.remove(); });
      if (!url) return;
      // Klikniecie klatki = „TA jest lepsza od zapisanej". Bywa, ze sasiednia
      // jest ostrzejsza, a to wlasnie ona powinna trafic do uczenia.
      // Ponowne klikniecie tej samej cofa wybor.
      ['s1', 's2', 's3'].forEach(function(cls) {
        var im = document.createElement('img');
        im.src = url;
        im.className = cls;
        im.title = 'Kliknij: ta klatka jest lepsza (trafi do uczenia)';
        im.style.cursor = 'pointer';
        im.onclick = function() { rvWybierzKlatke(url); };
        col.appendChild(im);
      });
      if (rvLepszaUrl && url === rvLepszaUrl) col.classList.add('rv-wybrana');
      else col.classList.remove('rv-wybrana');
    }
    // ktora klatka jest oznaczona jako lepsza (nazwa pliku z bazy -> URL)
    rvLepszaUrl = null;
    if (it.lepsza) {
      var wszystkie = [it.crop_url].concat(it.crops || []);
      for (var wi = 0; wi < wszystkie.length; wi++) {
        if (wszystkie[wi] && wszystkie[wi].split('/').pop() === it.lepsza) {
          rvLepszaUrl = wszystkie[wi];
          break;
        }
      }
    }
    var showCands = localStorage.getItem('lpr_cands') !== '0';
    document.getElementById('rv-col-prev').style.display = showCands ? '' : 'none';
    document.getElementById('rv-col-next').style.display = showCands ? '' : 'none';
    fillCol('rv-col-best', it.crop_url);
    fillCol('rv-col-prev', kv.km1 || kv.km2);
    fillCol('rv-col-next', kv.kp1 || kv.kp2);
    /* bonus: pozostałe klatki w miniaturze */
    var bonus = document.getElementById('rv-bonus');
    bonus.innerHTML = '';
    if (showCands) {
      var extras = [];
      if (kv.km2 && kv.km1) extras.push(kv.km2);
      if (kv.kp2 && kv.kp1) extras.push(kv.kp2);
      extras = extras.concat(legacy);
      extras.forEach(function(u) {
        var im = document.createElement('img');
        im.src = u;
        im.title = 'Kliknij: ta klatka jest lepsza (trafi do uczenia)';
        im.style.cursor = 'pointer';
        if (rvLepszaUrl && u === rvLepszaUrl) im.classList.add('wybrana');
        im.onclick = function() { rvWybierzKlatke(u); };
        bonus.appendChild(im);
      });
    }
    /* klatki dopiero co powstaly — nalozyc na nie biezace ustawienie obrazu
       (inaczej po zmianie EV nowe miniatury byly nietkniete) */
    if (window.rvTuneOdswiez) window.rvTuneOdswiez();
    var rvBox = document.getElementById('rv-box');
    rvBox.style.display = 'none';
    rvBox.className = '';
    if (it.vbbox) {
      rvBox.className = 'fixed';
      _placeBox('rv-full', 'rv-box', it.vbbox);
    } else if (it.bbox) {
      _placeBox('rv-full', 'rv-box', it.bbox);
    } else if (it.id !== undefined) {
      (function(reqId) {
        fetch('/api/locate/' + reqId)
          .then(function(r) { return r.json(); })
          .then(function(d) {
            if (d.ok && d.bbox && rvCurrent && rvCurrent.id === reqId) {
              it.bbox = d.bbox;
              _placeBox('rv-full', 'rv-box', d.bbox);
            }
          })
          .catch(function() {});
      })(it.id);
    }
  }

  var rvPrevQ = 0;

  /* ZALEGŁOŚCI: gdy bieżąca kolejka pusta, bierzemy najnowszy NIEOCENIONY
     zapis z całej bazy — tak da się przerobić wszystko wstecz. */
  var rvBacklog = [], rvBacklogTotal = 0, rvBacklogBusy = false;

  function rvFetchBacklog(cb) {
    if (rvBacklogBusy) return;
    rvBacklogBusy = true;
    fetch('/api/unreviewed?limit=40')
      .then(function(r) { return r.json(); })
      .then(function(d) {
        rvBacklogTotal = d.total || 0;
        rvBacklog = (d.items || []).filter(function(it) { return !rvSeen[it.id]; });
        rvBacklogBusy = false;
        if (cb) cb();
      })
      .catch(function() { rvBacklogBusy = false; });
  }

  window.rvRefresh = function() {
    if (!rvActive) return;
    var q = rvQueue();
    /* wibracja telefonu, gdy w edytorze pojawi się nowe rozpoznanie */
    if (q.length > rvPrevQ && navigator.vibrate) {
      try { navigator.vibrate(150); } catch (e) {}
    }
    rvPrevQ = q.length;
    var cnt = document.getElementById('rv-count');
    if (q.length > 0) {                       /* priorytet: świeże z jazdy */
      cnt.textContent = 'W kolejce: ' + q.length;
      rvTitle(false);
      /* NIE wywłaszczamy tego, co użytkownik właśnie ocenia. Nowe
         rozpoznanie czeka w kolejce i pokaże się po wydaniu oceny —
         inaczej zdjęcie podmieniało się pod palcem i klik trafiał
         w zupełnie inną tablicę. */
      if (rvCurrent && !rvSeen[rvCurrent.id]) return;
      rvShow(q[0]);
      return;
    }
    /* nic nowego → zaległości z bazy */
    rvBacklog = rvBacklog.filter(function(it) { return !rvSeen[it.id]; });
    if (!rvBacklog.length) {
      rvFetchBacklog(function() {
        if (rvActive && !rvQueue().length) window.rvRefresh();
      });
      if (!rvBacklog.length) {
        cnt.textContent = rvBacklogBusy ? 'Szukam zaległości…'
                                        : 'W kolejce: 0';
        if (!rvBacklogBusy && rvBacklogTotal === 0) {
          rvCurrent = null;
          document.getElementById('rv-wait').style.display = 'flex';
          document.getElementById('rv-body').style.display = 'none';
          document.getElementById('rv-btns').style.display = 'none';
          var st = document.getElementById('rv-stream');
          if (!st.getAttribute('src')) st.src = '/stream';
        }
        return;
      }
    }
    cnt.innerHTML = '<span style="color:var(--yellow)">zaleg&#322;e: ' +
      rvBacklogTotal + '</span>';
    rvTitle(true);
    if (rvCurrent && !rvSeen[rvCurrent.id]) return;   /* jw. — bez wywłaszczania */
    rvShow(rvBacklog[0]);
  };

  function rvTitle(backlog) {
    var t = document.getElementById('rv-title');
    if (t) t.textContent = backlog
      ? 'ZALEGŁOŚCI — najnowsze jeszcze nieocenione'
      : 'EDYTOR WERYFIKACJI — nowe odczyty od uruchomienia';
  }

  window.rvVerdict = function(verdict) {
    if (!rvCurrent) return;
    var it = rvCurrent;
    var payload = { id: it.id, verdict: verdict };
    if (verdict === 'fix') {
      var t = prompt('Poprawny numer dla #' + it.id + ':', it.plate || '');
      if (t === null) return;
      t = t.toUpperCase().replace(/[^A-Z0-9]/g, '');
      if (t.length < 2) return;
      payload.text = t;
    }
    /* nastepny element bierzemy z kolejki SPRZED oznaczenia — od razu,
       bez migniecia ekranu oczekiwania */
    var q = rvQueue();
    rvHistory.push(it);                    /* zapamiętaj do cofania „POPRZEDNIE" */
    if (rvHistory.length > 40) rvHistory.shift();
    rvSeen[it.id] = 1;
    it.verify = verdict;
    it.vtext = payload.text || null;
    fetch('/api/verify', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    }).catch(function() {});
    rvCurrent = null;
    var next = null;
    for (var i = 0; i < q.length; i++) {
      if (q[i].id !== it.id) { next = q[i]; break; }
    }
    if (next) {
      document.getElementById('rv-count').textContent =
        'W kolejce: ' + Math.max(0, q.length - 1);
      rvShow(next);
      return;
    }
    /* brak świeżych → jedziemy zaległościami, bez ekranu oczekiwania */
    rvBacklog = rvBacklog.filter(function(x) { return !rvSeen[x.id]; });
    if (rvBacklog.length) {
      rvBacklogTotal = Math.max(0, rvBacklogTotal - 1);
      document.getElementById('rv-count').innerHTML =
        '<span style="color:var(--yellow)">zaleg&#322;e: ' + rvBacklogTotal + '</span>';
      rvShow(rvBacklog[0]);
      if (rvBacklog.length <= 3) rvFetchBacklog();   /* dociągnij następne */
      return;
    }
    rvRefresh();
  };

  /* POPRZEDNIE: cofnij do ostatnio ocenionego — gdy klik poszedł za szybko.
     Zdejmuje ze stosu, kasuje „obejrzane" i pokazuje ponownie; kolejna ocena
     nadpisze poprzednią na serwerze (verify robi DELETE+INSERT). */
  window.rvPrev = function() {
    if (!rvActive || !rvHistory.length) return;
    var it = rvHistory.pop();
    delete rvSeen[it.id];
    rvShow(it);                 /* rvShow ustawia rvCurrent = it */
  };

  document.addEventListener('keydown', function(e) {
    if (e.key === 'ArrowLeft' && rvActive) {   /* strzałka w lewo = POPRZEDNIE */
      window.rvPrev();
      return;
    }
    if (e.key === 'Escape' && rvActive) {
      reviewerClose();
      return;
    }
    if (e.key === 'Escape') {
      closeLightbox(null, true);
    }
  });

  /* ── ZONE EDITOR ── */
  var zoneData = [];         // [[nx,ny], ...] — 4 punkty w norm. koordynatach
  var zoneDragIdx = -1;
  var zoneEditing = false;
  var zoneOriginal = [];

  function getImgRect() {
    var wrap = document.getElementById('stream-wrap');
    var img  = document.getElementById('stream-img');
    var cw = wrap.clientWidth, ch = wrap.clientHeight;
    var iw = img.naturalWidth  || 800;
    var ih = img.naturalHeight || 450;
    var scale = Math.min(cw / iw, ch / ih);
    var w = iw * scale, h = ih * scale;
    return { x: (cw - w) / 2, y: (ch - h) / 2, w: w, h: h };
  }

  function zoneRender() {
    if (!zoneData.length) return;
    var r = getImgRect();
    var pts = zoneData.map(function(p) {
      return (r.x + p[0] * r.w).toFixed(1) + ',' + (r.y + p[1] * r.h).toFixed(1);
    }).join(' ');
    document.getElementById('zone-poly').setAttribute('points', pts);
    zoneData.forEach(function(p, i) {
      var h = document.getElementById('zone-h' + i);
      h.setAttribute('cx', (r.x + p[0] * r.w).toFixed(1));
      h.setAttribute('cy', (r.y + p[1] * r.h).toFixed(1));
    });
  }

  window.zoneToggle = function() {
    if (zoneEditing) { window.zoneCancel(); return; }
    fetch('/api/zone').then(function(r){ return r.json(); }).then(function(d) {
      if (!d.ok) return;
      zoneOriginal = JSON.parse(JSON.stringify(d.zone));
      zoneData = JSON.parse(JSON.stringify(d.zone));
      zoneEditing = true;
      document.getElementById('zone-svg').classList.add('show');
      document.getElementById('zone-btn').classList.add('active');
      document.getElementById('zone-btn').textContent = 'ZAMKNIJ';
      document.getElementById('zone-save-row').classList.add('show');
      zoneRender();
    });
  };

  window.zoneConfirm = function() {
    fetch('/api/zone', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({zone: zoneData})
    }).then(function(r){ return r.json(); }).then(function(d) {
      if (d.ok) zoneClose();
    });
  };

  window.zoneCancel = function() {
    zoneData = JSON.parse(JSON.stringify(zoneOriginal));
    zoneClose();
  };

  function zoneClose() {
    zoneEditing = false;
    document.getElementById('zone-svg').classList.remove('show');
    document.getElementById('zone-btn').classList.remove('active');
    document.getElementById('zone-btn').textContent = 'STREFA';
    document.getElementById('zone-save-row').classList.remove('show');
  }

  document.querySelectorAll('.zone-handle').forEach(function(h, i) {
    h.addEventListener('pointerdown', function(e) {
      if (!zoneEditing) return;
      zoneDragIdx = i;
      h.setPointerCapture(e.pointerId);
      e.preventDefault();
    });
  });

  document.getElementById('zone-svg').addEventListener('pointermove', function(e) {
    if (zoneDragIdx < 0 || !zoneEditing) return;
    var wrap = document.getElementById('stream-wrap');
    var br   = wrap.getBoundingClientRect();
    var r    = getImgRect();
    var sx   = e.clientX - br.left;
    var sy   = e.clientY - br.top;
    zoneData[zoneDragIdx] = [
      Math.max(0, Math.min(1, (sx - r.x) / r.w)),
      Math.max(0, Math.min(1, (sy - r.y) / r.h))
    ];
    zoneRender();
  });

  document.getElementById('zone-svg').addEventListener('pointerup', function() {
    zoneDragIdx = -1;
  });

  window.addEventListener('resize', function() { if (zoneEditing) zoneRender(); });

  /* ── INIT ── */
  initStream();
  fetchConfig();
  pxIconyWstaw();          /* ikony pixel-art w naglowku i pasku narzedzi */
  fetchStats();
  fetchPlates(false);

  pollInterval = setInterval(function() {
    if (tablePage === 0) fetchPlates(true);   /* auto-odswiezaj tylko najnowsze */
  }, 3000);
  statsInterval = setInterval(fetchStats, 3000);

  /* PWA: service worker (instalacja jako aplikacja wymaga HTTPS) */
  if ('serviceWorker' in navigator && location.protocol === 'https:') {
    navigator.serviceWorker.register('/sw.js').catch(function() {});
  }

})();
</script>
</body>
</html>"""


def _v4l2_get_controls(device: str = '/dev/video0') -> dict:
    """Zwraca słownik kontrolek kamery odczytany z v4l2-ctl --list-ctrls-menus."""
    import subprocess as _sp
    try:
        r = _sp.run(['v4l2-ctl', f'--device={device}', '--list-ctrls-menus'],
                    capture_output=True, text=True, timeout=5)
        return _parse_v4l2_ctrls(r.stdout)
    except Exception:
        return {}


def _parse_v4l2_ctrls(text: str) -> dict:
    controls: dict = {}
    current_menu: Optional[str] = None
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped:
            current_menu = None
            continue
        # Wiersz opcji menu (wcięty tabulatorem): "0: Disabled"
        if current_menu and re.match(r'^\d+:\s', stripped):
            m = re.match(r'^(\d+):\s+(.+)', stripped)
            if m:
                controls[current_menu].setdefault('options', {})[int(m.group(1))] = m.group(2)
            continue
        # Wiersz kontrolki: "brightness 0x... (int) : min=-64 max=64 ..."
        m = re.match(r'(\w+)\s+0x[\da-fA-F]+\s+\((\w+)\)\s*:\s*(.+)', stripped)
        if not m:
            current_menu = None
            continue
        name, ctype, params_str = m.groups()
        params = dict(re.findall(r'(\w+)=(-?\d+)', params_str))
        ctrl: dict = {
            'type':     ctype,
            'value':    int(params.get('value', 0)),
            'inactive': 'inactive' in params_str or 'disabled' in params_str,
        }
        if ctype in ('int', 'bool'):
            ctrl['min']     = int(params.get('min',     0))
            ctrl['max']     = int(params.get('max',     1))
            ctrl['step']    = int(params.get('step',    1))
            ctrl['default'] = int(params.get('default', 0))
        elif ctype == 'menu':
            ctrl['min']     = int(params.get('min',     0))
            ctrl['max']     = int(params.get('max',     1))
            ctrl['default'] = int(params.get('default', 0))
            ctrl['options'] = {}
            current_menu    = name
        controls[name] = ctrl
    return controls


class WebServer:
    """Serwer HTTP Flask w wątku tła — podgląd live + historia tablic + konfiguracja."""

    PORT = 80

    def __init__(self, db_path: Path, save_dir: Path) -> None:
        self._db_path    = str(db_path)
        self._save_dir   = save_dir
        self._lock       = threading.Lock()
        self._config_lock = threading.Lock()
        self._jpeg:      Optional[bytes] = None
        self._encode_q:  _queue.SimpleQueue = _queue.SimpleQueue()
        self._viewers    = 0            # ilu klientów ogląda /stream
        self._last_enc   = 0.0          # mono czas ostatniego kodowania
        self._flask    = self._build()
        self._thread   = threading.Thread(
            target=self._run, daemon=True, name="web-server"
        )
        self._enc_thread = threading.Thread(
            target=self._encode_worker, daemon=True, name="jpeg-encoder"
        )

    def start(self) -> None:
        if self._flask:
            self._enc_thread.start()
            self._thread.start()

    def push_frame(self, frame_bgr: np.ndarray) -> None:
        """
        Wrzuć klatkę do kolejki — encode w osobnym wątku, callback nie blokuje.
        Oszczędzanie CPU: gdy nikt nie ogląda /stream, kodujemy tylko 1 fps
        (świeża migawka dla /api/snapshot); z widzami — max 10 fps.
        """
        self._last_frame = frame_bgr      # referencja dla /api/focus-view
        now = time.monotonic()
        min_gap = 0.1 if self._viewers > 0 else 1.0
        if now - self._last_enc < min_gap:
            return
        if self._encode_q.qsize() < 2:   # pomijamy jeśli koder nie nadąża
            self._last_enc = now
            self._encode_q.put(frame_bgr.copy())

    def _encode_worker(self) -> None:
        """Wątek tła: resize + JPEG encode, nie blokuje GStreamer callback."""
        while True:
            frame = self._encode_q.get()
            small = cv2.resize(frame, (800, 450), interpolation=cv2.INTER_LINEAR)
            ok, buf = cv2.imencode('.jpg', small, [cv2.IMWRITE_JPEG_QUALITY, 72])
            if ok:
                with self._lock:
                    self._jpeg = buf.tobytes()

    def _run(self) -> None:
        import logging as _log
        _log.getLogger('werkzeug').setLevel(_log.ERROR)
        self._flask.run(host='0.0.0.0', port=self.PORT,
                        threaded=True, use_reloader=False)

    def _stream(self):
        """Generator ramek MJPEG. GeneratorExit = klient się rozłączył."""
        self._viewers += 1
        try:
            while True:
                with self._lock:
                    frame = self._jpeg
                if frame:
                    yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n'
                           + frame + b'\r\n')
                time.sleep(1 / 10)
        except GeneratorExit:
            pass
        finally:
            self._viewers = max(0, self._viewers - 1)

    def _build(self):
        try:
            from flask import Flask, Response, jsonify, request, send_file
        except ImportError:
            print("[Web] Flask niedostępny — interfejs sieciowy wyłączony. "
                  "Zainstaluj: pip install flask")
            return None

        app      = Flask(__name__)
        db_path  = self._db_path
        save_dir = self._save_dir
        cfg_lock = self._config_lock

        # Limit POST: 4 MB. Wcześniej 16 KB (ochrona przed zalaniem), ale to
        # odrzucało import wsadowy baz — kilka tysięcy tablic to ~100 KB.
        app.config['MAX_CONTENT_LENGTH'] = 4 * 1024 * 1024

        @app.route('/')
        def index():
            return _HTML_PAGE, 200, {'Content-Type': 'text/html; charset=utf-8'}

        @app.route('/stream')
        def stream():
            return Response(self._stream(),
                            mimetype='multipart/x-mixed-replace; boundary=frame')

        @app.route('/api/snapshot')
        def snapshot():
            """Pojedyncza klatka JPEG — fallback dla Safari/iOS (brak MJPEG)."""
            with self._lock:
                frame = self._jpeg
            if frame:
                return Response(frame, mimetype='image/jpeg',
                                headers={'Cache-Control': 'no-store'})
            return '', 503

        @app.route('/api/plates')
        def plates():
            try:
                limit = min(max(1, int(request.args.get('limit', 60))), 500)
            except (ValueError, OverflowError):
                limit = 60
            try:
                offset = max(0, int(request.args.get('offset', 0)))
            except (ValueError, OverflowError):
                offset = 0
            try:
                con  = sqlite3.connect(db_path, timeout=5)
                total = con.execute(
                    "SELECT COUNT(*) FROM sightings").fetchone()[0]
                rows = con.execute("""
                    SELECT s.id, s.plate_text, s.seen_at, s.full_image, s.crop_image,
                           s.det_confidence, s.ocr_confidence, s.ocr_reads,
                           s.track_duration, s.bbox, a.is_correct, a.correct_text,
                           s.extra_crops, a.correct_bbox
                    FROM sightings s
                    LEFT JOIN annotations a ON a.sighting_id = s.id
                    ORDER BY s.seen_at DESC LIMIT ? OFFSET ?
                """, (limit, offset)).fetchall()
                con.close()
                result = []
                for (sid, plate, seen_at, full_img, crop_img, dc, oc, reads,
                     dur, bbox, is_corr, corr_text, extra, corr_bbox) in rows:
                    date_folder = seen_at[:10] if seen_at and len(seen_at) >= 10 else ""
                    crop_url = (f"/api/crop/{date_folder}/{crop_img}"
                                if crop_img and date_folder else None)
                    full_url = (f"/api/crop/{date_folder}/{full_img}"
                                if full_img and date_folder else None)
                    try:
                        bbox_list = [float(v) for v in bbox.split(";")] if bbox else None
                    except ValueError:
                        bbox_list = None
                    verify = None
                    if is_corr == 1:
                        verify = 'ok'
                    elif is_corr == 0:
                        verify = 'fix'
                    elif is_corr == -1:
                        verify = 'notplate'
                    elif is_corr == -2:
                        verify = 'partial'
                    elif is_corr == -3:
                        verify = 'unreadable'
                    crops = []      # kandydaci chronologicznie od najnowszego
                    if extra and date_folder:
                        crops = [f"/api/crop/{date_folder}/{n}"
                                 for n in extra.split(";") if n]
                    try:
                        vbbox = ([float(v) for v in corr_bbox.split(";")]
                                 if corr_bbox else None)
                    except ValueError:
                        vbbox = None
                    result.append({
                        'id': sid, 'plate': plate, 'seen_at': seen_at,
                        'crop_url': crop_url, 'full_url': full_url, 'crops': crops,
                        'det_conf': dc, 'ocr_conf': oc,
                        'ocr_reads': reads, 'duration': dur, 'bbox': bbox_list,
                        'verify': verify, 'vtext': corr_text, 'vbbox': vbbox,
                    })
                resp = jsonify(result)
                resp.headers['X-Total-Count'] = str(total)
                return resp
            except Exception as e:
                return jsonify({'error': str(e)}), 500

        @app.route('/api/unreviewed')
        def unreviewed():
            """Najnowsze NIEOCENIONE zapisy z CAŁEJ bazy (nie tylko ostatnie
            100) — edytor sięga po nie, gdy bieżąca kolejka się wyczerpie,
            żeby dało się przerobić zaległości wstecz."""
            try:
                limit = min(max(1, int(request.args.get('limit', 40))), 200)
            except (ValueError, OverflowError):
                limit = 40
            try:
                con = sqlite3.connect(db_path, timeout=5)
                rows = con.execute("""
                    SELECT s.id, s.plate_text, s.seen_at, s.full_image,
                           s.crop_image, s.det_confidence, s.ocr_confidence,
                           s.ocr_reads, s.track_duration, s.bbox,
                           s.extra_crops, a.correct_bbox, a.lepsza_klatka
                    FROM sightings s
                    LEFT JOIN annotations a ON a.sighting_id = s.id
                    WHERE a.sighting_id IS NULL OR a.is_correct IS NULL
                    ORDER BY s.id DESC LIMIT ?
                """, (limit,)).fetchall()
                total = con.execute("""
                    SELECT COUNT(*) FROM sightings s
                    LEFT JOIN annotations a ON a.sighting_id = s.id
                    WHERE a.sighting_id IS NULL OR a.is_correct IS NULL
                """).fetchone()[0]
                con.close()
            except Exception as e:
                return jsonify({'error': str(e)}), 500
            out = []
            for (sid, plate, seen_at, full_img, crop_img, dc, oc, reads,
                 dur, bbox, extra, corr_bbox, lepsza) in rows:
                day = seen_at[:10] if seen_at and len(seen_at) >= 10 else ""
                try:
                    bbox_list = [float(v) for v in bbox.split(";")] if bbox else None
                except ValueError:
                    bbox_list = None
                try:
                    vbbox = ([float(v) for v in corr_bbox.split(";")]
                             if corr_bbox else None)
                except ValueError:
                    vbbox = None
                out.append({
                    'id': sid, 'plate': plate, 'seen_at': seen_at,
                    'crop_url': (f"/api/crop/{day}/{crop_img}"
                                 if crop_img and day else None),
                    'full_url': (f"/api/crop/{day}/{full_img}"
                                 if full_img and day else None),
                    'crops': ([f"/api/crop/{day}/{n}"
                               for n in extra.split(";") if n]
                              if extra and day else []),
                    'det_conf': dc, 'ocr_conf': oc, 'ocr_reads': reads,
                    'duration': dur, 'bbox': bbox_list, 'verify': None,
                    'vtext': None, 'vbbox': vbbox,
                    # klatka wskazana ręcznie jako lepsza od zapisanej
                    'lepsza': lepsza or None,
                })
            return jsonify({'total': total, 'items': out})

        @app.route('/api/crop/<date>/<filename>')
        def crop(date, filename):
            import re as _re
            # Walidacja daty i nazwy pliku — ochrona przed path traversal
            if not _re.match(r'^\d{4}-\d{2}-\d{2}$', date):
                return '', 404
            if not _re.match(r'^[\w\-]+\.jpg$', filename):
                return '', 404
            path = (save_dir / date / filename).resolve()
            if not str(path).startswith(str(save_dir.resolve())):
                return '', 404  # canonicalize check
            if path.exists():
                return send_file(str(path), mimetype='image/jpeg')
            return '', 404

        @app.route('/api/log')
        def app_log():
            """Końcówka logu usługi (diagnostyka z telefonu/PC bez SSH)."""
            try:
                n = min(2000, max(20, int(request.args.get('n', 400))))
            except ValueError:
                n = 400
            try:
                p = Path("/home/admin/hailo-apps/logs/lpr_service.log")
                lines = p.read_text(errors="replace").splitlines()[-n:]
                txt = "\n".join(lines)
                txt = re.sub(r"\x1b\[[0-9;]*m", "", txt)   # kody ANSI precz
                return Response(txt, mimetype="text/plain; charset=utf-8",
                                headers={'Cache-Control': 'no-store'})
            except Exception as e:
                return Response(f"błąd odczytu logu: {e}",
                                mimetype="text/plain"), 500

        @app.route('/api/bluetooth', methods=['GET', 'POST'])
        def bluetooth_api():
            """Panel Bluetooth (jak w telefonie): stan + akcje. Operacje
            blokujące (connect/pair/scan) w tle — front odświeża stan."""
            global BT_DEVICE
            if request.method == 'POST':
                data = request.get_json(silent=True) or {}
                act = data.get('action')
                mac = str(data.get('mac') or '')

                def bg(fn, name="bt-act"):
                    threading.Thread(target=fn, daemon=True, name=name).start()

                if act == 'power':
                    _bt_run(["power", "on" if data.get('on') else "off"],
                            timeout=8)
                elif act == 'scan':
                    _bt_start_scan(12)
                elif act == 'connect' and mac:
                    bg(lambda: (_bt_run(["trust", mac], timeout=6),
                                _bt_connect(mac)))
                elif act == 'pair' and mac:
                    bg(lambda: (_bt_run(["pair", mac], timeout=25),
                                _bt_run(["trust", mac], timeout=6),
                                _bt_connect(mac)))
                elif act == 'disconnect' and mac:
                    bg(lambda: _bt_run(["disconnect", mac], timeout=15))
                elif act == 'remove' and mac:
                    if mac == BT_DEVICE:
                        BT_DEVICE = ""
                        _save_runtime_config()
                    bg(lambda: _bt_run(["remove", mac], timeout=10))
                elif act == 'default':
                    BT_DEVICE = mac if mac != BT_DEVICE else ""
                    _save_runtime_config()
                else:
                    return jsonify({'ok': False, 'error': 'zła akcja'}), 400
                _bt_status_cache and None      # unieważnij cache przy zmianie
                globals()['_bt_status_cache'] = (0.0, None)
                return jsonify({'ok': True})
            return jsonify(_bt_status())

        @app.route('/api/bluetooth/volume', methods=['GET', 'POST'])
        def bluetooth_volume_api():
            """Wspólna głośność PipeWire/JBL, odświeżana także z przycisków JBL."""
            if request.method == 'POST':
                data = request.get_json(silent=True) or {}
                if 'volume' not in data:
                    return jsonify({'ok': False, 'error': 'brak volume'}), 400
                ok = _audio_set_volume_percent(data['volume'])
                if not ok:
                    return jsonify({'ok': False,
                                    'error': 'głośnik niepołączony'}), 409
            state = _audio_live_get()
            mac, _name = _bt_live_get()
            connected = bool(mac and state["mac"] == mac
                             and state["id"] is not None)
            return jsonify({
                'ok': True,
                'connected': connected,
                'volume': state["volume"] if connected else None,
                'muted': bool(state["muted"]) if connected else False,
            })

        @app.route('/api/radar', methods=['GET', 'POST'])
        def radar_api():
            """Tryb fotoradaru: stan, ustawienia, lista zdarzeń."""
            if request.method == 'POST':
                d = request.get_json(silent=True) or {}
                zm = False
                if 'tryb' in d:
                    RADAR_CFG['tryb'] = bool(d['tryb']); zm = True
                if 'prog_kmh' in d:
                    try:
                        RADAR_CFG['prog_kmh'] = float(max(5, min(200, float(d['prog_kmh'])))); zm = True
                    except (TypeError, ValueError):
                        pass
                if d.get('kierunek') in ('oba', '+', '-'):
                    RADAR_CFG['kierunek'] = d['kierunek']; zm = True
                for k in ('zapis_zwykly', 'glos'):
                    if k in d:
                        RADAR_CFG[k] = bool(d[k]); zm = True
                # 02.10: kalibracja i okna też z www (zakresy rozsądne)
                for k, lo, hi in (('kam_K', 1000.0, 20000.0), ('kam_tol', 0.05, 1.0),
                                  ('okno_s', 2.0, 60.0), ('klip_przed_s', 0.0, 20.0),
                                  ('klip_po_s', 0.0, 30.0)):
                    if k in d:
                        try:
                            RADAR_CFG[k] = float(max(lo, min(hi, float(d[k])))); zm = True
                        except (TypeError, ValueError):
                            pass
                if 'min_probek' in d:
                    try:
                        RADAR_CFG['min_probek'] = int(max(1, min(20, int(d['min_probek'])))); zm = True
                    except (TypeError, ValueError):
                        pass
                if zm:
                    _save_runtime_config()
                return jsonify({'ok': True, 'cfg': RADAR_CFG})
            with _radar_lock:
                st = dict(_radar_stan)
            st['swieza'] = (time.monotonic() - st['t']) < 1.5 and st['t'] > 0
            lekki = request.args.get('lekki')
            if lekki:
                return jsonify({'cfg': RADAR_CFG, 'stan': st, 'zdarzenia': []})
            a = request.args
            f = {k: a.get(k) for k in ('od', 'do', 'v_min', 'v_max', 'tablica', 'kierunek', 'sort')}
            f['tylko_tablica'] = a.get('tylko_tablica') == '1'
            f['tylko_pojazd'] = a.get('tylko_pojazd') == '1'
            try:
                limit = max(1, min(500, int(a.get('limit', 60))))
            except ValueError:
                limit = 60
            return jsonify({'cfg': RADAR_CFG, 'stan': st,
                            'zdarzenia': _radar_lista(limit, f)})

        @app.route('/api/radar/komenda', methods=['POST'])
        def radar_komenda_api():
            """Parametry wewnętrzne modułu LD2415H (karta, rozdz. 6.3):
            {"typ":"odczyt"} → `43 46 07 + 10×00` (odpowiedź tekstowa X1..X0);
            {"typ":"param","min_kmh":1,"kat":0,"czulosc":5} → `43 46 01 …`;
            {"typ":"wyjscie","kierunek":0|1|2,"probkowanie":0..3} → `43 46 02 …`
            (jednostka zawsze km/h); {"typ":"antywibracja","wartosc":0..112}
            → `43 46 03 …`. Po zapisie od razu odczyt kontrolny."""
            d = request.get_json(silent=True) or {}
            typ = d.get('typ')
            rad = globals().get('RADAR')
            if rad is None:
                return jsonify({'ok': False, 'blad': 'radar nieuruchomiony'}), 503

            def _b(klucz, lo, hi, dom):
                try:
                    return int(max(lo, min(hi, int(d.get(klucz, dom)))))
                except (TypeError, ValueError):
                    return dom

            if typ == 'odczyt':
                bajty = bytes([0x43, 0x46, 0x07] + [0] * 10)
            elif typ == 'param':
                bajty = bytes([0x43, 0x46, 0x01, _b('min_kmh', 0, 255, 1),
                               _b('kat', 0, 90, 0), _b('czulosc', 1, 15, 5), 0x0d, 0x0a])
            elif typ == 'wyjscie':
                bajty = bytes([0x43, 0x46, 0x02, _b('kierunek', 0, 2, 0),
                               _b('probkowanie', 0, 3, 1), 0x00, 0x0d, 0x0a])
            elif typ == 'antywibracja':
                bajty = bytes([0x43, 0x46, 0x03, _b('wartosc', 0, 0x70, 0), 0, 0, 0x0d, 0x0a])
            else:
                return jsonify({'ok': False, 'blad': 'zły typ'}), 400
            wynik = rad.komenda(bajty, 1.5)
            print(f"[Radar] komenda {typ}: {bajty.hex(' ')} → "
                  f"{wynik.get('odp', wynik.get('blad', ''))[:160]!r}", flush=True)
            if typ != 'odczyt' and wynik.get('ok'):
                time.sleep(0.3)
                wynik['odczyt'] = rad.komenda(bytes([0x43, 0x46, 0x07] + [0] * 10), 1.5)
            for w in (wynik, wynik.get('odczyt') or {}):
                txt = w.get('odp') or ''
                par = dict(re.findall(r'X(\d):([0-9A-Fa-f]{2})', txt))
                if par:
                    w['param'] = {k: int(v, 16) for k, v in par.items()}
                    m = re.search(r'No\.?\s*[:：]?\s*(\S+)\s*(v?[\d.]+)', txt)
                    if m:
                        w['firmware'] = m.group(1) + ' ' + m.group(2)
            return jsonify(wynik)

        @app.route('/api/radar/probki')
        def radar_probki():
            """Ostatnie próbki radaru z pamięci: [[czas_unix, v_ze_znakiem], …]
            (do korelacji z przejazdami widzianymi przez kamerę)."""
            for _ in range(5):
                try:
                    dane = list(_radar_v_hist)
                    break
                except RuntimeError:
                    time.sleep(0.01)
            else:
                dane = []
            return jsonify({'probki': [[round(t, 3), v] for t, v in dane],
                            'teraz': time.time()})

        @app.route('/api/radar/test', methods=['POST'])
        def radar_test_api():
            """Symulacja: {"v": 88, "sighting_id": 123 | "auto"} — "auto" bierze
            ostatni zapis z tablicą i ramką (zdjęcie wygląda jak prawdziwe)."""
            d = request.get_json(silent=True) or {}
            sid = d.get('sighting_id')
            if sid == 'auto':
                try:
                    con = sqlite3.connect(db_path, timeout=5)
                    r = con.execute("SELECT id FROM sightings WHERE bbox IS NOT NULL "
                                    "AND plate_text != 'UNKNOWN' AND full_image IS NOT NULL "
                                    "ORDER BY id DESC LIMIT 1").fetchone()
                    con.close()
                    sid = r[0] if r else None
                except Exception:
                    sid = None
            try:
                v = float(d.get('v', 88.0))
            except (TypeError, ValueError):
                v = 88.0
            _radar_test(v, int(sid) if sid else None)
            return jsonify({'ok': True, 'sighting_id': sid})

        @app.route('/api/radar/plik/<d>/<nazwa>')
        def radar_plik(d, nazwa):
            import re as _re
            if (not _re.match(r'^\d{4}-\d{2}-\d{2}$', d)
                    or not _re.match(r'^[\w\-]+\.(jpg|mp4)$', nazwa)):
                return '', 404
            p = _radar_dir() / d / nazwa
            if not p.is_file():
                return '', 404
            if nazwa.endswith('.mp4'):
                return send_file(str(p), mimetype='video/mp4',
                                 conditional=True, max_age=86400)
            if request.args.get('mini') and not nazwa.endswith('_mini.jpg'):
                # 02.10: lista zdarzeń w www ładowała 60 pełnych zdjęć po
                # ~700 kB (40 MB na otwarcie modalu — przez tethering!).
                # Miniatura 320 px robiona raz, trzymana obok oryginału.
                mini = p.with_name(p.stem + '_mini.jpg')
                if not mini.is_file():
                    try:
                        img = cv2.imread(str(p))
                        if img is not None:
                            h, w = img.shape[:2]
                            nw = 320
                            m = cv2.resize(img, (nw, max(1, int(h * nw / w))),
                                           interpolation=cv2.INTER_AREA)
                            cv2.imwrite(str(mini), m, [cv2.IMWRITE_JPEG_QUALITY, 70])
                    except Exception as e:
                        _zglos("radar_mini", e)
                if mini.is_file():
                    return send_file(str(mini), mimetype='image/jpeg', max_age=86400)
            return send_file(str(p), mimetype='image/jpeg', max_age=86400)

        @app.route('/api/usb', methods=['GET', 'POST'])
        def usb_api():
            """Bezpieczne wyjmowanie pendrive'a + kopie zapasowe bazy."""
            if request.method == 'POST':
                act = (request.get_json(silent=True) or {}).get('action')
                if act == 'eject':
                    return jsonify({'ok': True,
                                    'backup': _usb_prepare_eject(str(db_path))})
                if act == 'resume':
                    _usb_resume()
                    return jsonify({'ok': True})
                if act == 'backup':
                    p = _db_backup(str(db_path), 'reczna')
                    threading.Thread(
                        target=lambda: _photos_backup(str(save_dir)),
                        daemon=True, name="photo-backup").start()
                    return jsonify({'ok': bool(p), 'backup': p})
                return jsonify({'ok': False, 'error': 'zła akcja'}), 400
            backups = []
            try:
                for f in sorted(Path(BACKUP_DIR).glob("plates_*.db"),
                                reverse=True)[:12]:
                    st = f.stat()
                    backups.append({
                        'name': f.name, 'kb': st.st_size // 1024,
                        'when': datetime.fromtimestamp(st.st_mtime)
                                .strftime('%Y-%m-%d %H:%M')})
            except Exception as e:
                _zglos("lista_kopii", e)
            try:
                sv = os.statvfs('/home/admin')
                free_sd = sv.f_bavail * sv.f_frsize // 1048576
            except OSError:
                free_sd = 0
            try:
                db_kb = os.path.getsize(str(db_path)) // 1024
            except OSError:
                db_kb = 0
            return jsonify({
                'state': _usb_state, 'writing': _usb_ready.is_set(),
                'present': os.path.exists(USB_DEV),
                'db_kb': db_kb, 'free_sd_mb': free_sd,
                'photos': dict(_photo_stat),
                'backup_dir': BACKUP_DIR, 'backups': backups})

        @app.route('/api/tts-preview')
        def tts_preview():
            """Synteza komunikatu → WAV do odtworzenia W PRZEGLĄDARCE
            (przy konfigurowaniu przy komputerze nie chcemy budzić auta)."""
            gid = request.args.get('gid', '')
            text = request.args.get('text', '')
            # rodzaj decyduje o głośności — podgląd alertu musi brzmieć tak,
            # jak zabrzmi w aucie, a nie jak zwykły komunikat
            kind = request.args.get('kind') or ('alert' if gid else 'system')
            if kind not in TTS_GAINS:
                kind = 'system'
            if gid:
                # przykład na PRAWDZIWYM wpisie z tej bazy — inaczej nie
                # słychać, jak zabrzmi marka doczytana z notatki
                plate, note = _alert_example(gid)
                text = _alert_speak(gid, plate, note)
            text = (text or '').strip()[:300]
            if not text:
                return '', 400
            data = SPEECH.synth_bytes(text, kind)
            if not data:
                return '', 500
            return Response(data, mimetype='audio/wav',
                            headers={'Cache-Control': 'no-store'})

        @app.route('/api/watchlist', methods=['GET', 'POST'])
        def watchlist_api():
            """Bazy obserwowanych (własny komunikat głosowy na bazę),
            import wsadowy, przełącznik czytania każdej tablicy."""
            global TTS_ALL

            def _norm(p):
                return re.sub(r'[^A-Z0-9]', '', str(p or '').upper())[:10]

            if request.method == 'POST':
                data = request.get_json(silent=True) or {}
                if 'add' in data:
                    a = data['add']
                    p = _norm(a.get('plate'))
                    if not (3 <= len(p) <= 10):
                        return jsonify({'ok': False, 'error': 'zły numer'}), 400
                    gid = str(a.get('group') or 'default')
                    if gid not in WATCH_GROUPS:
                        gid = next(iter(WATCH_GROUPS), 'default')
                    WATCHLIST[p] = {'group': gid,
                                    'note': str(a.get('note') or '')[:60]}
                elif 'bulk' in data:
                    b = data['bulk']
                    gid = str(b.get('group') or 'default')
                    if gid not in WATCH_GROUPS:
                        gid = next(iter(WATCH_GROUPS), 'default')
                    added = dup = bad = 0
                    for line in str(b.get('text') or '').splitlines():
                        if not line.strip():
                            continue
                        p, note = _parse_bulk_line(line)
                        # tablica MUSI mieć cyfrę — odsiewa nagłówki CSV
                        # ("rejestracja;samochód;...") i teksty
                        if not (3 <= len(p) <= 10) or not any(c.isdigit()
                                                              for c in p):
                            bad += 1
                            continue
                        if p in WATCHLIST:
                            dup += 1
                            continue
                        WATCHLIST[p] = {'group': gid, 'note': note}
                        added += 1
                    _save_runtime_config()
                    return jsonify({'ok': True, 'added': added,
                                    'dup': dup, 'bad': bad})
                elif 'remove' in data:
                    WATCHLIST.pop(_norm(data['remove']), None)
                elif 'group_save' in data:
                    g = data['group_save']
                    gid = str(g.get('gid') or '').strip()
                    if not gid:      # nowa baza — id z nazwy
                        base = re.sub(r'[^a-z0-9]+', '_',
                                      str(g.get('name') or 'baza').lower())[:20]
                        gid = base or 'baza'
                        n = 2
                        while gid in WATCH_GROUPS:
                            gid = f"{base}{n}"
                            n += 1
                    WATCH_GROUPS[gid] = {
                        'name': str(g.get('name') or gid)[:40],
                        'alert': str(g.get('alert')
                                     or _watch_default_group()['alert'])[:200],
                        'enabled': bool(g.get('enabled', True)),
                        'color': (str(g.get('color') or 'red')
                                  if str(g.get('color')) in ALERT_COLORS else 'red'),
                        'nota': bool(g.get('nota', True)),
                    }
                elif 'group_remove' in data:
                    gid = str(data['group_remove'])
                    if len(WATCH_GROUPS) <= 1:
                        return jsonify({'ok': False,
                                        'error': 'musi zostać jedna baza'}), 400
                    WATCH_GROUPS.pop(gid, None)
                    fallback = next(iter(WATCH_GROUPS))
                    keep = bool(data.get('keep_plates', True))
                    for p, e in list(WATCHLIST.items()):
                        if e.get('group') == gid:
                            if keep:
                                e['group'] = fallback
                            else:
                                WATCHLIST.pop(p, None)
                elif 'alert_test' in data:
                    gid = str(data['alert_test'])
                    SPEECH.say(_alert_speak(gid, *_alert_example(gid)), "alert")
                elif 'tts_all' in data:
                    TTS_ALL = bool(data['tts_all'])
                elif 'tts_brand' in data:
                    globals()['TTS_BRAND'] = bool(data['tts_brand'])
                elif 'spell_mode' in data:
                    if data['spell_mode'] in ('letters', 'phonetic'):
                        globals()['TTS_SPELL_MODE'] = data['spell_mode']
                elif 'tts_gains' in data:      # osobno per rodzaj komunikatu
                    for k, v in (data['tts_gains'] or {}).items():
                        if k in TTS_GAINS:
                            try:
                                TTS_GAINS[k] = max(1.0, min(4.0, float(v)))
                            except (TypeError, ValueError):
                                pass
                elif 'tts_gain' in data:       # stary suwak z www: ustaw wszystkie
                    try:
                        g = max(1.0, min(4.0, float(data['tts_gain'])))
                        globals()['TTS_GAIN'] = g
                        for k in TTS_GAINS:
                            TTS_GAINS[k] = g
                    except (TypeError, ValueError):
                        pass
                elif data.get('tts_test'):
                    SPEECH.say("Cześć! Tu system rozpoznawania tablic. "
                               "Test głosu.")
                _save_runtime_config()
            counts = {}
            for e in WATCHLIST.values():
                g = e.get('group') or 'default'
                counts[g] = counts.get(g, 0) + 1
            return jsonify({
                'ok': True, 'tts_all': TTS_ALL,
                'spell_mode': TTS_SPELL_MODE, 'tts_gain': TTS_GAIN,
                'tts_gains': dict(TTS_GAINS),
                'spell_example': _spell_plate("WA12345"),
                'tts_brand': TTS_BRAND,
                'brand_examples': [
                    {'gid': k,
                     'plate': _alert_example(k)[0],
                     'note': _alert_example(k)[1],
                     'text': _alert_speak(k, *_alert_example(k))}
                    for k in WATCH_GROUPS],
                'groups': [{'gid': k, 'count': counts.get(k, 0), **v}
                           for k, v in WATCH_GROUPS.items()],
                'plates': [{'plate': k, 'group': v.get('group'),
                            'note': v.get('note') or ''}
                           for k, v in sorted(WATCHLIST.items())]})

        @app.route('/api/snap', methods=['POST'])
        def manual_snap():
            """Ręczne zdjęcie (3 klatki) — jak klawisz P na ekranie w aucie."""
            return jsonify({'ok': request_manual_snap()})

        @app.route('/api/focus-view')
        def focus_view():
            """
            Asystent ostrości: klatka z czerwoną zebrą na krawędziach (focus
            peaking), histogramem luminancji, metryką ostrości (wyżej=ostrzej)
            i znacznikiem minimalnej wysokości tablicy. Liczone tylko na
            żądanie (otwarty panel OSTROŚĆ, ~1.5 kl/s).
            """
            frame = getattr(self, '_last_frame', None)
            if frame is None:
                return '', 503
            img = frame.copy()
            h, w = img.shape[:2]
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            # zebra: krawędzie (Laplace) na czerwono
            mag = cv2.convertScaleAbs(cv2.Laplacian(gray, cv2.CV_16S, ksize=3))
            img[mag > 42] = (0, 0, 255)
            # metryka ostrości: wariancja Laplace w centralnej 1/3 kadru
            ch3, cw3 = h // 3, w // 3
            score = float(cv2.Laplacian(gray[ch3:2 * ch3, cw3:2 * cw3],
                                        cv2.CV_64F).var())
            # histogram luminancji (lewy dolny róg)
            hist = cv2.calcHist([gray], [0], None, [128], [0, 256]).ravel()
            hist = (hist / (hist.max() + 1e-6) * 100).astype(int)
            hx, hy, hh_ = 20, h - 40, 100
            cv2.rectangle(img, (hx - 6, hy - hh_ - 6), (hx + 262, hy + 6),
                          (20, 20, 20), -1)
            for i, v in enumerate(hist):
                cv2.line(img, (hx + i * 2, hy), (hx + i * 2, hy - int(v)),
                         (200, 200, 200), 2)
            # znacznik minimalnej wysokości tablicy (OCR_MIN_PLATE_PX)
            gx, gy = w // 2, h // 2
            for dy in (0, OCR_MIN_PLATE_PX):
                cv2.line(img, (gx - 70, gy + dy), (gx + 70, gy + dy),
                         (0, 255, 80), 2)
            cv2.putText(img, f"min. tablica = {OCR_MIN_PLATE_PX}px",
                        (gx + 80, gy + OCR_MIN_PLATE_PX),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 80), 2, cv2.LINE_AA)
            cv2.putText(img, f"OSTROSC: {score:6.0f}  (wyzej = ostrzej)",
                        (20, 44), cv2.FONT_HERSHEY_DUPLEX, 1.0,
                        (0, 255, 80), 2, cv2.LINE_AA)
            ok, buf = cv2.imencode('.jpg', img, [cv2.IMWRITE_JPEG_QUALITY, 80])
            if not ok:
                return '', 500
            return Response(buf.tobytes(), mimetype='image/jpeg',
                            headers={'Cache-Control': 'no-store'})

        @app.route('/api/recordings')
        def recordings():
            items = []
            base = Path(_dashcam_dir())
            if base.is_dir():
                for p in base.rglob("*.mkv"):
                    try:
                        fst = p.stat()
                        th = p.with_suffix('.jpg')
                        items.append({
                            'sesja': p.parent.name, 'plik': p.name,
                            'size_mb': round(fst.st_size / 1048576, 1),
                            'mtime': datetime.fromtimestamp(fst.st_mtime)
                                     .strftime('%Y-%m-%d %H:%M:%S'),
                            'thumb': (f"/api/rec-thumb/{p.parent.name}/{th.name}"
                                      if th.exists() else None),
                        })
                    except OSError:
                        pass
            items.sort(key=lambda x: x['mtime'], reverse=True)
            return jsonify(items)

        def _rec_path(sesja, plik, ext='mkv'):
            import re as _re
            if (not _re.match(r'^[\w\-]+$', sesja)
                    or not _re.match(r'^[\w\-]+\.' + ext + r'$', plik)):
                return None
            p = (Path(_dashcam_dir()) / sesja / plik).resolve()
            if (not str(p).startswith(str(Path(_dashcam_dir()).resolve()))
                    or not p.exists()):
                return None
            return p

        @app.route('/api/rec-thumb/<sesja>/<plik>')
        def rec_thumb(sesja, plik):
            p = _rec_path(sesja, plik, ext='jpg')
            if not p:
                return '', 404
            # miniaturka się nie zmienia — telefon może ją trzymać w cache
            return send_file(str(p), mimetype='image/jpeg', max_age=86400)

        @app.route('/api/rec-prep/<sesja>/<plik>', methods=['POST'])
        def rec_prep(sesja, plik):
            """Podgląd MP4 dla segmentu: gotowy? Jak nie — odpal transkodowanie
            w tle (jedno naraz + ewentualny drugi slot dla janitora)."""
            p = _rec_path(sesja, plik)
            if not p:
                return jsonify({'ok': False}), 404
            if _rec_preview_path(p).exists():
                return jsonify({'ok': True, 'ready': True})
            key = str(p)
            with _rec_prep_lock:
                if key in _rec_prep_jobs:
                    # realny postęp z pliku -progress ffmpega
                    # (uwaga: out_time_ms to MIKROsekundy, nazwa myli)
                    pct = None
                    info = _rec_prep_info.get(key)
                    if info and info['dur']:
                        try:
                            txt = info['prog'].read_text()[-4000:]
                            ms = re.findall(r'out_time_ms=(\d+)', txt)
                            if ms:
                                pct = min(99, int(int(ms[-1]) / 1_000_000
                                                  / info['dur'] * 100))
                        except Exception as e:
                            _zglos("postep_nagrania", e)
                    return jsonify({'ok': True, 'ready': False,
                                    'msg': 'w trakcie', 'pct': pct})
                if len(_rec_prep_jobs) >= 2:
                    return jsonify({'ok': True, 'ready': False, 'msg': 'w kolejce'})
                _rec_prep_jobs.add(key)

            def work():
                try:
                    _make_rec_preview(p)
                finally:
                    with _rec_prep_lock:
                        _rec_prep_jobs.discard(key)

            threading.Thread(target=work, daemon=True,
                             name="rec-preview").start()
            return jsonify({'ok': True, 'ready': False, 'msg': 'start'})

        @app.route('/api/rec-prev/<sesja>/<plik>')
        def rec_prev(sesja, plik):
            p = _rec_path(sesja, plik, ext='mp4')
            if not p:
                return '', 404
            # conditional=True => Range requests => przewijanie działa
            return send_file(str(p), mimetype='video/mp4',
                             conditional=True, max_age=86400)

        @app.route('/api/rec/<sesja>/<plik>')
        def rec_download(sesja, plik):
            p = _rec_path(sesja, plik)
            if not p:
                return '', 404
            return send_file(str(p), mimetype='video/x-matroska',
                             as_attachment=True, conditional=True)

        @app.route('/api/rec-play/<sesja>/<plik>')
        def rec_play(sesja, plik):
            """
            Odtwarzanie w przeglądarce: przeglądarki nie grają MJPEG/MKV w <video>,
            więc ffmpeg strumieniuje JPEG-i do multipart (jak podgląd live).
            Domyślnie SKALUJEMY w dół (640 px / 12 fps): oryginał to ~45 Mbit/s
            i na telefonie/Tailscale buforował się w nieskończoność. Pełna
            jakość (-c copy, bez transkodowania): ?hd=1. Koszt CPU tylko na
            czas oglądania (nice 19 — LPR ma pierwszeństwo).
            """
            p = _rec_path(sesja, plik)
            if not p:
                return '', 404
            import subprocess as _sp
            if request.args.get('hd'):
                # pełna rozdzielczość; nagrania H.264 (z Arducama) wymagają
                # re-enkodowania do strumienia JPEG — koszt tylko na czas oglądania
                enc = ['-vf', _vf_z_obrotem('fps=15', p),
                       '-c:v', 'mjpeg', '-q:v', '5']
            else:
                enc = ['-vf', _vf_z_obrotem('fps=12,scale=640:-2', p),
                       '-c:v', 'mjpeg', '-q:v', '7']

            def gen():
                proc = _sp.Popen(
                    ['nice', '-n', '19', 'ffmpeg', '-nostdin',
                     '-loglevel', 'quiet', '-re', '-i', str(p)]
                    + enc + ['-f', 'mpjpeg', '-'],
                    stdout=_sp.PIPE)
                try:
                    while True:
                        chunk = proc.stdout.read(65536)
                        if not chunk:
                            break
                        yield chunk
                finally:
                    proc.kill()
            return Response(gen(),
                            mimetype='multipart/x-mixed-replace; boundary=ffmpeg')

        @app.route('/manifest.json')
        def manifest():
            return jsonify({
                "name": "LPR — rozpoznawanie tablic",
                "short_name": "LPR",
                "start_url": "/",
                "display": "standalone",
                "background_color": "#0e0910",
                "theme_color": "#0e0910",
                "icons": [
                    {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
                    {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"},
                ],
            })

        @app.route('/icon-<int:size>.png')
        def app_icon(size):
            if size not in (192, 512):
                return '', 404
            p = _WEB_ICON_DIR / f"icon-{size}.png"
            if not p.exists():
                _make_icon(p, size)
            return send_file(str(p), mimetype='image/png')

        @app.route('/font/<nazwa>')
        def web_font(nazwa):
            """Czcionki pikselowe serwowane LOKALNIE — panel ma działać bez
            internetu (tryb hotspotu, auto poza zasięgiem). Licencja OFL."""
            if nazwa not in ("PressStart2P-Regular.ttf", "VT323-Regular.ttf"):
                return '', 404
            p = _WEB_ICON_DIR / nazwa
            if not p.exists():
                return '', 404
            return send_file(str(p), mimetype='font/ttf',
                             max_age=604800)          # tydzień w cache

        @app.route('/sw.js')
        def service_worker():
            # minimalny SW — wymagany, by Chrome pozwolił zainstalować PWA
            return ("self.addEventListener('fetch', function(e) {});", 200,
                    {'Content-Type': 'application/javascript'})

        @app.route('/api/verify', methods=['POST'])
        def verify_api():
            """
            Weryfikacja odczytu przez człowieka (przyciski w tabeli www):
            verdict: 'ok' | 'fix' (+text = poprawny numer) | 'notplate' | 'clear'
            """
            data = request.get_json(silent=True) or {}
            try:
                sid = int(data.get('id'))
            except (TypeError, ValueError):
                return jsonify({'ok': False, 'error': 'brak id'}), 400
            verdict = data.get('verdict')
            text = re.sub(r'[^A-Z0-9]', '', str(data.get('text') or '').upper())[:10]
            con = sqlite3.connect(db_path, timeout=5)
            try:
                if verdict == 'bbox':
                    # ręcznie narysowana ramka tablicy — nie rusza werdyktu tekstu
                    b = data.get('bbox')
                    try:
                        b = [max(0.0, min(1.0, float(v))) for v in b]
                        assert len(b) == 4 and b[0] < b[2] and b[1] < b[3]
                    except (TypeError, ValueError, AssertionError):
                        return jsonify({'ok': False, 'error': 'zła ramka'}), 400
                    bstr = ";".join("%.4f" % v for v in b)
                    con.execute("""
                        INSERT INTO annotations (sighting_id, correct_bbox, reviewed_at)
                        VALUES (?,?,datetime('now','localtime'))
                        ON CONFLICT(sighting_id) DO UPDATE SET
                            correct_bbox = excluded.correct_bbox,
                            reviewed_at  = excluded.reviewed_at
                    """, (sid, bstr))
                elif verdict == 'lepsza':
                    # Człowiek wskazał, że SĄSIEDNIA klatka jest lepsza od
                    # zapisanej (bywa ostrzejsza). Nie rusza werdyktu tekstu —
                    # zapisujemy tylko, która klatka ma trafić do uczenia.
                    # Pusta wartość = cofnięcie wyboru (wraca „najlepsza”).
                    # SAMA nazwa pliku — bez ukośników i bez „..". Pierwsza
                    # wersja dopuszczała „/", więc „../../etc/passwd" przechodziło
                    # (wykryte testem). Nazwa trafia potem do ścieżek przy
                    # eksporcie do uczenia, więc musi być nieszkodliwa.
                    plik = str(data.get('plik') or '')[:200]
                    if plik and (not re.fullmatch(r'[\w.\-]+', plik)
                                 or '..' in plik):
                        return jsonify({'ok': False, 'error': 'zła nazwa'}), 400
                    con.execute("""
                        INSERT INTO annotations (sighting_id, lepsza_klatka, reviewed_at)
                        VALUES (?,?,datetime('now','localtime'))
                        ON CONFLICT(sighting_id) DO UPDATE SET
                            lepsza_klatka = excluded.lepsza_klatka,
                            reviewed_at   = excluded.reviewed_at
                    """, (sid, plik or None))
                elif verdict == 'clear':
                    con.execute("DELETE FROM annotations WHERE sighting_id=?", (sid,))
                else:
                    if verdict == 'ok':
                        ic, ct = 1, None
                    elif verdict == 'notplate':
                        ic, ct = -1, None
                    elif verdict == 'partial':
                        ic, ct = -2, None   # tablica niepełna / ucięta
                    elif verdict == 'unreadable':
                        ic, ct = -3, None   # nieczytelna nawet dla człowieka
                    elif verdict == 'fix':
                        if not (2 <= len(text) <= 10):
                            return jsonify({'ok': False, 'error': 'zły numer'}), 400
                        ic, ct = 0, text
                    else:
                        return jsonify({'ok': False, 'error': 'zły verdict'}), 400
                    con.execute("""
                        INSERT INTO annotations (sighting_id, is_correct, correct_text, reviewed_at)
                        VALUES (?,?,?,datetime('now','localtime'))
                        ON CONFLICT(sighting_id) DO UPDATE SET
                            is_correct   = excluded.is_correct,
                            correct_text = excluded.correct_text,
                            reviewed_at  = excluded.reviewed_at
                    """, (sid, ic, ct))
                con.commit()
            finally:
                con.close()
            return jsonify({'ok': True})

        @app.route('/api/locate/<int:sid>')
        def locate(sid):
            """
            Pozycja tablicy na pełnej klatce. Nowe zapisy mają bbox w bazie;
            dla starych odnajdujemy crop na klatce (template matching na
            zmniejszonych obrazach) i zapamiętujemy wynik.
            """
            con = sqlite3.connect(db_path, timeout=5)
            row = con.execute(
                "SELECT seen_at, full_image, crop_image, bbox "
                "FROM sightings WHERE id=?", (sid,)).fetchone()
            if not row:
                con.close()
                return jsonify({'ok': False}), 404
            seen_at, full_img, crop_img, bbox = row
            if bbox:
                con.close()
                return jsonify({'ok': True,
                                'bbox': [float(v) for v in bbox.split(';')]})
            date_folder = seen_at[:10] if seen_at else ""
            full = cv2.imread(str(save_dir / date_folder / (full_img or "")))
            crop = cv2.imread(str(save_dir / date_folder / (crop_img or "")))
            if full is None or crop is None:
                con.close()
                return jsonify({'ok': False}), 404
            scale = 480.0 / full.shape[1]
            fs = cv2.resize(full, (480, max(8, int(full.shape[0] * scale))))
            cs = cv2.resize(crop, (max(8, int(crop.shape[1] * scale)),
                                   max(8, int(crop.shape[0] * scale))))
            if cs.shape[0] >= fs.shape[0] or cs.shape[1] >= fs.shape[1]:
                con.close()
                return jsonify({'ok': False}), 404
            res = cv2.matchTemplate(fs, cs, cv2.TM_CCOEFF_NORMED)
            _, maxv, _, maxloc = cv2.minMaxLoc(res)
            b = [round(maxloc[0] / fs.shape[1], 4),
                 round(maxloc[1] / fs.shape[0], 4),
                 round((maxloc[0] + cs.shape[1]) / fs.shape[1], 4),
                 round((maxloc[1] + cs.shape[0]) / fs.shape[0], 4)]
            ok = maxv > 0.5
            if ok:   # zapamiętaj — następnym razem bez liczenia
                con.execute("UPDATE sightings SET bbox=? WHERE id=?",
                            (";".join(str(v) for v in b), sid))
                con.commit()
            con.close()
            return jsonify({'ok': ok, 'bbox': b})

        @app.route('/api/stats')
        def stats():
            try:
                st   = os.statvfs(str(save_dir))
                free = round(st.f_bavail * st.f_frsize / 1_048_576, 0)
            except Exception:
                free = -1
            try:
                con   = sqlite3.connect(db_path, timeout=5)
                total = con.execute("SELECT COUNT(*) FROM sightings").fetchone()[0]
                today = con.execute(
                    "SELECT COUNT(*) FROM sightings WHERE seen_at >= date('now')"
                ).fetchone()[0]
                con.close()
            except Exception:
                total = today = 0
            with _stat_lock:
                return jsonify({
                    'total_plates': total, 'today_plates': today, 'free_disk_mb': free,
                    'fps': round(_stat_fps, 1), 'cpu': round(_stat_cpu, 1),
                    'disp_fps': round(_stat_disp_fps, 1),
                    'ram_gb': round(_stat_ram, 2),
                    'cpu_temp': round(_stat_cpu_temp, 1), 'hailo_temp': round(_stat_hailo_temp, 1),
                    'zasilanie': dict(_stat_zasilanie),
                    'ocr_backend': getattr(_diag_ref.get("ocr"), "backend", "cpu"),
                    'ocr_infer_ms': round(getattr(getattr(_diag_ref.get("ocr"), "_hailo", None), "last_ms", 0.0), 1),
                })

        @app.route('/api/config', methods=['GET', 'POST'])
        def config():
            global OCR_MIN_MATCHING, OCR_MIN_CONF, PLATE_MIN_CONF
            if request.method == 'POST':
                data = request.get_json(silent=True) or {}
                with cfg_lock:
                    if 'ocr_min_matching' in data:
                        OCR_MIN_MATCHING = max(1, int(data['ocr_min_matching']))
                    if 'ocr_min_conf' in data:
                        OCR_MIN_CONF = max(0.1, min(0.99, float(data['ocr_min_conf'])))
                    if 'plate_min_conf' in data:
                        PLATE_MIN_CONF = max(0.1, min(0.99, float(data['plate_min_conf'])))
                _save_runtime_config()      # przeżywa restart aplikacji
                return jsonify({'ok': True})
            with cfg_lock:
                return jsonify({
                    'ocr_min_matching': OCR_MIN_MATCHING,
                    'ocr_min_conf':     OCR_MIN_CONF,
                    'plate_min_conf':   PLATE_MIN_CONF,
                })

        @app.route('/api/zone', methods=['GET', 'POST'])
        def zone_api():
            global DETECT_ZONE
            if request.method == 'POST':
                data = request.get_json(silent=True) or {}
                pts = data.get('zone')
                if not pts or len(pts) != 4:
                    return jsonify({'ok': False, 'error': 'Wymagane 4 punkty'}), 400
                try:
                    validated = [[max(0.0, min(1.0, float(p[0]))),
                                  max(0.0, min(1.0, float(p[1])))] for p in pts]
                except Exception:
                    return jsonify({'ok': False, 'error': 'Nieprawidłowe dane'}), 400
                with _zone_lock:
                    DETECT_ZONE = validated
                _save_runtime_config()      # przeżywa restart aplikacji
                return jsonify({'ok': True})
            with _zone_lock:
                zone_copy = [list(p) for p in DETECT_ZONE]
            return jsonify({'ok': True, 'zone': zone_copy})

        @app.route('/api/camera', methods=['GET', 'POST'])
        def camera_api():
            import subprocess as _sp

            def set_ctrl(name, value):
                r = _sp.run(['v4l2-ctl', '--device=/dev/video0',
                             f'--set-ctrl={name}={int(value)}'],
                            capture_output=True, text=True, timeout=5)
                return r.returncode == 0, r.stderr.strip()[:80]

            if CAM_SOURCE == 'csi':
                if request.method == 'POST':
                    data = request.get_json(silent=True) or {}
                    if data.pop('reset_auto', None):
                        CSI_CAM_SETTINGS.update(CSI_CAM_DEFAULTS)
                        _csi_apply_live()
                        _save_runtime_config()
                        return jsonify({'ok': True})
                    errors = []
                    if 'ae_mode' in data:
                        if data['ae_mode'] in ('normal', 'short', 'long'):
                            CSI_CAM_SETTINGS['ae_mode'] = data['ae_mode']
                        else:
                            errors.append('zły ae_mode')
                    if 'metering' in data:
                        if data['metering'] in ('centre', 'spot', 'matrix'):
                            CSI_CAM_SETTINGS['metering'] = data['metering']
                        else:
                            errors.append('zły metering')
                    if 'ev' in data:
                        try:
                            CSI_CAM_SETTINGS['ev'] = max(-8.0, min(8.0,
                                                         float(data['ev'])))
                        except (TypeError, ValueError):
                            errors.append('złe ev')
                    if 'manual' in data:
                        CSI_CAM_SETTINGS['manual'] = bool(data['manual'])
                    if 'exposure_us' in data:
                        try:
                            CSI_CAM_SETTINGS['exposure_us'] = max(50, min(
                                33000, int(data['exposure_us'])))
                        except (TypeError, ValueError):
                            errors.append('zła migawka')
                    if 'gain' in data:
                        try:
                            CSI_CAM_SETTINGS['gain'] = max(1.0, min(16.0,
                                                           float(data['gain'])))
                        except (TypeError, ValueError):
                            errors.append('złe wzmocnienie')
                    if 'max_shutter_us' in data:
                        try:
                            v = int(data['max_shutter_us'])
                            if v in (0, 16666, 8333, 4000):
                                CSI_CAM_SETTINGS['max_shutter_us'] = v
                            else:
                                errors.append('zły limit migawki')
                        except (TypeError, ValueError):
                            errors.append('zły limit migawki')
                    if errors:
                        return jsonify({'ok': False, 'errors': errors}), 400
                    ok = _csi_apply_live()
                    _save_runtime_config()   # przeżywa restart
                    return jsonify({'ok': True, 'live': ok})
                meta = dict(_csi_meta)
                return jsonify({'ok': True, 'csi': True,
                                'settings': dict(CSI_CAM_SETTINGS),
                                'meta': meta})

            if request.method == 'POST':
                data = request.get_json(silent=True) or {}
                if data.pop('reset_auto', None):
                    # powrót do fabrycznego auto + wyczyszczenie zapisanych
                    # nadpisań (po restarcie kamera zostaje na auto)
                    for n, v in (('auto_exposure', 3), ('gain', 0),
                                 ('white_balance_automatic', 1),
                                 ('power_line_frequency', 1)):
                        set_ctrl(n, v)
                    CAMERA_OVERRIDES.clear()
                    _save_runtime_config()
                    return jsonify({'ok': True})
                errors = []
                for name, value in data.items():
                    if not re.match(r'^[a-z_]+$', name):
                        errors.append(f'Nieprawidłowa nazwa: {name}')
                        continue
                    try:
                        ok, err = set_ctrl(name, value)
                        if ok:
                            CAMERA_OVERRIDES[name] = int(value)
                        else:
                            errors.append(f'{name}: {err}')
                    except Exception as exc:
                        errors.append(str(exc))
                _save_runtime_config()   # ustawienia przeżywają restart
                if errors:
                    return jsonify({'ok': False, 'errors': errors}), 400
                return jsonify({'ok': True})
            return jsonify({'ok': True, 'controls': _v4l2_get_controls()})

        return app


# ── Dashcam: podglądy MP4 do odtwarzania w przeglądarce ─────────────────────
# Strumieniowanie MJPG na żywo lagowało i nie dawało przewijania — zamiast
# tego każdy segment dostaje mały H.264 (640p/12fps, ~4-6 MB), który <video>
# gra natywnie: pasek czasu, prędkości, range-requests, cache.

_rec_prep_jobs: set = set()
_rec_prep_lock = threading.Lock()
_rec_prep_info: dict = {}    # str(mkv) -> {'dur': s, 'prog': Path} (postęp ffmpeg)


def _rec_preview_path(p: Path) -> Path:
    return p.with_name(p.stem + "_prev.mp4")


def _make_rec_preview(p: Path) -> bool:
    """Transkoduje segment do małego MP4 (nice 19 — LPR ma pierwszeństwo)
    z wypalonym zegarem (data+czas, biegnie z nagraniem). Zapis przez plik
    tymczasowy: nigdy nie serwujemy niedokończonego MP4."""
    import subprocess as _sp
    prev = _rec_preview_path(p)
    tmp = p.with_name(p.stem + "_prev.tmp.mp4")
    # start segmentu = koniec zapisu (mtime) - czas trwania nagrania
    try:
        dur = float(_sp.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(p)],
            capture_output=True, text=True, timeout=30).stdout.strip()
            or DASHCAM_SEG_SEC)
    except Exception:
        dur = float(DASHCAM_SEG_SEC)
    try:
        start = int(p.stat().st_mtime - dur)
    except OSError:
        return False
    # plik postępu ffmpeg (-progress): czyta go /api/rec-prep -> pasek w www
    prog = p.with_name(p.stem + "_prev.progress")
    _rec_prep_info[str(p)] = {"dur": dur, "prog": prog}
    vf = _vf_z_obrotem("fps=12,scale=640:-2", p)
    font = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    if os.path.exists(font):
        # %{pts:localtime:START} = zegar scienny biegnacy z filmem
        vf += (f",drawtext=fontfile={font}:"
               f"text='%{{pts\\:localtime\\:{start}}}':"
               "fontcolor=white:fontsize=20:box=1:boxcolor=black@0.45:"
               "boxborderw=6:x=12:y=h-th-12")
    try:
        r = _sp.run(["nice", "-n", "19", "ffmpeg", "-nostdin",
                     "-loglevel", "quiet",
                     "-progress", str(prog), "-nostats",
                     "-i", str(p),
                     "-vf", vf,
                     "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30",
                     "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                     "-an", "-y", str(tmp)],
                    capture_output=True, timeout=300)
        if r.returncode == 0 and tmp.exists() and tmp.stat().st_size > 1000:
            tmp.rename(prev)
            return True
    except Exception as e:
        _zglos("podglad_nagrania", e)
    finally:
        _rec_prep_info.pop(str(p), None)
        try:
            prog.unlink(missing_ok=True)
        except OSError:
            pass
    try:
        tmp.unlink(missing_ok=True)
    except OSError:
        pass
    return False


# ── Dashcam: rotacja nagrań (20 GB) ──────────────────────────────────────────

def _dashcam_janitor() -> None:
    """Wątek tła: pilnuje limitów (pendrive DASHCAM_MAX_GB, bufor na SD
    DASHCAM_SD_MAX_GB), kasuje najstarsze segmenty."""
    while True:
        # sen NA POCZĄTKU pętli — pierwszy przebieg zaraz po starcie skasował
        # świeżo utworzony (jeszcze pusty) katalog sesji → crash splitmuxsink
        time.sleep(60)
        for katalog, gb in ((DASHCAM_DIR, DASHCAM_MAX_GB),
                            (DASHCAM_SD_DIR, DASHCAM_SD_MAX_GB)):
            if katalog == DASHCAM_DIR and not _pendrive_dostepny():
                continue
            if os.path.isdir(katalog):
                _dashcam_janitor_przebieg(Path(katalog), int(gb * 1_073_741_824))


def _dashcam_janitor_przebieg(base: Path, limit: int) -> None:
    try:
        files = []
        for p in base.rglob("*.mkv"):
            try:
                st = p.stat()
                files.append((st.st_mtime, st.st_size, p))
            except OSError:
                pass
        total = sum(s for _, s, _ in files)
        if total > limit:
            files.sort()                      # najstarsze najpierw
            for _, s, p in files:
                if total <= limit:
                    break
                try:
                    p.unlink()
                    total -= s
                except OSError:
                    pass
        for d in base.iterdir():  # puste katalogi sesji precz
            # tylko starsze niż 10 min — nigdy bieżącej, dopiero co utworzonej
            if (d.is_dir() and not any(d.iterdir())
                    and time.time() - d.stat().st_mtime > 600):
                d.rmdir()
        # miniaturki/podglądy osierocone po rotacji (mkv skasowany) — precz
        for j in base.rglob("*.jpg"):
            if not j.with_suffix(".mkv").exists():
                try:
                    j.unlink()
                except OSError:
                    pass
        for m in base.rglob("*_prev.mp4"):
            src = m.with_name(m.name[:-len("_prev.mp4")] + ".mkv")
            if not src.exists():
                try:
                    m.unlink()
                except OSError:
                    pass
        # porzucone pliki postępu (po ubitym ffmpegu) — starsze niż 10 min
        for g in base.rglob("*_prev.progress"):
            try:
                if time.time() - g.stat().st_mtime > 600:
                    g.unlink()
            except OSError:
                pass
        # miniaturki Z GÓRY dla zamkniętych segmentów (max 3 na cykl,
        # nice 19 — lista nagrań w www ma się otwierać natychmiast,
        # bez generowania podglądów na żądanie)
        import subprocess as _sp
        made = 0
        segs = sorted(base.rglob("*.mkv"),
                      key=lambda x: x.stat().st_mtime, reverse=True)
        for p in segs:
            if made >= 3:
                break
            try:
                if time.time() - p.stat().st_mtime < DASHCAM_SEG_SEC + 15:
                    continue          # ten segment jeszcze się nagrywa
                t = p.with_suffix(".jpg")
                if t.exists():
                    continue
                _sp.run(["nice", "-n", "19", "ffmpeg", "-nostdin",
                         "-loglevel", "quiet",
                         "-ss", str(DASHCAM_SEG_SEC // 2), "-i", str(p),
                         "-frames:v", "1",
                         "-vf", _vf_z_obrotem(
                             f"scale={DASHCAM_THUMB_W}:-2", p),
                         "-q:v", "7", str(t)],
                        capture_output=True, timeout=30)
                made += 1
            except Exception as e:
                _zglos("miniatura_dashcam", e)
        # podglądy MP4 z góry: 1 na cykl, od najnowszych; odpuszczamy,
        # gdy trwa już transkodowanie na żądanie (klik w www) ALBO gdy
        # CPU zajęte (jazda z OCR) — transkodowanie podbijało chwilowo
        # do ~90% i przycinało fps; na postoju backlog się nadrobi
        if not _rec_prep_jobs and _psutil.cpu_percent(interval=0.5) < 55:
            for p in segs:
                try:
                    if time.time() - p.stat().st_mtime < DASHCAM_SEG_SEC + 15:
                        continue
                    if _rec_preview_path(p).exists():
                        continue
                    key = str(p)
                    with _rec_prep_lock:
                        if _rec_prep_jobs:
                            break
                        _rec_prep_jobs.add(key)
                    try:
                        _make_rec_preview(p)
                    finally:
                        with _rec_prep_lock:
                            _rec_prep_jobs.discard(key)
                    break
                except Exception as e:
                    _zglos("podglad_dashcam", e)
    except Exception as e:
        _zglos("dashcam_janitor", e)


def _pendrive_dostepny() -> bool:
    """/media/lpr to punkt AUTOMONTOWANIA — bez pendrive'a `isdir` mówi True,
    a dopiero listdir/mkdir rzuca ENODEV (01.10.2026: dashcam przez to nie
    wystartował na SD). Dlatego sprawdzamy urządzenie po UUID i próbujemy
    wylistować katalog."""
    try:
        if not os.path.exists(USB_DEV):
            return False
        os.listdir(os.path.dirname(DASHCAM_DIR))
        return True
    except OSError:
        return False


def _dashcam_dir() -> str:
    """Katalog nagrań: pendrive, a bez niego bufor na SD (01.10.2026)."""
    return DASHCAM_DIR if _pendrive_dostepny() else DASHCAM_SD_DIR


def _przeprowadzka_z_sd(usb_root: str) -> None:
    """Start Z pendrive'em, a na SD zostały pliki z pracy BEZ niego (np. noc
    przy drodze 01.10.2026): przenosimy je na pendrive, żeby nic nie zginęło.
    - sesje dashcamu  logs/dashcam/<sesja>   → /media/lpr/dashcam/<sesja>
    - zdarzenia radaru logs/radar/<dzień>/*  → /media/lpr/radar/<dzień>/
      (CSV próbek/kalibracji dopisywane do istniejących, zdjęcia/klipy
      przy kolizji nazwy dostają przyrostek _sd)
    NIE scalamy bazy logs/lpr/plates.db (kolizje id między bazami) — to
    osobne narzędzie; tu tylko log, ile tam czeka."""
    import shutil
    _ustap_procesor(15)
    time.sleep(90)                    # po starcie rozpoznawanie ma pierwszeństwo
    n = 0
    try:
        src = Path(DASHCAM_SD_DIR)
        dst = Path(usb_root) / "dashcam"
        if src.is_dir() and any(src.iterdir()):
            dst.mkdir(parents=True, exist_ok=True)
            for ses in sorted(src.iterdir()):
                if not ses.is_dir():
                    continue
                cel = dst / ses.name
                if cel.exists():
                    cel = dst / (ses.name + "_sd")
                shutil.move(str(ses), str(cel))
                n += 1
        src = Path("/home/admin/hailo-apps/logs/radar")
        dst = Path(usb_root) / "radar"
        if src.is_dir():
            for dzien in sorted(src.iterdir()):
                if not dzien.is_dir():
                    continue
                (dst / dzien.name).mkdir(parents=True, exist_ok=True)
                for f in sorted(dzien.iterdir()):
                    cel = dst / dzien.name / f.name
                    if cel.exists() and f.suffix.lower() == ".csv":
                        with open(f, encoding="utf-8") as a:
                            wiersze = a.readlines()
                        if wiersze and not wiersze[0][:1].isdigit():
                            wiersze = wiersze[1:]        # bez drugiego nagłówka
                        with open(cel, "a", encoding="utf-8") as b:
                            b.writelines(wiersze)
                        f.unlink()
                    else:
                        if cel.exists():
                            cel = cel.with_name(f.stem + "_sd" + f.suffix)
                        shutil.move(str(f), str(cel))
                    n += 1
                try:
                    dzien.rmdir()
                except OSError:
                    pass
        sd_db = Path("/home/admin/hailo-apps/logs/lpr/plates.db")
        if sd_db.exists():
            try:
                con = sqlite3.connect(f"file:{sd_db}?mode=ro", uri=True)
                ile = con.execute("SELECT COUNT(*) FROM sightings").fetchone()[0]
                try:
                    ile_r = con.execute("SELECT COUNT(*) FROM radar_zdarzenia").fetchone()[0]
                except sqlite3.Error:
                    ile_r = 0
                con.close()
                print(f"[Przeprowadzka] baza na SD ({sd_db}): {ile} zapisów, "
                      f"{ile_r} zdarzeń radaru — scalam narzędziem scal_baze_sd.py")
                # 02.10: scalanie robi osobne narzędzie (kopia bazy docelowej
                # `plates.db.przed_scaleniem_*`, wstawianie bez id, oceny
                # przepisane, zdjęcia kopiowane, baza SD → plates.db.scalona_*;
                # idempotentne). Przetestowane na kopii 02.10 (10 zapisów,
                # 10 ocen, 41 zdarzeń radaru, 110 plików).
                if ile or ile_r:
                    r = subprocess.run(
                        ["nice", "-n", "15", "python3",
                         "/home/admin/hailo-apps/scal_baze_sd.py",
                         "--sd", str(sd_db), "--usb", str(usb_root)],
                        capture_output=True, text=True, timeout=1800)
                    for linia in (r.stdout + r.stderr).strip().splitlines():
                        print(f"[Przeprowadzka/baza] {linia}", flush=True)
                    if r.returncode != 0:
                        _zglos("przeprowadzka_scalanie",
                               RuntimeError(f"scal_baze_sd.py kod {r.returncode}"))
            except Exception as e:
                _zglos("przeprowadzka_baza", e)
        print(f"[Przeprowadzka] przeniesiono z SD na pendrive: {n} sesji/plików")
    except Exception as e:
        _zglos("przeprowadzka_sd", e)


def _start_csi_dashcam() -> None:
    """Tryb CSI: wideorejestrator z TYCH SAMYCH klatek Arducama, które idą do
    LPR — wątek kamery wrzuca co 2. klatkę (15 fps) do appsrc, dalej x264
    (ultrafast, ~0.5 rdzenia) i segmenty MKV jak dotąd. C1 PRO odłączona
    2026-07-17 (szeroki dashcam jest osobno w aucie; tele-nagrania z Arducama
    dokumentują to, co daleko). Awaria nagrywania nie rusza LPR."""
    global _dashcam_active, _dash_pipeline, _dash_appsrc, _dash_caps_ok
    _dash_caps_ok = False        # nowy appsrc → caps trzeba ustawić od nowa
    base = _dashcam_dir()
    if base != DASHCAM_DIR:
        print(f"[Dashcam/CSI] brak pendrive — nagrywam na SD: {base} "
              f"(bufor {DASHCAM_SD_MAX_GB:g} GB)")
    try:
        import gi
        gi.require_version('Gst', '1.0')
        from gi.repository import Gst
        if not Gst.is_initialized():
            Gst.init(None)
        seg_dir = Path(base) / datetime.now().strftime("%Y%m%d_%H%M%S")
        seg_dir.mkdir(parents=True, exist_ok=True)
        # Obraz z sensora jest JUZ obrocony (Transform), wiec przy odtwarzaniu
        # nie wolno dokladac hflip,vflip. Znacznik odroznia te nagrania od
        # starszych, ktore obrotu nadal wymagaja. Tryb USB znacznika NIE
        # dostaje — tam kamera nie ma sprzetowego obrotu.
        try:
            (seg_dir / DASHCAM_ZNACZNIK).touch()
        except OSError as e:
            _zglos("znacznik_obrotu_dashcam", e)
        desc = (
            f"appsrc name=dash_src is-live=true format=time "
            f"max-buffers=30 leaky-type=downstream ! "
            f"queue max-size-buffers=30 max-size-bytes=0 max-size-time=0 "
            f"leaky=downstream ! "
            # 24.09: klatki przychodzą w RGB (wspólny lores z potokiem LPR);
            # x264 chce I420 — konwersja 1280×720 @15 fps ≈ 2 ms/klatkę
            f"videoconvert n-threads=1 ! video/x-raw,format=I420 ! "
            f"x264enc speed-preset=ultrafast tune=zerolatency "
            f"bitrate={DASHCAM_KBPS} key-int-max={DASHCAM_FPS * 2} threads=2 ! "
            f"h264parse ! splitmuxsink name=dash_mux muxer-factory=matroskamux "
            f"location={seg_dir}/jazda_%05d.mkv "
            f"max-size-time={DASHCAM_SEG_SEC * 1_000_000_000}")
        _dash_pipeline = Gst.parse_launch(desc)
        # Sesja PRZED oddaniem appsrc wątkowi kamery (on dopisuje t0_mono i
        # dokładny start przy pierwszej klatce). `pliki`: czas ścienny
        # otwarcia każdego segmentu — z PTS pierwszej próbki nowego pliku
        # (sygnał splitmuxsink), bez sumowania długości z ffprobe (dryf).
        sesja = {"dir": seg_dir, "start": datetime.now(), "pliki": {}}
        globals()["_dash_sesja"] = sesja

        def _nowy_plik(_el, frag_id, sample):
            nazwa = f"jazda_{frag_id:05d}.mkv"
            start, pts, teraz = None, None, datetime.now()
            try:
                buf = sample.get_buffer() if sample is not None else None
                pts = buf.pts if buf is not None else None
                if (pts is not None and pts != Gst.CLOCK_TIME_NONE
                        and "t0_mono" in sesja):
                    # x264enc dodaje do stempli 1000 h (3,6e15 ns), żeby DTS
                    # nie było ujemne — zmierzone 02.10 (pts 3600000000000000
                    # dla pierwszej klatki); bierzemy wariant pasujący do zegara
                    for kand in (pts, pts - 3_600_000 * 1_000_000_000):
                        if kand < 0:
                            continue
                        s = sesja["start"] + timedelta(seconds=kand / 1e9)
                        if -1.0 <= (teraz - s).total_seconds() <= 6.0:
                            start = s
                            break
            except Exception as e:
                _zglos("dashcam_segment_start", e)
            # sygnał przychodzi ≤ ~3 s po pierwszej klatce pliku (splitmuxsink
            # trzyma jeden GOP = 2 s); wynik poza tym oknem = zły PTS →
            # bierzemy zegar ścienny minus GOP (02.10: pierwszy test dał +16 h)
            if start is None or not (-1.0 <= (teraz - start).total_seconds() <= 6.0):
                if frag_id < 5:
                    print(f"[Dashcam/CSI] segment {nazwa}: PTS {pts} nie pasuje "
                          f"do zegara (start {start}) — biorę teraz − 2 s", flush=True)
                start = teraz - timedelta(seconds=0.0 if frag_id == 0 else 2.0)
            sesja["pliki"][nazwa] = start
            if frag_id < 3:
                print(f"[Dashcam/CSI] segment {nazwa}: start "
                      f"{start.strftime('%H:%M:%S.%f')[:-3]} (sygnał o "
                      f"{teraz.strftime('%H:%M:%S.%f')[:-3]}, pts {pts})", flush=True)
            return str(seg_dir / nazwa)

        try:
            _dash_pipeline.get_by_name("dash_mux").connect(
                "format-location-full", _nowy_plik)
        except Exception as e:
            _zglos("dashcam_format_location", e)
        _dash_appsrc = _dash_pipeline.get_by_name("dash_src")
        bus = _dash_pipeline.get_bus()
        bus.add_signal_watch()

        def _on_err(_bus, msg):
            global _dash_appsrc
            err, _dbg = msg.parse_error()
            print(f"[Dashcam/CSI] błąd — wyłączam nagrywanie: {err}")
            _dash_appsrc = None
            try:
                _dash_pipeline.set_state(Gst.State.NULL)
            except Exception as e2:
                _zglos("dashcam_stop_po_bledzie", e2)

        bus.connect("message::error", _on_err)
        watki_przed = _watki_teraz()
        _dash_pipeline.set_state(Gst.State.PLAYING)
        _dashcam_active = True
        # (`_dash_sesja` ustawione wyżej, przed appsrc — 02.10)

        def _priorytety():
            time.sleep(4.0)               # koder musi utworzyć wątki
            ile = _ustap_watki_dashcam(watki_przed, 12)
            wroc = _przywroc_priorytety_toru()
            print(f"[Dashcam/CSI] {ile} wątków nagrywania ustępuje"
                  + (f" (przywrócono {wroc} wątkom toru)" if wroc else ""))
        threading.Thread(target=_priorytety, daemon=True,
                         name="dash-prio").start()
        print(f"[Dashcam/CSI] {seg_dir} (H.264 {DASHCAM_KBPS} kbps, "
              f"{DASHCAM_FPS} fps, segmenty {DASHCAM_SEG_SEC} s)")
    except Exception as e:
        print(f"[Dashcam/CSI] wyłączony: {e}")


def _stop_dash_pipeline() -> None:
    global _dash_pipeline, _dash_appsrc
    _dash_appsrc = None
    if _dash_pipeline is None:
        return
    try:
        from gi.repository import Gst
        _dash_pipeline.set_state(Gst.State.NULL)
    except Exception as e:
        _zglos("dashcam_zatrzymanie", e)
    _dash_pipeline = None


# ── Kopie zapasowe bazy + bezpieczne wyjmowanie pendrive'a ───────────────────
# Baza (rozpoznania + OCENY człowieka) to najcenniejsza rzecz w systemie —
# zdjęcia da się nagrać ponownie, godzin weryfikacji nie. Kopie lądują na
# karcie SD, czyli na INNYM nośniku niż pendrive.

BACKUP_DIR      = "/home/admin/hailo-apps/backup"
PHOTO_BACKUP_DIR = "/home/admin/hailo-apps/backup/zdjecia"
BACKUP_KEEP     = 20      # rotacja kopii "auto"
BACKUP_EVERY_MIN = 30
PHOTO_MIN_FREE_GB = 5.0   # poniżej tego NIE kopiujemy zdjęć (karta SD)
_photo_stat = {"files": 0, "mb": 0, "when": "", "busy": False}
USB_UUID = os.environ.get("LPR_USB_UUID", "XXXX-XXXX")   # UUID pendrive'a z danymi (blkid)
USB_DEV  = f"/dev/disk/by-uuid/{USB_UUID}"

_usb_ready = threading.Event()
_usb_ready.set()          # ustawiony = wolno pisać na pendrive
# Ustawiony = TRWA przygotowanie do wyjęcia (checkpoint + kopia + sync).
# Bez tego kafel pokazywał „można wyjąć" już w pierwszej milisekundzie
# operacji, a rsync 3300 zdjęć potrafi lecieć kilkadziesiąt sekund —
# kierowca wyciągałby pendrive dokładnie w trakcie kopiowania.
_usb_busy = threading.Event()
_usb_state = "ok"         # ok | eject (przygotowany do wyjęcia) | gone


def _db_backup(db_path: str, tag: str = "auto") -> Optional[str]:
    """Spójna kopia bazy na SD — SQLite backup API działa na ŻYWEJ bazie
    (zwykłe kopiowanie pliku w trakcie zapisu dałoby uszkodzony plik)."""
    try:
        Path(BACKUP_DIR).mkdir(parents=True, exist_ok=True)
        if not os.path.exists(db_path):
            return None
        out = (Path(BACKUP_DIR) /
               f"plates_{datetime.now():%Y%m%d_%H%M%S}_{tag}.db")
        src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=15)
        dst = sqlite3.connect(str(out))
        with dst:
            src.backup(dst)
        src.close()
        dst.close()
        # rotacja tylko kopii automatycznych — ręczne i "eject" zostają
        autos = sorted(Path(BACKUP_DIR).glob("plates_*_auto.db"))
        for f in autos[:-BACKUP_KEEP]:
            try:
                f.unlink()
            except OSError:
                pass
        print(f"[Backup] {out.name} ({out.stat().st_size // 1024} kB)")
        return str(out)
    except Exception as e:
        print(f"[Backup] błąd: {e}")
        return None


def _photos_backup(save_dir: str) -> dict:
    """Przyrostowa kopia ZDJĘĆ (pełne klatki + cropy + kandydaci) na kartę SD.
    rsync --ignore-existing: JPG nigdy się nie zmieniają, więc kopiujemy tylko
    NOWE — pierwszy przebieg trwa, kolejne są błyskawiczne. Bez --delete:
    kopia przechowuje też to, co z pendrive'a zniknęło."""
    import subprocess as _sp
    if _photo_stat["busy"]:
        return {"ok": False, "reason": "kopia już trwa"}
    try:
        sv = os.statvfs("/home/admin")
        free_gb = sv.f_bavail * sv.f_frsize / (1024 ** 3)
        if free_gb < PHOTO_MIN_FREE_GB:
            print(f"[Backup] SD ma tylko {free_gb:.1f} GB — zdjęcia pominięte")
            return {"ok": False, "reason": f"mało miejsca ({free_gb:.1f} GB)"}
        _photo_stat["busy"] = True
        Path(PHOTO_BACKUP_DIR).mkdir(parents=True, exist_ok=True)
        r = _sp.run(["nice", "-n", "19", "rsync", "-a", "--ignore-existing",
                     save_dir.rstrip("/") + "/", PHOTO_BACKUP_DIR + "/"],
                    capture_output=True, text=True, timeout=3600)
        n = mb = 0
        for root, _dirs, files in os.walk(PHOTO_BACKUP_DIR):
            for f in files:
                n += 1
                try:
                    mb += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
        _photo_stat.update(files=n, mb=mb // 1048576,
                           when=datetime.now().strftime("%Y-%m-%d %H:%M"))
        print(f"[Backup] zdjęcia: {n} plików / {mb // 1048576} MB na SD"
              + ("" if r.returncode == 0 else f" (rsync kod {r.returncode})"))
        return {"ok": r.returncode == 0, "files": n, "mb": mb // 1048576}
    except Exception as e:
        print(f"[Backup] zdjęcia — błąd: {e}")
        return {"ok": False, "reason": str(e)[:80]}
    finally:
        _photo_stat["busy"] = False


def _usb_prepare_eject(db_path: str) -> Optional[str]:
    """Przygotuj pendrive do wyjęcia: stop zapisów, scal dziennik WAL,
    kopia na SD, sync. Po tym można bezpiecznie wyciągnąć."""
    global _usb_state
    import subprocess as _sp
    _usb_ready.clear()          # najpierw stop zapisom, potem dopiero reszta
    _usb_busy.set()
    _usb_state = "eject"
    _menu_banner("NIE WYCIAGAJ - przygotowuje pendrive...", "orange", 300.0)
    _stop_dash_pipeline()
    time.sleep(0.8)                     # daj dokończyć bieżący zapis
    try:
        con = sqlite3.connect(db_path, timeout=15)
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.close()
    except Exception as e:
        print(f"[USB] checkpoint: {e}")
    path = _db_backup(db_path, "eject")
    # zdjęcia z ostatniej jazdy też muszą trafić na SD przed wyjęciem
    _photos_backup(str(Path(db_path).parent))
    try:
        _sp.run(["sync"], timeout=60, capture_output=True)
    except Exception as e:
        # nieudany sync przed wyjęciem = ryzyko utraty danych, a użytkownik
        # i tak zobaczy „można wyjąć" — to MUSI być w logu
        _zglos("sync_przed_wyjeciem", e)
    _usb_busy.clear()
    _menu_banner("Pendrive gotowy - mozna wyjac", "green", 20.0)
    print("[USB] pendrive przygotowany — można wyjąć")
    return path


def _usb_resume() -> None:
    global _usb_state
    _usb_state = "ok"
    _usb_ready.set()
    if DASHCAM_ENABLED and CAM_SOURCE == "csi" and _dash_pipeline is None:
        _start_csi_dashcam()
    print("[USB] wznowiono zapis")


def _menu_banner(text: str, color: str = "blue", secs: float = 6.0) -> None:
    """Komunikat na dolnym banerze ekranu (ten sam co alerty z baz)."""
    global _alert_text, _alert_until, _alert_color, _alert_img
    _alert_text = text
    _alert_color = color
    _alert_img = None          # to nie trafienie — bez zdjęcia pojazdu
    _alert_until = time.monotonic() + secs


def _menu_backup_now() -> None:
    """Menu na ekranie: kopia bazy + zdjęć teraz, w tle, z info na banerze."""
    if _photo_stat["busy"]:
        _menu_banner("Kopia juz trwa...", "orange", 4.0)
        return
    if not SAVE_DB_PATH:
        _menu_banner("Brak pendrive - nie ma czego kopiowac", "red", 5.0)
        return

    def work():
        _menu_banner("Robie kopie zapasowa...", "blue", 600.0)
        db = _db_backup(SAVE_DB_PATH, "reczna")
        r = _photos_backup(str(Path(SAVE_DB_PATH).parent))
        if db and r.get("ok"):
            _menu_banner(f"Kopia gotowa: baza + {r.get('files', 0)} zdjec",
                         "green", 7.0)
        else:
            _menu_banner("Kopia z bledem - sprawdz logi", "red", 7.0)

    threading.Thread(target=work, daemon=True, name="menu-backup").start()


def _usb_watcher(db_path: str) -> None:
    """Pilnuje fizycznej obecności pendrive'a: zniknął → natychmiast stop
    zapisów (ochrona przed uszkodzeniem przy wyrwaniu bez ostrzeżenia),
    wrócił → wznowienie (automount montuje sam)."""
    global _usb_state
    while True:
        time.sleep(3)
        try:
            present = os.path.exists(USB_DEV)
            if not present and _usb_state != "gone":
                _usb_state = "gone"
                _usb_ready.clear()
                _stop_dash_pipeline()
                print("[USB] pendrive wyjęty — zapis wstrzymany")
            elif present and _usb_state == "gone":
                time.sleep(2)            # daj automount zamontować
                try:
                    os.listdir("/media/lpr")
                except OSError:
                    pass
                _usb_resume()
                _db_backup(db_path, "powrot")
        except Exception as e:
            print(f"[USB] watcher: {e}")


# ── Przeglądarka offline na pendrive ─────────────────────────────────────────

VIEWER_MASTER = "/home/admin/hailo-apps/local_resources/LPR_Przegladarka.exe"
VIEWER_NAME   = "LPR_Przegladarka.exe"


def _ensure_viewer_on_usb(usb_root: str) -> None:
    """Pilnuje, żeby na pendrive zawsze leżała windowsowa aplikacja do
    przeglądania zapisów offline (użytkownik zabiera pendrive z auta).
    Kopiuje wzorzec z local_resources gdy brak lub inna wersja (rozmiar).
    W tle — kopiowanie ~15 MB na pendrive nie może opóźniać startu LPR."""
    def work():
        try:
            master = Path(VIEWER_MASTER)
            if not master.exists():
                return
            target = Path(usb_root) / VIEWER_NAME
            if (target.exists()
                    and target.stat().st_size == master.stat().st_size):
                return
            import shutil
            tmp = target.with_suffix(".exe.part")
            shutil.copy2(master, tmp)
            tmp.replace(target)
            print(f"[Viewer] aplikacja offline wgrana na pendrive: {target}")
        except Exception as e:
            print(f"[Viewer] nie skopiowałem aplikacji: {e}")

    threading.Thread(target=work, daemon=True, name="viewer-usb").start()


# ── Wykrywanie pendrive'a ─────────────────────────────────────────────────────

def find_usb() -> Optional[str]:
    import subprocess, json
    try:
        out = subprocess.run(
            ["lsblk", "-J", "-o", "NAME,TRAN,MOUNTPOINT,TYPE"],
            capture_output=True, text=True, timeout=5,
        )
        for dev in json.loads(out.stdout).get("blockdevices", []):
            for part in [dev] + dev.get("children", []):
                tran = part.get("tran") or dev.get("tran")
                if tran != "usb":
                    continue
                mp = part.get("mountpoint")
                if not mp:
                    continue
                try:
                    test = Path(mp) / ".write_test"
                    test.touch()
                    test.unlink()
                    return mp
                except OSError:
                    continue
    except Exception as e:
        _zglos("szukanie_pendrive", e)
    media = Path("/media/admin")
    if media.exists():
        for d in sorted(media.iterdir()):
            if d.name.startswith(".") or not d.is_dir():
                continue
            try:
                test = d / ".write_test"
                test.touch()
                test.unlink()
                return str(d)
            except OSError:
                continue
    return None


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    _set_boot("start", "Uruchamianie…", "Wczytywanie ustawień")
    _ensure_wallpaper()      # pulpit wygląda jak ekran startowy (brak migania)
    _load_runtime_config()   # progi + strefa + kamera zapisane przez www
    print(f"[LPR] Źródło obrazu: {CAM_SOURCE}")
    if CAM_SOURCE == "csi":
        _apply_csi_settings()      # HDR sensora PRZED otwarciem przez Picamera2
        _csi_write_tuning()        # sufit migawki: env PRZED startem libcamery
        _patch_picamera_thread()   # kontrolki AE w wątku kamery
    else:
        _apply_camera_settings()
    usb = find_usb()
    if usb:
        save_dir = Path(usb) / "lpr"
        print(f"[LPR] Pendrive: {usb}  →  {save_dir}")
        _ensure_viewer_on_usb(usb)   # aplikacja offline zawsze na pendrive
    else:
        save_dir = Path("/home/admin/hailo-apps/logs/lpr")
        print(f"[LPR] Brak pendrive'a — zapis lokalny: {save_dir}")

    save_dir.mkdir(parents=True, exist_ok=True)
    db_path = save_dir / "plates.db"

    ocr  = OcrWorker()
    save = SaveWorker(save_dir, db_path)
    global SAVE_WORKER, SAVE_DB_PATH
    SAVE_WORKER = save        # ręczne FOTO (klawisz P / www) pisze tą samą drogą
    SAVE_DB_PATH = str(db_path)
    # radar LD2415H (25.09): wątek czyta port zawsze (tani, idle bez ruchu);
    # zdarzenia buduje tylko w trybie RADAR
    globals()["RADAR"] = RadarWorker()
    RADAR.start()
    mgr  = TrackManager(ocr, save)

    # Serwer webowy
    web = WebServer(db_path, save_dir)
    web.start()
    ip  = _get_local_ip()
    print(f"[Web] Interfejs dostępny: http://{ip}:{WebServer.PORT}")

    st   = LprState(mgr, web_server=web, web_ip=ip)
    app  = LprApp(app_callback, st)

    if DASHCAM_ENABLED:
        threading.Thread(target=_dashcam_janitor, daemon=True,
                         name="dashcam-janitor").start()
    if DASHCAM_ENABLED and CAM_SOURCE == "csi":
        # Nagrywanie startuje DOPIERO, gdy potok rozpoznawania płynie.
        # Dwa powody: (1) rozpoznawanie ma pierwszeństwo przy rozruchu,
        # (2) dopiero wtedy wątki potoku mają swoje nazwy, więc da się
        # bezpiecznie odróżnić od nich wątki kodera i spowolnić tylko je.
        def _dashcam_po_starcie():
            czekaj_do = time.monotonic() + 40.0
            while time.monotonic() < czekaj_do:
                if _stat_cb_count > 30:      # klatki realnie płyną
                    break
                time.sleep(1.0)
            time.sleep(2.0)
            _start_csi_dashcam()
        threading.Thread(target=_dashcam_po_starcie, daemon=True,
                         name="dashcam-start").start()
    # strażnik priorytetów NIEZALEŻNIE od nagrywania — rozpoznawanie ma
    # mieć pierwszeństwo zawsze
    threading.Thread(target=_straznik_priorytetow, daemon=True,
                     name="prio-guard").start()
    threading.Thread(target=_audio_straznik, daemon=True,
                     name="audio-guard").start()
    threading.Thread(target=_audio_zdarzenia, daemon=True,
                     name="audio-events").start()   # pasek głośności bez zwłoki
    _bt_autoconnect()          # zapamiętany głośnik BT (stały nadzór w tle)
    if usb:
        _db_backup(str(db_path), "start")
        threading.Thread(target=_przeprowadzka_z_sd, args=(usb,),
                         daemon=True, name="przeprowadzka-sd").start()
        # pierwsza kopia zdjęć w tle (kolejne przyrostowo — tylko nowe pliki)
        threading.Thread(target=lambda: _photos_backup(str(save_dir)),
                         daemon=True, name="photo-backup-start").start()
        threading.Thread(target=_usb_watcher, args=(str(db_path),),
                         daemon=True, name="usb-watch").start()

        def _backup_loop():
            _ustap_procesor(15)   # rsync tysięcy plików w tle
            while True:
                time.sleep(BACKUP_EVERY_MIN * 60)
                if _usb_ready.is_set():
                    _db_backup(str(db_path), "auto")
                    _photos_backup(str(save_dir))   # przyrostowo, tylko nowe

        threading.Thread(target=_backup_loop, daemon=True,
                         name="db-backup").start()

    try:
        app.run()
    finally:
        print("[LPR] Zamykanie — finalizacja otwartych tracków...")
        _stop_dash_pipeline()
        mgr.flush()
        time.sleep(3)
        save.stop()
        ocr.stop()
        print("[LPR] Zakończono.")


if __name__ == "__main__":
    main()
