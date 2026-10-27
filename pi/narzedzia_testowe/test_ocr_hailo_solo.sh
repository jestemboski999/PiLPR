#!/bin/bash
set -euo pipefail
cd /home/admin/hailo-apps
export XDG_RUNTIME_DIR=/run/user/1000
trap 'systemctl --user start lpr.service' EXIT
systemctl --user stop lpr.service
timeout 45 venv_hailo_apps/bin/python -u narzedzia_testowe/test_ocr_hailo.py \
    local_resources/ocr/hailo_20261006 --json ocr_test_20261006/wynik_solo.json
