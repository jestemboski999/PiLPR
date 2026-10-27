# -*- coding: utf-8 -*-
"""Zrzut ekranu przez CDP z dokladna emulacja viewportu (jak telefon)."""
import base64
import json
import subprocess
import sys
import time
import urllib.request

import websocket

URL, W, H, OUT = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
PORT = 9333
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
import os
if not os.path.exists(EDGE):
    EDGE = r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"

proc = subprocess.Popen([
    EDGE, "--headless=new", "--disable-gpu", "--remote-debugging-port=%d" % PORT,
    "--remote-allow-origins=*",
    "--user-data-dir=" + os.path.join(os.path.dirname(OUT), "edgeprof_cdp"),
    "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    ws_url = None
    for _ in range(50):
        try:
            tabs = json.load(urllib.request.urlopen(
                "http://127.0.0.1:%d/json" % PORT, timeout=2))
            pages = [t for t in tabs if t.get("type") == "page"]
            if pages:
                ws_url = pages[0]["webSocketDebuggerUrl"]
                break
        except Exception:
            pass
        time.sleep(0.3)
    if not ws_url:
        sys.exit("brak CDP")

    ws = websocket.create_connection(ws_url, timeout=30)
    mid = [0]

    def cmd(method, **params):
        mid[0] += 1
        ws.send(json.dumps({"id": mid[0], "method": method, "params": params}))
        while True:
            msg = json.loads(ws.recv())
            if msg.get("id") == mid[0]:
                return msg.get("result", {})

    cmd("Emulation.setDeviceMetricsOverride", width=W, height=H,
        deviceScaleFactor=1, mobile=True)
    cmd("Page.enable")
    cmd("Page.navigate", url=URL)
    time.sleep(5)          # dane + obrazki
    shot = cmd("Page.captureScreenshot", format="png")
    open(OUT, "wb").write(base64.b64decode(shot["data"]))
    size = cmd("Runtime.evaluate", expression=(
        "JSON.stringify({w:innerWidth,h:innerHeight,"
        "scrollW:document.documentElement.scrollWidth})"),
        returnByValue=True)
    print("OK", OUT, size["result"]["value"])
    ws.close()
finally:
    proc.kill()
