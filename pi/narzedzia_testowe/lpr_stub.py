# -*- coding: utf-8 -*-
"""
Lokalny stub serwera LPR — serwuje _HTML_PAGE wyciete z lpr.py + sztuczne
dane API, zeby zweryfikowac layout (telefon/desktop) bez malinki.
Uruchomienie: python lpr_stub.py  ->  http://localhost:8765
"""
import json
import os
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2
import numpy as np

LPR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lpr.py")

src = open(LPR, encoding="utf-8").read()
m = re.search(r'_HTML_PAGE = r"""(.*?)"""', src, re.S)
if not m:
    raise SystemExit("nie znalazlem _HTML_PAGE")
HTML = m.group(1).encode("utf-8")

PLATES = ["WA1234A", "KX8821C", "PX55XYZ", "UNKNOWN", "GX331TK", "WX70000",
          "SX4451E", "DX9AB13", "LX8402F", "UNKNOWN", "WXX5512", "EX2C447"]
BBOX = [0.42, 0.55, 0.58, 0.62]
T0 = time.time()


def full_jpg(txt):
    img = np.full((1080, 1920, 3), 38, np.uint8)
    cv2.rectangle(img, (0, 700), (1920, 1080), (55, 55, 55), -1)   # droga
    cv2.rectangle(img, (700, 380), (1250, 720), (90, 80, 70), -1)  # "auto"
    x1, y1 = int(BBOX[0] * 1920), int(BBOX[1] * 1080)
    x2, y2 = int(BBOX[2] * 1920), int(BBOX[3] * 1080)
    cv2.rectangle(img, (x1, y1), (x2, y2), (235, 235, 235), -1)
    cv2.putText(img, txt, (x1 + 14, y2 - 20), cv2.FONT_HERSHEY_SIMPLEX,
                1.6, (20, 20, 20), 4)
    return cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])[1].tobytes()


def crop_jpg(txt, blur=0):
    img = np.full((60, 260, 3), 235, np.uint8)
    cv2.rectangle(img, (0, 0), (259, 59), (120, 120, 120), 2)
    cv2.putText(img, txt, (12, 43), cv2.FONT_HERSHEY_SIMPLEX, 1.1, (20, 20, 20), 3)
    if blur:
        img = cv2.GaussianBlur(img, (2 * blur + 1, 2 * blur + 1), 0)
    return cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])[1].tobytes()


def rows():
    n = len(PLATES) + int((time.time() - T0) // 9)   # co 9 s "nowe rozpoznanie"
    out = []
    for i in range(n):
        pid = 900 + i
        plate = PLATES[i % len(PLATES)]
        day = "2026-07-12"
        base = f"/api/crop/{day}/{plate}_{pid}"
        out.append({
            "id": pid, "plate": plate,
            "seen_at": f"{day} 14:{(10 + i) % 60:02d}:{(7 * i) % 60:02d}",
            "crop_url": base + "_crop.jpg", "full_url": base + "_full.jpg",
            "crops": [base + s + ".jpg" for s in ("_kp2", "_kp1", "_km1", "_km2")],
            "det_conf": 0.93 - 0.02 * (i % 5),
            "ocr_conf": None if plate == "UNKNOWN" else 0.97 - 0.03 * (i % 6),
            "ocr_reads": (i % 7) + 1, "duration": 1.5 + 0.4 * (i % 6),
            "bbox": BBOX, "verify": "ok" if i % 5 == 0 else None,
            "vtext": None, "vbbox": None,
        })
    out.sort(key=lambda r: -r["id"])   # najnowsze pierwsze, jak na malince
    return out


class H(BaseHTTPRequestHandler):
    def _send(self, body, ctype="application/json", code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/":
            return self._send(HTML, "text/html; charset=utf-8")
        if p == "/api/plates":
            return self._send(json.dumps(rows()).encode())
        if p == "/api/stats":
            return self._send(json.dumps({
                "today_plates": 37, "total_plates": 1284,
                "free_disk_mb": 23400.0, "fps": 29.7, "cpu": 41.0,
                "cpu_temp": 55.0, "hailo_temp": 49.0}).encode())
        if p == "/api/config":
            return self._send(json.dumps({
                "plate_min_conf": 0.5, "ocr_min_conf": 0.74,
                "ocr_min_matching": 3}).encode())
        if p == "/api/zone":
            return self._send(json.dumps({"ok": True, "zone": None}).encode())
        if p == "/api/camera":
            import os as _os
            if _os.environ.get("STUB_CSI"):   # test panelu trybu CSI
                return self._send(json.dumps(
                    {"ok": True, "csi": True,
                     "settings": {"ae_mode": "short", "ev": 0.5,
                                  "metering": "centre", "manual": False,
                                  "exposure_us": 4000, "gain": 4.0},
                     "meta": {"exposure_us": 4750, "gain": 2.3,
                              "lux": 312.5}}).encode())
            return self._send(json.dumps({"ok": True, "controls": {
                "auto_exposure": {"type": "menu", "value": 1,
                                  "options": {"1": "Manual Mode",
                                              "3": "Aperture Priority Mode"}},
                "exposure_time_absolute": {"type": "int", "value": 40,
                                           "min": 1, "max": 5000, "step": 1},
                "gain": {"type": "int", "value": 12, "min": 0, "max": 100, "step": 1},
                "brightness": {"type": "int", "value": 0, "min": -64,
                               "max": 64, "step": 1},
                "white_balance_automatic": {"type": "bool", "value": 1},
            }}).encode())
        if p == "/api/bluetooth":
            return self._send(json.dumps({
                "powered": True, "scanning": False,
                "default": "AA:BB:CC:DD:EE:01",
                "devices": [
                    {"mac": "AA:BB:CC:DD:EE:01", "name": "JBL Charge 5",
                     "icon": "speaker", "paired": True, "connected": True,
                     "trusted": True, "battery": 78},
                    {"mac": "AA:BB:CC:DD:EE:02", "name": "Volkswagen UHV",
                     "icon": "car", "paired": True, "connected": False,
                     "trusted": True, "battery": None},
                    {"mac": "AA:BB:CC:DD:EE:03", "name": "WH-1000XM4",
                     "icon": "headphones", "paired": True, "connected": False,
                     "trusted": True, "battery": None},
                    {"mac": "AA:BB:CC:DD:EE:09", "name": "Soundcore Mini",
                     "icon": "speaker", "paired": False, "connected": False,
                     "trusted": False, "battery": None},
                ]}).encode())
        if p == "/api/watchlist":
            alert = "Uwaga! Nieoznakowany radiowóz. Tablica {tablica}."
            wpisy = [("WX273AA", "Skoda Octavia · Mazowieckie"),
                     ("SX2A000", "Kia Ceed · Śląskie"),
                     ("BX416AA", "Toyota Corolla · Podlaskie"),
                     ("DX5875X", "radiowoz · Dolnośląskie")]
            return self._send(json.dumps({
                "ok": True, "tts_all": False, "tts_brand": True,
                "spell_mode": "letters", "tts_gain": 1.0,
                "spell_example": "wu a, jeden dwa trzy cztery pięć",
                "groups": [{"gid": "policja", "name": "policja",
                            "alert": alert, "enabled": True,
                            "color": "red", "count": len(wpisy)}],
                "brand_examples": [{
                    "gid": "policja", "plate": "WX273AA",
                    "note": "Skoda Octavia · Mazowieckie",
                    "text": "Uwaga! Nieoznakowany radiowóz. Tablica wu iks,"
                            " dwa siedem trzy a a."
                            " Skoda Octavia."}],
                "plates": [{"plate": t, "group": "policja", "note": n}
                           for t, n in wpisy]}).encode())
        if p == "/api/recordings":
            recs = []
            for day, hh, n in (("2026-07-16", "09", 6), ("2026-07-15", "18", 5),
                               ("2026-07-15", "07", 4), ("2026-07-12", "14", 5)):
                ses = f"{day.replace('-','')}_{hh}4146"
                for i in range(n, 0, -1):
                    recs.append({
                        "sesja": ses, "plik": f"jazda_{i:05d}.mkv",
                        "mtime": f"{day} {hh}:{40+i:02d}:00", "size_mb": 160 + i,
                        "thumb": (f"/api/rec-thumb/{ses}/jazda_{i:05d}.jpg"
                                  if i % 5 else None),
                    })
            recs.sort(key=lambda r: r["mtime"], reverse=True)
            return self._send(json.dumps(recs).encode())
        if re.match(r"^/api/rec-thumb/", p):
            img = cv2.resize(
                cv2.imdecode(np.frombuffer(full_jpg("WA1234A"), np.uint8),
                             cv2.IMREAD_COLOR), (320, 180))
            return self._send(
                cv2.imencode(".jpg", img)[1].tobytes(), "image/jpeg")
        if p in ("/stream", "/api/snapshot", "/api/focus-view"):
            return self._send(full_jpg("WA1234A"), "image/jpeg")
        mcrop = re.match(r"^/api/crop/[\d-]+/(.+)\.jpg$", p)
        if mcrop:
            name = mcrop.group(1)
            plate = name.split("_")[0]
            if name.endswith("_full"):
                return self._send(full_jpg(plate), "image/jpeg")
            blur = {"km2": 4, "km1": 2, "kp1": 1, "kp2": 3}.get(name[-3:], 0)
            return self._send(crop_jpg(plate, blur), "image/jpeg")
        return self._send(b"not found", "text/plain", 404)

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        return self._send(json.dumps({"ok": True}).encode())


if __name__ == "__main__":
    print("stub na http://localhost:8765")
    ThreadingHTTPServer(("127.0.0.1", 8765), H).serve_forever()
