# -*- coding: utf-8 -*-
"""Detekcje modelu LPR (lpr_yolov8n_v2.hef, 5 klas, NMS na chipie) na dowolnych
zdjęciach — do materiałów/diagnostyki. Wypisuje JSON: {plik: [[klasa, score,
x1, y1, x2, y2 (znormalizowane do pełnego zdjęcia)], …]}.
Użycie (na Pi): python3 detekcja_na_zdjeciu.py wynik.json zdj1.jpg zdj2.jpg …
Dzieli Hailo z działającą usługą lpr (grupa SHARED); gdy to się nie uda —
kod wyjścia 2 (wtedy zatrzymać usługę na czas uruchomienia)."""
import sys, json
import numpy as np, cv2
from hailo_platform import VDevice, FormatType

HEF_PATH = "/usr/local/hailo/resources/models/hailo10h/lpr_yolov8n_v2.hef"
KLASY = ["car", "bus", "truck", "motorcycle", "license_plate"]
N_CLS = 5
out_path, pliki = sys.argv[1], sys.argv[2:]


def letterbox(img, size=640, color=114):
    h, w = img.shape[:2]
    s = size / max(h, w)
    nw, nh = int(round(w * s)), int(round(h * s))
    r = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA)
    out = np.full((size, size, 3), color, np.uint8)
    y0, x0 = (size - nh) // 2, (size - nw) // 2
    out[y0:y0 + nh, x0:x0 + nw] = r
    return out, (x0, y0, nw, nh)


def parse_nms(buf):
    buf = np.asarray(buf, dtype=np.float32).reshape(-1)
    out, off = [], 0
    for _ in range(N_CLS):
        n = int(buf[off]) if off < buf.size else 0
        off += 1
        out.append(buf[off:off + 5 * n].reshape(n, 5) if n > 0 else np.zeros((0, 5), np.float32))
        off += 5 * n
    return out


try:
    params = VDevice.create_params()
    params.group_id = "SHARED"
    dev = VDevice(params)
except Exception as e:
    print("VDevice SHARED:", e, file=sys.stderr)
    sys.exit(2)
wynik = {}
with dev:
    im = dev.create_infer_model(HEF_PATH)
    im.input().set_format_type(FormatType.UINT8)
    im.output().set_format_type(FormatType.FLOAT32)
    n = int(np.prod(im.output().shape)) if im.output().shape else 0
    with im.configure() as configured:
        b = configured.create_bindings()
        outbuf = np.zeros(max(n, 2505), dtype=np.float32)
        b.output().set_buffer(outbuf)
        for p in pliki:
            full = cv2.imread(p)
            if full is None:
                print("brak", p, file=sys.stderr)
                continue
            rgb = cv2.cvtColor(full, cv2.COLOR_BGR2RGB)
            lb, (x0, y0, nw, nh) = letterbox(rgb)
            b.input().set_buffer(np.ascontiguousarray(lb))
            configured.run([b], 5000)
            dets = []
            for cls, arr in enumerate(parse_nms(outbuf.copy())):
                for ymin, xmin, ymax, xmax, score in arr:
                    # współrzędne NMS są znormalizowane do 640x640 letterboxa
                    X1 = (xmin * 640 - x0) / nw; X2 = (xmax * 640 - x0) / nw
                    Y1 = (ymin * 640 - y0) / nh; Y2 = (ymax * 640 - y0) / nh
                    dets.append([KLASY[cls], round(float(score), 3),
                                 round(float(max(0, X1)), 4), round(float(max(0, Y1)), 4),
                                 round(float(min(1, X2)), 4), round(float(min(1, Y2)), 4)])
            wynik[p.split("/")[-1]] = dets
            print(p.split("/")[-1], len(dets), "detekcji", flush=True)
json.dump(wynik, open(out_path, "w"), ensure_ascii=False, indent=1)
print("zapisano", out_path)
