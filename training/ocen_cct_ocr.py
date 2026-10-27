#!/usr/bin/env python3
"""Evaluate OCR models on one explicit immutable split."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
import time

import cv2
import numpy as np
import onnxruntime as ort

from fast_plate_ocr import LicensePlateRecognizer


LPRNET_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def levenshtein(left: str, right: str) -> int:
    previous = list(range(len(right) + 1))
    for left_index, left_char in enumerate(left, start=1):
        current = [left_index]
        for right_index, right_char in enumerate(right, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[right_index] + 1,
                    previous[right_index - 1] + (left_char != right_char),
                )
            )
        previous = current
    return previous[-1]


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as source:
        return list(csv.DictReader(source))


def parse_bool(value: str | bool | None) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes"}


def subset_metrics(rows: list[dict[str, object]]) -> dict[str, object]:
    if not rows:
        return {"samples": 0}
    exact = sum(bool(row["correct"]) for row in rows)
    reference_chars = sum(len(str(row["ground_truth"])) for row in rows)
    edit_distance = sum(int(row["edit_distance"]) for row in rows)
    similarities = [
        1.0
        - int(row["edit_distance"])
        / max(1, len(str(row["ground_truth"])), len(str(row["prediction"])))
        for row in rows
    ]
    return {
        "samples": len(rows),
        "exact_matches": exact,
        "exact_accuracy": exact / len(rows),
        "character_accuracy": max(
            0.0, 1.0 - edit_distance / max(1, reference_chars)
        ),
        "mean_edit_similarity": sum(similarities) / len(similarities),
        "length_accuracy": sum(
            len(str(row["ground_truth"])) == len(str(row["prediction"]))
            for row in rows
        )
        / len(rows),
    }


def exact_mcnemar_p(wins: int, losses: int) -> float:
    discordant = wins + losses
    if discordant == 0:
        return 1.0
    tail = min(wins, losses)
    probability = sum(
        math.comb(discordant, value) for value in range(tail + 1)
    ) / (2**discordant)
    return min(1.0, 2.0 * probability)


def bootstrap_delta_ci(
    baseline: np.ndarray, candidate: np.ndarray, iterations: int = 20_000
) -> list[float]:
    random = np.random.default_rng(42)
    deltas = np.empty(iterations, dtype=np.float64)
    sample_count = len(baseline)
    for start in range(0, iterations, 500):
        count = min(500, iterations - start)
        indices = random.integers(0, sample_count, size=(count, sample_count))
        deltas[start : start + count] = (
            candidate[indices].mean(axis=1) - baseline[indices].mean(axis=1)
        )
    low, high = np.quantile(deltas, [0.025, 0.975])
    return [float(low), float(high)]


def decode_lprnet(logits: np.ndarray) -> tuple[list[str], list[float]]:
    if logits.ndim != 4 or logits.shape[1] != len(LPRNET_CHARS) + 1:
        raise ValueError(f"unexpected LPRNet output shape: {logits.shape}")
    raw = logits[:, :, 0, :].transpose(0, 2, 1)
    shifted = raw - raw.max(axis=2, keepdims=True)
    probabilities = np.exp(shifted)
    probabilities /= probabilities.sum(axis=2, keepdims=True)
    indices = raw.argmax(axis=2)
    texts: list[str] = []
    confidences: list[float] = []
    for row, row_probabilities in zip(indices, probabilities, strict=True):
        characters: list[str] = []
        selected_probabilities: list[float] = []
        previous = 0
        for position, value in enumerate(row):
            if value != 0 and value != previous:
                characters.append(LPRNET_CHARS[value - 1])
                selected_probabilities.append(
                    float(row_probabilities[position, value])
                )
            previous = value
        texts.append("".join(characters))
        confidences.append(
            float(np.mean(selected_probabilities))
            if selected_probabilities
            else 0.0
        )
    return texts, confidences


def run_cct(
    model_path: Path,
    config_path: Path,
    samples: list[dict[str, object]],
    batch_size: int,
) -> tuple[list[dict[str, object]], float, list[str]]:
    recognizer = LicensePlateRecognizer(
        device="cpu",
        onnx_model_path=model_path,
        plate_config_path=config_path,
    )
    model_predictions: list[dict[str, object]] = []
    started = time.perf_counter()
    for batch_start in range(0, len(samples), batch_size):
        batch = samples[batch_start : batch_start + batch_size]
        outputs = recognizer.run(
            [str(row["image_path"]) for row in batch],
            return_confidence=True,
        )
        for sample, output in zip(batch, outputs, strict=True):
            prediction = output.plate
            char_probs = output.char_probs
            mean_confidence = (
                float(np.mean(char_probs))
                if char_probs is not None and len(char_probs)
                else 0.0
            )
            model_predictions.append(
                {
                    **sample,
                    "prediction": prediction,
                    "correct": prediction == sample["ground_truth"],
                    "edit_distance": levenshtein(
                        str(sample["ground_truth"]), prediction
                    ),
                    "mean_char_confidence": mean_confidence,
                }
            )
    return (
        model_predictions,
        time.perf_counter() - started,
        recognizer.model.get_providers(),
    )


def run_lprnet(
    model_path: Path,
    samples: list[dict[str, object]],
    batch_size: int,
) -> tuple[list[dict[str, object]], float, list[str]]:
    session = ort.InferenceSession(
        str(model_path), providers=["CPUExecutionProvider"]
    )
    input_definition = session.get_inputs()[0]
    input_name = input_definition.name
    effective_batch_size = (
        1 if input_definition.shape[0] == 1 else batch_size
    )
    model_predictions: list[dict[str, object]] = []
    started = time.perf_counter()
    for batch_start in range(0, len(samples), effective_batch_size):
        batch = samples[batch_start : batch_start + effective_batch_size]
        images: list[np.ndarray] = []
        for sample in batch:
            image = cv2.imread(str(sample["image_path"]), cv2.IMREAD_COLOR)
            if image is None:
                raise ValueError(f"cannot decode {sample['image_path']}")
            image = cv2.resize(image, (96, 32))
            images.append(
                (image.astype(np.float32) / 255.0).transpose(2, 0, 1)
            )
        logits = session.run(None, {input_name: np.stack(images)})[0]
        decoded, confidence_values = decode_lprnet(logits)
        for sample, prediction, mean_confidence in zip(
            batch, decoded, confidence_values, strict=True
        ):
            model_predictions.append(
                {
                    **sample,
                    "prediction": prediction,
                    "correct": prediction == sample["ground_truth"],
                    "edit_distance": levenshtein(
                        str(sample["ground_truth"]), prediction
                    ),
                    "mean_char_confidence": mean_confidence,
                }
            )
    return (
        model_predictions,
        time.perf_counter() - started,
        session.get_providers(),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--test-csv", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument(
        "--partition",
        choices=("validation", "test"),
        default="test",
    )
    parser.add_argument(
        "--model",
        required=True,
        action="append",
        help="Model specification: name|model.onnx|plate_config.yaml",
    )
    parser.add_argument(
        "--lprnet-model",
        action="append",
        default=[],
        help="LPRNet specification: name|model.onnx",
    )
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--batch-size", type=int, default=64)
    args = parser.parse_args()

    test_rows = read_csv(args.test_csv.resolve())
    manifest_rows = read_csv(args.manifest.resolve())
    manifest_by_path = {
        str(Path(row["frozen_path"]).resolve()): row
        for row in manifest_rows
        if row["partition"] == args.partition
    }
    if not test_rows:
        raise ValueError("test CSV is empty")

    samples: list[dict[str, object]] = []
    for row in test_rows:
        image_path = Path(row["image_path"]).resolve()
        if not image_path.is_file():
            raise FileNotFoundError(image_path)
        metadata = manifest_by_path.get(str(image_path))
        if metadata is None:
            raise ValueError(
                f"{args.partition} image is absent from manifest: {image_path}"
            )
        samples.append(
            {
                "sighting_id": int(metadata["sighting_id"]),
                "seen_at": metadata["seen_at"],
                "verdict": int(metadata["verdict"]),
                "unseen_plate_in_train": parse_bool(
                    metadata["unseen_plate_in_train"]
                ),
                "image_path": str(image_path),
                "ground_truth": row["plate_text"].strip().upper(),
            }
        )

    model_specs: list[tuple[str, str, Path, Path | None]] = []
    model_names: set[str] = set()
    for value in args.model:
        fields = value.split("|")
        if len(fields) != 3:
            raise ValueError(f"invalid --model: {value!r}")
        name, model_value, config_value = fields
        if not name or name in model_names:
            raise ValueError(f"empty or duplicate model name: {name!r}")
        model_names.add(name)
        model_specs.append(
            (
                name,
                "cct",
                Path(model_value).resolve(),
                Path(config_value).resolve(),
            )
        )
    for value in args.lprnet_model:
        fields = value.split("|")
        if len(fields) != 2:
            raise ValueError(f"invalid --lprnet-model: {value!r}")
        name, model_value = fields
        if not name or name in model_names:
            raise ValueError(f"empty or duplicate model name: {name!r}")
        model_names.add(name)
        model_specs.append((name, "lprnet", Path(model_value).resolve(), None))

    args.output.mkdir(parents=True, exist_ok=True)
    metrics: dict[str, object] = {
        "partition": args.partition,
        "test_csv": str(args.test_csv.resolve()),
        "test_csv_sha256": sha256(args.test_csv.resolve()),
        "manifest": str(args.manifest.resolve()),
        "manifest_sha256": sha256(args.manifest.resolve()),
        "models": {},
        "paired_comparisons": {},
    }
    predictions_by_model: dict[str, list[dict[str, object]]] = {}

    for name, model_kind, model_path, config_path in model_specs:
        if not model_path.is_file():
            raise FileNotFoundError(model_path)
        if model_kind == "cct":
            if config_path is None or not config_path.is_file():
                raise FileNotFoundError(config_path)
            model_predictions, elapsed, providers = run_cct(
                model_path, config_path, samples, args.batch_size
            )
        else:
            model_predictions, elapsed, providers = run_lprnet(
                model_path, samples, args.batch_size
            )
        predictions_by_model[name] = model_predictions

        corrected = [row for row in model_predictions if row["verdict"] == 0]
        confirmed = [row for row in model_predictions if row["verdict"] == 1]
        unseen = [
            row for row in model_predictions if row["unseen_plate_in_train"]
        ]
        metrics["models"][name] = {
            "model_kind": model_kind,
            "onnx_path": str(model_path),
            "onnx_sha256": sha256(model_path),
            "config_path": str(config_path) if config_path else None,
            "config_sha256": sha256(config_path) if config_path else None,
            "providers": providers,
            "elapsed_seconds": elapsed,
            "milliseconds_per_image": elapsed * 1000 / len(samples),
            "all": subset_metrics(model_predictions),
            "human_corrected": subset_metrics(corrected),
            "human_confirmed": subset_metrics(confirmed),
            "unseen_plate_identity": subset_metrics(unseen),
        }

    baseline_name = model_specs[0][0]
    baseline_rows = predictions_by_model[baseline_name]
    baseline_correct = np.array(
        [bool(row["correct"]) for row in baseline_rows], dtype=np.float64
    )
    for candidate_name, _, _, _ in model_specs[1:]:
        candidate_rows = predictions_by_model[candidate_name]
        candidate_correct = np.array(
            [bool(row["correct"]) for row in candidate_rows], dtype=np.float64
        )
        wins = int(np.sum((baseline_correct == 0) & (candidate_correct == 1)))
        losses = int(np.sum((baseline_correct == 1) & (candidate_correct == 0)))
        metrics["paired_comparisons"][f"{candidate_name}_vs_{baseline_name}"] = {
            "baseline": baseline_name,
            "candidate": candidate_name,
            "accuracy_delta": float(candidate_correct.mean() - baseline_correct.mean()),
            "accuracy_delta_95pct_bootstrap_ci": bootstrap_delta_ci(
                baseline_correct, candidate_correct
            ),
            "candidate_wins": wins,
            "candidate_losses": losses,
            "both_correct": int(np.sum((baseline_correct == 1) & (candidate_correct == 1))),
            "both_wrong": int(np.sum((baseline_correct == 0) & (candidate_correct == 0))),
            "mcnemar_exact_p": exact_mcnemar_p(wins, losses),
        }

    prediction_fields = [
        "sighting_id",
        "seen_at",
        "verdict",
        "unseen_plate_in_train",
        "image_path",
        "ground_truth",
    ]
    combined_rows: list[dict[str, object]] = []
    for index, sample in enumerate(samples):
        combined: dict[str, object] = {
            field: sample[field] for field in prediction_fields
        }
        for name, _, _, _ in model_specs:
            prediction = predictions_by_model[name][index]
            combined[f"{name}_prediction"] = prediction["prediction"]
            combined[f"{name}_correct"] = prediction["correct"]
            combined[f"{name}_edit_distance"] = prediction["edit_distance"]
            combined[f"{name}_mean_char_confidence"] = prediction[
                "mean_char_confidence"
            ]
        combined_rows.append(combined)

    result_fields = list(combined_rows[0])
    with (args.output / "predictions.csv").open(
        "w", newline="", encoding="utf-8"
    ) as destination:
        writer = csv.DictWriter(destination, fieldnames=result_fields)
        writer.writeheader()
        writer.writerows(combined_rows)
    (args.output / "metrics.json").write_text(
        json.dumps(metrics, indent=2), encoding="utf-8"
    )
    print(json.dumps(metrics, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
