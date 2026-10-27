#!/usr/bin/env python3
"""Test fizycznego HEF na dokładnie 254 obrazach z pakietu kwantyzacji.

Samodzielny proces: można uruchomić obok LPR, ponieważ używa grupy SHARED.
"""
import argparse
import json
from pathlib import Path
import sys
import time

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lpr_hailo_ocr import HailoEngine


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("package", type=Path)
    parser.add_argument("--hef", type=Path)
    parser.add_argument("--rounding", choices=("round", "floor", "dither", "none"), default="round")
    parser.add_argument("--input-format", choices=("UINT8", "FLOAT32"), default="UINT8")
    parser.add_argument("--rgb", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    raw_path = args.package / "test_X_raw.npy"
    if raw_path.exists():
        raw = np.load(raw_path, mmap_mode="r")
    else:
        with np.load(args.package / "test_X_raw.npz") as packed:
            raw = packed["data"]
    truth = (args.package / "test_gts.txt").read_text(encoding="utf-8").splitlines()
    if len(raw) != len(truth):
        raise ValueError("Liczba obrazów nie zgadza się z etykietami")
    n = min(args.limit or len(truth), len(truth))
    engine = HailoEngine(args.hef or args.package / "pl_lpr_rec.hef",
                         args.package / "pl_dict.txt", input_format=args.input_format)
    results, times = [], []
    started = time.monotonic()
    try:
        print(json.dumps(engine.info), flush=True)
        for iteration in range(args.repeat):
            correct = 0
            for index in range(n):
                image = raw[index]
                if args.rounding == "round":
                    image = np.rint(image)
                elif args.rounding == "floor":
                    image = np.floor(image)
                elif args.rounding == "dither":
                    image = image.copy()
                    checker = 127 + np.indices(image.shape[:2]).sum(axis=0) % 2
                    mask = (image == 127.5).all(axis=2)
                    image[mask] = checker[mask, None]
                if args.rgb:
                    image = image[..., ::-1]
                text, conf, ms = engine.infer_raw(image)
                correct += text == truth[index]
                times.append(ms)
                if iteration == 0:
                    results.append({"index": index, "truth": truth[index], "text": text,
                                    "confidence": conf, "ms": ms})
                if (index + 1) % 50 == 0:
                    print(f"{index + 1}/{n}, poprawne {correct}, {np.median(times):.1f} ms", flush=True)
            print(f"Przebieg {iteration + 1}: {correct}/{n} = {100 * correct / n:.1f}%", flush=True)
    finally:
        engine.close()
    report = {"physical_device_tested": True, "rounding": args.rounding,
              "input_format": args.input_format, "rgb": args.rgb,
              "samples": n, "repeat": args.repeat,
              "correct": sum(r["truth"] == r["text"] for r in results),
              "latency_median_ms": float(np.median(times)),
              "latency_p95_ms": float(np.percentile(times, 95)),
              "elapsed_sec": time.monotonic() - started,
              "streams": engine.info, "predictions": results}
    print(json.dumps({k: v for k, v in report.items() if k != "predictions"}), flush=True)
    if args.json:
        args.json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
