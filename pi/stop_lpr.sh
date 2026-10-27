#!/bin/bash
# Zatrzymuje system LPR (usluga systemd + ewentualne procesy luzem).
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
systemctl --user stop lpr.service 2>/dev/null && echo "usluga lpr zatrzymana"
# sprzatanie po ewentualnych recznych uruchomieniach
pkill -f start_lpr.sh 2>/dev/null
sleep 1
pkill -9 -f "Hailo Detection App" 2>/dev/null
pkill -9 -f "lpr.py" 2>/dev/null
sleep 1
if pgrep -f "Hailo Detection|lpr.py" > /dev/null; then
    echo "UWAGA: cos jeszcze dziala:"; pgrep -af "Hailo Detection|lpr.py"
else
    echo "OK — LPR calkowicie zatrzymany."
    echo "Start ponowny: systemctl --user start lpr   (albo ikona LPR na pulpicie / reboot)"
fi
