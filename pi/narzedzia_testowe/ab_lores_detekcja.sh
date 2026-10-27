#!/bin/bash
export XDG_RUNTIME_DIR=/run/user/1000
LOG=/tmp/ab.txt
exec >$LOG 2>&1
trap 'systemctl --user start lpr.service; sleep 3; echo "[$(date +%T)] lpr.service: $(systemctl --user is-active lpr.service)"; echo KONIEC' EXIT
echo "[$(date +%T)] stop lpr.service"
systemctl --user stop lpr.service
for i in $(seq 1 30); do pgrep -f "Hailo Detection" >/dev/null || break; sleep 1; done
pgrep -f "Hailo Detection" >/dev/null && echo "UWAGA: proces nadal zyje"
echo "[$(date +%T)] start testu"
python3 /tmp/ab.py 900
echo "[$(date +%T)] test zakonczony rc=$?"
