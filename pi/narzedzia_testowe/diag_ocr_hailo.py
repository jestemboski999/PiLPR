#!/usr/bin/env python3
"""Diagnostyka formatu wejścia fizycznego HEF."""
from pathlib import Path
import sys
import json
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lpr_hailo_ocr import DEFAULT_DIR
from hailo_platform import HEF

hef = HEF(str(DEFAULT_DIR / "pl_lpr_rec.hef"))
for info in hef.get_input_vstream_infos() + hef.get_output_vstream_infos():
    q = info.quant_info
    print(info.name, info.shape, "quant", q.qp_scale, q.qp_zp,
          "range", q.limvals_min, q.limvals_max, flush=True)
print("Raw predictions:")
report = json.loads((Path(__file__).resolve().parents[1] / "ocr_test_20261006/wynik_round.json").read_text())
for pred in report["predictions"][:15]:
    print(pred)
