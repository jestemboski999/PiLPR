#!/bin/bash
# Skąd biorą się zacięcia obrazu. Jedno przejście, wszystko na raz.
# Uruchomienie (git-bash):  bash diagnoza_zaciec.sh
PI="${LPR_PI_SSH:?ustaw LPR_PI_SSH=uzytkownik@adres_malinki}"
# Logowanie kluczem SSH (ssh-copy-id), bez hasła w skrypcie.

ssh -o ConnectTimeout=15 $PI 'export XDG_RUNTIME_DIR=/run/user/1000
echo "=== 1/7 USŁUGA ==="
echo -n "stan: "; systemctl --user is-active lpr.service
systemctl --user show lpr.service -p NRestarts -p ExecMainStatus -p ActiveEnterTimestamp

echo; echo "=== 2/7 ZACIĘCIA zarejestrowane w logu ==="
journalctl --user -u lpr.service --since "-24h" --no-pager 2>/dev/null \
  | grep -c "ZACIECIE" | xargs -I{} echo "  zdarzeń: {}"
journalctl --user -u lpr.service --since "-24h" --no-pager 2>/dev/null \
  | grep "ZACIECIE" | tail -15

echo; echo "=== 3/7 THROTTLING / TEMPERATURA (podkręcenie zasilania i grzanie) ==="
vcgencmd measure_temp 2>/dev/null
T=$(vcgencmd get_throttled 2>/dev/null | cut -d= -f2)
echo "throttled=$T   (0x0 = OK; bit0 under-voltage, bit1 limit czestotliwosci,"
echo "                bit2 dławienie, bit16-19 = wystąpiło od startu)"

echo; echo "=== 4/7 OBCIĄŻENIE ==="
uptime
curl -s http://localhost/api/stats; echo
PID=$(pgrep -f "python3 .*lpr.py" | head -1)
echo "--- najcięższe wątki procesu (pid=$PID):"
top -H -b -n 1 -p $PID 2>/dev/null | tail -14

echo; echo "=== 5/7 BŁĘDY I OSTRZEŻENIA (ostatnia doba) ==="
journalctl --user -u lpr.service --since "-24h" --no-pager 2>/dev/null \
  | grep -iE "traceback|error|błąd|blad|warn|dropped|underrun|late|QoS" \
  | grep -viE "ZACIECIE" | tail -20

echo; echo "=== 6/7 GUBIONE KLATKI GSTREAMERA ==="
journalctl --user -u lpr.service --since "-24h" --no-pager 2>/dev/null \
  | grep -icE "dropp|qos|underrun" | xargs -I{} echo "  linii o gubieniu: {}"

echo; echo "=== 7/7 PAMIĘĆ I DYSK ==="
free -m | head -2
df -h /media/lpr /home/admin 2>/dev/null | tail -3
echo -n "zdjęć na pendrive: "; find /media/lpr/lpr -name "*.jpg" 2>/dev/null | wc -l
echo "--- czy trwa kopia/rsync:"; pgrep -a rsync || echo "  (nie)"
'
