#!/usr/bin/env bash
# Przenosny przebieg dla istniejacego srodowiska Hailo DFC (Linux/WSL).
# Nie instaluje pakietow, nie zmienia HOME, nie laczy sie z Raspberry Pi.
set -euo pipefail
if [ "$#" != 3 ]; then
    echo "Uzycie: bash kwantyzuj_wariant.sh PAKIET_ZRODLOWY KONFIG_ALLS NOWY_KATALOG_WYNIKU" >&2
    exit 2
fi
PACKAGE="$(realpath -- "$1")"
CONFIG="$(realpath -- "$2")"
OUT="$(realpath -m -- "$3")"
test -f "$PACKAGE/pl_lpr_rec.har"
test -f "$PACKAGE/calib_raw.npy"
test -f "$CONFIG"
command -v hailo >/dev/null
python -c 'import hailo_sdk_client, numpy; print("SDK gotowe")'
if [ -e "$OUT" ]; then
    echo "Katalog wyniku juz istnieje; wybierz nowa nazwe: $OUT" >&2
    exit 2
fi
mkdir -p "$(dirname "$OUT")"
mkdir "$OUT"
cp -- "$CONFIG" "$OUT/norm.alls"
export XDG_CACHE_HOME="$OUT/cache"
export MPLCONFIGDIR="$OUT/mpl"
export TMPDIR="$OUT/tmp"
mkdir -p "$XDG_CACHE_HOME" "$MPLCONFIGDIR" "$TMPDIR"
cd "$OUT"
python -m pip show hailo-dataflow-compiler > environment.txt
sha256sum "$PACKAGE/pl_lpr_rec.har" "$PACKAGE/calib_raw.npy" "$CONFIG" > source_SHA256SUMS.txt
hailo optimize "$PACKAGE/pl_lpr_rec.har" --hw-arch hailo10h \
    --calib-set-path "$PACKAGE/calib_raw.npy" --model-script "$OUT/norm.alls" \
    --output-har-path "$OUT/pl_lpr_rec_quant.har" 2>&1 | tee optimize.log
python "$PACKAGE/test_hef2.py" "$OUT/pl_lpr_rec_quant.har" --fp 2>&1 | tee emulacja.log
hailo compiler "$OUT/pl_lpr_rec_quant.har" --hw-arch hailo10h \
    --output-dir "$OUT" 2>&1 | tee compile.log
sha256sum "$OUT"/*.har "$OUT"/*.hef > SHA256SUMS.txt
echo "Gotowe: $OUT. Wynik emulacji nie potwierdza dokladnosci na Hailo."
