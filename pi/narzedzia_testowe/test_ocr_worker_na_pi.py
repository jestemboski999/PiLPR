#!/usr/bin/env python3
"""Rzeczywisty OcrWorker na oryginalnych JPEG-ach, bez importu GUI/kamery."""
import argparse
import ast
import json
import logging
import os
from pathlib import Path
import queue as _queue
import re
import sys
import threading
import time
import zipfile

import cv2
import numpy as np
from fast_plate_ocr import LicensePlateRecognizer as _LPR

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("images", type=Path)
    parser.add_argument("--source", type=Path, default=ROOT / "lpr.py")
    parser.add_argument("--backend", choices=("cpu", "hailo"), required=True)
    parser.add_argument("--hef", type=Path)
    parser.add_argument("--json", type=Path, required=True)
    args = parser.parse_args()
    nodes = ast.parse(args.source.read_text(encoding="utf-8")).body
    keep = [n for n in nodes if (isinstance(n, ast.ClassDef) and n.name == "OcrWorker")
            or (isinstance(n, ast.FunctionDef) and n.name in ("_is_valid_plate", "_fix_pl_plate"))]
    scope = dict(globals())
    scope.update(_diag_ref={}, OCR_BACKEND=args.backend, PLATE_MIN_LEN=4, PLATE_MAX_LEN=9,
                 logger=logging.getLogger("ocr-test"), _ustap_procesor=lambda _n: None,
                 OCR_MODEL=str(ROOT / "local_resources/ocr/cct_s_v2_pl.onnx"),
                 OCR_PLATE_CONFIG=str(ROOT / "local_resources/ocr/cct_s_v2_pl_plate_config.yaml"))
    if args.hef:
        import lpr_hailo_ocr
        real_process = lpr_hailo_ocr.HailoOcrProcess
        lpr_hailo_ocr.HailoOcrProcess = lambda: real_process(hef_path=args.hef)
    exec(compile(ast.Module(body=keep, type_ignores=[]), str(args.source), "exec"), scope)
    worker = scope["OcrWorker"]()
    if worker.backend != args.backend:
        worker.stop()
        raise RuntimeError("Test nie może używać CPU fallback zamiast żądanego Hailo")
    rows, times = [], []
    try:
        with zipfile.ZipFile(args.images) as images:
            manifest = json.loads(images.read("manifest.json"))
            for index, row in enumerate(manifest):
                crop = cv2.imdecode(np.frombuffer(images.read(row["file"]), np.uint8), cv2.IMREAD_COLOR)
                started = time.monotonic()
                if not worker.submit(index, 7, crop, index):
                    raise RuntimeError("Nieoczekiwana pełna kolejka OCR")
                tid, gen, text, conf, seq, returned_crop = worker.result_queue.get(timeout=10)
                elapsed = (time.monotonic() - started) * 1000
                if (tid, gen, seq) != (index, 7, index) or not np.array_equal(returned_crop, crop):
                    raise RuntimeError("Błąd korelacji kolejki/wycinka OCR")
                rows.append({"file": row["file"], "truth": row["truth"], "text": text,
                             "confidence": conf, "ms": elapsed, "height": crop.shape[0]})
                times.append(elapsed)
                if (index + 1) % 50 == 0:
                    print(index + 1, "cropów, mediana", round(float(np.median(times)), 1), "ms", flush=True)
        if worker.backend != args.backend:
            raise RuntimeError("Backend zmienił się podczas testu")
    finally:
        worker.stop()
    report = {"backend": args.backend, "samples": len(rows),
              "correct": sum(r["truth"] == r["text"] for r in rows),
              "latency_median_ms": float(np.median(times)),
              "latency_p95_ms": float(np.percentile(times, 95)), "predictions": rows}
    print(json.dumps({k: v for k, v in report.items() if k != "predictions"}), flush=True)
    args.json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
