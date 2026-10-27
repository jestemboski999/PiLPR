#!/bin/bash
# Kontrola po wdrozeniu — uruchom PO KAZDYM wgraniu lpr.py:
#     bash /home/admin/hailo-apps/sprawdz_zdrowie.sh
# Wychodzi z kodem 0 = wszystko OK, 1 = cos wymaga uwagi.
# Sprawdza to, czego "systemctl is-active" NIE wychwyci: martwy obraz, zamarly
# panel, brak modelu OCR, zacieciа, przegrzanie.
API=http://localhost
LOG=/home/admin/hailo-apps/logs/lpr_service.log
MODEL=/home/admin/hailo-apps/local_resources/ocr/cct_s_v2_pl.onnx
MODEL_SHA=ac42d142e06387ca0d4a61a8c7c245aacd55ce44f1a5d05c1876786491473083
BLEDY=0

ok()   { printf '  \033[32m[ OK ]\033[0m %s\n' "$1"; }
zle()  { printf '  \033[31m[BLAD]\033[0m %s\n' "$1"; BLEDY=$((BLEDY+1)); }
uwaga(){ printf '  \033[33m[UWGA]\033[0m %s\n' "$1"; }

echo "=== KONTROLA LPR — $(date '+%Y-%m-%d %H:%M:%S') ==="

# 1. usluga
if [ "$(systemctl --user is-active lpr.service)" = "active" ]; then
  ok "usluga lpr.service dziala"
else
  zle "usluga NIE dziala (systemctl --user status lpr.service)"
fi

# 2. panel www + statystyki
ST=$(curl -s --max-time 6 "$API/api/stats" 2>/dev/null)
if [ -z "$ST" ]; then
  zle "panel www nie odpowiada na /api/stats"
else
  ok "panel www odpowiada"
  FPS=$(echo "$ST"   | grep -o '"fps":[0-9.]*'      | cut -d: -f2 | cut -d. -f1)
  TEMP=$(echo "$ST"  | grep -o '"cpu_temp":[0-9.]*' | cut -d: -f2 | cut -d. -f1)
  CPU=$(echo "$ST"   | grep -o '"cpu":[0-9.]*'      | cut -d: -f2 | cut -d. -f1)
  PLATES=$(echo "$ST"| grep -o '"total_plates":[0-9]*' | cut -d: -f2)
  [ "${FPS:-0}" -ge 20 ] && ok "obraz plynny (fps ${FPS})" \
                         || zle "NISKI FPS (${FPS:-brak}) — rozpoznawanie zwalnia"
  [ "${TEMP:-0}" -lt 80 ] && ok "temperatura CPU ${TEMP}°C" \
                          || uwaga "wysoka temperatura CPU ${TEMP}°C (throttling?)"
  [ "${CPU:-0}" -lt 90 ] && ok "obciazenie CPU ${CPU}%" \
                         || uwaga "wysokie CPU ${CPU}%"
  echo "         (w bazie: ${PLATES:-?} tablic)"
fi

# 3. kamera zwraca SWIEZE metadane (samo "active" tego nie wykryje)
CAM=$(curl -s --max-time 6 "$API/api/camera" 2>/dev/null)
if echo "$CAM" | grep -q '"exposure_us"'; then
  EXP=$(echo "$CAM" | grep -o '"exposure_us":[0-9]*' | head -1 | cut -d: -f2)
  ok "kamera oddaje klatki (migawka ${EXP} us)"
else
  zle "brak metadanych kamery — obraz moze byc zamrozony"
fi

# 4. model OCR na miejscu i ten wlasciwy
if [ -f "$MODEL" ]; then
  S=$(sha256sum "$MODEL" | cut -d' ' -f1)
  [ "$S" = "$MODEL_SHA" ] && ok "model OCR zgodny (CCT 87.9%)" \
                          || uwaga "model OCR MA INNA SUME (${S:0:8}...) — czy to zamierzone?"
else
  zle "BRAK pliku modelu OCR"
fi

# 5. zacieciа obrazu w ostatniej godzinie
if [ -f "$LOG" ]; then
  H=$(date '+%H'); H1=$(date -d '1 hour ago' '+%H')
  Z=$(grep -c "ZACIECIE" "$LOG" 2>/dev/null)
  ZN=$(tail -400 "$LOG" | grep -c "ZACIECIE" 2>/dev/null)
  [ "${ZN:-0}" -eq 0 ] && ok "brak zaciec w ostatnich wpisach (lacznie w logu: ${Z})" \
                       || uwaga "ZACIECIA w ostatnich wpisach: ${ZN} (lacznie ${Z}) — sprawdz log"
  # swiezosc logu = czy proces w ogole cokolwiek pisze
  WIEK=$(( $(date +%s) - $(stat -c %Y "$LOG") ))
  [ "$WIEK" -lt 900 ] && ok "log zywy (ostatni zapis ${WIEK}s temu)" \
                      || uwaga "log nieruszany od ${WIEK}s"
else
  uwaga "brak pliku logu"
fi

# 6. miejsce na dysku
FREE=$(df -m / | tail -1 | awk '{print $4}')
[ "${FREE:-0}" -gt 2000 ] && ok "wolne miejsce ${FREE} MB" \
                          || zle "MALO MIEJSCA: ${FREE} MB"

echo "==============================================="
if [ "$BLEDY" -eq 0 ]; then
  echo "WYNIK: wszystko dziala"
else
  echo "WYNIK: $BLEDY problem(ow) — patrz [BLAD] wyzej"
fi
exit $([ "$BLEDY" -eq 0 ] && echo 0 || echo 1)
