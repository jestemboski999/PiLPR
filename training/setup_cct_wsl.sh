#!/usr/bin/env bash
set -euo pipefail

VENV_PATH="${1:-$HOME/.venvs/lpr-cct}"

python3 -m venv "$VENV_PATH"
source "$VENV_PATH/bin/activate"
python -m pip install --upgrade pip setuptools wheel
python -m pip install \
  "tensorflow[and-cuda]==2.21.0" \
  "fast-plate-ocr[train,onnx]==1.1.0"

# TensorFlow's loader searches its own package directory. In this WSL setup
# pip keeps the CUDA shared objects in sibling nvidia/*/lib directories.
TF_PACKAGE_DIR="$(python -c 'import pathlib, tensorflow; print(pathlib.Path(tensorflow.__file__).parent)')"
find "$VENV_PATH/lib" -path '*/site-packages/nvidia/*/lib/*.so*' -type f \
  -exec ln -sf {} "$TF_PACKAGE_DIR/" \;
NVIDIA_LIB_DIRS="$(
  find "$VENV_PATH/lib" -path '*/site-packages/nvidia/*/lib' -type d \
    | sort \
    | paste -sd: -
)"
export LD_LIBRARY_PATH="${NVIDIA_LIB_DIRS}${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

python - <<'PY'
import json
import tensorflow as tf
from importlib.metadata import version

devices = [device.name for device in tf.config.list_physical_devices("GPU")]
result = {
    "tensorflow": tf.__version__,
    "fast_plate_ocr": version("fast-plate-ocr"),
    "gpu_devices": devices,
}
print(json.dumps(result, indent=2))
if not devices:
    raise SystemExit("TensorFlow did not detect the RTX GPU")
PY

python -m pip freeze > "$VENV_PATH/requirements.freeze.txt"
