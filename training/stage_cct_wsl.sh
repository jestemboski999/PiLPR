#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 || $# -gt 3 ]]; then
  echo "usage: $0 EXPERIMENT_NAME [SOURCE_ROOT] [STAGE_ROOT]" >&2
  exit 2
fi

EXPERIMENT_NAME="$1"
SOURCE_ROOT="${2:-$HOME/lpr_trening}"
STAGE_ROOT="${3:-$HOME/lpr-cct-data}"
SOURCE_EXPERIMENT="$SOURCE_ROOT/experiments/$EXPERIMENT_NAME"
STAGE_EXPERIMENT="$STAGE_ROOT/experiments/$EXPERIMENT_NAME"

for required in \
  "$SOURCE_ROOT/ocr_dataset/crops_rtx" \
  "$SOURCE_ROOT/ocr_dataset/crops_user" \
  "$SOURCE_EXPERIMENT/images/train" \
  "$SOURCE_EXPERIMENT/images/validation" \
  "$SOURCE_EXPERIMENT/train_cct_weighted_wsl.csv" \
  "$SOURCE_EXPERIMENT/validation_wsl.csv"; do
  if [[ ! -e "$required" ]]; then
    echo "missing source: $required" >&2
    exit 1
  fi
done

mkdir -p "$STAGE_ROOT" "$STAGE_EXPERIMENT"

tar -C "$SOURCE_ROOT" -cf - ocr_dataset/crops_rtx ocr_dataset/crops_user \
  | tar -C "$STAGE_ROOT" -xf -
tar -C "$SOURCE_EXPERIMENT" -cf - \
  images/train \
  images/validation \
  train_cct_weighted_wsl.csv \
  validation_wsl.csv \
  | tar -C "$STAGE_EXPERIMENT" -xf -

STAGE_EXPERIMENT="$STAGE_EXPERIMENT" python3 - <<'PY'
import csv
import hashlib
import json
import os
from pathlib import Path

experiment = Path(os.environ["STAGE_EXPERIMENT"])
result = {}
for name in ("train_cct_weighted_wsl.csv", "validation_wsl.csv"):
    csv_path = experiment / name
    missing = []
    rows = 0
    with csv_path.open(newline="", encoding="utf-8-sig") as source:
        for row in csv.DictReader(source):
            rows += 1
            image_path = (experiment / row["image_path"]).resolve()
            if not image_path.is_file() or image_path.stat().st_size == 0:
                missing.append(str(image_path))
    result[name] = {
        "rows": rows,
        "missing": len(missing),
        "sha256": hashlib.sha256(csv_path.read_bytes()).hexdigest(),
    }
    if missing:
        raise SystemExit(f"{name}: {len(missing)} missing images")

print(json.dumps({"stage_experiment": str(experiment), "csv": result}, indent=2))
PY
