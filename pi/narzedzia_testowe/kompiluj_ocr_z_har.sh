#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
export CUDA_VISIBLE_DEVICES=-1
export XDG_CACHE_HOME="$PWD/ocr_recompile_20261006/cache"
mkdir -p ocr_recompile_20261006/cache
source "${HAILO_DFC_VENV:-$HOME/.venvs/hailo-dfc}/bin/activate"
python - <<'PY'
import os
from hailo_sdk_client import ClientRunner
har = 'local_resources/ocr/hailo_20261006/pl_lpr_rec_quant.har'
runner = ClientRunner(har=har)
print('HAR loaded:', runner.get_hn()['name'], flush=True)
hef = runner.compile()
with open('ocr_recompile_20261006/pl_lpr_rec_recompiled.hef', 'wb') as stream:
    stream.write(hef)
runner.save_har('ocr_recompile_20261006/pl_lpr_rec_compiled.har')
print('COMPILED', len(hef), 'bytes', flush=True)
PY
