#!/usr/bin/env python3
"""
Zbieracz danych tablic rejestracyjnych do treningu YOLO11.

Zasady zapisu (dane treningowe na pendrivie):
  Tablica  : środek tablicy (pewność ≥ 0.6) w ROI przez 3 kolejne klatki
             → zapis klatki + adnotacji YOLO, cooldown 5s
  Tło      : brak jakiejkolwiek tablicy (pewność ≥ 0.6) przez 3 kolejne klatki
             + minęły 2 minuty od ostatniego tła → zapis klatki bez adnotacji

Historia tablic (plates_history/ na pendrivie):
  Przy każdym zapisie tablicy: wycinek → OCR na Hailo (CRNN, shared VDevice)
  → plik JPG z numerem + wpis do SQLite.
  Liczy unikalne przejazdy (24 h przerwy między liczeniem).
  W podglądzie (prawy-dolny róg): lista ostatnio rozpoznanych tablic z licznikami.

ESC        : zamknięcie skryptu

Klasy docelowego modelu:
  0: car  1: bus  2: truck  3: motorcycle  4: plate
"""

import multiprocessing
import numpy as np
import os
import queue as _queue
import re
import signal
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import cv2
import hailo


# ── Atomiczny zapis plików ────────────────────────────────────────────────────

def _fsync_dir(directory: Path) -> None:
    fd = os.open(str(directory), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def safe_write_image(path: Path, img_bgr, quality: int = 92) -> bool:
    # Tymczasowy plik musi mieć rozszerzenie .jpg — OpenCV określa format po nim
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
        print(f"[Kolektor] BŁĄD zapisu obrazu {path.name}: {e}")
        try:
            tmp.unlink()
        except OSError:
            pass
        return False


def safe_write_text(path: Path, content: str) -> bool:
    tmp = path.with_suffix(".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(tmp), str(path))
        _fsync_dir(path.parent)
        return True
    except Exception as e:
        print(f"[Kolektor] BŁĄD zapisu tekstu {path.name}: {e}")
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        return False


# ── Monkey-patch: fullscreen cv2 + obsługa ESC ───────────────────────────────

def _fullscreen_display(user_data):
    import os, signal
    cv2.namedWindow("Kolektor", cv2.WINDOW_NORMAL)
    cv2.setWindowProperty("Kolektor", cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)
    while user_data.running:
        frame = user_data.get_frame()
        if frame is not None:
            cv2.imshow("Kolektor", frame)
        key = cv2.waitKey(1) & 0xFF
        if key == 27:  # ESC
            user_data.exit_requested.set()
            os.kill(user_data.main_pid, signal.SIGINT)
            break
        elif key in (ord('t'), ord('T')):  # T — ręczne zdjęcie
            user_data.manual_capture.set()
    cv2.destroyAllWindows()

import hailo_apps.python.core.gstreamer.gstreamer_app as _gsa
_gsa.display_user_data_frame = _fullscreen_display

from hailo_apps.python.pipeline_apps.detection.detection_pipeline import GStreamerDetectionApp
from hailo_apps.python.core.common.buffer_utils import get_caps_from_pad, get_numpy_from_buffer
from hailo_apps.python.core.gstreamer.gstreamer_app import app_callback_class
from hailo_apps.python.core.common.hailo_logger import get_logger
from hailo_apps.python.core.common.hailo_inference import HailoInfer
from hailo_apps.python.core.common.core import resolve_hef_path

logger = get_logger(__name__)

# ── Konfiguracja ──────────────────────────────────────────────────────────────

ROI              = (0.00, 0.33, 1.00, 0.67)
PLATE_MIN_CONF   = 0.6
PLATE_CONSEC     = 3
COOLDOWN_SEC     = 5.0
BG_CONSEC        = 3
BG_INTERVAL_SEC  = 120.0
JPEG_QUALITY     = 92
FLASH_SEC        = 0.5

OCR_MIN_CONF     = 0.55   # minimalna pewność CRNN — poniżej = UNKNOWN
HISTORY_DIRNAME  = "plates_history"
UNIQUE_GAP_H     = 24     # godziny przerwy wymagane do liczenia jako nowy przejazd
OCR_DISPLAY_SEC  = 12.0   # jak długo wynik OCR widoczny w nakładce

LABEL_TO_CLASS = {"vehicle": 0, "license_plate": 4}
CLASSES        = ["car", "bus", "truck", "motorcycle", "plate"]

# Jakość danych treningowych
BLUR_MIN_SCORE   = 40.0   # Laplacian variance — poniżej = klatka zbyt rozmyta, odrzuć
DISK_MIN_MB      = 300    # minimalne wolne miejsce na pendrivie (MB) — poniżej = stop
PLATE_BBOX_MIN_AREA   = 0.0005  # min. powierzchnia bbox tablicy (% klatki) — odfiltruj szum
PLATE_BBOX_ASPECT_MIN = 1.2     # min. współczynnik szer/wys dla tablicy (prostokąt poziomy)
PLATE_BBOX_ASPECT_MAX = 8.0     # max. współczynnik szer/wys

COLOR_VEHICLE    = (0, 165, 255)
COLOR_PLATE      = (0, 255, 0)
COLOR_ROI_IDLE   = (0, 200, 255)
COLOR_ROI_ARMED  = (0, 255, 180)
COLOR_ROI_SAVED  = (0, 255, 0)
COLOR_BG_SAVED   = (255, 100, 0)


# ── Wykrywanie pendrive'a ─────────────────────────────────────────────────────

def find_usb() -> str | None:
    """
    Zwraca punkt montowania pierwszego zapisywalnego dysku USB.
    Używa lsblk (TRAN=usb) więc działa niezależnie od nazwy punktu montowania.
    Fallback: skanuje /media/admin/ testując zapis.
    """
    import subprocess, json

    # Metoda 1: lsblk — pewne wykrycie po typie transportu
    try:
        out = subprocess.run(
            ["lsblk", "-J", "-o", "NAME,TRAN,MOUNTPOINT,TYPE"],
            capture_output=True, text=True, timeout=5
        )
        for dev in json.loads(out.stdout).get("blockdevices", []):
            # Sprawdź sam dysk i jego partycje
            candidates = [dev] + dev.get("children", [])
            for part in candidates:
                # USB transport na poziomie dysku-rodzica lub partycji
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
    except Exception:
        pass

    # Fallback: skanuj /media/admin/ testując zapis (działa gdy lsblk niedostępny)
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


# ── Walidacja i deduplicacja tablic ──────────────────────────────────────────

PLATE_MIN_LEN    = 4     # minimalna długość tekstu tablicy
PLATE_MAX_LEN    = 9     # maksymalna długość (polskie tablice: 5-8)
DEDUP_WINDOW_SEC = 60.0  # okno czasowe fuzzy-dedup
FUZZY_MAX_DIST   = 2     # maks. odległość edycji → ta sama tablica


def _edit_distance(a: str, b: str) -> int:
    """Odległość Levenshteina — szybkie odrzucenie gdy różnica długości > FUZZY_MAX_DIST."""
    if abs(len(a) - len(b)) > FUZZY_MAX_DIST:
        return FUZZY_MAX_DIST + 1
    m, n = len(a), len(b)
    dp = list(range(n + 1))
    for i in range(1, m + 1):
        prev, dp[0] = dp[0], i
        for j in range(1, n + 1):
            temp = dp[j]
            dp[j] = prev if a[i-1] == b[j-1] else 1 + min(prev, dp[j], dp[j-1])
            prev = temp
    return dp[n]


def _is_valid_plate(text: str) -> bool:
    """Sprawdza czy tekst wygląda jak numer rejestracyjny (nie czyste śmieci OCR)."""
    if not (PLATE_MIN_LEN <= len(text) <= PLATE_MAX_LEN):
        return False
    has_letter = any(c.isalpha() for c in text)
    has_digit  = any(c.isdigit() for c in text)
    return has_letter and has_digit


# ── OCR: preprocessing + dekodowanie CRNN ────────────────────────────────────

_OCR_CHARACTERS = [
    'blank','0','1','2','3','4','5','6','7','8','9',':',';','<','=','>','?','@',
    'A','B','C','D','E','F','G','H','I','J','K','L','M','N','O','P','Q','R','S',
    'T','U','V','W','X','Y','Z','[','\\',']','^','_','`','a','b','c','d','e','f',
    'g','h','i','j','k','l','m','n','o','p','q','r','s','t','u','v','w','x','y',
    'z','{','|','}','~','!','"','#','$','%','&',"'",'(',')',
    '*','+',',','-','.','/','  ','  ',
]


def _resize_for_ocr(crop_bgr: np.ndarray,
                    target_h: int = 48, target_w: int = 320,
                    pad: int = 128) -> np.ndarray:
    h, w = crop_bgr.shape[:2]
    if h == 0 or w == 0:
        return np.full((target_h, target_w, 3), pad, dtype=np.uint8)
    scale = min(target_w / w, target_h / h)
    nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
    resized = cv2.resize(crop_bgr, (nw, nh), interpolation=cv2.INTER_CUBIC)
    pt = (target_h - nh) // 2;  pb = target_h - nh - pt
    pl = (target_w - nw) // 2;  pr = target_w - nw - pl
    return cv2.copyMakeBorder(resized, pt, pb, pl, pr,
                               cv2.BORDER_CONSTANT, value=[pad] * 3)


def _decode_crnn(raw: np.ndarray):
    """Dekoduje wyjście CRNN → lista (text, confidence)."""
    if raw.ndim == 2:
        raw = raw[np.newaxis]
    text_idx  = raw.argmax(axis=2)
    text_prob = raw.max(axis=2)
    results = []
    for b in range(len(text_idx)):
        sel = np.ones(len(text_idx[b]), dtype=bool)
        sel[1:] = text_idx[b][1:] != text_idx[b][:-1]
        sel    &= text_idx[b] != 0
        chars   = [_OCR_CHARACTERS[i]
                   for i in text_idx[b][sel]
                   if i < len(_OCR_CHARACTERS)]
        conf = float(text_prob[b][sel].mean()) if sel.any() else 0.0
        results.append(("".join(chars), conf))
    return results


# ── Worker: historia tablic (OCR + SQLite) ────────────────────────────────────

class PlatesHistoryWorker:
    """
    Wątek tła: OCR na Hailo (CRNN, shared VDevice) + SQLite.
    Liczy unikalne przejazdy: ta sama tablica w oknie 24 h = jeden przejazd.
    Wyniki odsyła przez result_queue → nakładka na podglądzie.
    """

    def __init__(self, history_dir: Path):
        self.history_dir = history_dir
        try:
            history_dir.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            # FAT32 może mieć problem z uprawnieniami jeśli folder tworzył root
            # Próbujemy chmod, jeśli nie — kontynuujemy bez historii
            try:
                import subprocess
                subprocess.run(["sudo", "chmod", "777", str(history_dir)],
                               capture_output=True, timeout=5)
                history_dir.mkdir(parents=True, exist_ok=True)
            except Exception:
                raise  # odda wyjątek do main() który obsłuży go łagodnie

        db_path       = history_dir / "plates.db"
        self._db_path = str(db_path)
        self._setup_db()

        # Kolejka wyników → CollectorState (do nakładki)
        self.result_queue: _queue.Queue = _queue.Queue(maxsize=100)

        # Model CRNN na Hailo
        self._ocr   = None
        self._ocr_h = 48
        self._ocr_w = 320
        try:
            ocr_hef = resolve_hef_path("ocr", "paddle_ocr", "hailo10h")
            self._ocr = HailoInfer(str(ocr_hef), batch_size=1, priority=1)
            shape = self._ocr.get_input_shape()
            self._ocr_h, self._ocr_w = shape[0], shape[1]
            print(f"[Historia] OCR na Hailo aktywny — {self._ocr_w}×{self._ocr_h} px")
        except Exception as e:
            print(f"[Historia] UWAGA: OCR niedostępny ({e})")

        # Pamięć krótkoterminowa do fuzzy-dedup: {plate_text: (timestamp, ocr_conf)}
        self._recent: dict = {}

        self._wqueue  = _queue.Queue(maxsize=30)
        self._stopped = threading.Event()
        self._thread  = threading.Thread(target=self._loop,
                                         name="plates-history", daemon=True)
        self._thread.start()

    # ── Baza danych ───────────────────────────────────────────────────────────

    def _setup_db(self):
        con = sqlite3.connect(self._db_path)
        con.executescript("""
            CREATE TABLE IF NOT EXISTS plates (
                plate_text   TEXT PRIMARY KEY,
                first_seen   TEXT NOT NULL,
                last_seen    TEXT NOT NULL,
                total_count  INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS sightings (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                plate_text      TEXT    NOT NULL,
                seen_at         TEXT    NOT NULL,
                image_filename  TEXT,
                det_confidence  REAL,
                ocr_confidence  REAL,
                is_new_sighting INTEGER DEFAULT 1
            );
            CREATE INDEX IF NOT EXISTS idx_sightings_plate
                ON sightings(plate_text);
        """)
        con.commit()
        con.close()

    # ── API ───────────────────────────────────────────────────────────────────

    def submit(self, frame_bgr: np.ndarray,
               plate_bboxes: list, det_confs: list, ts: str):
        """Non-blocking submit z callbacku GStreamer."""
        try:
            self._wqueue.put_nowait((frame_bgr.copy(), plate_bboxes, det_confs, ts))
        except _queue.Full:
            pass

    def stop(self):
        self._stopped.set()
        self._wqueue.put(None)
        self._thread.join(timeout=5)
        if self._ocr:
            try:
                self._ocr.close()
            except Exception:
                pass

    # ── Wątek roboczy ─────────────────────────────────────────────────────────

    def _loop(self):
        while not self._stopped.is_set():
            item = self._wqueue.get()
            if item is None:
                break
            try:
                self._process(*item)
            except Exception as e:
                print(f"[Historia] błąd: {e}")

    def _ocr_one(self, crop_bgr: np.ndarray):
        """OCR jednego wycinka → (clean_text, conf)."""
        if self._ocr is None:
            return "UNKNOWN", 0.0
        try:
            prep    = _resize_for_ocr(crop_bgr, self._ocr_h, self._ocr_w)
            _result = []

            def _cb(completion_info, bindings_list=None):
                if not completion_info.exception and bindings_list:
                    _result.append(bindings_list[0].output().get_buffer())

            job = self._ocr.run([prep], _cb)
            job.wait(3000)

            if not _result:
                return "UNKNOWN", 0.0
            decoded = _decode_crnn(_result[0])
            if not decoded:
                return "UNKNOWN", 0.0
            text, conf = decoded[0]
            clean = re.sub(r'[^A-Z0-9]', '', text.upper())
            return (clean, conf) if clean and conf >= OCR_MIN_CONF else ("UNKNOWN", conf)
        except Exception as e:
            logger.debug("OCR error: %s", e)
            return "UNKNOWN", 0.0

    def _fuzzy_match_recent(self, text: str, now_ts: float):
        """
        Szuka w _recent tablicy podobnej do text (odl. Levenshteina ≤ FUZZY_MAX_DIST,
        widzianej w ciągu DEDUP_WINDOW_SEC sekund).
        Zwraca (canonical_text, ocr_conf) najlepszego dopasowania lub None.
        """
        best = None
        best_len = 0
        # Usuń wygasłe wpisy
        expired = [k for k, (t, _) in self._recent.items()
                   if now_ts - t > DEDUP_WINDOW_SEC]
        for k in expired:
            del self._recent[k]

        for existing, (_, conf) in self._recent.items():
            dist = _edit_distance(text, existing)
            if dist <= FUZZY_MAX_DIST:
                # Jeśli wiele dopasowań, wybierz dłuższe (więcej znaków = pełniejszy odczyt)
                if len(existing) > best_len:
                    best      = (existing, conf)
                    best_len  = len(existing)
        return best

    def _process(self, frame_bgr, plate_bboxes, det_confs, ts):
        h, w = frame_bgr.shape[:2]
        results = []   # (plate_text, det_conf, ocr_conf, is_new_sighting, total_count)

        now_dt  = datetime.now()
        now_ts  = now_dt.timestamp()
        now_str = now_dt.isoformat(sep=" ", timespec="seconds")
        gap     = timedelta(hours=UNIQUE_GAP_H)

        con = sqlite3.connect(self._db_path)

        for bbox, det_conf in zip(plate_bboxes, det_confs):
            xmin, ymin, xmax, ymax = bbox
            x1 = max(0, int(xmin * w));  y1 = max(0, int(ymin * h))
            x2 = min(w, int(xmax * w));  y2 = min(h, int(ymax * h))
            crop = frame_bgr[y1:y2, x1:x2]
            plate_text, ocr_conf = self._ocr_one(crop)

            # ── Filtr 1: podstawowa walidacja ─────────────────────────────────
            if not _is_valid_plate(plate_text):
                print(f"[Historia] odrzucono '{plate_text}' — nieprawidłowy format")
                continue

            # ── Filtr 2: fuzzy dedup w oknie {DEDUP_WINDOW_SEC}s ─────────────
            match = self._fuzzy_match_recent(plate_text, now_ts)
            if match:
                canonical, _ = match
                if len(plate_text) > len(canonical):
                    # Nowy odczyt jest dłuższy → poprawka w _recent
                    del self._recent[canonical]
                    self._recent[plate_text] = (now_ts, ocr_conf)
                    print(f"[Historia] korekta: '{canonical}' → '{plate_text}'")
                    plate_text = plate_text   # użyj nowego
                else:
                    # Istniejący jest dłuższy/równy → użyj go jako kanonicznego
                    plate_text = canonical
                    self._recent[canonical] = (now_ts, ocr_conf)
            else:
                self._recent[plate_text] = (now_ts, ocr_conf)

            # ── Logika 24h: czy nowe unikalne wykrycie? ───────────────────────
            row = con.execute(
                "SELECT last_seen, total_count FROM plates WHERE plate_text=?",
                (plate_text,)
            ).fetchone()

            if row:
                last_seen_dt  = datetime.fromisoformat(row[0])
                old_count     = row[1]
                is_new        = (now_dt - last_seen_dt) >= gap
            else:
                old_count = 0
                is_new    = True

            new_count = old_count + (1 if is_new else 0)

            con.execute("""
                INSERT INTO plates (plate_text, first_seen, last_seen, total_count)
                VALUES (?, ?, ?, 1)
                ON CONFLICT(plate_text) DO UPDATE SET
                    last_seen   = excluded.last_seen,
                    total_count = total_count + ?
            """, (plate_text, now_str, now_str, 1 if is_new else 0))

            con.execute("""
                INSERT INTO sightings
                    (plate_text, seen_at, image_filename, det_confidence,
                     ocr_confidence, is_new_sighting)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (plate_text, now_str, None, det_conf, ocr_conf, int(is_new)))

            results.append((plate_text, det_conf, ocr_conf, is_new, new_count))

        # Zapisz zdjęcie (nazwa z rozpoznanych numerów)
        known     = [r[0] for r in results if r[0] != "UNKNOWN"]
        name_part = "_".join(dict.fromkeys(known)) if known else "UNKNOWN"
        filename  = f"{name_part}_{ts}.jpg"
        img_path  = self.history_dir / filename

        # Zaktualizuj image_filename w sightings
        con.execute(
            "UPDATE sightings SET image_filename=? WHERE seen_at=? AND image_filename IS NULL",
            (filename, now_str)
        )
        con.commit()
        con.close()

        safe_write_image(img_path, frame_bgr, quality=JPEG_QUALITY)

        # Odeślij wyniki do nakładki
        for plate_text, det_conf, ocr_conf, is_new, count in results:
            try:
                self.result_queue.put_nowait((plate_text, count, is_new))
            except _queue.Full:
                pass

        labels = [f"{r[0]}({'nowa' if r[3] else str(r[4])+'×'})" for r in results]
        print(f"[Historia] {filename}  |  {', '.join(labels)}")


# ── Subklasa: fakesink zamiast fpsdisplaysink ────────────────────────────────

class CollectorDetectionApp(GStreamerDetectionApp):
    def __init__(self, app_callback, user_data):
        if "--use-frame" not in sys.argv:
            sys.argv.append("--use-frame")
        super().__init__(app_callback, user_data)
        self.options_menu.use_frame = True
        user_data.use_frame = True

    def get_pipeline_string(self):
        pipeline = super().get_pipeline_string()
        pipeline = re.sub(
            r"fpsdisplaysink\s+name=\S+.*",
            "fakesink sync=false",
            pipeline,
        )
        return pipeline


# ── Stan callbacku ────────────────────────────────────────────────────────────

class CollectorState(app_callback_class):
    def __init__(self, save_dir: str, ocr_worker: PlatesHistoryWorker | None = None):
        super().__init__()

        self.save_dir = Path(save_dir)
        self.save_dir.mkdir(parents=True, exist_ok=True)
        safe_write_text(self.save_dir / "classes.txt", "\n".join(CLASSES) + "\n")

        self.bg_dir = self.save_dir / "background"
        self.bg_dir.mkdir(exist_ok=True)
        safe_write_text(self.bg_dir / "classes.txt", "\n".join(CLASSES) + "\n")

        self.ocr_worker       = ocr_worker
        # Wyniki OCR z workerka: {plate_text: (count, is_new, expire_monotonic)}
        self.ocr_display: dict = {}
        self.ocr_result_queue = ocr_worker.result_queue if ocr_worker else None

        self.plate_consec    = 0
        self.no_plate_consec = 0
        self.last_save_time  = 0.0
        self.last_bg_time    = -BG_INTERVAL_SEC
        self.last_flash_time = -999.0
        self.last_flash_bg   = -999.0
        self.saved_count     = 0
        self.bg_count        = 0

        self.exit_requested  = multiprocessing.Event()
        self.manual_capture  = multiprocessing.Event()
        self.manual_count    = 0
        self.last_flash_manual = -999.0
        self.main_pid        = os.getpid()

        print(f"\n[Kolektor] Folder sesji : {self.save_dir}")
        print(f"[Kolektor] Folder tła   : {self.bg_dir}")
        print(f"[Kolektor] ROI          : pełna szerokość, środkowa 1/3 wys.")
        print(f"[Kolektor] Pewność      : ≥ {PLATE_MIN_CONF}  |  Klatki: {PLATE_CONSEC} z rzędu")
        print(f"[Kolektor] Tło          : {BG_CONSEC} klatki bez tablicy co {int(BG_INTERVAL_SEC//60)} min\n")


# ── Nakładka OCR (prawy-dolny róg) ───────────────────────────────────────────

def _draw_ocr_overlay(frame: np.ndarray, ocr_display: dict, now: float) -> None:
    """Rysuje listę ostatnio rozpoznanych tablic w prawym-dolnym rogu."""
    # Usuń wygasłe wpisy
    expired = [k for k, v in ocr_display.items() if now >= v[2]]
    for k in expired:
        del ocr_display[k]

    if not ocr_display:
        return

    # Posortuj od najnowszego
    entries = sorted(ocr_display.items(), key=lambda x: x[1][2], reverse=True)[:6]

    font   = cv2.FONT_HERSHEY_SIMPLEX
    fscale = 0.55
    fthick = 1
    pad    = 7
    lh     = 20   # wysokość linii

    # Zbuduj teksty
    lines = []
    for plate_text, (count, is_new, _) in entries:
        if is_new:
            suffix = "  NOWA!"
        else:
            suffix = f"  {count}x"
        lines.append((plate_text + suffix, is_new))

    # Szerokość bloku
    max_tw = max(cv2.getTextSize(t, font, fscale, fthick)[0][0] for t, _ in lines)
    box_w  = max_tw + pad * 2
    box_h  = len(lines) * lh + pad * 2

    h, w = frame.shape[:2]
    bx = w - box_w - 8
    by = h - box_h - 8

    # Czarne tło (lekko przezroczyste przez blending)
    overlay = frame.copy()
    cv2.rectangle(overlay, (bx - 2, by - 2), (bx + box_w + 2, by + box_h + 2),
                  (0, 0, 0), -1)
    cv2.addWeighted(overlay, 0.65, frame, 0.35, 0, frame)

    for i, (text, is_new) in enumerate(lines):
        color = (100, 255, 100) if is_new else (255, 255, 255)
        ty = by + pad + (i + 1) * lh - 3
        cv2.putText(frame, text, (bx + pad, ty), font, fscale, color, fthick,
                    cv2.LINE_AA)


# ── Callback ──────────────────────────────────────────────────────────────────

def app_callback(element, buffer, user_data: CollectorState):
    if buffer is None:
        return

    pad_el = element.get_static_pad("src")
    fmt, width, height = get_caps_from_pad(pad_el)
    if not all([fmt, width, height]):
        return

    frame_rgb = None
    if user_data.use_frame:
        frame_rgb = get_numpy_from_buffer(buffer, fmt, width, height)

    roi_buf    = hailo.get_roi_from_buffer(buffer)
    detections = roi_buf.get_objects_typed(hailo.HAILO_DETECTION)

    now = time.monotonic()

    # Odbierz wyniki OCR z workerka (non-blocking)
    if user_data.ocr_result_queue is not None:
        try:
            while True:
                plate_text, count, is_new = user_data.ocr_result_queue.get_nowait()
                expire = now + OCR_DISPLAY_SEC
                user_data.ocr_display[plate_text] = (count, is_new, expire)
        except _queue.Empty:
            pass

    rx1, ry1, rx2, ry2 = ROI
    plate_in_roi    = False
    any_plate_frame = False

    for det in detections:
        if det.get_label() != "license_plate":
            continue
        if det.get_confidence() < PLATE_MIN_CONF:
            continue
        any_plate_frame = True
        b  = det.get_bbox()
        cx = b.xmin() + b.width()  / 2
        cy = b.ymin() + b.height() / 2
        if rx1 <= cx <= rx2 and ry1 <= cy <= ry2:
            plate_in_roi = True

    if plate_in_roi:
        user_data.plate_consec += 1
    else:
        user_data.plate_consec = 0

    if not any_plate_frame:
        user_data.no_plate_consec += 1
    else:
        user_data.no_plate_consec = 0

    cooldown_ok = (now - user_data.last_save_time) >= COOLDOWN_SEC

    # ── Zapis tablicy ─────────────────────────────────────────────────────────
    if user_data.plate_consec >= PLATE_CONSEC and cooldown_ok:
        user_data.last_save_time  = now
        user_data.last_flash_time = now
        user_data.plate_consec    = 0
        user_data.saved_count    += 1

        ts       = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        img_path = user_data.save_dir / f"{ts}.jpg"
        txt_path = user_data.save_dir / f"{ts}.txt"

        if frame_rgb is None:
            print(f"[Kolektor] UWAGA: brak klatki (use_frame={user_data.use_frame}) — pomijam")
            user_data.plate_consec = 0
        else:
            frame_bgr = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR)

            # ── Filtr rozmycia: odrzuć nieostre klatki (wibracje, ruch) ─────────
            blur_score = cv2.Laplacian(
                cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY), cv2.CV_64F
            ).var()
            if blur_score < BLUR_MIN_SCORE:
                print(f"[Kolektor] Pominięto rozmytą klatkę (blur={blur_score:.1f}<{BLUR_MIN_SCORE})")
                user_data.saved_count -= 1  # cofnij licznik — nie zapisujemy
                # Nie resetuj plate_consec — spróbuj następną klatkę
                return

            # ── Kontrola miejsca na dysku ─────────────────────────────────────
            try:
                stat = os.statvfs(str(user_data.save_dir))
                free_mb = stat.f_bavail * stat.f_frsize / (1024 * 1024)
                if free_mb < DISK_MIN_MB:
                    print(f"[Kolektor] STOP: za mało miejsca ({free_mb:.0f} MB < {DISK_MIN_MB} MB)")
                    user_data.saved_count -= 1
                    return
            except OSError:
                pass

            img_ok = safe_write_image(img_path, frame_bgr, JPEG_QUALITY)
            if img_ok:
                lines        = []
                plate_bboxes = []
                det_confs    = []
                for det in detections:
                    cls = LABEL_TO_CLASS.get(det.get_label())
                    if cls is None:
                        continue
                    b  = det.get_bbox()
                    bw = max(0.0, min(1.0, b.width()))
                    bh = max(0.0, min(1.0, b.height()))

                    # ── Filtr jakości bbox tablicy ────────────────────────────
                    if det.get_label() == "license_plate":
                        area   = bw * bh
                        aspect = bw / (bh + 1e-6)
                        if area < PLATE_BBOX_MIN_AREA:
                            print(f"[Kolektor] Odrzucono bbox za mały ({area:.5f})")
                            continue
                        if not (PLATE_BBOX_ASPECT_MIN <= aspect <= PLATE_BBOX_ASPECT_MAX):
                            print(f"[Kolektor] Odrzucono bbox złe proporcje ({aspect:.2f})")
                            continue

                    cx = max(0.0, min(1.0, b.xmin() + bw / 2))
                    cy = max(0.0, min(1.0, b.ymin() + bh / 2))
                    lines.append(f"{cls} {cx:.6f} {cy:.6f} {bw:.6f} {bh:.6f}")
                    if det.get_label() == "license_plate":
                        plate_bboxes.append((b.xmin(), b.ymin(),
                                             b.xmin() + b.width(),
                                             b.ymin() + b.height()))
                        det_confs.append(det.get_confidence())

                safe_write_text(txt_path, "\n".join(lines) + ("\n" if lines else ""))
                print(f"[Kolektor] TABLICA #{user_data.saved_count:04d}  {img_path.name}  "
                      f"({len(lines)} bbox, blur={blur_score:.0f})")

                if user_data.ocr_worker and plate_bboxes:
                    user_data.ocr_worker.submit(frame_bgr, plate_bboxes, det_confs, ts)

    # ── Ręczne zdjęcie (klawisz T) ───────────────────────────────────────────
    if user_data.manual_capture.is_set():
        user_data.manual_capture.clear()
        if frame_rgb is None:
            print("[Kolektor] T: brak klatki — pomijam")
        else:
            user_data.manual_count    += 1
            user_data.last_flash_manual = now
            ts       = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
            img_path = user_data.save_dir / f"manual_{ts}.jpg"
            txt_path = user_data.save_dir / f"manual_{ts}.txt"
            frame_bgr_m = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR)
            if safe_write_image(img_path, frame_bgr_m, JPEG_QUALITY):
                lines        = []
                plate_bboxes = []
                det_confs    = []
                for det in detections:
                    cls = LABEL_TO_CLASS.get(det.get_label())
                    if cls is None:
                        continue
                    b  = det.get_bbox()
                    cx = max(0.0, min(1.0, b.xmin() + b.width()  / 2))
                    cy = max(0.0, min(1.0, b.ymin() + b.height() / 2))
                    bw = max(0.0, min(1.0, b.width()))
                    bh = max(0.0, min(1.0, b.height()))
                    lines.append(f"{cls} {cx:.6f} {cy:.6f} {bw:.6f} {bh:.6f}")
                    if det.get_label() == "license_plate":
                        plate_bboxes.append((b.xmin(), b.ymin(),
                                             b.xmin() + b.width(),
                                             b.ymin() + b.height()))
                        det_confs.append(det.get_confidence())
                safe_write_text(txt_path, "\n".join(lines) + ("\n" if lines else ""))
                print(f"[Kolektor] RĘCZNE   #{user_data.manual_count:04d}  {img_path.name}  ({len(lines)} bbox)")
                if user_data.ocr_worker and plate_bboxes:
                    user_data.ocr_worker.submit(frame_bgr_m, plate_bboxes, det_confs, ts)

    # ── Zapis tła ─────────────────────────────────────────────────────────────
    bg_time_ok = (now - user_data.last_bg_time) >= BG_INTERVAL_SEC
    if user_data.no_plate_consec >= BG_CONSEC and bg_time_ok:
        user_data.last_bg_time    = now
        user_data.last_flash_bg   = now
        user_data.no_plate_consec = 0
        user_data.bg_count       += 1

        ts      = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        bg_path = user_data.bg_dir / f"bg_{ts}.jpg"
        if frame_rgb is not None:
            if safe_write_image(bg_path, cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR), JPEG_QUALITY):
                safe_write_text(user_data.bg_dir / f"bg_{ts}.txt", "")
        print(f"[Kolektor] TŁO      #{user_data.bg_count:04d}  {bg_path.name}")

    # ── Podgląd cv2 ───────────────────────────────────────────────────────────
    if frame_rgb is not None:
        frame = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR)

        # Boxy detekcji
        for det in detections:
            label = det.get_label()
            conf  = det.get_confidence()
            color = COLOR_PLATE if label == "license_plate" else COLOR_VEHICLE
            if label == "license_plate" and conf < PLATE_MIN_CONF:
                color = tuple(c // 3 for c in color)
            b  = det.get_bbox()
            x1 = int(b.xmin() * width)
            y1 = int(b.ymin() * height)
            x2 = int((b.xmin() + b.width())  * width)
            y2 = int((b.ymin() + b.height()) * height)
            cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
            cv2.putText(frame, f"{label} {conf:.2f}",
                        (x1, max(0, y1 - 5)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1)

        # ROI
        flash_plate = (now - user_data.last_flash_time) < FLASH_SEC
        flash_bg    = (now - user_data.last_flash_bg)   < FLASH_SEC
        if flash_plate:
            roi_color = COLOR_ROI_SAVED
        elif flash_bg:
            roi_color = COLOR_BG_SAVED
        elif plate_in_roi:
            roi_color = COLOR_ROI_ARMED
        else:
            roi_color = COLOR_ROI_IDLE

        px1 = int(rx1 * width);  py1 = int(ry1 * height)
        px2 = int(rx2 * width);  py2 = int(ry2 * height)
        cv2.rectangle(frame, (px1, py1), (px2, py2), roi_color, 2)

        if plate_in_roi and not flash_plate:
            cv2.putText(frame, f"{user_data.plate_consec}/{PLATE_CONSEC}",
                        (px1 + 5, py1 + 22),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, roi_color, 2)

        # Licznik sesji (lewy-dolny róg)
        cv2.putText(frame,
                    f"Tablice: {user_data.saved_count}  Tla: {user_data.bg_count}  Reczne: {user_data.manual_count}",
                    (10, height - 10),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, roi_color, 2)

        # Flash przy ręcznym zdjęciu — biała ramka przez FLASH_SEC
        if (now - user_data.last_flash_manual) < FLASH_SEC:
            cv2.rectangle(frame, (0, 0), (width - 1, height - 1), (255, 255, 255), 6)

        # Cooldown bar
        if not cooldown_ok and not flash_plate:
            ratio = (now - user_data.last_save_time) / COOLDOWN_SEC
            bar_w = int((px2 - px1) * ratio)
            cv2.rectangle(frame, (px1, py2 + 4), (px1 + bar_w, py2 + 10),
                          COLOR_ROI_IDLE, -1)

        # Nakładka historii tablic (prawy-dolny róg)
        _draw_ocr_overlay(frame, user_data.ocr_display, now)

        user_data.set_frame(frame)


# ── Logowanie do pliku ───────────────────────────────────────────────────────

LOG_DIR = Path("/home/admin/hailo-apps/logs")

class _TeeWriter:
    """Pisze jednocześnie do terminala i pliku logu z timestampem."""
    def __init__(self, terminal, logfile):
        self._term = terminal
        self._log  = logfile
        self._bol  = True   # beginning of line — czy dodać timestamp
        self._lock = threading.Lock()  # chroni przed przeplataniem z wątków OCR/historii

    def write(self, data: str):
        with self._lock:
            self._term.write(data)
            if data:
                # Dodaj timestamp na początku każdej linii
                stamped = []
                for ch in data:
                    if self._bol:
                        stamped.append(datetime.now().strftime("[%H:%M:%S] "))
                        self._bol = False
                    stamped.append(ch)
                    if ch == "\n":
                        self._bol = True
                self._log.write("".join(stamped))

    def flush(self):
        self._term.flush()
        self._log.flush()

    def fileno(self):
        return self._term.fileno()

    def isatty(self):
        return False


def _setup_logging() -> Path:
    """Otwiera plik logu i przekierowuje stdout/stderr. Zwraca ścieżkę logu."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    # Zachowaj tylko ostatnie 10 logów
    old_logs = sorted(LOG_DIR.glob("collect_*.log"))
    for old in old_logs[:-9]:
        try:
            old.unlink()
        except OSError:
            pass

    log_path = LOG_DIR / f"collect_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    log_file = open(log_path, "w", encoding="utf-8", buffering=1)

    sys.stdout = _TeeWriter(sys.__stdout__, log_file)
    sys.stderr = _TeeWriter(sys.__stderr__, log_file)

    return log_path


# ── Main ──────────────────────────────────────────────────────────────────────

def find_camera(device: str = "/dev/video0") -> bool:
    return Path(device).exists()


def main():
    log_path = _setup_logging()
    print(f"[Kolektor] Log sesji: {log_path}")

    camera = "/dev/video0"
    if not find_camera(camera):
        print(f"\n[Kolektor] BRAK KAMERY USB pod {camera}")
        print( "[Kolektor] Podłącz kamerę C1 PRO i uruchom ponownie.\n")
        input("Naciśnij Enter aby zamknąć...")
        sys.exit(1)

    usb = find_usb()
    if usb is None:
        print("\n[Kolektor] Brak pendrive'a – czekam na podłączenie...")
        while usb is None:
            time.sleep(2)
            usb = find_usb()
        print(f"[Kolektor] Pendrive wykryty: {usb}")

    session  = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    save_dir = os.path.join(usb, session)
    print(f"[Kolektor] Pendrive: {usb}")

    history_dir = Path(usb) / HISTORY_DIRNAME
    ocr_worker  = None
    try:
        ocr_worker = PlatesHistoryWorker(history_dir)
    except Exception as e:
        print(f"[Historia] UWAGA: nie można uruchomić historii tablic: {e}")
        print( "[Historia]         Zbieranie danych treningowych działa normalnie.")
        print( "[Historia]         Aby naprawić: wyciągnij i włóż ponownie pendrive.")

    user_data = CollectorState(save_dir, ocr_worker=ocr_worker)
    app       = CollectorDetectionApp(app_callback, user_data)

    try:
        app.run()
    finally:
        if ocr_worker:
            ocr_worker.stop()
            print("[Kolektor] Worker historii zatrzymany.")


if __name__ == "__main__":
    main()
