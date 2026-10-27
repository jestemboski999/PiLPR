# -*- coding: utf-8 -*-
"""
LPR — przegląd offline (aplikacja na pendrive, Windows). Wersja 2 (09.2026).

Wygląda i działa jak panel www z malinki (motyw PiLPR, czcionki pikselowe,
tabela z ocenami, EDYTOR WERYFIKACJI z kandydatami, suwakami obrazu, ramką
i „lepszą klatką", lightbox). Czyta lpr/plates.db i katalogi dzienne
WZGLĘDEM położenia pliku .exe (pendrive z malinki: <root>/lpr/...).
Oceny zapisuje do tej samej tabeli annotations co www — po włożeniu pendrive
z powrotem do malinki werdykty widzi www i eksporter treningowy.

Okno: Edge/Chrome w trybie aplikacji (--app, własny profil) — bez pywebview
i WebView2, bo tego brakowało/psuło się na obcych komputerach. Gdy nie ma
Edge/Chrome, otwiera domyślną przeglądarkę. Parametry:
  --browser      wymuś domyślną przeglądarkę
  --data KATALOG wskaż katalog z plates.db (do testów na PC)
Log: LPR_Przegladarka.log obok pliku .exe (start, port, błędy).

Budowanie EXE (na PC, w katalogu przegladarka_usb):
  pyinstaller --onefile --noconsole --name LPR_Przegladarka \
              --add-data "fonts;fonts" lpr_viewer.py
"""
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

APP_TITLE = "LPR — przegląd offline"
VERSION = "2.0"


# ── Położenie plików ─────────────────────────────────────────────────────────

def _base_dir() -> Path:
    # exe z PyInstallera: katalog pliku .exe; skrypt: katalog pliku .py
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent


def _res_dir() -> Path:
    # zasoby dołączone do EXE (--add-data) leżą w katalogu tymczasowym
    return Path(getattr(sys, "_MEIPASS", str(_base_dir())))


def _find_data_dir() -> "Path | None":
    """Szuka katalogu z plates.db: obok aplikacji (pendrive) lub w ./lpr."""
    if "--data" in sys.argv:
        i = sys.argv.index("--data")
        if i + 1 < len(sys.argv):
            p = Path(sys.argv[i + 1])
            if (p / "plates.db").exists():
                return p
            if (p / "lpr" / "plates.db").exists():
                return p / "lpr"
    base = _base_dir()
    for cand in (base / "lpr", base, base.parent / "lpr"):
        if (cand / "plates.db").exists():
            return cand
    return None


LOG_PATH = _base_dir() / "LPR_Przegladarka.log"
_log_lock = threading.Lock()


def log(msg: str) -> None:
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + msg
    with _log_lock:
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass
    try:
        print(line)
    except Exception:
        pass


def _msgbox(text: str, flags: int = 0x10) -> None:
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, text, APP_TITLE, flags)
    except Exception:
        print(text)


DATA_DIR = _find_data_dir()
# 02.10: sekcja RADAR na pendrive — zdarzenia w plates.db (tabela radar_zdarzenia,
# scalona z SD przez scal_baze_sd.py), pliki w <pendrive>/radar/<dzień>/
RADAR_DIR = (DATA_DIR.parent / "radar") if DATA_DIR else None


def _radar_sql_filtr(qs: dict):
    """(WHERE, params, ORDER BY) — ta sama semantyka co /api/radar na malince."""
    g = lambda k: (qs.get(k) or [""])[0]
    w, par = [], []
    if g("od"):
        w.append("czas >= ?"); par.append(g("od")[:10] + " 00:00:00")
    if g("do"):
        w.append("czas <= ?"); par.append(g("do")[:10] + " 23:59:59")
    for k, op in (("v_min", ">="), ("v_max", "<=")):
        try:
            if g(k):
                w.append(f"predkosc {op} ?"); par.append(float(g(k)))
        except ValueError:
            pass
    if g("tablica"):
        w.append("UPPER(tablica) LIKE ?"); par.append("%" + g("tablica").upper() + "%")
    if g("tylko_tablica") == "1":
        w.append("tablica IS NOT NULL AND tablica != '' AND tablica != 'UNKNOWN'")
    if g("tylko_pojazd") == "1":
        w.append("(pojazd IS NULL OR pojazd = 1)")
    if g("kierunek") in ("+", "-"):
        w.append("kierunek = ?"); par.append(g("kierunek"))
    order = {"czas_asc": "czas ASC, id ASC", "v_desc": "predkosc DESC, id DESC",
             "v_asc": "predkosc ASC, id ASC"}.get(g("sort"), "id DESC")
    return (("WHERE " + " AND ".join(w)) if w else ""), par, order


def api_radar(qs: dict) -> dict:
    out = {"zdarzenia": [], "razem": 0, "katalog": str(RADAR_DIR or "")}
    if not DATA_DIR:
        return out
    try:
        limit = max(1, min(500, int((qs.get("limit") or ["60"])[0])))
    except ValueError:
        limit = 60
    con = sqlite3.connect(str(DATA_DIR / "plates.db"), timeout=10)
    con.row_factory = sqlite3.Row
    try:
        if not con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='radar_zdarzenia'").fetchone():
            return out
        kol = {r[1] for r in con.execute("PRAGMA table_info(radar_zdarzenia)")}
        where, par, order = _radar_sql_filtr(qs)
        out["razem"] = con.execute(f"SELECT COUNT(*) FROM radar_zdarzenia {where}", par).fetchone()[0]
        rows = con.execute(f"SELECT * FROM radar_zdarzenia {where} ORDER BY {order} LIMIT ?",
                           (*par, limit)).fetchall()
    finally:
        con.close()
    for r in rows:
        d = (r["czas"] or "")[:10]
        def url(n, mini=False):
            if not n or not d:
                return None
            return f"/api/radar/plik/{d}/{n}" + ("?mini=1" if mini else "")
        out["zdarzenia"].append({
            "id": r["id"], "czas": r["czas"], "v": r["predkosc"], "kierunek": r["kierunek"],
            "probek": r["probek"],
            "tablica": (r["tablica"] if r["tablica"] not in (None, "", "UNKNOWN") else None),
            "ocr": r["ocr_conf"],
            "v_kam": r["v_kamera"] if "v_kamera" in kol else None,
            "pewne": r["pewne"] if "pewne" in kol else None,
            "pojazd": r["pojazd"] if "pojazd" in kol else None,
            "uwagi": r["uwagi"], "debug": r["debug"] if "debug" in kol else None,
            "zdjecie": url(r["zdjecie"]), "mini": url(r["zdjecie"], True),
            "crop": url(r["crop"]), "klip": url(r["klip"])})
    return out


def _radar_mini(p: Path) -> Path:
    """Miniatura 320 px obok oryginału (jak na malince); bez PIL — oryginał."""
    mini = p.with_name(p.stem + "_mini.jpg")
    if mini.is_file() and mini.stat().st_size > 0:
        return mini
    try:
        from PIL import Image
        im = Image.open(p)
        im.thumbnail((320, 320))
        im.convert("RGB").save(mini, "JPEG", quality=70)
        return mini
    except Exception as e:
        log(f"mini {p.name}: {e}")
        return p


# ── Baza ─────────────────────────────────────────────────────────────────────

def _db():
    con = sqlite3.connect(DATA_DIR / "plates.db", timeout=10)
    con.row_factory = sqlite3.Row
    return con


def _ensure_schema() -> None:
    """Kolumny dopisane przez malinkę później (ramka ręczna, lepsza klatka).
    Starsza baza może ich nie mieć — dopisujemy, bo verify je zapisuje."""
    con = _db()
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(annotations)")}
        if not cols:
            con.execute("""
                CREATE TABLE IF NOT EXISTS annotations (
                    sighting_id  INTEGER PRIMARY KEY,
                    is_correct   INTEGER,
                    correct_text TEXT,
                    reviewed_at  TEXT,
                    notes        TEXT, correct_bbox TEXT, lepsza_klatka TEXT,
                    FOREIGN KEY (sighting_id) REFERENCES sightings(id))""")
        else:
            for c in ("notes", "correct_bbox", "lepsza_klatka"):
                if c not in cols:
                    con.execute(f"ALTER TABLE annotations ADD COLUMN {c} TEXT")
                    log(f"[DB] dopisano kolumnę annotations.{c}")
        con.commit()
    finally:
        con.close()


_VERIFY = {1: "ok", 0: "fix", -1: "notplate", -2: "partial", -3: "unreadable"}


def _row_item(r) -> dict:
    d = r["seen_at"][:10] if r["seen_at"] else ""
    keys = r.keys()
    def _box4(s):
        try:
            b = [float(v) for v in (s or "").split(";")]
            return b if len(b) == 4 else None
        except ValueError:
            return None
    bbox = _box4(r["bbox"])
    vbbox = _box4(r["correct_bbox"]) if "correct_bbox" in keys else None
    extra = r["extra_crops"] or ""
    crops = [f"/api/crop/{d}/{n}" for n in extra.split(";") if n] if d else []
    ic = r["is_correct"] if "is_correct" in keys else None
    return {
        "id": r["id"], "plate": r["plate_text"], "seen_at": r["seen_at"],
        "full_url": (f"/api/crop/{d}/{r['full_image']}"
                     if r["full_image"] and d else None),
        "crop_url": (f"/api/crop/{d}/{r['crop_image']}"
                     if r["crop_image"] and d else None),
        "crops": crops,
        "det_conf": r["det_confidence"], "ocr_conf": r["ocr_confidence"],
        "ocr_reads": r["ocr_reads"], "duration": r["track_duration"],
        "bbox": bbox, "vbbox": vbbox,
        "verify": _VERIFY.get(ic) if ic is not None else None,
        "vtext": r["correct_text"] if "correct_text" in keys else None,
        "lepsza": (r["lepsza_klatka"] if "lepsza_klatka" in keys else None) or None,
        "plate_v1": (r["plate_text_v1"] if "plate_text_v1" in keys else None) or None,
    }


def _filters(qs: dict):
    """Wspólne filtry tabeli i edytora: dzień, szukajka, tylko nieocenione."""
    day = qs.get("day", [""])[0].strip()
    q = re.sub(r"[^A-Z0-9]", "", qs.get("q", [""])[0].upper())
    un = qs.get("un", ["0"])[0] in ("1", "true")
    where, args = [], []
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        where.append("substr(s.seen_at,1,10)=?")
        args.append(day)
    if q:
        where.append("(upper(s.plate_text) LIKE ? OR upper(a.correct_text) LIKE ?)")
        args += ["%" + q + "%", "%" + q + "%"]
    if un:
        where.append("(a.sighting_id IS NULL OR a.is_correct IS NULL)")
    return (" WHERE " + " AND ".join(where)) if where else "", args


def _ma_kolumne_v1() -> bool:
    """04.10: `reocr_nieocenione.py` przepisuje nieocenione zapisy nowym modelem OCR;
    stary odczyt ląduje w `sightings.plate_text_v1`. Starsze bazy nie mają kolumny."""
    try:
        if not DATA_DIR:
            return False
        con = sqlite3.connect(str(DATA_DIR / "plates.db"), timeout=5)
        try:
            return "plate_text_v1" in {r[1] for r in con.execute("PRAGMA table_info(sightings)")}
        finally:
            con.close()
    except Exception as e:
        log(f"plate_text_v1: {e}")
        return False


_V1 = " s.plate_text_v1," if _ma_kolumne_v1() else " NULL AS plate_text_v1,"
_SELECT = ("SELECT s.id, s.plate_text, s.seen_at, s.full_image, s.crop_image,"
           " s.det_confidence, s.ocr_confidence, s.ocr_reads, s.track_duration,"
           " s.bbox, s.extra_crops, a.is_correct, a.correct_text, a.correct_bbox,"
           " a.lepsza_klatka," + _V1 + " s.id AS _id2"
           " FROM sightings s LEFT JOIN annotations a ON a.sighting_id=s.id")


def api_days() -> list:
    con = _db()
    try:
        rows = con.execute(
            "SELECT substr(s.seen_at,1,10) d, COUNT(*) n,"
            " SUM(CASE WHEN a.sighting_id IS NULL OR a.is_correct IS NULL"
            "     THEN 1 ELSE 0 END) un"
            " FROM sightings s LEFT JOIN annotations a ON a.sighting_id=s.id"
            " GROUP BY d ORDER BY d DESC").fetchall()
        return [{"d": r["d"], "n": r["n"], "un": r["un"]} for r in rows if r["d"]]
    finally:
        con.close()


def api_plates(qs: dict):
    try:
        limit = min(max(1, int(qs.get("limit", ["100"])[0])), 500)
    except ValueError:
        limit = 100
    try:
        offset = max(0, int(qs.get("offset", ["0"])[0]))
    except ValueError:
        offset = 0
    where, args = _filters(qs)
    con = _db()
    try:
        total = con.execute(
            "SELECT COUNT(*) FROM sightings s LEFT JOIN annotations a"
            " ON a.sighting_id=s.id" + where, args).fetchone()[0]
        rows = con.execute(
            _SELECT + where + " ORDER BY s.seen_at DESC, s.id DESC LIMIT ? OFFSET ?",
            args + [limit, offset]).fetchall()
        return [_row_item(r) for r in rows], total
    finally:
        con.close()


def api_unreviewed(qs: dict) -> dict:
    """Najnowsze NIEOCENIONE zapisy pasujące do filtra (dzień/szukajka)."""
    try:
        limit = min(max(1, int(qs.get("limit", ["40"])[0])), 200)
    except ValueError:
        limit = 40
    qs = dict(qs)
    qs["un"] = ["1"]
    where, args = _filters(qs)
    # pominięte w tej sesji (→) — nie zwracać ich ponownie, ale liczyć do total
    ex = [int(v) for v in qs.get("exclude", [""])[0].split(",") if v.isdigit()][:1000]
    where_ex = where
    if ex:
        where_ex += (" AND " if where else " WHERE ") + \
                    "s.id NOT IN (%s)" % ",".join("?" * len(ex))
    con = _db()
    try:
        total = con.execute(
            "SELECT COUNT(*) FROM sightings s LEFT JOIN annotations a"
            " ON a.sighting_id=s.id" + where, args).fetchone()[0]
        rows = con.execute(
            _SELECT + where_ex +
            " ORDER BY (s.plate_text_v1 IS NOT NULL AND s.plate_text_v1 != s.plate_text) DESC, s.id DESC LIMIT ?",
            args + ex + [limit]).fetchall()
        return {"total": total, "items": [_row_item(r) for r in rows]}
    finally:
        con.close()


def api_stats() -> dict:
    con = _db()
    try:
        total = con.execute("SELECT COUNT(*) FROM sightings").fetchone()[0]
        reviewed = con.execute(
            "SELECT COUNT(*) FROM annotations WHERE is_correct IS NOT NULL"
        ).fetchone()[0]
        unrev = con.execute(
            "SELECT COUNT(*) FROM sightings s LEFT JOIN annotations a"
            " ON a.sighting_id=s.id"
            " WHERE a.sighting_id IS NULL OR a.is_correct IS NULL").fetchone()[0]
        last = con.execute("SELECT MAX(seen_at) FROM sightings").fetchone()[0]
    finally:
        con.close()
    try:
        free = round(shutil.disk_usage(str(DATA_DIR)).free / 1_048_576)
    except Exception:
        free = -1
    return {"total": total, "reviewed": reviewed, "unreviewed": unrev,
            "last_seen": last, "free_disk_mb": free,
            "data_dir": str(DATA_DIR), "version": VERSION}


def api_verify(data: dict) -> dict:
    try:
        sid = int(data.get("id"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "brak id"}
    verdict = data.get("verdict")
    text = re.sub(r"[^A-Z0-9]", "", str(data.get("text") or "").upper())[:10]
    con = _db()
    try:
        if verdict == "bbox":
            # ręcznie narysowana ramka tablicy — nie rusza werdyktu tekstu
            try:
                b = [max(0.0, min(1.0, float(v))) for v in data.get("bbox")]
                if not (len(b) == 4 and b[0] < b[2] and b[1] < b[3]):
                    raise ValueError("ramka")
            except (TypeError, ValueError):
                return {"ok": False, "error": "zła ramka"}
            bstr = ";".join("%.4f" % v for v in b)
            con.execute("""
                INSERT INTO annotations (sighting_id, correct_bbox, reviewed_at)
                VALUES (?,?,datetime('now','localtime'))
                ON CONFLICT(sighting_id) DO UPDATE SET
                    correct_bbox = excluded.correct_bbox,
                    reviewed_at  = excluded.reviewed_at
            """, (sid, bstr))
        elif verdict == "lepsza":
            # sąsiednia klatka wskazana jako lepsza od zapisanej (do uczenia);
            # pusta nazwa = cofnięcie wyboru. Sama nazwa pliku, bez ścieżek.
            plik = str(data.get("plik") or "")[:200]
            if plik and (not re.fullmatch(r"[\w.\-]+", plik) or ".." in plik):
                return {"ok": False, "error": "zła nazwa"}
            con.execute("""
                INSERT INTO annotations (sighting_id, lepsza_klatka, reviewed_at)
                VALUES (?,?,datetime('now','localtime'))
                ON CONFLICT(sighting_id) DO UPDATE SET
                    lepsza_klatka = excluded.lepsza_klatka,
                    reviewed_at   = excluded.reviewed_at
            """, (sid, plik or None))
        elif verdict == "clear":
            # cofnięcie werdyktu NIE kasuje ręcznej ramki ani lepszej klatki;
            # wiersz znika dopiero, gdy nic w nim nie zostało
            con.execute("UPDATE annotations SET is_correct=NULL, correct_text=NULL,"
                        " reviewed_at=datetime('now','localtime') WHERE sighting_id=?",
                        (sid,))
            con.execute("DELETE FROM annotations WHERE sighting_id=? AND correct_bbox"
                        " IS NULL AND lepsza_klatka IS NULL AND notes IS NULL", (sid,))
        else:
            ic_map = {"ok": 1, "notplate": -1, "partial": -2, "unreadable": -3}
            if verdict == "fix":
                if not (2 <= len(text) <= 10):
                    return {"ok": False, "error": "zły numer"}
                ic, ct = 0, text
            elif verdict in ic_map:
                ic, ct = ic_map[verdict], None
            else:
                return {"ok": False, "error": "zły verdict"}
            con.execute("""
                INSERT INTO annotations (sighting_id, is_correct, correct_text,
                                         reviewed_at)
                VALUES (?,?,?,datetime('now','localtime'))
                ON CONFLICT(sighting_id) DO UPDATE SET
                    is_correct   = excluded.is_correct,
                    correct_text = excluded.correct_text,
                    reviewed_at  = excluded.reviewed_at
            """, (sid, ic, ct))
        con.commit()
        return {"ok": True}
    finally:
        con.close()


# ── HTTP ─────────────────────────────────────────────────────────────────────

_last_ping = 0.0      # heartbeat strony — po zamknięciu okna serwer się kończy
_bye_at = 0.0         # strona zgłosiła zamknięcie (pagehide); 0 = nie zgłosiła
_FONTS = ("PressStart2P-Regular.ttf", "VT323-Regular.ttf")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, body, ctype="application/json", code=200, cache=False,
              extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control",
                         "max-age=604800" if cache else "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _radar_plik(self, d: str, nazwa: str, qs: dict):
        """Zdjęcie / klip zdarzenia z pendrive'a. MP4 z obsługą Range (Edge
        potrzebuje go do przewijania); ?mini=1 → miniatura 320 px."""
        if not RADAR_DIR:
            return self._send(b"", "text/plain", 404)
        f = RADAR_DIR / d / nazwa
        try:
            if not f.is_file() or f.stat().st_size == 0:
                return self._send(b"", "text/plain", 404)
        except OSError:
            return self._send(b"", "text/plain", 404)
        if nazwa.endswith(".jpg"):
            if (qs.get("mini") or [""])[0] and not nazwa.endswith("_mini.jpg"):
                f = _radar_mini(f)
            return self._send(f.read_bytes(), "image/jpeg", cache=True)
        size = f.stat().st_size
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        if rng and rng.startswith("bytes="):
            a, _, b = rng[6:].partition("-")
            try:
                if a:
                    start = int(a); end = int(b) if b else size - 1
                elif b:
                    start = max(0, size - int(b))
            except ValueError:
                start, end = 0, size - 1
            end = min(end, size - 1)
        czesc = bool(rng) and (start > 0 or end < size - 1)
        self.send_response(206 if czesc else 200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if czesc:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Cache-Control", "max-age=604800")
        self.end_headers()
        with open(f, "rb") as fh:
            fh.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = fh.read(min(1 << 20, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)

    def _json(self, obj, code=200, extra=None):
        self._send(json.dumps(obj, ensure_ascii=False).encode(), code=code,
                   extra=extra)

    def do_GET(self):
        global _last_ping
        u = urlparse(self.path)
        p = unquote(u.path)
        qs = parse_qs(u.query)
        try:
            if p == "/":
                return self._send(HTML.encode(), "text/html; charset=utf-8")
            if p == "/api/ping":
                _last_ping = time.monotonic()
                return self._json({"ok": True})
            if p == "/api/days":
                return self._json(api_days())
            if p == "/api/radar":
                return self._json(api_radar(qs))
            m = re.match(r"^/api/radar/plik/(\d{4}-\d{2}-\d{2})/([\w\-]+\.(?:jpg|mp4))$", p)
            if m:
                return self._radar_plik(m.group(1), m.group(2), qs)
            if p == "/api/stats":
                return self._json(api_stats())
            if p == "/api/plates":
                items, total = api_plates(qs)
                return self._json(items, extra={"X-Total-Count": str(total)})
            if p == "/api/unreviewed":
                return self._json(api_unreviewed(qs))
            m = re.match(r"^/api/locate/(\d+)$", p)
            if m:
                # bez OpenCV nie odnajdujemy tablicy na klatce; nowe zapisy
                # mają bbox w bazie, więc to dotyczy tylko najstarszych
                return self._json({"ok": False})
            m = re.match(r"^/api/crop/(\d{4}-\d{2}-\d{2})/([\w\-]+\.jpg)$", p)
            if m:
                # regex dopuszcza tylko datę i nazwę bez ukośników/„..",
                # więc ścieżka nie wyjdzie poza katalog danych (bez resolve —
                # dowiązania/junction na Windows psułyby porównanie)
                f = DATA_DIR / m.group(1) / m.group(2)
                # 02.10: po naprawie exFAT (fsck) część plików ma 0 B, a uszkodzony
                # wpis katalogu rzuca OSError (WinError 1392) — oba = „brak", nie
                # pusty obrazek ani wyjątek w logu
                try:
                    if f.is_file() and f.stat().st_size > 0:
                        return self._send(f.read_bytes(), "image/jpeg", cache=True)
                except OSError:
                    pass
                return self._send(b"", "image/jpeg", 404)
            m = re.match(r"^/font/([\w\-]+\.ttf)$", p)
            if m and m.group(1) in _FONTS:
                for cand in (_res_dir() / "fonts" / m.group(1),
                             _base_dir() / "fonts" / m.group(1)):
                    if cand.exists():
                        return self._send(cand.read_bytes(), "font/ttf",
                                          cache=True)
                return self._send(b"", "font/ttf", 404)
            return self._send(b"not found", "text/plain", 404)
        except (ConnectionError, BrokenPipeError):
            return          # przeglądarka anulowała pobieranie (lazy-load) — cisza
        except Exception as e:
            log("[HTTP] " + p + " -> " + repr(e) + "\n" + traceback.format_exc())
            try:
                return self._json({"error": str(e)}, 500)
            except (ConnectionError, BrokenPipeError):
                return

    def do_POST(self):
        ln = int(self.headers.get("Content-Length") or 0)
        try:
            data = json.loads(self.rfile.read(ln) or b"{}")
        except Exception:
            data = {}
        global _bye_at
        p = urlparse(self.path).path
        if p == "/api/verify":
            try:
                return self._json(api_verify(data))
            except Exception as e:
                log("[verify] " + repr(e))
                return self._json({"ok": False, "error": str(e)}, 500)
        if p == "/api/bye":            # strona zamykana (pagehide)
            _bye_at = time.monotonic()
            return self._json({"ok": True})
        return self._json({}, 404)


# ── HTML (motyw PiLPR z panelu www malinki, bez części „na żywo") ───────────

HTML = r"""<!DOCTYPE html>
<html lang="pl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LPR — przegląd offline</title>
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

/* Motyw PiLPR (VHS) — te same kolory co panel www na malince */
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

/* czcionki pikselowe serwowane z EXE (bez internetu) */
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
  font-size: 17px;
  line-height: 1.25;
  overflow: hidden;
  -webkit-font-smoothing: none;
  font-smooth: never;
}
body { display: flex; flex-direction: column; }

button, input, select, .rvb, .pg-btn, #refresh-btn, #reviewer-btn, .vbtn {
  border-radius: 0 !important;
  font-family: 'PixelData', 'Courier New', monospace;
  letter-spacing: 0.5px;
  text-transform: uppercase;
}
/* PixelHead nie ma polskich znaków — tylko tam, gdzie napisy bez ogonków */
.brand-name, #header button, .pg-btn, #refresh-btn, #reviewer-btn {
  font-family: 'PixelHead', 'PixelData', monospace;
  font-size: 11px;
  letter-spacing: 1px;
}
.brand-name { font-size: 13px; }
.brand-sub, .stat { font-family: 'PixelData', monospace; letter-spacing: 0.5px; }

/* ── HEADER ── */
#header {
  display: flex; align-items: center; height: 38px;
  background: var(--bg-head); border-bottom: 1px solid var(--border);
  padding: 0 12px; gap: 0; flex-shrink: 0; white-space: nowrap;
}
#header .brand { display: flex; align-items: center; gap: 8px; margin-right: 20px; }
#header .brand-icon {
  width: 22px; height: 22px; background: var(--blue); display: flex;
  align-items: center; justify-content: center; font-size: 11px;
  font-weight: 700; color: #fff; letter-spacing: -0.5px;
}
#header .brand-name { font-size: 13px; font-weight: 600; color: #fff; letter-spacing: 0.5px; }
#header .brand-sub { font-size: 11px; color: var(--dim); letter-spacing: 0.3px; }
#header .sep { width: 1px; height: 20px; background: var(--border); margin: 0 14px; }
#header .stat { font-size: 12px; color: var(--dim); }
#header .stat span { color: var(--text); font-weight: 600; }
#header .stat span.warn { color: var(--yellow); }
#header .spacer { flex: 1; }
#header .status-online { font-size: 12px; font-weight: 600; color: var(--green); letter-spacing: 0.3px; }
#header .status-offline { font-size: 12px; font-weight: 600; color: var(--red); letter-spacing: 0.3px; }
#header .clock { font-size: 13px; font-family: 'PixelData', monospace; color: var(--text); margin-left: 14px; letter-spacing: 0.5px; }

/* ── MAIN ── */
#main { flex: 1; display: flex; min-height: 0; }
#events-panel { flex: 1; display: flex; flex-direction: column; overflow: hidden; background: var(--bg); }

#events-toolbar {
  height: 36px; display: flex; align-items: center; padding: 0 8px;
  background: var(--bg-th); border-bottom: 1px solid var(--border);
  gap: 6px; flex-shrink: 0;
}
#events-toolbar label { font-size: 11px; color: var(--dim); text-transform: uppercase; letter-spacing: 0.4px; }
#events-toolbar label.chk { display: flex; align-items: center; gap: 4px; cursor: pointer; user-select: none; white-space: nowrap; }
#events-toolbar label.chk input { accent-color: var(--blue); }
#search-input, #day-sel {
  height: 24px; background: var(--bg); border: 1px solid var(--border);
  color: var(--text); font-size: 12px; font-family: 'PixelData', monospace;
  padding: 0 7px; outline: none;
}
#search-input { flex: 1; min-width: 0; }
#search-input:focus, #day-sel:focus { border-color: var(--blue); }
#refresh-btn, #reviewer-btn {
  height: 24px; padding: 0 8px; background: var(--bg-th);
  border: 1px solid var(--border); color: var(--text); font-size: 12px;
  cursor: pointer; letter-spacing: 0.3px;
}
#refresh-btn:hover, #reviewer-btn:hover { background: var(--border); }
#refresh-btn:active { background: #241a2e; }
#reviewer-btn { border-color: var(--blue); color: var(--blue); }

#tbl-pager {
  display: none; flex-shrink: 0; align-items: center; justify-content: center;
  gap: 12px; padding: 8px; background: var(--bg-th); border-top: 1px solid var(--border);
}
.pg-btn {
  background: var(--bg-head); color: var(--text); border: 1px solid var(--border);
  padding: 6px 14px; font-size: 13px; cursor: pointer;
}
.pg-btn:disabled { opacity: 0.4; cursor: default; }
.pg-btn:not(:disabled):hover { border-color: var(--blue); }
.pg-info { font-size: 12px; color: var(--dim); }

/* ── TABELA ── */
#table-wrap { flex: 1; overflow-y: auto; overflow-x: auto; }
#table-wrap::-webkit-scrollbar { width: 6px; }
#table-wrap::-webkit-scrollbar-track { background: var(--bg); }
#table-wrap::-webkit-scrollbar-thumb { background: var(--border); }
#table-wrap::-webkit-scrollbar-thumb:hover { background: var(--dim); }

table#events-table { width: 100%; border-collapse: collapse; table-layout: fixed; font-size: 12px; }
table#events-table thead { position: sticky; top: 0; z-index: 10; }
table#events-table th {
  background: var(--bg-th); color: var(--dim); font-size: 10px; font-weight: 600;
  text-transform: uppercase; letter-spacing: 0.5px; padding: 0 6px; height: 26px;
  text-align: left; border-bottom: 1px solid var(--border);
  border-right: 1px solid var(--border); white-space: nowrap; overflow: hidden;
}
table#events-table th:last-child { border-right: none; }
table#events-table col.c-id     { width: 52px; }
table#events-table col.c-time   { width: 110px; }
table#events-table col.c-plate  { width: 130px; }
table#events-table col.c-det    { width: 48px; }
table#events-table col.c-ocr    { width: 48px; }
table#events-table col.c-dur    { width: 44px; }
table#events-table col.c-img    { width: 120px; }
table#events-table col.c-verify { width: 190px; }
table#events-table td {
  height: 38px; padding: 0 6px; border-bottom: 1px solid var(--border);
  border-right: 1px solid var(--border); vertical-align: middle; overflow: hidden;
}
table#events-table td:last-child { border-right: none; }
table#events-table tbody tr:nth-child(odd) { background: var(--bg-row); }
table#events-table tbody tr:nth-child(even) { background: var(--bg-row2); }
table#events-table tbody tr:hover { background: #15202e; }
.td-time { font-size: 11px; color: var(--dim); font-family: 'PixelData', monospace; white-space: nowrap; }
.td-plate { font-family: 'PixelData', Consolas, monospace; font-size: 14px; font-weight: 700; color: #fff; letter-spacing: 0.5px; white-space: nowrap; }
.td-plate.unknown { color: var(--yellow); }
tr.unknown-row td:nth-child(3) { border-left: 3px solid var(--yellow); padding-left: 4px; }
.conf-green { color: var(--green); font-size: 11px; font-family: 'Courier New', monospace; }
.conf-yellow { color: var(--yellow); font-size: 11px; font-family: 'Courier New', monospace; }
.conf-red { color: var(--red); font-size: 11px; font-family: 'Courier New', monospace; }
.td-dur { font-size: 11px; color: var(--dim); text-align: right; font-family: 'PixelData', monospace; }
.td-img { text-align: right; padding: 3px 6px; }
.td-img img {
  width: 96px; height: 32px; object-fit: cover; display: block; margin-left: auto;
  cursor: pointer; border: 1px solid var(--border);
}
.td-img img:hover { border-color: var(--blue); }
.td-img .no-img { font-size: 10px; color: var(--border); text-align: right; }

/* przyciski weryfikacji w tabeli */
.td-verify { text-align: right; white-space: nowrap; }
.vbtn {
  width: 30px; height: 30px; margin-left: 3px; background: var(--bg-th);
  color: var(--dim); border: 1px solid var(--border); cursor: pointer;
  font-size: 14px; line-height: 1;
}
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
.v-text { font-family: 'PixelData', monospace; font-size: 12px; font-weight: 700; color: var(--green); margin-top: 3px; }

/* ── EDYTOR WERYFIKACJI (pełny ekran) ── */
#reviewer { display: none; position: fixed; inset: 0; background: var(--bg); z-index: 1200; flex-direction: column; }
#reviewer.open { display: flex; }
#rv-head {
  display: flex; align-items: center; height: 44px; padding: 0 14px;
  background: var(--bg-head); border-bottom: 1px solid var(--border); flex-shrink: 0;
}
#rv-count { font-size: 14px; font-weight: 700; color: var(--blue); min-width: 140px; }
#rv-title { flex: 1; text-align: center; font-size: 13px; color: var(--dim); letter-spacing: 1px; }
#rv-close { font-size: 22px; color: var(--dim); cursor: pointer; padding: 4px 8px; }
#rv-close:hover { color: var(--text); }
#rv-body {
  flex: 1; display: flex; flex-direction: column; align-items: center;
  justify-content: flex-start; gap: 6px; overflow-y: auto; overflow-x: hidden; padding: 6px;
}
#rv-meta { display: flex; align-items: center; justify-content: center; gap: 14px; flex-shrink: 0; }
#rv-head #rv-boxfix { margin-right: 14px; }
/* klatka nie moze sie „zgniatac" (obraz wychodzilby na pasek suwakow) —
   przy niskim oknie cala kolumna po prostu przewija sie w #rv-body */
#rv-imgwrap { position: relative; line-height: 0; flex-shrink: 0; }
#rv-full { max-width: 96vw; max-height: 30vh; object-fit: contain; border: 1px solid var(--border); }
#rv-box  { position: absolute; border: 2px solid var(--green);
           box-shadow: 0 0 8px rgba(61,154,61,0.9); display: none; pointer-events: none; }

/* pasek podkręcania obrazu (tylko podgląd — plik zostaje nietknięty) */
#rv-tune {
  display: flex; align-items: center; gap: 6px; padding: 5px 8px; flex-wrap: wrap;
  background: var(--bg-th); border-top: 1px solid var(--border);
}
.tn-btn {
  background: var(--bg-head); color: var(--text); border: 1px solid var(--border);
  padding: 5px 9px; font-size: 12px; cursor: pointer; min-height: 30px;
}
.tn-btn:hover { border-color: var(--blue); }
.tn-btn:active { background: var(--blue); color: #fff; }
.tn-reset { color: var(--dim); }
.tn-sl { display: inline-flex; align-items: center; gap: 6px; font-size: 10px;
         color: var(--dim); letter-spacing: .06em; margin-right: 10px; }
.tn-sl input[type=range] { width: 80px; accent-color: var(--blue); }
.tn-sl span { min-width: 30px; text-align: right; color: var(--text); font-family: PixelData, monospace; }
.tn-sep { width: 1px; height: 22px; background: var(--border); margin: 0 4px; }
.tn-pre { color: var(--blue); }
#tn-neg.on { background: var(--yellow); color: #111; border-color: var(--yellow); }
#tn-lupa.on { border-color: var(--blue); color: var(--blue); }

/* lupa 3x pod kursorem na duzej klatce */
#rv-lupa { display: none; position: fixed; z-index: 1300; pointer-events: none;
           border: 2px solid var(--yellow); background: #000;
           box-shadow: 0 4px 18px rgba(0,0,0,.7); }

/* POPRAW: numer wpisywany w miejscu tablicy, bez okienka */
#rv-fix { display: none; width: 240px; font-family: 'PixelData', 'Arial Narrow', sans-serif;
          font-size: 32px; font-weight: 700; color: #111; background: #fffbe0;
          border: none; outline: 3px solid var(--yellow); letter-spacing: 4px;
          padding: 4px 10px 4px 12px; text-transform: uppercase; }
#rv-fix-hint { display: none; font-size: 11px; color: var(--yellow); }
#rv-meta.fixing #rv-plate-num, #rv-meta.fixing #rv-sid { display: none; }
#rv-meta.fixing #rv-fix, #rv-meta.fixing #rv-fix-hint { display: inline-block; }

/* kolumny kandydatów: starsza | NAJLEPSZA | nowsza, każda w 3 rozmiarach */
#rv-cands { display: flex; width: 100%; gap: 12px; align-items: flex-start; justify-content: center; }
.rv-col { flex: 1; min-width: 0; display: flex; flex-direction: column; align-items: center; gap: 6px; }
.rv-col-cap { font-size: 11px; color: var(--dim); letter-spacing: 0.5px; }
.rv-col img { object-fit: contain; border: 1px solid var(--border); background: #000; max-width: 100%; width: auto; }
.rv-col img.s1 { height: 15vh; }
.rv-col img.s2 { height: 9.5vh; }
.rv-col img.s3 { height: 5.5vh; }
.rv-col-best img.s1 { border: 2px solid var(--green); }
.rv-col.rv-wybrana img { border-color: var(--yellow) !important; }
.rv-col.rv-wybrana .rv-col-cap::after { content: '  ★ DO UCZENIA'; color: var(--yellow); }
#rv-bonus img.wybrana { border: 2px solid var(--yellow); }
#rv-bonus { display: flex; gap: 6px; justify-content: center; flex-wrap: nowrap; overflow: hidden; flex-shrink: 0; }
#rv-bonus img { height: 4.5vh; border: 1px solid var(--border); background: #000; }

#rv-cands-toggle {
  height: 26px; padding: 0 10px; margin-right: 14px; background: var(--bg-th);
  border: 1px solid var(--border); color: var(--text); font-size: 11px; cursor: pointer;
}
#rv-cands-toggle:hover { background: var(--border); }

/* numer jak na prawdziwej tablicy */
#rv-plate {
  display: inline-flex; align-items: stretch; background: #f5f5f5;
  border: 2px solid #222; border-radius: 7px; overflow: hidden;
  box-shadow: 0 2px 10px rgba(0,0,0,0.5);
}
#rv-plate .pl-strip {
  background: #003399; color: #fff; font-family: 'PixelHead', monospace;
  font-size: 13px; font-weight: 700; display: flex; align-items: flex-end; padding: 4px 7px;
}
#rv-plate #rv-plate-num {
  font-family: 'PixelData', 'Arial Narrow', sans-serif; font-size: 32px; line-height: 1.2;
  font-weight: 700; color: #111; letter-spacing: 4px; padding: 4px 10px 4px 12px;
  display: flex; align-items: center; white-space: nowrap;
}
#rv-sid { font-size: 12px; color: var(--dim); }
#rv-sid .v { color: var(--green); }

/* ręczna korekta ramki */
#rv-boxfix, #lb-boxfix {
  height: 26px; padding: 0 10px; background: var(--bg-th);
  border: 1px solid rgba(255, 157, 46, 0.5); color: #ff9d2e; font-size: 11px;
  letter-spacing: 0.4px; cursor: pointer;
}
#rv-boxfix:hover, #lb-boxfix:hover { background: var(--border); }
#rv-boxfix.armed, #lb-boxfix.armed { background: #ff9d2e; color: #111; border-color: #ff9d2e; }
#lb-boxfix { position: absolute; top: 16px; left: 20px; z-index: 1; }
#rv-box.fixed, #lightbox-box.fixed { border-color: #ff9d2e; box-shadow: 0 0 8px rgba(255, 157, 46, 0.9); }

#rv-wait {
  flex: 1; display: none; flex-direction: column; align-items: center;
  justify-content: center; gap: 14px; font-size: 24px; color: var(--green); text-align: center;
}
#rv-wait .rv-sub { font-size: 14px; color: var(--dim); }

#rv-btns {
  display: grid; grid-template-columns: 0.8fr repeat(5, 1fr) 0.8fr; gap: 8px; padding: 10px;
  flex-shrink: 0; background: var(--bg-head); border-top: 1px solid var(--border);
}
.rvb {
  min-height: 64px; font-size: 16px; font-weight: 700; letter-spacing: 0.5px;
  cursor: pointer; border: 1px solid var(--border); background: var(--bg-th); color: var(--text);
}
.rvb small { display: block; font-size: 10px; font-weight: 400; opacity: .75; margin-top: 3px; }
.rvb.rv-ok   { color: #fff; background: var(--green);  border-color: var(--green); }
.rvb.rv-fix  { color: #fff; background: var(--yellow); border-color: var(--yellow); }
.rvb.rv-part { color: #fff; background: var(--blue);   border-color: var(--blue); }
.rvb.rv-unr  { color: #fff; background: #5f6368;       border-color: #5f6368; }
.rvb.rv-not  { color: #fff; background: var(--red);    border-color: var(--red); }
.rvb.sel     { outline: 3px solid #fff; outline-offset: -3px; }
.rvb:active  { filter: brightness(1.3); }

/* tryb rysowania ramki: klatka na cały ekran */
#reviewer.boxfix #rv-meta, #reviewer.boxfix #rv-cands,
#reviewer.boxfix #rv-bonus, #reviewer.boxfix #rv-tune,
#reviewer.boxfix #rv-btns { display: none !important; }
#reviewer.boxfix #rv-full { max-height: 84vh; }
#lightbox.boxfix #lightbox-crop, #lightbox.boxfix #lightbox-plate { display: none !important; }
#lightbox.boxfix #lightbox-img { max-height: 86vh; }
#reviewer.boxfix, #lightbox.boxfix { overflow: hidden; }
#reviewer.boxfix #rv-imgwrap, #lightbox.boxfix #lightbox-imgwrap { touch-action: none; }

/* ── LIGHTBOX ── */
#lightbox { display: none; position: fixed; inset: 0; background: rgba(0, 0, 0, 0.88); z-index: 1000; align-items: center; justify-content: center; }
#lightbox.open { display: flex; }
#lightbox-wrap { display: flex; flex-direction: column; align-items: center; gap: 12px; max-width: 92vw; }
#lightbox-imgwrap { position: relative; line-height: 0; }
#lightbox-img { max-width: 92vw; max-height: 50vh; object-fit: contain; border: 1px solid var(--border); }
#lightbox-box { position: absolute; border: 2px solid var(--green); box-shadow: 0 0 8px rgba(61, 154, 61, 0.9); display: none; pointer-events: none; }
#lightbox-crop { max-width: 88vw; max-height: 34vh; object-fit: contain; border: 1px solid var(--border); }
#lightbox-close { position: absolute; top: 16px; right: 20px; color: var(--dim); font-size: 22px; cursor: pointer; font-family: monospace; line-height: 1; }
#lightbox-close:hover { color: var(--text); }
#lightbox-plate {
  position: absolute; bottom: 24px; left: 50%; transform: translateX(-50%);
  font-family: 'PixelData', Consolas, monospace; font-size: 22px; font-weight: 700;
  color: #fff; letter-spacing: 2px; background: rgba(0,0,0,0.6); padding: 4px 18px;
}

* { scrollbar-width: thin; scrollbar-color: var(--border) var(--bg); }

@media (max-width: 820px) {
  #header .brand-sub { display: none; }
  #header .sep { margin: 0 6px; }
  table#events-table td:nth-child(1), table#events-table th:nth-child(1),
  table#events-table td:nth-child(4), table#events-table th:nth-child(4),
  table#events-table td:nth-child(5), table#events-table th:nth-child(5),
  table#events-table td:nth-child(6), table#events-table th:nth-child(6) { display: none; }
  table#events-table { table-layout: auto; }
  #rv-title { display: none; }
  #rv-btns { grid-template-columns: repeat(2, 1fr); }
  .rvb { min-height: 52px; font-size: 14px; }
  #rv-plate #rv-plate-num { font-size: 24px; letter-spacing: 3px; }
  #rv-full { max-height: 24vh; }
  .rv-col img.s1 { height: 10vh; }
  .rv-col img.s2 { height: 6.5vh; }
  .rv-col img.s3 { display: none; }
  #rv-bonus { display: none; }
}
/* ── RADAR (02.10) ── */
#radar-modal { display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.85); z-index: 900; align-items: center; justify-content: center; }
#radar-box { background: var(--bg); border: 1px solid var(--border); width: min(1200px, 96vw); max-height: 92vh; display: flex; flex-direction: column; }
#radar-head { display: flex; align-items: center; gap: 10px; padding: 8px 12px; border-bottom: 1px solid var(--border); font-size: 13px; letter-spacing: 1px; }
#radar-head button { height: 28px; }
#radar-body { overflow: auto; padding: 10px 12px; }
.rd-row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 10px; font-size: 13px; }
.rd-row label { color: var(--dim); font-size: 11px; text-transform: uppercase; }
.rd-row input, .rd-row select { height: 28px; background: var(--bg); color: var(--text); border: 1px solid var(--border); font-size: 13px; padding: 0 6px; }
.rd-row input[type=number] { width: 76px; }
.rd-row .rd-chk, .rd-chk { display: flex; align-items: center; gap: 4px; cursor: pointer; text-transform: none; font-size: 12px; color: var(--text); }
.rd-ev { display: flex; gap: 10px; align-items: center; padding: 8px; border-bottom: 1px solid var(--border); }
.rd-ev img { width: 150px; height: 84px; object-fit: cover; border: 1px solid var(--border); cursor: pointer; }
.rd-ev .v { font-family: monospace; font-size: 26px; font-weight: 700; color: var(--red); min-width: 90px; }
.rd-ev .t { font-size: 12px; color: var(--dim); }
.rd-ev .pl { font-family: monospace; font-size: 18px; color: #fff; }
.rd-ev a { color: var(--yellow); font-size: 12px; }
</style>
</head>
<body>

<!-- HEADER -->
<div id="header">
  <div class="brand">
    <div class="brand-icon">LP</div>
    <div><div class="brand-name">LPR / ANPR</div></div>
  </div>
  <div class="brand-sub">PRZEGL&#260;D OFFLINE &mdash; PENDRIVE Z AUTA</div>
  <div class="sep"></div>
  <div class="stat"><span id="h-total">&mdash;</span> zapis&oacute;w</div>
  <div class="sep"></div>
  <div class="stat"><span id="h-rev">&mdash;</span> ocenionych</div>
  <div class="sep"></div>
  <div class="stat"><span id="h-un" class="warn">&mdash;</span> do oceny</div>
  <div class="spacer"></div>
  <div id="h-status" class="status-online" title="">&#x25CF; BAZA OK</div>
  <div class="clock" id="h-clock">--:--:--</div>
</div>

<!-- MAIN -->
<div id="main">
  <div id="events-panel">
    <div id="events-toolbar">
      <label>DZIE&#323;:</label>
      <select id="day-sel"><option value="">wszystkie dni</option></select>
      <label>SZUKAJ:</label>
      <input type="text" id="search-input" placeholder="np. WA1234A" autocomplete="off" spellcheck="false">
      <label class="chk"><input type="checkbox" id="un-chk"> tylko nieocenione</label>
      <button id="refresh-btn" onclick="fetchPlates(true)"><span data-ico="ref"></span> ODSWIEZ</button>
      <button id="reviewer-btn" onclick="reviewerOpen()"><span data-ico="list"></span> EDYTOR</button>
      <button id="radar-btn" onclick="radarOpen()" title="Zdarzenia fotoradaru zapisane na pendrive">&#9673; RADAR</button>
    </div>
    <div id="table-wrap">
      <table id="events-table">
        <colgroup>
          <col class="c-id"><col class="c-time"><col class="c-plate"><col class="c-det">
          <col class="c-ocr"><col class="c-dur"><col class="c-img"><col class="c-verify">
        </colgroup>
        <thead><tr>
          <th>#</th><th>CZAS</th><th>TABLICA</th><th>DET%</th><th>OCR%</th><th>CZAS</th>
          <th style="text-align:right">MINIATURA</th><th style="text-align:right">OCENA</th>
        </tr></thead>
        <tbody id="events-tbody">
          <tr><td colspan="8" style="text-align:center;color:var(--dim);padding:20px;font-size:11px">Ladowanie danych...</td></tr>
        </tbody>
      </table>
    </div>
    <div id="tbl-pager"></div>
  </div>
</div>

<!-- RADAR: zdarzenia fotoradaru (02.10) -->
<div id="radar-modal" onclick="if(event.target===this)radarClose()">
  <div id="radar-box">
    <div id="radar-head"><span>&#9673; RADAR &mdash; ZDARZENIA FOTORADARU (pendrive)</span>
      <span style="flex:1"></span>
      <label class="rd-chk" style="font-size:11px"><input type="checkbox" id="rd-dbg" onchange="radarDebug(this.checked)"> DEBUG</label>
      <button onclick="radarClose()">&#10005;</button></div>
    <div id="radar-body">Ladowanie...</div>
  </div>
</div>

<!-- EDYTOR WERYFIKACJI -->
<div id="reviewer">
  <div id="rv-head">
    <span id="rv-count">zaleg&#322;e: 0</span>
    <span id="rv-title" title="1-5 oceny &middot; &#8592; poprzednie &middot; &#8594; pomi&#324; &middot; R ramka &middot; N negatyw &middot; L lupa &middot; +/- EV &middot; 0 reset &middot; Esc">ZALEG&#321;O&#346;CI &mdash; najnowsze nieocenione &middot; 1-5 oceny &middot; &#8592; poprzednie &middot; &#8594; pomi&#324; &middot; R N L +/- 0</span>
    <button id="rv-boxfix" onclick="bboxStart('rv')">&#x25A2; POPRAW RAMK&#280;</button>
    <button id="rv-cands-toggle" onclick="rvToggleCands()">KANDYDACI: WL</button>
    <span id="rv-close" onclick="reviewerClose()">&#x2715;</span>
  </div>
  <div id="rv-body">
    <div id="rv-imgwrap">
      <img id="rv-full" src="" alt="">
      <div id="rv-box"></div>
    </div>
    <div id="rv-tune">
      <label class="tn-sl">EV
        <input type="range" id="tn-ev" min="-4" max="4" step="0.25" value="0" oninput="rvTuneSuwak()">
        <span id="tn-ev-v">0</span>
      </label>
      <label class="tn-sl">KONTRAST
        <input type="range" id="tn-c" min="0" max="3" step="0.1" value="0" oninput="rvTuneSuwak()">
        <span id="tn-c-v">0</span>
      </label>
      <label class="tn-sl" title="Sciaga same swiatla — ratuje przeswietlone tablice">PRZESWIETLENIA
        <input type="range" id="tn-h" min="0" max="1" step="0.05" value="0" oninput="rvTuneSuwak()">
        <span id="tn-h-v">0</span>
      </label>
      <label class="tn-sl" title="Podnosi same cienie (gamma) — ratuje niedoswietlone tablice">CIENIE
        <input type="range" id="tn-g" min="0" max="1" step="0.05" value="0" oninput="rvTuneSuwak()">
        <span id="tn-g-v">0</span>
      </label>
      <label class="tn-sl" title="Wyostrzenie (maska) — pomaga przy lekkim rozmyciu">OSTROSC
        <input type="range" id="tn-s" min="0" max="2" step="0.1" value="0" oninput="rvTuneSuwak()">
        <span id="tn-s-v">0</span>
      </label>
      <button class="tn-btn" id="tn-neg" onclick="rvTune('neg')" title="Negatyw — czesto najskuteczniejszy przy odblasku (N)">NEGATYW</button>
      <span class="tn-sep"></span>
      <button class="tn-btn tn-pre" onclick="rvTune('ciemna')" title="EV+2, kontrast, cienie">CIEMNA</button>
      <button class="tn-btn tn-pre" onclick="rvTune('jasna')" title="EV-1, przeswietlenia, kontrast">PRZESWIETLONA</button>
      <button class="tn-btn tn-pre" onclick="rvTune('rozmyta')" title="ostrosc + kontrast">ROZMYTA</button>
      <button class="tn-btn tn-reset" onclick="rvTune('reset')" title="Powrot do oryginalu (0)">RESET</button>
      <span class="tn-sep"></span>
      <button class="tn-btn" id="tn-lupa" onclick="rvLupaToggle()" title="Lupa 3x pod kursorem na duzej klatce (L)">LUPA: WL</button>
    </div>
    <svg width="0" height="0" style="position:absolute" aria-hidden="true">
      <filter id="fltSwiatla">
        <feComponentTransfer>
          <feFuncR id="fnR" type="table" tableValues="0 0.25 0.5 0.75 1"/>
          <feFuncG id="fnG" type="table" tableValues="0 0.25 0.5 0.75 1"/>
          <feFuncB id="fnB" type="table" tableValues="0 0.25 0.5 0.75 1"/>
        </feComponentTransfer>
      </filter>
      <!-- cienie: gamma < 1 podnosi dol zakresu, biel zostaje biela -->
      <filter id="fltCienie">
        <feComponentTransfer>
          <feFuncR id="gmR" type="gamma" amplitude="1" exponent="1" offset="0"/>
          <feFuncG id="gmG" type="gamma" amplitude="1" exponent="1" offset="0"/>
          <feFuncB id="gmB" type="gamma" amplitude="1" exponent="1" offset="0"/>
        </feComponentTransfer>
      </filter>
      <!-- ostrosc: maska wyostrzajaca 3x3, sila = ile odjac od sasiadow -->
      <filter id="fltOstrosc">
        <feConvolveMatrix id="cvM" order="3" kernelMatrix="0 0 0 0 1 0 0 0 0"
                          divisor="1" preserveAlpha="true" edgeMode="duplicate"/>
      </filter>
    </svg>
    <canvas id="rv-lupa" width="360" height="180"></canvas>
    <div id="rv-meta">
      <div id="rv-plate"><span class="pl-strip">PL</span><span id="rv-plate-num">&mdash;</span>
        <input id="rv-fix" maxlength="10" autocomplete="off" spellcheck="false" placeholder="NUMER"></div>
      <div id="rv-sid"></div>
      <span id="rv-fix-hint">Enter = zapisz &middot; Esc = anuluj</span>
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
    <span class="rv-sub" id="rv-wait-sub">brak nieocenionych zapis&oacute;w w tym filtrze</span>
  </div>
  <div id="rv-btns">
    <button class="rvb rv-prev" onclick="rvPrev()">&#x25C0; POPRZEDNIE<small>&#8592;</small></button>
    <button class="rvb rv-ok"   onclick="rvVerdict('ok')">&#x2713; DOBRZE<small>1</small></button>
    <button class="rvb rv-fix"  onclick="rvVerdict('fix')">&#x270E; POPRAW<small>2</small></button>
    <button class="rvb rv-part" onclick="rvVerdict('partial')">&#xBD; NIEPE&#321;NA<small>3</small></button>
    <button class="rvb rv-unr"  onclick="rvVerdict('unreadable')">? NIECZYTELNA<small>4</small></button>
    <button class="rvb rv-not"  onclick="rvVerdict('notplate')">&#x2298; NIE TABLICA<small>5</small></button>
    <button class="rvb rv-skip" onclick="rvSkip()">POMI&#323; &#x25B6;<small>&#8594;</small></button>
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

<script>
(function() {
  'use strict';

  /* ── heartbeat: po zamknięciu okna serwer w EXE sam się kończy ── */
  setInterval(function() { fetch('/api/ping').catch(function() {}); }, 2000);
  fetch('/api/ping').catch(function() {});
  document.addEventListener('visibilitychange', function() {
    if (!document.hidden) fetch('/api/ping').catch(function() {});
  });
  /* zamkniecie karty/okna: powiedz serwerowi wprost, zamiast czekac na
     wygasniecie pingow (przy schowanej karcie przegladarka je dlawi) */
  window.addEventListener('pagehide', function() {
    try { navigator.sendBeacon('/api/bye', ''); } catch (e) {}
  });

  /* ── zegar ── */
  function updateClock() {
    var d = new Date();
    var p = function(n) { return (n < 10 ? '0' : '') + n; };
    document.getElementById('h-clock').textContent =
      p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
  }
  setInterval(updateClock, 1000); updateClock();

  function timeAgo(isoStr) { return isoStr || ''; }
  function formatTimeShort(isoStr) {
    if (!isoStr) return '—';
    return isoStr.slice(5, 16);            /* MM-DD HH:MM */
  }
  function confClass(val) {
    if (val === null || val === undefined) return 'conf-red';
    if (val >= 0.8) return 'conf-green';
    if (val >= 0.6) return 'conf-yellow';
    return 'conf-red';
  }
  function confText(val) {
    if (val === null || val === undefined) return '—';
    return Math.round(val * 100) + '%';
  }

  /* ── IKONY PIXEL-ART (jak w www) ── */
  var PXI = {
    ok:   [[1,3],[2,4],[3,5],[4,4],[5,3],[6,2],[7,1]],
    fix:  [[7,0],[6,1],[5,2],[4,3],[3,4],[2,5],[1,6],[0,7],[1,7],[0,6]],
    part: [[1,1],[2,1],[3,1],[1,2],[2,2],[3,2],[1,3],[2,3],[3,3],
           [1,4],[2,4],[3,4],[1,5],[2,5],[3,5],[1,6],[2,6],[3,6],
           [4,1],[5,1],[6,1],[6,2],[6,3],[6,4],[6,5],[4,6],[5,6],[6,6]],
    unr:  [[2,1],[3,1],[4,1],[5,2],[4,3],[3,4],[3,5],[3,7]],
    not:  [[2,1],[3,1],[4,1],[5,1],[1,2],[6,2],[1,3],[6,3],[1,4],[6,4],
           [1,5],[6,5],[2,6],[3,6],[4,6],[5,6],[2,5],[3,4],[4,3],[5,2]],
    list: [[1,1],[2,1],[3,1],[4,1],[5,1],[6,1],[1,3],[2,3],[3,3],[4,3],
           [5,3],[6,3],[1,5],[2,5],[3,5],[4,5],[5,5],[6,5]],
    ref:  [[2,1],[3,1],[4,1],[1,2],[5,2],[1,3],[6,3],[5,4],[6,4],[1,5],
           [2,5],[3,5],[4,5],[5,5],[6,2]]
  };
  function pxIcon(nazwa, rozmiar) {
    var p = PXI[nazwa]; if (!p) return '';
    var r = '';
    for (var i = 0; i < p.length; i++)
      r += '<rect x="' + p[i][0] + '" y="' + p[i][1] + '" width="1" height="1"/>';
    var s = rozmiar || 13;
    return '<svg viewBox="0 0 8 8" width="' + s + '" height="' + s +
           '" fill="currentColor" shape-rendering="crispEdges" ' +
           'style="vertical-align:-1px">' + r + '</svg>';
  }
  (function() {
    var el = document.querySelectorAll('[data-ico]');
    for (var i = 0; i < el.length; i++)
      el[i].innerHTML = pxIcon(el[i].getAttribute('data-ico'), 14);
  })();

  /* ── stan filtra ── */
  var flt = { day: '', q: '', un: false };
  function fltQs() {
    return '&day=' + encodeURIComponent(flt.day) + '&q=' + encodeURIComponent(flt.q) +
           '&un=' + (flt.un ? '1' : '0');
  }

  /* ── STATYSTYKI ── */
  function fetchStats() {
    fetch('/api/stats').then(function(r) { return r.json(); }).then(function(d) {
      document.getElementById('h-total').textContent = d.total;
      document.getElementById('h-rev').textContent = d.reviewed;
      document.getElementById('h-un').textContent = d.unreviewed;
      var st = document.getElementById('h-status');
      st.className = 'status-online';
      st.innerHTML = '&#x25CF; BAZA OK';
      st.title = d.data_dir + (d.last_seen ? ('  ·  ostatni zapis ' + d.last_seen) : '');
    }).catch(function() {
      var st = document.getElementById('h-status');
      st.className = 'status-offline';
      st.innerHTML = '&#x25CF; BRAK BAZY';
    });
  }

  /* ── DNI ── */
  function fetchDays() {
    fetch('/api/days').then(function(r) { return r.json(); }).then(function(days) {
      var sel = document.getElementById('day-sel');
      var cur = sel.value;
      var h = '<option value="">wszystkie dni</option>';
      days.forEach(function(d) {
        h += '<option value="' + d.d + '">' + d.d + '  (' + d.n +
             (d.un ? ', do oceny ' + d.un : '') + ')</option>';
      });
      sel.innerHTML = h;
      sel.value = cur;
    });
  }

  /* ── TABELA ── */
  var allRows = [], rowTd = {};
  var tablePage = 0, tableTotal = 0, PAGE_SIZE = 100;

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

  function sendVerify(item, verdict, td) {
    if (item.verify === verdict && verdict !== 'fix') verdict = 'clear';  /* drugi klik = cofnij */
    var payload = { id: item.id, verdict: verdict };
    if (verdict === 'fix') {
      var t = prompt('Poprawny numer dla #' + item.id + ':', item.vtext || item.plate || '');
      if (t === null) return;
      t = t.toUpperCase().replace(/[^A-Z0-9]/g, '');
      if (t.length < 2) return;
      if (t.length > 10) { alert('Za dlugi numer (maks. 10 znakow)'); return; }
      payload.text = t;
    }
    fetch('/api/verify', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload) })
      .then(function(r) { return r.json(); })
      .then(function(d) {
        if (!d.ok) { alert(d.error || 'Blad zapisu'); return; }
        item.verify = (verdict === 'clear') ? null : verdict;
        item.vtext = payload.text || null;
        renderVerify(td, item);
        fetchStats();
      })
      .catch(function() { alert('Blad polaczenia z baza'); });
  }

  function buildRow(item) {
    var plate = (item.plate || 'UNKNOWN').trim().toUpperCase();
    var isUnknown = plate === 'UNKNOWN' || plate === '' || plate === '?';
    var tr = document.createElement('tr');
    if (isUnknown) tr.classList.add('unknown-row');
    tr.style.cursor = 'pointer';
    tr.onclick = function() { openLightbox(item); };

    var tdId = document.createElement('td');
    tdId.style.color = 'var(--dim)';
    tdId.textContent = item.id;
    tr.appendChild(tdId);

    var tdTime = document.createElement('td');
    tdTime.className = 'td-time';
    tdTime.title = timeAgo(item.seen_at);
    tdTime.textContent = formatTimeShort(item.seen_at);
    tr.appendChild(tdTime);

    var tdPlate = document.createElement('td');
    tdPlate.className = 'td-plate' + (isUnknown ? ' unknown' : '');
    tdPlate.textContent = plate;
    tr.appendChild(tdPlate);

    var tdDet = document.createElement('td');
    tdDet.className = confClass(item.det_conf);
    tdDet.textContent = confText(item.det_conf);
    tr.appendChild(tdDet);

    var tdOcr = document.createElement('td');
    tdOcr.className = confClass(item.ocr_conf);
    tdOcr.textContent = confText(item.ocr_conf);
    tr.appendChild(tdOcr);

    var tdDur = document.createElement('td');
    tdDur.className = 'td-dur';
    if (item.duration !== null && item.duration !== undefined) {
      tdDur.textContent = parseFloat(item.duration).toFixed(1) + 's';
    } else { tdDur.textContent = '—'; tdDur.style.color = 'var(--border)'; }
    tr.appendChild(tdDur);

    var tdImg = document.createElement('td');
    tdImg.className = 'td-img';
    if (item.crop_url) {
      var img = document.createElement('img');
      img.src = item.crop_url; img.alt = plate; img.title = plate; img.loading = 'lazy';
      img.onerror = function() { this.parentNode.innerHTML = '<span class="no-img">BRAK</span>'; };
      img.onclick = function(e) { e.stopPropagation(); openLightbox(item); };
      tdImg.appendChild(img);
    } else {
      tdImg.innerHTML = '<span class="no-img">—</span>';
    }
    tr.appendChild(tdImg);

    var tdVer = document.createElement('td');
    tdVer.className = 'td-verify';
    renderVerify(tdVer, item);
    rowTd[item.id] = { td: tdVer, item: item };
    tr.appendChild(tdVer);
    return tr;
  }

  function renderTable(data) {
    var tbody = document.getElementById('events-tbody');
    rowTd = {};
    if (!data.length) {
      tbody.innerHTML = '<tr><td colspan="8" style="text-align:center;color:var(--dim);padding:20px;font-size:11px">Brak wynikow</td></tr>';
      return;
    }
    var frag = document.createDocumentFragment();
    data.forEach(function(item) { frag.appendChild(buildRow(item)); });
    tbody.innerHTML = '';
    tbody.appendChild(frag);
  }

  function renderPager() {
    var pager = document.getElementById('tbl-pager');
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

  var fetchSeq = 0;
  window.fetchPlates = function(resetPage) {
    if (resetPage) tablePage = 0;
    var offset = tablePage * PAGE_SIZE, totalHdr = 0, seq = ++fetchSeq;
    fetch('/api/plates?limit=' + PAGE_SIZE + '&offset=' + offset + fltQs())
      .then(function(r) {
        totalHdr = parseInt(r.headers.get('X-Total-Count') || '0', 10) || 0;
        return r.json();
      })
      .then(function(data) {
        if (!Array.isArray(data) || seq !== fetchSeq) return;   /* spozniona odpowiedz starego filtra */
        tableTotal = totalHdr;
        allRows = data;
        renderTable(data);
        renderPager();
        document.getElementById('table-wrap').scrollTop = 0;
      })
      .catch(function() {});
    fetchStats();
  };

  window.tableGoto = function(page) {
    var maxPage = Math.max(0, Math.ceil(tableTotal / PAGE_SIZE) - 1);
    tablePage = Math.max(0, Math.min(maxPage, page));
    fetchPlates(false);
  };

  var qTimer = null;
  document.getElementById('search-input').addEventListener('input', function() {
    flt.q = this.value.trim();
    clearTimeout(qTimer);
    qTimer = setTimeout(function() { fetchPlates(true); }, 250);
  });
  document.getElementById('day-sel').addEventListener('change', function() {
    flt.day = this.value; fetchPlates(true);
  });
  document.getElementById('un-chk').addEventListener('change', function() {
    flt.un = this.checked; fetchPlates(true);
  });

  /* ── PODKRĘCANIE OBRAZU W EDYTORZE (filtr na podglądzie, plik nietknięty) ── */
  var rvT = { ev: 0, c: 0, h: 0, g: 0, s: 0, neg: false };
  var rvFilter = '';          /* aktualny filtr CSS — uzywa go tez lupa */
  function rvTuneApply() {
    var jasn  = Math.pow(2, rvT.ev * 0.5);
    var kontr = 1 + rvT.c * 0.35;
    var f = 'brightness(' + jasn.toFixed(3) + ') contrast(' + kontr.toFixed(2) + ')';
    if (rvT.neg) f += ' invert(1)';
    if (rvT.g > 0) {            /* cienie: gamma 1 -> 0.45 */
      var ex = (1 - 0.55 * rvT.g).toFixed(3);
      ['gmR', 'gmG', 'gmB'].forEach(function(id) {
        var el = document.getElementById(id);
        if (el) el.setAttribute('exponent', ex);
      });
      f += ' url(#fltCienie)';
    }
    if (rvT.h > 0) {
      var t = [0, 0.25, 0.5, (0.75 - 0.10 * rvT.h).toFixed(3), (1 - 0.45 * rvT.h).toFixed(3)].join(' ');
      ['fnR', 'fnG', 'fnB'].forEach(function(id) {
        var el = document.getElementById(id);
        if (el) el.setAttribute('tableValues', t);
      });
      f += ' url(#fltSwiatla)';
    }
    if (rvT.s > 0) {            /* maska wyostrzajaca: srodek 1+4a, sasiedzi -a */
      var a = (rvT.s * 0.5).toFixed(2), ce = (1 + 4 * rvT.s * 0.5).toFixed(2);
      var cv = document.getElementById('cvM');
      if (cv) cv.setAttribute('kernelMatrix',
        ['0', '-' + a, '0', '-' + a, ce, '-' + a, '0', '-' + a, '0'].join(' '));
      f += ' url(#fltOstrosc)';
    }
    rvFilter = f;
    var cele = document.querySelectorAll('#rv-full, #rv-cands img, #rv-bonus img');
    for (var i = 0; i < cele.length; i++) cele[i].style.filter = f;
    var p = function(id, v) { var el = document.getElementById(id); if (el) el.textContent = v; };
    p('tn-ev-v', (rvT.ev > 0 ? '+' : '') + rvT.ev);
    p('tn-c-v', rvT.c.toFixed(1));
    p('tn-h-v', rvT.h.toFixed(2));
    p('tn-g-v', rvT.g.toFixed(2));
    p('tn-s-v', rvT.s.toFixed(1));
    document.getElementById('tn-neg').classList.toggle('on', rvT.neg);
    var sl = function(id, v) { var el = document.getElementById(id); if (el) el.value = v; };
    sl('tn-ev', rvT.ev); sl('tn-c', rvT.c); sl('tn-h', rvT.h); sl('tn-g', rvT.g); sl('tn-s', rvT.s);
    rvLupaRedraw();
  }
  window.rvTuneSuwak = function() {
    var v = function(id) { var el = document.getElementById(id); return el ? parseFloat(el.value) : 0; };
    rvT.ev = v('tn-ev'); rvT.c = v('tn-c'); rvT.h = v('tn-h'); rvT.g = v('tn-g'); rvT.s = v('tn-s');
    rvTuneApply();
  };
  /* presety = punkt startowy; suwaki mozna potem dokrecic */
  window.rvTune = function(co) {
    if (co === 'reset')        rvT = { ev: 0, c: 0, h: 0, g: 0, s: 0, neg: false };
    else if (co === 'neg')     rvT.neg = !rvT.neg;
    else if (co === 'ciemna')  rvT = { ev: 2, c: 1, h: 0, g: 0.6, s: 0, neg: false };
    else if (co === 'jasna')   rvT = { ev: -1, c: 0.5, h: 0.8, g: 0, s: 0, neg: false };
    else if (co === 'rozmyta') rvT = { ev: 0, c: 1, h: 0, g: 0, s: 1.2, neg: false };
    else if (co === 'ev+')     rvT.ev = Math.min(4, rvT.ev + 0.5);
    else if (co === 'ev-')     rvT.ev = Math.max(-4, rvT.ev - 0.5);
    rvTuneApply();
  };

  /* ── LUPA: powiekszenie 3x pod kursorem na duzej klatce (z tym samym
     filtrem co podglad) — zeby nie klikac w tryb ramki, by przyjrzec sie
     tablicy ── */
  var lupaOn = localStorage.getItem('lpr_lupa') !== '0', lupaPos = null;
  var LUPA_Z = 3, lupaCv = document.getElementById('rv-lupa');
  function rvLupaBtn() {
    var b = document.getElementById('tn-lupa');
    b.textContent = 'LUPA: ' + (lupaOn ? 'WL' : 'WYL');
    b.classList.toggle('on', lupaOn);
  }
  window.rvLupaToggle = function() {
    lupaOn = !lupaOn;
    localStorage.setItem('lpr_lupa', lupaOn ? '1' : '0');
    rvLupaBtn();
    if (!lupaOn) lupaCv.style.display = 'none';
  };
  rvLupaBtn();
  function rvLupaRedraw() {
    var img = document.getElementById('rv-full');
    if (!lupaOn || !lupaPos || bbx.mode || !img.naturalWidth) { lupaCv.style.display = 'none'; return; }
    var r = img.getBoundingClientRect();
    if (!r.width || !r.height) return;
    var nx = (lupaPos[0] - r.left) / r.width, ny = (lupaPos[1] - r.top) / r.height;
    if (nx < 0 || nx > 1 || ny < 0 || ny > 1) { lupaCv.style.display = 'none'; return; }
    var scale = img.naturalWidth / r.width;           /* piksele obrazu na piksel ekranu */
    var sw = lupaCv.width / LUPA_Z * scale, sh = lupaCv.height / LUPA_Z * scale;
    var sx = nx * img.naturalWidth - sw / 2, sy = ny * img.naturalHeight - sh / 2;
    var ctx = lupaCv.getContext('2d');
    ctx.clearRect(0, 0, lupaCv.width, lupaCv.height);
    try { ctx.filter = rvFilter || 'none'; } catch (e) {}
    ctx.imageSmoothingEnabled = true;
    ctx.drawImage(img, sx, sy, sw, sh, 0, 0, lupaCv.width, lupaCv.height);
    /* pozycja: przy kursorze, nie wychodzi poza ekran */
    var W = lupaCv.width + 4, H = lupaCv.height + 4;
    var x = lupaPos[0] + 24, y = lupaPos[1] + 24;
    if (x + W > window.innerWidth) x = lupaPos[0] - 24 - W;
    if (y + H > window.innerHeight) y = lupaPos[1] - 24 - H;
    lupaCv.style.left = Math.max(0, x) + 'px';
    lupaCv.style.top = Math.max(0, y) + 'px';
    lupaCv.style.display = 'block';
  }
  (function() {
    var img = document.getElementById('rv-full');
    img.addEventListener('mousemove', function(e) { lupaPos = [e.clientX, e.clientY]; rvLupaRedraw(); });
    img.addEventListener('mouseleave', function() { lupaPos = null; lupaCv.style.display = 'none'; });
  })();

  /* ── RAMKA NA KLATCE ── */
  function _placeBox(imgId, boxId, b) {
    var img = document.getElementById(imgId);
    var box = document.getElementById(boxId);
    var src = img.src;          /* ramka nalezy do TEGO obrazka */
    var place = function() {
      if (img.src !== src) return;   /* stary onload po podmianie zdjecia */
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

  /* ── RĘCZNA KOREKTA RAMKI (mysz/dotyk, zoom kółkiem) ── */
  var bbx = { mode: null, start: null, tmp: null };
  var _lbItem = null, rvCurrent = null;

  function _bbxCfg(mode) {
    return mode === 'rv'
      ? { img: 'rv-full', box: 'rv-box', btn: 'rv-boxfix', wrap: 'reviewer',
          imgwrap: 'rv-imgwrap', item: function() { return rvCurrent; } }
      : { img: 'lightbox-img', box: 'lightbox-box', btn: 'lb-boxfix', wrap: 'lightbox',
          imgwrap: 'lightbox-imgwrap', item: function() { return _lbItem; } };
  }
  var _zoom = { s: 1, tx: 0, ty: 0, base: null, wrapEl: null };
  function _zoomApply() {
    if (_zoom.wrapEl) {
      _zoom.wrapEl.style.transformOrigin = '0 0';
      _zoom.wrapEl.style.transform = 'translate(' + _zoom.tx + 'px,' + _zoom.ty + 'px) scale(' + _zoom.s + ')';
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
      (b[2] - b[0]) * iw, (b[3] - b[1]) * ih, 0, 0, c.width, c.height);
    return c.toDataURL('image/jpeg', 0.92);
  }
  /* obraz zmienia rozmiar przy wejściu/wyjściu z trybu rysowania — ramkę
     trzeba przeliczyć na nowe piksele, inaczej zostaje w starym miejscu */
  function _bbxReplace(mode) {
    var c = _bbxCfg(mode), it = c.item();
    var b = it && (it.vbbox || it.bbox);
    if (b) requestAnimationFrame(function() { _placeBox(c.img, c.box, b); });
  }
  window.bboxStart = function(mode) {
    /* bez pelnej klatki nie ma czego poprawiac: ramka narysowana na cropie
       trafilaby do bazy jako wspolrzedne pelnej klatki = zepsute dane */
    var cur = _bbxCfg(mode).item();
    if (bbx.mode !== mode && (!cur || !cur.full_url)) return;
    if (bbx.mode === mode) {           /* drugi klik = anuluj tryb */
      bbx.mode = null; bbx.start = null;
      var c0 = _bbxCfg(mode);
      document.getElementById(c0.img).style.cursor = '';
      document.getElementById(c0.btn).classList.remove('armed');
      document.getElementById(c0.wrap).classList.remove('boxfix');
      _zoomReset(null);
      _bbxReplace(mode);
      return;
    }
    bbx.mode = mode; bbx.start = null; bbx.tmp = null;
    var c = _bbxCfg(mode);
    document.getElementById(c.img).style.cursor = 'crosshair';
    document.getElementById(c.btn).classList.add('armed');
    document.getElementById(c.wrap).classList.add('boxfix');
    _zoomReset(document.getElementById(c.imgwrap));
    _bbxReplace(mode);
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
    var pinch = null;
    function down(e) {
      if (bbx.mode !== mode) return;
      e.preventDefault(); e.stopPropagation();
      if (e.touches && e.touches.length >= 2) {
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
        _zoom.tx += mx - pinch.mx; _zoom.ty += my - pinch.my;
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
      var f = Math.exp(-e.deltaY * (e.ctrlKey ? 0.01 : 0.002));
      _zoomAt(e.clientX, e.clientY, f);
    }
    function up(e) {
      if (bbx.mode !== mode) return;
      if (e.touches && e.touches.length) { pinch = null; return; }
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
        if (it && (it.vbbox || it.bbox)) {
          box.className = it.vbbox ? 'fixed' : '';
          _placeBox(c.img, c.box, it.vbbox || it.bbox);
        }
        return;
      }
      it.vbbox = b;
      bbx.justDrawn = true;   /* click po mouseup poza obrazkiem nie zamyka lightboxa */
      setTimeout(function() { bbx.justDrawn = false; }, 300);
      box.className = 'fixed';
      _placeBox(c.img, c.box, b);
      var du = null;
      try { du = _cropDataUrl(img, b); } catch (err) {}
      if (du) {
        if (mode === 'rv') {
          Array.prototype.slice.call(document.querySelectorAll('#rv-col-best img'))
            .forEach(function(n) { n.src = du; });
        } else {
          document.getElementById('lightbox-crop').src = du;
        }
      }
      fetch('/api/verify', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: it.id, verdict: 'bbox', bbox: b }) }).catch(function() {});
    }
    img.addEventListener('mousedown', down);
    document.addEventListener('mousemove', mv);
    document.addEventListener('mouseup', up);
    img.addEventListener('touchstart', down, { passive: false });
    document.addEventListener('touchmove', mv, { passive: false });
    document.addEventListener('touchend', up);
    document.getElementById(c.wrap).addEventListener('wheel', wheel, { passive: false });
  }
  _bbxAttach('rv');
  _bbxAttach('lb');

  /* ── LIGHTBOX ── */
  window.openLightbox = function(item) {
    var full = document.getElementById('lightbox-img');
    var crop = document.getElementById('lightbox-crop');
    var lbBox = document.getElementById('lightbox-box');
    lbBox.style.display = 'none'; lbBox.className = '';
    full.onload = null;         /* stary onload z _placeBox poprzedniego zapisu */
    _lbItem = item;
    var fsrc = item.full_url || item.crop_url || '';
    var csrc = (item.full_url && item.crop_url) ? item.crop_url : '';
    /* 02.10: brak pełnej klatki (0 B po naprawie pendrive'a) → pokaż crop, a gdy i jego nie ma — schowaj */
    (function() {
      var rf = document.getElementById('rv-full');
      if (!rf) return;
      rf.style.display = '';
      rf.dataset.fallback = csrc || '';
      rf.onerror = function() {
        if (this.dataset.fallback && this.src.indexOf(this.dataset.fallback) < 0) { this.src = this.dataset.fallback; }
        else { this.style.display = 'none'; }
      };
    })();
    full.src = fsrc;
    full.style.display = fsrc ? '' : 'none';
    crop.src = csrc;
    crop.style.display = csrc ? '' : 'none';
    if (item.full_url && item.crop_url) {
      if (item.vbbox) { lbBox.className = 'fixed'; _lbShowBox(item.vbbox); }
      else if (item.bbox) _lbShowBox(item.bbox);
    }
    var plate = (item.plate || 'UNKNOWN').trim().toUpperCase();
    document.getElementById('lightbox-plate').textContent = '#' + item.id + '   ' + plate;
    document.getElementById('lightbox').classList.add('open');
  };
  window.closeLightbox = function(e, force) {
    if (!force && bbx.justDrawn) return;
    if (force || !e || e.target === document.getElementById('lightbox')) {
      if (bbx.mode === 'lb') bboxStart('lb');   /* zdejmij tryb rysowania */
      document.getElementById('lightbox').classList.remove('open');
      document.getElementById('lightbox-img').src = '';
      document.getElementById('lightbox-crop').src = '';
      document.getElementById('lightbox-box').style.display = 'none';
      _lbItem = null;
    }
  };

  /* ── EDYTOR WERYFIKACJI (zaległości z bazy wg bieżącego filtra) ── */
  var rvActive = false, rvSeen = {}, rvHistory = [];
  var rvBacklog = [], rvBacklogTotal = 0, rvBacklogBusy = false, rvBacklogCb = null;
  var rvSkipped = {};        /* pominiete (→) — serwer ma ich nie zwracac ponownie */
  var rvLepszaUrl = null;

  window.reviewerOpen = function() {
    rvActive = true; rvSeen = {}; rvSkipped = {}; rvCurrent = null; rvHistory = [];
    rvBacklog = []; rvBacklogTotal = 0; rvDone = 0;
    document.getElementById('rv-cands-toggle').textContent =
      'KANDYDACI: ' + (localStorage.getItem('lpr_cands') !== '0' ? 'WL' : 'WYL');
    document.getElementById('reviewer').classList.add('open');
    rvRefresh();
  };
  window.reviewerClose = function() {
    if (bbx.mode === 'rv') bboxStart('rv');     /* zdejmij tryb rysowania */
    rvFixClose();
    rvActive = false;
    document.getElementById('reviewer').classList.remove('open');
    document.getElementById('rv-full').src = '';
    ['rv-col-prev', 'rv-col-best', 'rv-col-next'].forEach(function(id) {
      Array.prototype.slice.call(document.getElementById(id).querySelectorAll('img'))
        .forEach(function(n) { n.remove(); });
    });
    document.getElementById('rv-bonus').innerHTML = '';
    fetchPlates(false);      /* tabela pokazuje świeże oceny */
    fetchDays();
  };
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

  /* ── RADAR: lista zdarzeń z pendrive'a ── */
  window.radarOpen = function() {
    document.getElementById('radar-modal').style.display = 'flex';
    var c = document.getElementById('rd-dbg'); if (c) c.checked = localStorage.getItem('lpr_radar_debug') === '1';
    radarRefresh(true);
  };
  window.radarClose = function() { document.getElementById('radar-modal').style.display = 'none'; };
  window.radarDebug = function(on) { localStorage.setItem('lpr_radar_debug', on ? '1' : '0'); radarRefresh(true); };
  function radarRefresh(force) {
    fetch('/api/radar' + rdQuery()).then(function(r) { return r.json(); }).then(radarRender)
      .catch(function(e) { document.getElementById('radar-body').innerHTML = '<div style="color:var(--red)">b&#322;&#261;d: ' + e + '</div>'; });
  }
  function radarRender(d) {
    var dbg = localStorage.getItem('lpr_radar_debug') === '1';
    var h = rdFiltrHtml(d.razem != null ? (d.zdarzenia.length < d.razem ? d.zdarzenia.length + ' z ' + d.razem : d.razem) : null);
    h += (d.zdarzenia || []).map(function(e) {
      var dd = null, dbgHtml = '';
      if (dbg && e.debug) { try { dd = JSON.parse(e.debug); } catch (x) {} }
      if (dbg) {
        dbgHtml = '<div class="t" style="margin-top:4px;color:var(--yellow)">kierunek ' + (e.kierunek || '?') +
          ' &middot; pr&oacute;bek ' + e.probek + ' &middot; kamera ' + (e.v_kam != null ? Math.round(e.v_kam) + ' km/h' : 'brak') +
          ' &middot; ' + (e.pewne ? 'zgodne' : 'niepewne') + (e.uwagi ? ' &middot; ' + e.uwagi : '') + '</div>';
        if (dd) {
          dbgHtml += '<div class="t">pr&oacute;g ' + dd.prog + ' &middot; min pr&oacute;bek ' + dd.min_probek + ' &middot; okno ' + dd.okno_s +
            ' s &middot; K=' + dd.kam_K + ' tol=' + dd.kam_tol + ' &middot; trwanie ' + dd.trwanie_s + ' s' +
            (dd.v_szczyt != null ? ' &middot; szczyt ' + dd.v_szczyt + ' km/h' : '') + '</div>';
          dbgHtml += '<div class="t">kandydaci: ' + ((dd.kandydaci || []).map(function(k) {
            return (k.tablica || '?') + ' (track ' + k.tid + ', kamera ' + (k.v_kam != null ? k.v_kam : '-') + ', ocr ' + k.ocr + ', ' +
              (k.zgodny ? 'zgodny' : 'niezgodny') + ', +' + k.po_s + ' s)'; }).join('; ') || 'brak') + '</div>';
          if (dd.pojazdy_ruch) dbgHtml += '<div class="t">pojazdy (ruch, pole, klatek): ' + JSON.stringify(dd.pojazdy_ruch) + '</div>';
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
        (e.klip ? '<a href="' + e.klip + '" target="_blank">&#9654; klip</a>' : '<span class="t">bez klipu</span>') +
        dbgHtml + '</div></div>';
    }).join('') || '<div style="color:var(--dim);padding:8px">brak zdarze&#324;</div>';
    document.getElementById('radar-body').innerHTML = h;
  }
  window.rvToggleCands = function() {
    var on = localStorage.getItem('lpr_cands') !== '0';
    localStorage.setItem('lpr_cands', on ? '0' : '1');
    document.getElementById('rv-cands-toggle').textContent = 'KANDYDACI: ' + (on ? 'WYL' : 'WL');
    if (rvCurrent) { var it = rvCurrent; rvCurrent = null; rvShow(it); }
  };

  window.rvWybierzKlatke = function(url) {
    if (!rvCurrent || rvCurrent.id === undefined) return;
    var nowa = (rvLepszaUrl === url) ? '' : url;   /* ponowny klik = cofnięcie */
    var plik = nowa ? nowa.split('/').pop() : '';
    fetch('/api/verify', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: rvCurrent.id, verdict: 'lepsza', plik: plik }) })
      .then(function(r) { return r.json(); })
      .then(function(d) {
        if (!d || d.ok === false) return;
        rvLepszaUrl = nowa || null;
        rvCurrent.lepsza = plik || null;
        var it = rvCurrent; rvCurrent = null; rvShow(it);
      }).catch(function() {});
  };

  function rvMarkSel(it) {
    var map = { ok: 'rv-ok', fix: 'rv-fix', partial: 'rv-part', unreadable: 'rv-unr', notplate: 'rv-not' };
    Array.prototype.slice.call(document.querySelectorAll('.rvb')).forEach(function(b) {
      b.classList.remove('sel');
    });
    if (it.verify && map[it.verify]) {
      var b = document.querySelector('.rvb.' + map[it.verify]);
      if (b) b.classList.add('sel');
    }
  }

  function rvShow(it) {
    rvCurrent = it;
    document.getElementById('rv-wait').style.display = 'none';
    document.getElementById('rv-body').style.display = 'flex';
    document.getElementById('rv-btns').style.display = 'grid';
    var fullEl = document.getElementById('rv-full');
    fullEl.onload = null;       /* stary onload z _placeBox poprzedniego zapisu */
    fullEl.src = it.full_url || it.crop_url || '';
    rvTune('reset');
    document.getElementById('rv-plate-num').textContent = (it.plate || 'UNKNOWN').trim().toUpperCase();
    var sid = '#' + it.id + (it.seen_at ? ('   ' + it.seen_at) : '');
    if (it.ocr_conf) sid += '   OCR ' + Math.round(it.ocr_conf * 100) + '%';
    if (it.verify === 'fix' && it.vtext) sid += '   <span class="v">→ ' + it.vtext + '</span>';
    if (it.plate_v1 && it.plate_v1 !== it.plate) sid += '   <span class="v" style="color:var(--yellow)" title="odczyt starego modelu z auta; na gorze jest odczyt NOWEGO modelu — oceniasz nowy">stary model: ' + it.plate_v1 + '</span>';
    document.getElementById('rv-sid').innerHTML = sid;
    rvMarkSel(it);

    var kv = {}, legacy = [];
    (it.crops || []).forEach(function(u) {
      var m = u.match(/_(km2|km1|kp1|kp2)\.jpg$/);
      if (m) kv[m[1]] = u; else legacy.push(u);
    });
    function fillCol(colId, url) {
      var col = document.getElementById(colId);
      Array.prototype.slice.call(col.querySelectorAll('img')).forEach(function(n) { n.remove(); });
      if (!url) return;
      ['s1', 's2', 's3'].forEach(function(cls) {
        var im = document.createElement('img');
        im.src = url; im.className = cls;
        im.title = 'Kliknij: ta klatka jest lepsza (trafi do uczenia)';
        im.onerror = function() { this.remove(); };   /* brak pliku → bez pustego kafla */
        im.style.cursor = 'pointer';
        im.onclick = function() { rvWybierzKlatke(url); };
        col.appendChild(im);
      });
      if (rvLepszaUrl && url === rvLepszaUrl) col.classList.add('rv-wybrana');
      else col.classList.remove('rv-wybrana');
    }
    rvLepszaUrl = null;
    if (it.lepsza) {
      var wszystkie = [it.crop_url].concat(it.crops || []);
      for (var wi = 0; wi < wszystkie.length; wi++) {
        if (wszystkie[wi] && wszystkie[wi].split('/').pop() === it.lepsza) { rvLepszaUrl = wszystkie[wi]; break; }
      }
    }
    var showCands = localStorage.getItem('lpr_cands') !== '0';
    document.getElementById('rv-col-prev').style.display = showCands ? '' : 'none';
    document.getElementById('rv-col-next').style.display = showCands ? '' : 'none';
    fillCol('rv-col-best', it.crop_url);
    fillCol('rv-col-prev', kv.km1 || kv.km2);
    fillCol('rv-col-next', kv.kp1 || kv.kp2);
    var bonus = document.getElementById('rv-bonus');
    bonus.innerHTML = '';
    if (showCands) {
      var extras = [];
      if (kv.km2 && kv.km1) extras.push(kv.km2);
      if (kv.kp2 && kv.kp1) extras.push(kv.kp2);
      extras = extras.concat(legacy);
      extras.forEach(function(u) {
        var im = document.createElement('img');
        im.src = u; im.title = 'Kliknij: ta klatka jest lepsza (trafi do uczenia)';
        im.onerror = function() { this.remove(); };
        im.style.cursor = 'pointer';
        if (rvLepszaUrl && u === rvLepszaUrl) im.classList.add('wybrana');
        im.onclick = function() { rvWybierzKlatke(u); };
        bonus.appendChild(im);
      });
    }
    rvTuneApply();
    var rvBox = document.getElementById('rv-box');
    rvBox.style.display = 'none'; rvBox.className = '';
    /* ramka tylko na PELNEJ klatce (bbox jest wzgledem niej, nie cropu) */
    if (!it.full_url) return;
    if (it.vbbox) { rvBox.className = 'fixed'; _placeBox('rv-full', 'rv-box', it.vbbox); }
    else if (it.bbox) _placeBox('rv-full', 'rv-box', it.bbox);
  }

  function rvCount() {
    document.getElementById('rv-count').innerHTML =
      '<span style="color:var(--yellow)">zaleg&#322;e: ' + rvBacklogTotal + '</span>' +
      (rvDone ? '<span style="color:var(--dim);font-size:12px"> &middot; ocenione teraz: ' +
                rvDone + '</span>' : '');
  }

  function rvFetchBacklog(cb) {
    if (rvBacklogBusy) {       /* trwa juz pobieranie — oddzwonimy po nim */
      if (cb) rvBacklogCb = cb;
      return;
    }
    rvBacklogBusy = true;
    /* pominiete zapisy sa w bazie dalej nieocenione — bez wykluczenia serwer
       zwracalby wciaz te same 40 najnowszych i edytor „slepnie" */
    var ex = Object.keys(rvSkipped).slice(-400).join(',');
    var done = function() {
      rvBacklogBusy = false;
      var c = cb || rvBacklogCb; rvBacklogCb = null;
      if (c) c();
      /* prefetch bez callbacku wrocil, a ekran czeka — pokaz nastepny */
      else if (rvActive && !rvCurrent && rvBacklog.length) { rvCount(); rvShow(rvBacklog[0]); }
    };
    fetch('/api/unreviewed?limit=40' + fltQs() + (ex ? '&exclude=' + ex : ''))
      .then(function(r) { return r.json(); })
      .then(function(d) {
        rvBacklogTotal = d.total || 0;
        rvBacklog = (d.items || []).filter(function(it) { return !rvSeen[it.id]; });
        done();
      })
      .catch(done);
  }

  window.rvRefresh = function() {
    if (!rvActive) return;
    rvBacklog = rvBacklog.filter(function(it) { return !rvSeen[it.id]; });
    if (!rvBacklog.length) {
      rvFetchBacklog(function() {
        if (!rvActive) return;
        if (rvBacklog.length) { rvCount(); if (!rvCurrent || rvSeen[rvCurrent.id]) rvShow(rvBacklog[0]); return; }
        rvCount();
        rvCurrent = null;
        document.getElementById('rv-wait').style.display = 'flex';
        document.getElementById('rv-body').style.display = 'none';
        document.getElementById('rv-btns').style.display = 'none';
        document.getElementById('rv-wait-sub').textContent = flt.day || flt.q
          ? 'brak nieocenionych zapisow w tym filtrze (' + (flt.day || 'wszystkie dni') + (flt.q ? ', „' + flt.q + '”' : '') + ')'
          : 'brak nieocenionych zapisow w calej bazie';
      });
      document.getElementById('rv-count').textContent = 'Szukam zaleglosci...';
      return;
    }
    rvCount();
    if (rvCurrent && !rvSeen[rvCurrent.id]) return;
    rvShow(rvBacklog[0]);
  };

  function rvNext(it) {
    rvCurrent = null;
    rvBacklog = rvBacklog.filter(function(x) { return !rvSeen[x.id]; });
    if (rvBacklog.length) {
      rvCount();
      rvShow(rvBacklog[0]);
      if (rvBacklog.length <= 3) rvFetchBacklog();   /* dociągnij następne */
      return;
    }
    rvRefresh();
  }

  /* POPRAW: pole w miejscu tablicy (Enter = zapis, Esc = anuluj) zamiast
     okienka prompt — szybciej i widac zdjecie podczas wpisywania */
  var fixEl = document.getElementById('rv-fix');
  function rvFixOpen() {
    var it = rvCurrent;
    fixEl.value = (it.vtext || it.plate || '').replace(/UNKNOWN/i, '');
    document.getElementById('rv-meta').classList.add('fixing');
    fixEl.focus(); fixEl.select();
  }
  function rvFixClose() {
    document.getElementById('rv-meta').classList.remove('fixing');
    fixEl.blur();
  }
  fixEl.addEventListener('keydown', function(e) {
    e.stopPropagation();
    if (e.key === 'Escape') { rvFixClose(); return; }
    if (e.key === 'Enter') {
      var t = fixEl.value.toUpperCase().replace(/[^A-Z0-9]/g, '');
      if (t.length < 2) return;
      rvFixClose();
      rvVerdict('fix', t);
    }
  });
  fixEl.addEventListener('blur', function() { setTimeout(rvFixClose, 150); });
  fixEl.addEventListener('input', function() {
    var p = fixEl.selectionStart;
    fixEl.value = fixEl.value.toUpperCase().replace(/[^A-Z0-9]/g, '');
    try { fixEl.setSelectionRange(p, p); } catch (e) {}
  });

  var rvDone = 0;             /* ocenione w tej sesji */

  window.rvVerdict = function(verdict, text) {
    if (!rvCurrent || bbx.mode) return;
    var it = rvCurrent;
    var payload = { id: it.id, verdict: verdict };
    if (verdict === 'fix') {
      if (!text) { rvFixOpen(); return; }
      payload.text = text;
    }
    rvFixClose();
    rvHistory.push(it);
    if (rvHistory.length > 40) rvHistory.shift();
    rvSeen[it.id] = 1;
    it.verify = verdict;
    it.vtext = payload.text || null;
    rvDone += 1;
    rvBacklogTotal = Math.max(0, rvBacklogTotal - 1);
    fetch('/api/verify', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload) })
      .then(function(r) { return r.json(); })
      .then(function(d) { if (!d.ok) alert(d.error || 'Blad zapisu'); })
      .catch(function() { alert('Blad polaczenia z baza'); });
    var rt = rowTd[it.id];
    if (rt) { rt.item.verify = it.verify; rt.item.vtext = it.vtext; renderVerify(rt.td, rt.item); }
    rvNext(it);
  };

  window.rvSkip = function() {          /* → pomiń bez oceny */
    if (!rvCurrent || bbx.mode) return;
    var it = rvCurrent;
    rvFixClose();
    rvHistory.push(it);
    if (rvHistory.length > 40) rvHistory.shift();
    rvSeen[it.id] = 1;
    if (!it.verify) rvSkipped[it.id] = 1;
    rvNext(it);
  };

  window.rvPrev = function() {
    if (!rvActive || !rvHistory.length) return;
    rvFixClose();
    var it = rvHistory.pop();
    delete rvSeen[it.id]; delete rvSkipped[it.id];
    if (it.verify) { rvBacklogTotal += 1; rvDone = Math.max(0, rvDone - 1); }
    rvCount();
    rvShow(it);
  };

  /* suwaki oddaja focus po zmianie — inaczej strzalki ruszalyby suwak
     zamiast cofac/pomijac, a 1-5 by nie dzialalo */
  Array.prototype.slice.call(document.querySelectorAll('#rv-tune input[type=range]'))
    .forEach(function(el) { el.addEventListener('change', function() { el.blur(); }); });

  document.addEventListener('keydown', function(e) {
    var tag = (e.target && e.target.tagName) || '';
    if (tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA') return;
    if (e.repeat || e.ctrlKey || e.altKey || e.metaKey) return;   /* przytrzymany klawisz = jedna ocena */
    if (rvActive) {
      if (e.key === 'ArrowLeft') { rvPrev(); return; }
      if (e.key === 'ArrowRight') { rvSkip(); return; }
      if (e.key === 'Escape') { if (bbx.mode) bboxStart(bbx.mode); else reviewerClose(); return; }
      var keys = { '1': 'ok', '2': 'fix', '3': 'partial', '4': 'unreadable', '5': 'notplate' };
      /* preventDefault: klawisz „2" otwiera pole POPRAW i nie moze do niego wpasc */
      if (keys[e.key]) { e.preventDefault(); rvVerdict(keys[e.key]); return; }
      var k = e.key.toLowerCase();
      if (k === 'r') { bboxStart('rv'); return; }
      if (k === 'n') { rvTune('neg'); return; }
      if (k === 'l') { rvLupaToggle(); return; }
      if (e.key === '0') { rvTune('reset'); return; }
      if (e.key === '+' || e.key === '=') { rvTune('ev+'); return; }
      if (e.key === '-') { rvTune('ev-'); return; }
      return;
    }
    if (e.key === 'Escape') { if (bbx.mode) bboxStart(bbx.mode); else closeLightbox(null, true); }
  });
  window.addEventListener('resize', function() {
    if (rvCurrent) {
      var b = rvCurrent.vbbox || rvCurrent.bbox;
      if (b) _placeBox('rv-full', 'rv-box', b);
    }
  });

  /* start */
  fetchDays();
  fetchPlates(true);
})();
</script>
</body>
</html>
"""


# ── Okno / start ─────────────────────────────────────────────────────────────

def _find_browser() -> "str | None":
    """Edge lub Chrome — do trybu aplikacji (--app). Edge jest na każdym
    Windows 10/11, więc nie potrzebujemy WebView2 ani pywebview."""
    import winreg
    cands = []
    for key in (r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe",
                r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"):
        for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(root, key) as k:
                    cands.append(winreg.QueryValue(k, None))
            except OSError:
                pass
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    la = os.environ.get("LOCALAPPDATA", "")
    cands += [
        pf86 + r"\Microsoft\Edge\Application\msedge.exe",
        pf + r"\Microsoft\Edge\Application\msedge.exe",
        pf + r"\Google\Chrome\Application\chrome.exe",
        pf86 + r"\Google\Chrome\Application\chrome.exe",
        la + r"\Google\Chrome\Application\chrome.exe",
    ]
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return None


def main() -> None:
    log(f"=== start v{VERSION}  exe={sys.executable}  argv={sys.argv[1:]}")
    if DATA_DIR is None:
        log(f"[BŁĄD] brak lpr/plates.db obok aplikacji ({_base_dir()})")
        _msgbox("Nie znalazłem danych (lpr\\plates.db) obok aplikacji.\n\n"
                f"Szukałem w: {_base_dir()}\n"
                "Umieść aplikację w głównym katalogu pendrive z malinki\n"
                "(tam, gdzie leży katalog lpr).")
        sys.exit(1)
    log(f"dane: {DATA_DIR}")
    try:
        _ensure_schema()
        st = api_stats()
        log(f"baza: {st['total']} zapisów, {st['reviewed']} ocen, "
            f"{st['unreviewed']} do oceny")
    except Exception as e:
        log("[BŁĄD] baza: " + repr(e) + "\n" + traceback.format_exc())
        _msgbox("Nie mogę otworzyć bazy plates.db:\n" + str(e) +
                f"\n\nSzczegóły w pliku:\n{LOG_PATH}")
        sys.exit(1)

    class Serwer(ThreadingHTTPServer):
        # bez SO_REUSEADDR: na Windows domyślne ustawienie pozwala związać
        # port, który już zajmuje inna kopia programu — dwa serwery na jednym
        # porcie i okno gada z cudzą bazą. Ma się nie udać i wziąć następny.
        allow_reuse_address = False
        daemon_threads = True

    srv = None
    port = 8123
    for port in range(8123, 8143):
        try:
            srv = Serwer(("127.0.0.1", port), Handler)
            break
        except OSError:
            continue
    if srv is None:
        log("[BŁĄD] brak wolnego portu 8123-8142")
        _msgbox("Brak wolnego portu (8123-8142). Zamknij inne kopie programu.")
        sys.exit(1)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}/"
    log(f"serwer: {url}")

    proc = None
    browser = None if "--browser" in sys.argv else _find_browser()
    if "--no-window" in sys.argv:          # testy: sam serwer, bez okna
        browser = None
    if browser:
        prof = Path(os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP")
                    or str(_base_dir())) / "LPR_Przegladarka_profil"
        try:
            prof.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        args = [browser, f"--app={url}", f"--user-data-dir={prof}",
                "--window-size=1280,880", "--no-first-run",
                "--no-default-browser-check", "--disable-sync",
                "--disable-features=msEdgeWelcome,msHubApps"]
        try:
            proc = subprocess.Popen(args)
            log(f"okno: {browser}")
        except Exception as e:
            log("[okno] nie uruchomiłem przeglądarki w trybie aplikacji: "
                + repr(e))
            proc = None
    if proc is None and "--no-window" not in sys.argv:
        log("okno: domyślna przeglądarka")
        webbrowser.open(url)

    # Koniec pracy: okno zamknięte (proces przeglądarki zakończony) i strona
    # przestała pingować. Sam proces nie wystarczy — Edge potrafi oddać okno
    # już działającej instancji i od razu wyjść. Strona zgłasza też zamknięcie
    # wprost (/api/bye z pagehide) — wtedy nie czekamy na wygaśnięcie pingów.
    # Bez procesu (zwykła przeglądarka) próg jest długi, bo schowana karta
    # dławi pingi do 1/min, a to nie znaczy, że użytkownik skończył.
    start = time.monotonic()
    try:
        while True:
            time.sleep(1.0)
            now = time.monotonic()
            pinged = _last_ping > 0
            cicho = (now - _last_ping) if pinged else (now - start)
            if _bye_at and _last_ping < _bye_at and now - _bye_at > 4:
                break                      # zamknięte i nikt nie wrócił (reload pingowałby)
            if proc is not None and proc.poll() is not None:
                if (pinged and cicho > 6) or (not pinged and cicho > 30):
                    break
            elif proc is None:
                if (pinged and cicho > 120) or (not pinged and cicho > 600):
                    break
    except KeyboardInterrupt:
        pass
    log("koniec")
    srv.shutdown()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        log("[BŁĄD] " + repr(e) + "\n" + traceback.format_exc())
        _msgbox("Program napotkał błąd:\n" + str(e) +
                f"\n\nSzczegóły w pliku:\n{LOG_PATH}")
        sys.exit(1)
