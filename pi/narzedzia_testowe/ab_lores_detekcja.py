# -*- coding: utf-8 -*-
"""A/B: czy detektor (lpr_yolov8n_v2.hef, 640x640, NMS na chipie) znajduje
tablice tak samo dobrze, gdy potok dostaje 1280x720 skalowane w ISP zamiast
pelnych 2304x1296? Symulacja: A = dzisiejsza sciezka (2304 -> 640 letterbox
INTER_AREA, jak hailocropper), B = 2304 -> 1280x720 (symulacja skalera ISP)
-> 640 letterbox INTER_AREA. Warianty pomocnicze: B_lin (ISP jako
INTER_LINEAR = pesymistycznie), C = 1920x1080. Prawda: ramki tablic z bazy
potwierdzone przez czlowieka (is_correct 1/0)."""
import sqlite3, sys, time, collections
import numpy as np, cv2
from hailo_platform import (HEF, VDevice, ConfigureParams, HailoStreamInterface,
                            InferVStreams, InputVStreamParams, OutputVStreamParams,
                            FormatType)

HEF_PATH = "/usr/local/hailo/resources/models/hailo10h/lpr_yolov8n_v2.hef"
DATA = "/media/lpr/lpr"
PLATE_CLS = 4
PROG = 0.53          # PLATE_MIN_CONF w lpr.py
LIMIT = int(sys.argv[1]) if len(sys.argv) > 1 else 100000

con = sqlite3.connect(f"file:{DATA}/plates.db?mode=ro", uri=True)
rows = con.execute("""
    SELECT s.seen_at, s.full_image, COALESCE(a.correct_bbox, s.bbox)
    FROM sightings s JOIN annotations a ON a.sighting_id = s.id
    WHERE a.is_correct IN (1, 0) AND s.full_image IS NOT NULL
      AND COALESCE(a.correct_bbox, s.bbox) IS NOT NULL
    ORDER BY s.id DESC LIMIT ?""", (LIMIT,)).fetchall()

def letterbox(img, size=640, color=114):
    h, w = img.shape[:2]
    s = size / max(h, w)
    nw, nh = int(round(w * s)), int(round(h * s))
    r = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA)
    out = np.full((size, size, 3), color, np.uint8)
    y0, x0 = (size - nh) // 2, (size - nw) // 2
    out[y0:y0 + nh, x0:x0 + nw] = r
    return out, (x0, y0, nw, nh)

def warianty(full):
    a, g = letterbox(full)
    b, _ = letterbox(cv2.resize(full, (1280, 720), interpolation=cv2.INTER_AREA))
    bl, _ = letterbox(cv2.resize(full, (1280, 720), interpolation=cv2.INTER_LINEAR))
    c, _ = letterbox(cv2.resize(full, (1920, 1080), interpolation=cv2.INTER_AREA))
    return {"A_2304": a, "B_1280_area": b, "B_1280_lin": bl, "C_1920": c}, g

def iou(p, q):
    ix1, iy1 = max(p[0], q[0]), max(p[1], q[1])
    ix2, iy2 = min(p[2], q[2]), min(p[3], q[3])
    inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
    ua = (p[2]-p[0])*(p[3]-p[1]) + (q[2]-q[0])*(q[3]-q[1]) - inter
    return inter / ua if ua > 0 else 0.0

# HailoRT 5.x / Hailo-10H: stare API VStreams zwraca HAILO_NOT_IMPLEMENTED —
# uzywamy InferModel (async API). Wyjscie NMS-by-class to surowy bufor float32:
# dla kazdej klasy [liczba] + liczba*5 x (ymin, xmin, ymax, xmax, score).
N_CLS = 5
with VDevice() as dev:
    im = dev.create_infer_model(HEF_PATH)
    im.input().set_format_type(FormatType.UINT8)
    im.output().set_format_type(FormatType.FLOAT32)
    in_name = im.input().name
    print("wejscie", in_name, im.input().shape, "wyjscie", im.output().name, im.output().shape, flush=True)
    def parse_nms(buf):
        buf = np.asarray(buf, dtype=np.float32).reshape(-1)
        out, off = [], 0
        for _ in range(N_CLS):
            n = int(buf[off]) if off < buf.size else 0
            off += 1
            out.append(buf[off:off + 5 * n].reshape(n, 5) if n > 0 else np.zeros((0, 5), np.float32))
            off += 5 * n
        return out
    class _Pipe:
        def __init__(self, configured):
            self.c = configured
            self.b = configured.create_bindings()
            # NMS by class: 5 x (1 + 100*5) = 2505 float32 = 10020 B (parse-hef)
            n = int(np.prod(im.output().shape)) if im.output().shape else 0
            self.out = np.zeros(max(n, 2505), dtype=np.float32)
            self.b.output().set_buffer(self.out)
        def infer(self, d):
            img = np.ascontiguousarray(list(d.values())[0][0])
            self.b.input().set_buffer(img)
            self.c.run([self.b], 5000)
            return {out_name: [parse_nms(self.out.copy())]}
    out_name = im.output().name
    configured_cm = im.configure()
    stat = {}
    def st(k):
        return stat.setdefault(k, {"n": 0, "hit53": 0, "hit30": 0, "score": [], "fp": 0,
                                   "bins": collections.defaultdict(lambda: [0, 0])})
    t0 = time.time(); n_img = 0
    with configured_cm as configured:
            pipe = _Pipe(configured)
            for seen, fname, bstr in rows:
                p = f"{DATA}/{seen[:10]}/{fname}"
                full = cv2.imread(p)
                if full is None:
                    continue
                full = cv2.cvtColor(full, cv2.COLOR_BGR2RGB)   # HEF chce RGB
                try:
                    gt = [float(v) for v in bstr.split(";")]
                    assert len(gt) == 4
                except Exception:
                    continue
                H, W = full.shape[:2]
                gt_px = (gt[3] - gt[1]) * H
                bin_ = "<30" if gt_px < 30 else "30-45" if gt_px < 45 else "45-70" if gt_px < 70 else ">70"
                war, (x0, y0, nw, nh) = warianty(full)
                n_img += 1
                for k, img in war.items():
                    res = pipe.infer({in_name: np.expand_dims(img, 0)})[out_name][0]
                    dets = res[PLATE_CLS] if len(res) > PLATE_CLS else np.zeros((0, 5))
                    s = st(k); s["n"] += 1; s["bins"][bin_][0] += 1
                    best, best_sc = 0.0, 0.0
                    for d in np.asarray(dets).reshape(-1, 5):
                        ymin, xmin, ymax, xmax, sc = [float(v) for v in d]
                        # z ukladu 640x640 letterbox do znormalizowanego obrazu
                        box = ((xmin * 640 - x0) / nw, (ymin * 640 - y0) / nh,
                               (xmax * 640 - x0) / nw, (ymax * 640 - y0) / nh)
                        i = iou(box, gt)
                        if i >= 0.4:
                            if sc > best_sc:
                                best, best_sc = i, sc
                        elif i < 0.05 and sc >= PROG:
                            s["fp"] += 1
                    if best_sc >= 0.30:
                        s["hit30"] += 1
                    if best_sc >= PROG:
                        s["hit53"] += 1; s["score"].append(best_sc); s["bins"][bin_][1] += 1
                if n_img % 100 == 0:
                    print(f"  {n_img} klatek, {time.time()-t0:.0f} s", flush=True)
    print(f"\nKLATEK: {n_img}  czas {time.time()-t0:.0f} s")
    print(f"{'wariant':14s} {'recall@0.53':>11s} {'recall@0.30':>11s} {'sr.score':>9s} {'FP/klatka':>9s}   "
          + "  ".join(f"{b:>7s}" for b in ("<30", "30-45", "45-70", ">70")))
    for k in ("A_2304", "B_1280_area", "B_1280_lin", "C_1920"):
        s = stat.get(k)
        if not s or not s["n"]:
            continue
        bins = "  ".join(f"{s['bins'][b][1]}/{s['bins'][b][0]:<3d}" for b in ("<30", "30-45", "45-70", ">70"))
        print(f"{k:14s} {s['hit53']/s['n']:11.3f} {s['hit30']/s['n']:11.3f} "
              f"{(np.mean(s['score']) if s['score'] else 0):9.3f} {s['fp']/s['n']:9.2f}   {bins}")
