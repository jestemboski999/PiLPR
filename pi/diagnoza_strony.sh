#!/bin/bash
# Dlaczego http://<adres malinki> nie odpowiada — jedno przejscie diagnostyczne.
# Uruchomienie (git-bash):  bash diagnoza_strony.sh
PI="${LPR_PI_SSH:?ustaw LPR_PI_SSH=uzytkownik@adres_malinki}"
# Logowanie kluczem SSH (ssh-copy-id), bez hasła w skrypcie.

echo "== 1/4 czy w ogole wchodzimy po SSH =="
if ! ssh -o ConnectTimeout=10 $PI "echo polaczono: \$(hostname), uptime:\$(uptime -p)"; then
    echo ">>> SSH tez nie dziala. Problem jest z siecia/Tailscale, nie ze strona."
    exit 1
fi

echo
echo "== 2/4 czy usluga LPR zyje =="
ssh $PI "export XDG_RUNTIME_DIR=/run/user/1000
         echo -n 'stan: '; systemctl --user is-active lpr.service
         systemctl --user show lpr.service -p NRestarts -p ExecMainStatus
         echo '--- ostatnie 15 linii dziennika uslugi:'
         journalctl --user -u lpr.service -n 15 --no-pager | tail -15"

echo
echo "== 3/4 kto sluchа na porcie 80 =="
ssh $PI "ss -tlnp 2>/dev/null | grep -E ':80 |:443 ' || echo 'NIKT nie sluchа na 80/443  <-- to jest przyczyna'
         echo -n 'sysctl (musi byc 80): '; sysctl -n net.ipv4.ip_unprivileged_port_start
         echo -n 'lokalnie strona zwraca: '
         curl -s -o /dev/null -w '%{http_code}\n' --max-time 8 http://localhost/ || echo '(brak odpowiedzi)'
         echo '--- czy Tailscale przejal port (serve):'
         tailscale serve status 2>/dev/null || echo '(serve nieaktywny — dobrze)'"

echo
echo "== 4/4 ostatnie bledy z logu aplikacji =="
ssh $PI "tail -40 /home/admin/hailo-apps/logs/lpr_service.log 2>/dev/null |
         grep -iE 'error|traceback|blad|błąd|address already|permission' | tail -12 ||
         echo '(brak linii z bledami w ogonie logu)'"
