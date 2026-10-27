#!/usr/bin/env python3
"""Build a leak-free OCR train/validation/test experiment from review data."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3

import cv2


ALPHABET = set("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ")
CROP_MARGIN_W = 0.20
CROP_MARGIN_H = 0.16
CCT_NEW_DOMAIN_REPEAT = 5


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def normalize_plate(value: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def read_legacy_csv(path: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    with path.open(newline="", encoding="utf-8-sig") as source:
        for row in csv.DictReader(source):
            image_value = (row.get("image_path") or "").strip()
            text = normalize_plate(row.get("plate_text"))
            if not image_value or not (2 <= len(text) <= 10):
                continue
            image_path = Path(image_value)
            if not image_path.is_absolute():
                image_path = (path.parent / image_path).resolve()
            rows.append(
                {
                    "image_path": str(image_path),
                    "plate_text": text,
                    "plate_region": (row.get("plate_region") or "Poland").strip(),
                }
            )
    return rows


def resolve_source(roots: list[Path], day: str, filename: str | None) -> Path | None:
    if not filename:
        return None
    basename = Path(filename).name
    for root in roots:
        for candidate in (root / day / basename, root / basename):
            if candidate.is_file() and candidate.stat().st_size > 0:
                return candidate
    return None


def parse_bbox(value: str) -> tuple[float, float, float, float]:
    fields = [float(item) for item in value.split(";")]
    if len(fields) != 4:
        raise ValueError(f"invalid bbox: {value!r}")
    x1, y1, x2, y2 = fields
    if not (0 <= x1 < x2 <= 1 and 0 <= y1 < y2 <= 1):
        raise ValueError(f"invalid normalized bbox: {value!r}")
    return x1, y1, x2, y2


def crop_manual_bbox(source: Path, bbox_value: str, destination: Path) -> None:
    image = cv2.imread(str(source), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError(f"cannot decode image: {source}")
    height, width = image.shape[:2]
    x1, y1, x2, y2 = parse_bbox(bbox_value)
    box_width = x2 - x1
    box_height = y2 - y1
    px1 = max(0, int((x1 - box_width * CROP_MARGIN_W) * width))
    py1 = max(0, int((y1 - box_height * CROP_MARGIN_H) * height))
    px2 = min(width, int((x2 + box_width * CROP_MARGIN_W) * width))
    py2 = min(height, int((y2 + box_height * CROP_MARGIN_H) * height))
    if py2 - py1 < 30:
        center_y = (py1 + py2) // 2
        py1 = max(0, center_y - 15)
        py2 = min(height, center_y + 15)
    if px2 <= px1 or py2 <= py1:
        raise ValueError(f"empty crop for {source}: {bbox_value!r}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(destination), image[py1:py2, px1:px2]):
        raise OSError(f"cannot save crop: {destination}")


def write_csv(path: Path, rows: list[dict[str, object]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as destination:
        writer = csv.DictWriter(destination, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_wsl_annotations(
    path: Path, rows: list[dict[str, object]], fields: list[str]
) -> None:
    translated = [
        {
            **row,
            "image_path": Path(
                os.path.relpath(Path(str(row["image_path"])), path.parent)
            ).as_posix(),
        }
        for row in rows
    ]
    write_csv(path, translated, fields)


def partition_for_day(
    day: str, train_through: str, validation_day: str, test_day: str
) -> str | None:
    if day <= train_through:
        return "train"
    if day == validation_day:
        return "validation"
    if day == test_day:
        return "test"
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--image-root", required=True, action="append", type=Path)
    parser.add_argument("--legacy-train", required=True, type=Path)
    parser.add_argument("--legacy-val", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--train-through", default="2026-07-17")
    parser.add_argument("--validation-day", default="2026-07-22")
    parser.add_argument("--test-day", default="2026-07-24")
    parser.add_argument("--allow-missing-non-test", action="store_true")
    args = parser.parse_args()

    database = args.database.resolve()
    image_roots = [root.resolve() for root in args.image_root]
    output = args.output.resolve()
    frozen_root = output / "images"
    output.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise RuntimeError(f"database integrity check failed: {integrity}")

    # `lepsza_klatka` (25.09.2026): w edytorze człowiek może wskazać klatkę
    # SĄSIEDNIĄ (km1/km2/kp1/kp2) jako lepszą od zapisanej — np. gdy zapisana
    # jest zasłonięta albo ucięta. Wtedy do uczenia idzie TA klatka, a zapisany
    # crop jest POMIJANY (człowiek właśnie powiedział, że jest gorszy).
    # Starsze bazy nie mają tej kolumny — wtedy pusta.
    kolumny = {r[1] for r in connection.execute("PRAGMA table_info(annotations)")}
    lepsza_sql = "a.lepsza_klatka" if "lepsza_klatka" in kolumny else "NULL AS lepsza_klatka"
    query = f"""
        SELECT a.sighting_id, a.is_correct, a.correct_text, a.reviewed_at,
               a.correct_bbox, s.plate_text, s.seen_at, s.crop_image,
               s.full_image, s.extra_crops, {lepsza_sql}
        FROM annotations a
        JOIN sightings s ON s.id = a.sighting_id
        WHERE a.is_correct IN (0, 1)
        ORDER BY s.seen_at, a.sighting_id
    """
    annotations = list(connection.execute(query))
    connection.close()

    legacy_rows = [
        *read_legacy_csv(args.legacy_train.resolve()),
        *read_legacy_csv(args.legacy_val.resolve()),
    ]
    candidate_train: list[dict[str, object]] = []
    seen_legacy_paths: dict[str, str] = {}
    legacy_conflicts: list[dict[str, str]] = []
    missing: list[dict[str, object]] = []
    for row in legacy_rows:
        image_path = Path(str(row["image_path"]))
        key = os.path.normcase(str(image_path))
        previous = seen_legacy_paths.get(key)
        if previous and previous != row["plate_text"]:
            legacy_conflicts.append(
                {
                    "image_path": str(image_path),
                    "first_text": previous,
                    "second_text": str(row["plate_text"]),
                }
            )
            continue
        if previous:
            continue
        seen_legacy_paths[key] = str(row["plate_text"])
        if not image_path.is_file():
            missing.append(
                {
                    "kind": "legacy",
                    "partition": "train",
                    "image_path": str(image_path),
                    "plate_text": row["plate_text"],
                }
            )
            continue
        candidate_train.append({**row, "origin": "legacy"})

    new_train: list[dict[str, object]] = []
    validation: list[dict[str, object]] = []
    test: list[dict[str, object]] = []
    manifest: list[dict[str, object]] = []

    for row in annotations:
        day = str(row["seen_at"])[:10]
        partition = partition_for_day(
            day, args.train_through, args.validation_day, args.test_day
        )
        if partition is None:
            continue
        ground_truth = normalize_plate(
            row["plate_text"] if row["is_correct"] == 1 else row["correct_text"]
        )
        if not (2 <= len(ground_truth) <= 10) or not set(ground_truth) <= ALPHABET:
            missing.append(
                {
                    "kind": "invalid_label",
                    "partition": partition,
                    "sighting_id": row["sighting_id"],
                    "plate_text": ground_truth,
                }
            )
            continue

        variants: list[tuple[str, str | None, bool]] = []
        lepsza = (row["lepsza_klatka"] or "").strip() if "lepsza_klatka" in row.keys() else ""
        extras = [
            value.strip()
            for value in (row["extra_crops"] or "").split(";")
            if value.strip()
        ]
        if row["correct_bbox"]:
            variants.append(("manual_bbox", row["full_image"], True))
        elif lepsza and lepsza != row["crop_image"]:
            # człowiek wskazał lepszą klatkę: ona jest główna, zapisany crop
            # wypada; pozostali sąsiedzi zostają jako dodatkowe (tylko train)
            variants.append(("lepsza", lepsza, False))
            if partition == "train":
                variants.extend(
                    (f"extra_{index}", filename, False)
                    for index, filename in enumerate(
                        [f for f in extras if f != lepsza], start=1)
                )
        else:
            variants.append(("primary", row["crop_image"], False))
            if partition == "train":
                variants.extend(
                    (f"extra_{index}", filename, False)
                    for index, filename in enumerate(extras, start=1)
                )

        for variant, filename, needs_crop in variants:
            source = resolve_source(image_roots, day, filename)
            if source is None:
                missing.append(
                    {
                        "kind": "new",
                        "partition": partition,
                        "sighting_id": row["sighting_id"],
                        "day": day,
                        "variant": variant,
                        "filename": filename,
                    }
                )
                continue

            suffix = ".jpg"
            frozen_name = f"s{row['sighting_id']}_{variant}{suffix}"
            destination = frozen_root / partition / frozen_name
            destination.parent.mkdir(parents=True, exist_ok=True)
            if needs_crop:
                crop_manual_bbox(source, str(row["correct_bbox"]), destination)
            else:
                shutil.copy2(source, destination)

            sample = {
                "image_path": str(destination),
                "plate_text": ground_truth,
                "plate_region": "Poland",
            }
            if partition == "train":
                new_train.append({**sample, "origin": "new_imx708"})
            elif partition == "validation":
                validation.append(sample)
            else:
                test.append(sample)

            manifest.append(
                {
                    "sighting_id": row["sighting_id"],
                    "partition": partition,
                    "variant": variant,
                    "seen_at": row["seen_at"],
                    "reviewed_at": row["reviewed_at"],
                    "verdict": row["is_correct"],
                    "plate_text": ground_truth,
                    "source_path": str(source),
                    "frozen_path": str(destination),
                    "sha256": sha256(destination),
                }
            )

    if missing:
        write_csv(
            output / "missing_files.csv",
            missing,
            sorted({key for row in missing for key in row}) or ["kind"],
        )
    if legacy_conflicts:
        write_csv(
            output / "legacy_conflicts.csv",
            legacy_conflicts,
            ["image_path", "first_text", "second_text"],
        )
    missing_test = [
        row for row in missing if row.get("partition") == "test"
    ]
    unsafe_missing = [
        row for row in missing if row.get("kind") != "new"
    ]
    must_abort = bool(legacy_conflicts) or bool(missing_test) or bool(
        unsafe_missing
    )
    must_abort = must_abort or (
        bool(missing) and not args.allow_missing_non_test
    )
    if must_abort:
        report = {
            "state": "incomplete",
            "database": str(database),
            "database_sha256": sha256(database),
            "missing_count": len(missing),
            "missing_test_count": len(missing_test),
            "legacy_conflict_count": len(legacy_conflicts),
        }
        (output / "prepare_report.json").write_text(
            json.dumps(report, indent=2), encoding="utf-8"
        )
        raise RuntimeError(
            f"experiment is incomplete: {len(missing)} missing, "
            f"{len(legacy_conflicts)} conflicts"
        )

    candidate_train.extend(new_train)
    train_labels = {str(row["plate_text"]) for row in candidate_train}
    for row in manifest:
        row["unseen_plate_in_train"] = (
            row["partition"] == "test" and row["plate_text"] not in train_labels
        )

    annotation_fields = ["image_path", "plate_text", "plate_region"]
    cct_weighted_train = [
        *candidate_train,
        *(new_train * (CCT_NEW_DOMAIN_REPEAT - 1)),
    ]
    write_csv(output / "train.csv", candidate_train, annotation_fields)
    write_csv(
        output / "train_cct_weighted.csv",
        cct_weighted_train,
        annotation_fields,
    )
    write_csv(output / "validation.csv", validation, annotation_fields)
    write_csv(output / "test.csv", test, annotation_fields)
    write_wsl_annotations(
        output / "train_wsl.csv", candidate_train, annotation_fields
    )
    write_wsl_annotations(
        output / "train_cct_weighted_wsl.csv",
        cct_weighted_train,
        annotation_fields,
    )
    write_wsl_annotations(
        output / "validation_wsl.csv", validation, annotation_fields
    )
    write_wsl_annotations(output / "test_wsl.csv", test, annotation_fields)
    write_csv(
        output / "split_manifest.csv",
        manifest,
        [
            "sighting_id",
            "partition",
            "variant",
            "seen_at",
            "reviewed_at",
            "verdict",
            "plate_text",
            "source_path",
            "frozen_path",
            "sha256",
            "unseen_plate_in_train",
        ],
    )

    report = {
        "state": (
            "complete_with_missing_non_test" if missing else "complete"
        ),
        "database": str(database),
        "database_sha256": sha256(database),
        "image_roots": [str(root) for root in image_roots],
        "split_policy": {
            "train_through": args.train_through,
            "validation_day": args.validation_day,
            "test_day": args.test_day,
        },
        "legacy_train_samples": len(candidate_train) - len(new_train),
        "new_train_samples": len(new_train),
        "candidate_train_samples": len(candidate_train),
        "cct_new_domain_repeat": CCT_NEW_DOMAIN_REPEAT,
        "cct_weighted_train_samples": len(cct_weighted_train),
        "validation_samples": len(validation),
        "test_samples": len(test),
        "test_unseen_plate_samples": sum(
            bool(row["unseen_plate_in_train"]) for row in manifest
        ),
        "missing_non_test_samples": len(missing),
        "manifest_sha256": sha256(output / "split_manifest.csv"),
        "wsl_csv_sha256": {
            name: sha256(output / name)
            for name in (
                "train_wsl.csv",
                "train_cct_weighted_wsl.csv",
                "validation_wsl.csv",
                "test_wsl.csv",
            )
        },
    }
    (output / "prepare_report.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
