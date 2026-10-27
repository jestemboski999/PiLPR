#!/bin/bash
# Wgrywa lpr.py na malinkę, commituje, restartuje usługę i weryfikuje.
# Uruchomienie (git-bash / narzędzie Bash):  bash wgraj_na_malinke.sh ["opis commita"]
# Adres malinki w zmiennej LPR_PI_SSH, np. LPR_PI_SSH=admin@raspberrypi.local
set -e
PI="${LPR_PI_SSH:?ustaw LPR_PI_SSH=uzytkownik@adres_malinki}"
DIR="$(cd "$(dirname "$0")" && pwd)"
MSG="${1:-Aktualizacja lpr.py}"

# Logowanie kluczem SSH (ssh-copy-id), bez hasła w skrypcie.

echo "== 1/5 łączność =="
ssh -o ConnectTimeout=10 $PI "echo polaczono: \$(hostname)"

echo "== 2/5 kopia + kompilacja =="
ssh $PI "mkdir -p /home/admin/hailo-apps/local_resources \
         /home/admin/hailo-apps/local_resources/ocr/hailo_20261006 \
         /home/admin/hailo-apps/wireplumber \
         /home/admin/.config/wireplumber/wireplumber.conf.d"
scp "$DIR/lpr.py" $PI:/home/admin/hailo-apps/lpr.py
scp "$DIR/lpr_hailo_ocr.py" $PI:/home/admin/hailo-apps/lpr_hailo_ocr.py
scp "$DIR/lpr_splash.py" $PI:/home/admin/hailo-apps/lpr_splash.py
# Model leży w lokalnej kopii; nie przesyłaj ponownie, gdy SHA256 już się zgadza.
OCR_DIR="$DIR/local_resources/ocr/hailo_20261006"
OCR_REMOTE=/home/admin/hailo-apps/local_resources/ocr/hailo_20261006
OCR_SHA=4a1a31653fd31fd8c926ad6e0a561a42ce1aa184f7aef858a6ef5f5d3e67cdc9
if ! ssh $PI "echo '$OCR_SHA  $OCR_REMOTE/pl_lpr_rec.hef' | sha256sum -c - >/dev/null 2>&1"; then
    scp "$OCR_DIR/pl_lpr_rec.hef" "$PI:$OCR_REMOTE/pl_lpr_rec.hef.part"
    ssh $PI "echo '$OCR_SHA  $OCR_REMOTE/pl_lpr_rec.hef.part' | sha256sum -c - && mv '$OCR_REMOTE/pl_lpr_rec.hef.part' '$OCR_REMOTE/pl_lpr_rec.hef'"
fi
scp "$OCR_DIR/pl_dict.txt" "$PI:$OCR_REMOTE/pl_dict.txt"
scp "$DIR/lpr_run.sh" $PI:/home/admin/hailo-apps/lpr_run.sh
scp "$DIR/local_resources/lpr_splash_pilpr_pixel.png" \
    $PI:/home/admin/hailo-apps/local_resources/lpr_splash_pilpr_pixel.png
scp "$DIR/wireplumber/51-lpr-bluetooth.conf" \
    $PI:/home/admin/hailo-apps/wireplumber/51-lpr-bluetooth.conf
ssh $PI "WP_SRC=/home/admin/hailo-apps/wireplumber/51-lpr-bluetooth.conf;
         WP_DST=/home/admin/.config/wireplumber/wireplumber.conf.d/51-lpr-bluetooth.conf;
         if ! cmp -s \"\$WP_SRC\" \"\$WP_DST\"; then
           install -m 0644 \"\$WP_SRC\" \"\$WP_DST\";
           systemctl --user restart wireplumber.service;
         fi"
ssh $PI "chmod +x /home/admin/hailo-apps/lpr_run.sh; python3 -m py_compile /home/admin/hailo-apps/lpr.py /home/admin/hailo-apps/lpr_hailo_ocr.py && echo 'kompilacja OK'"

echo "== 3/5 commit na malince =="
# fork: gałąź kamera-usb wskazuje ostatnią wersję sprzed przejścia na CSI
ssh $PI "cd /home/admin/hailo-apps && git branch kamera-usb 2>/dev/null && echo 'utworzono gałąź kamera-usb (fork wersji USB)' || echo 'gałąź kamera-usb: już istnieje'"
ssh $PI "cd /home/admin/hailo-apps && git add lpr.py lpr_hailo_ocr.py lpr_splash.py lpr_run.sh local_resources/lpr_splash_pilpr_pixel.png wireplumber/51-lpr-bluetooth.conf && git commit -m '$MSG' || echo 'commit: brak zmian'"

echo "== 4/5 restart usługi =="
ssh $PI "export XDG_RUNTIME_DIR=/run/user/1000; systemctl --user restart lpr.service; sleep 8; systemctl --user is-active lpr.service"

echo "== 5/5 weryfikacja HTTP =="
ssh $PI "ready=0;
         for attempt in \$(seq 1 45); do
           if curl -fsS -o /dev/null --max-time 2 http://localhost/api/stats 2>/dev/null; then ready=1; break; fi;
           sleep 1;
         done;
         if [ \"\$ready\" != 1 ]; then echo 'BLAD: serwer HTTP nie wystartowal'; exit 1; fi;
         curl -fsS -o /dev/null -w 'strona glowna: %{http_code}\n' http://localhost/ || exit 1;
         curl -fsS -o /dev/null -w 'focus-view:    %{http_code}\n' http://localhost/api/focus-view || exit 1;
         echo -n 'stats: '; curl -s http://localhost/api/stats | head -c 160; echo"

echo "== info: gotowość toru CSI (Arducam IMX708) =="
ssh $PI "gst-inspect-1.0 libcamerasrc >/dev/null 2>&1 && echo 'libcamerasrc: JEST' || echo 'libcamerasrc: BRAK (sudo apt install gstreamer1.0-libcamera)';
         ls /dev/v4l-subdev* 2>/dev/null | head -4 || echo 'brak /dev/v4l-subdev* (kamera CSI niepodłączona)';
         command -v rpicam-hello >/dev/null && rpicam-hello --list-cameras 2>/dev/null | head -6 || true"

echo
echo "GOTOWE. Sprawdź na telefonie: http://${PI#*@}"
echo "Przełączenie na kamerę CSI: w lpr_runtime_config.json ustaw \"cam_source\": \"csi\" i zrestartuj usługę."
