#!/bin/bash
# Uruchamia LPR pod systemd (usluga uzytkownika "lpr").
# UWAGA: restartami po awarii zarzadza systemd (Restart=on-failure w lpr.service)
# — ten skrypt NIE ma petli restartujacej.

cd /home/admin/hailo-apps

# Rotacja loga uslugi: >50 MB -> zachowaj jedna poprzednia kopie
LOG=/home/admin/hailo-apps/logs/lpr_service.log
if [ -f "$LOG" ] && [ "$(stat -c%s "$LOG" 2>/dev/null || echo 0)" -gt 52428800 ]; then
    mv -f "$LOG" "$LOG.1"
fi

# ── Lustro logu na pendrive (diagnostyka bez SSH — czytasz plik na PC) ──────
# logi/<data>.log = pelna kopia biezacego logu; poprzednie_uruchomienie.txt =
# koncowka logu sprzed startu (przyczyna ostatniego padu od reki)
PDLOGDIR=/media/lpr/logi
if mkdir -p "$PDLOGDIR" 2>/dev/null && [ -w "$PDLOGDIR" ]; then
    find "$PDLOGDIR" -name '*.log' -mtime +14 -delete 2>/dev/null
    { echo "=== zrzut $(date) — koncowka POPRZEDNIEGO uruchomienia ==="
      tail -n 200 "$LOG" 2>/dev/null; } > "$PDLOGDIR/poprzednie_uruchomienie.txt"
    echo "[$(date)] START" >> "$PDLOGDIR/historia_startow.log"
    exec > >(tee -a "$PDLOGDIR/$(date +%F).log") 2>&1
fi

echo "============================================"
echo "[$(date)] lpr_run.sh START"

# ── Srodowisko sesji graficznej ──────────────────────────────────────────────
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
# przejmij zmienne z user managera (sesja je tam importuje), z fallbackami
eval "$(systemctl --user show-environment 2>/dev/null \
        | grep -E '^(WAYLAND_DISPLAY|DISPLAY|XAUTHORITY)=' | sed 's/^/export /')"
export WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-wayland-0}"
export DISPLAY="${DISPLAY:-:0}"
export XAUTHORITY="${XAUTHORITY:-/home/admin/.Xauthority}"

# Poczekaj na kompozytor Wayland (max 30 s) — przy starcie systemu usluga
# potrafi wystartowac przed pulpitem
for i in $(seq 1 30); do
    [ -S "$XDG_RUNTIME_DIR/$WAYLAND_DISPLAY" ] && { echo "[$(date)] Wayland gotowy (${i}s)"; break; }
    sleep 1
done

# ── Hailo (max ~45 s) ────────────────────────────────────────────────────────
for i in $(seq 1 15); do
    hailortcli fw-control identify &>/dev/null && { echo "[$(date)] Hailo gotowy"; break; }
    sleep 3
done

# ── Zrodlo obrazu wg configu www: usb (C1 PRO) | csi (Arducam IMX708 HDR) ───
CAM_SOURCE=$(python3 -c "
import json
try:
    print(json.load(open('/home/admin/hailo-apps/local_resources/lpr_runtime_config.json')).get('cam_source','usb'))
except Exception:
    print('usb')
" 2>/dev/null)
if [ "$CAM_SOURCE" = "csi" ]; then
    INPUT="rpi"
    # 2304x1296 = natywny tryb HDR IMX708, max detal tablic. Dziala na
    # ~60% CPU DZIEKI formatowi BGR888 w lpr.py (bez cvtColor na CPU) —
    # bez tej optymalizacji bylo 80% i 27 fps. Awaryjnie: 1920x1080 (~42%).
    # 24.09.2026: to jest rozmiar strumienia `lores` do POTOKU (detekcja +
    # podglad), skalowanego w ISP. Pelne 2304x1296 (`main`) kamera dalej
    # oddaje - lpr.py trzyma je w pierscieniu do wycinkow OCR i zdjec
    # (stale LPR_MAIN_W/H). Test A/B: detekcja z 1280x720 bez straty,
    # a hailocropper zamiast 18,8 ms/klatke robi 0,8 ms.
    VW=1280; VH=720
    # Obrot 180 robi SENSOR (Transform w lpr.py), wiec potok NIE dostaje
    # --horizontal-mirror/--vertical-mirror. Videoflip na 2304x1296
    # kosztowal 11,2 ms/klatke = 33% rdzenia przy 30 fps. Zmiana z
    # 02.09.2026 - te dwa miejsca musza sie zgadzac.
    MIRROR=""
    echo "[$(date)] Zrodlo: kamera CSI (Picamera2, ${VW}x${VH} HDR)"
    # Sufit migawki w AUTO: przyciete drabinki AGC w kopii tuningu IMX708.
    # Env MUSI byc wyeksportowany TUTAJ (srodowisko startowe procesu) —
    # os.environ z wnetrza Pythona nie dociera do IPA libcamery
    # (zweryfikowane 2026-07-17: rpicam z env dziala, python putenv nie).
    TUNE=$(python3 - <<'PY' 2>/dev/null
import json
try:
    cfg = json.load(open('/home/admin/hailo-apps/local_resources/lpr_runtime_config.json'))
    cap = int((cfg.get('csi_camera') or {}).get('max_shutter_us', 16666))
    if cap:
        t = json.load(open('/usr/share/libcamera/ipa/rpi/pisp/imx708.json'))
        for a in t.get('algorithms', []):
            agc = a.get('rpi.agc')
            if not agc:
                continue
            for ch in agc.get('channels', [agc]):
                for m in (ch.get('exposure_modes') or {}).values():
                    if m.get('shutter'):
                        m['shutter'] = [min(int(s), cap) for s in m['shutter']]
        out = '/home/admin/hailo-apps/local_resources/imx708_lpr.json'
        json.dump(t, open(out, 'w'))
        print(out)
except Exception:
    pass
PY
)
    if [ -n "$TUNE" ]; then
        export LIBCAMERA_RPI_TUNING_FILE="$TUNE"
        echo "[$(date)] Tuning kamery (sufit migawki): $TUNE"
    fi
    # KRYTYCZNE przy zimnym starcie (crash-loop 17.07 13:31): /dev/video0 to
    # PiSP i istnieje od razu, ale sensor imx708 wstaje POZNIEJ — czekamy,
    # az libcamera naprawde widzi kamere (max 60 s)
    for i in $(seq 1 30); do
        if rpicam-hello --list-cameras 2>/dev/null | grep -q imx708; then
            echo "[$(date)] Kamera CSI wykryta (proba $i)"
            break
        fi
        sleep 2
    done
else
    INPUT="/dev/video0"
    VW=1920; VH=1080
    # Kamera USB nie ma sprzetowego obrotu - prostuje potok.
    MIRROR="--horizontal-mirror --vertical-mirror"
    echo "[$(date)] Zrodlo: kamera USB ($INPUT, ${VW}x${VH})"
fi

# ── Kamera (max 40 s) + zwolnienie od konkurentow ───────────────────────────
for i in $(seq 1 20); do
    [ -e /dev/video0 ] && break
    sleep 2
done
pkill -9 -f collect_plates.py 2>/dev/null
if [ "$CAM_SOURCE" != "csi" ]; then
    fuser -k /dev/video0 2>/dev/null
fi
sleep 1

# Ustawienia kamery (migawka/gain/AWB) sa zarzadzane przez lpr.py:
# panel KAMERA na www -> zapis do lpr_runtime_config.json -> aplikowane
# przy kazdym starcie. Tutaj nic nie ustawiamy na sztywno.

source setup_env.sh

# 09.10: osobny proces animowanego ekranu ladowania (pierscien, gwiazdki, pas VHS) —
# chodzi przez caly import Hailo i budowe potoku; lpr.py konczy go plikiem /tmp/lpr_splash.stop
rm -f /tmp/lpr_splash.stop
# (przy starcie systemu animacje pokazuje mpv z autostartu labwc — wtedy proces splasha zbedny)
pgrep -x mpv >/dev/null || python3 -u /home/admin/hailo-apps/lpr_splash.py &
echo "[$(date)] Startuje lpr.py"
exec python3 -u lpr.py \
    --input "$INPUT" \
    --frame-rate 15 \
    --width "$VW" \
    --height "$VH" \
    --hef-path lpr_yolov8n_v3 \
    --labels-json /home/admin/hailo-apps/local_resources/hailo_5_classes_v2.json \
    --remove-labels /home/admin/hailo-apps/local_resources/labels/remove_labels_license_plate.txt \
    --track-class-id 5 \
    $MIRROR
