#!/usr/bin/env python3
"""Export LPRNet v3 to ONNX and build representative Hailo calibration data."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
import random

import cv2
import numpy as np
import onnxruntime as ort
import torch

from train_lprnet_v3 import IMAGE_HEIGHT, IMAGE_WIDTH, LPRNet


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_paths(csv_path: Path, new_root: Path) -> tuple[list[Path], list[Path]]:
    legacy: list[Path] = []
    new: list[Path] = []
    seen: set[str] = set()
    with csv_path.open(newline="", encoding="utf-8-sig") as source:
        for row in csv.DictReader(source):
            path = Path(row["image_path"])
            if not path.is_absolute():
                path = (csv_path.parent / path).resolve()
            key = str(path).lower()
            if key in seen:
                continue
            seen.add(key)
            if not path.is_file():
                raise FileNotFoundError(path)
            if path.resolve().is_relative_to(new_root):
                new.append(path)
            else:
                legacy.append(path)
    return legacy, new


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--weights", required=True, type=Path)
    parser.add_argument("--train-csv", required=True, type=Path)
    parser.add_argument("--new-domain-root", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--calibration-samples", type=int, default=1024)
    parser.add_argument("--new-domain-fraction", type=float, default=0.75)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()

    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    weights = args.weights.resolve()
    new_root = args.new_domain_root.resolve()
    legacy_paths, new_paths = load_paths(args.train_csv.resolve(), new_root)

    model = LPRNet()
    model.load_state_dict(torch.load(weights, map_location="cpu"))
    model.eval()
    dummy = torch.zeros(1, 3, IMAGE_HEIGHT, IMAGE_WIDTH)
    onnx_path = output / "lprnet_v3.onnx"
    torch.onnx.export(
        model,
        dummy,
        onnx_path,
        opset_version=13,
        input_names=["input"],
        output_names=["logits"],
        do_constant_folding=True,
        dynamo=False,
    )

    random_generator = random.Random(args.seed)
    random_generator.shuffle(legacy_paths)
    random_generator.shuffle(new_paths)
    wanted_new = round(args.calibration_samples * args.new_domain_fraction)
    selected_new = new_paths[:wanted_new]
    remaining = args.calibration_samples - len(selected_new)
    selected_legacy = legacy_paths[:remaining]
    if len(selected_new) + len(selected_legacy) < args.calibration_samples:
        used = {str(path).lower() for path in [*selected_new, *selected_legacy]}
        reserve = [
            path
            for path in [*new_paths, *legacy_paths]
            if str(path).lower() not in used
        ]
        selected_legacy.extend(
            reserve[
                : args.calibration_samples
                - len(selected_new)
                - len(selected_legacy)
            ]
        )
    selected = [*selected_new, *selected_legacy]
    if len(selected) < args.calibration_samples:
        raise ValueError(
            f"only {len(selected)} unique calibration images are available"
        )

    calibration_images: list[np.ndarray] = []
    for path in selected:
        image = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError(f"cannot decode calibration image: {path}")
        image = cv2.resize(image, (IMAGE_WIDTH, IMAGE_HEIGHT))
        calibration_images.append(image.astype(np.float32))
    calibration = np.stack(calibration_images)
    calibration_path = output / "lprnet_v3_calibration.npy"
    np.save(calibration_path, calibration)

    session = ort.InferenceSession(
        str(onnx_path), providers=["CPUExecutionProvider"]
    )
    sample = (calibration[0] / 255.0).transpose(2, 0, 1)[None].astype(
        np.float32
    )
    onnx_output = session.run(None, {"input": sample})[0]
    with torch.no_grad():
        torch_output = model(torch.from_numpy(sample)).numpy()
    maximum_error = float(np.abs(onnx_output - torch_output).max())
    if maximum_error > 1.0e-4:
        raise RuntimeError(f"ONNX parity check failed: {maximum_error}")

    report = {
        "weights": str(weights),
        "weights_sha256": sha256(weights),
        "onnx": str(onnx_path),
        "onnx_sha256": sha256(onnx_path),
        "calibration": str(calibration_path),
        "calibration_sha256": sha256(calibration_path),
        "calibration_shape": list(calibration.shape),
        "new_domain_calibration_samples": len(selected_new),
        "legacy_calibration_samples": len(selected) - len(selected_new),
        "onnx_max_absolute_error": maximum_error,
        "input": "BGR float32 NCHW 0..1 for ONNX",
        "hailo_input": "BGR float32 NHWC 0..255; normalization is in .alls",
    }
    (output / "export_report.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
