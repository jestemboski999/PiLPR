# -*- coding: utf-8 -*-
"""Scenariusze CDP: otwiera widok LPR w headless Edge, wykonuje kroki JS,
robi zrzut. Uzycie: python cdp_do.py <url> <w> <h> <out.png> <scenariusz>"""
import base64
import json
import os
import subprocess
import sys
import time
import urllib.request

import websocket

URL, W, H, OUT, SCEN = (sys.argv[1], int(sys.argv[2]), int(sys.argv[3]),
                        sys.argv[4], sys.argv[5])
PORT = 9333
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
if not os.path.exists(EDGE):
    EDGE = r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"

WHEEL = """
(function(){
  var wrap = document.getElementById('rv-imgwrap') ||
             document.getElementById('lightbox-imgwrap');
  var r = wrap.getBoundingClientRect();
  var cx = r.left + r.width/2, cy = r.top + r.height/2;
  var host = document.getElementById('reviewer');
  for (var i=0;i<%d;i++)
    host.dispatchEvent(new WheelEvent('wheel',
      {clientX:cx, clientY:cy, deltaY:-250, ctrlKey:true, bubbles:true,
       cancelable:true}));
  return wrap.style.transform;
})()
"""

DRAW = """
(function(){
  var img = document.getElementById('rv-full');
  var r = img.getBoundingClientRect();
  function ev(t, x, y){ img.dispatchEvent(new MouseEvent(t,
    {clientX:x, clientY:y, bubbles:true, cancelable:true})); }
  var x1 = r.left + r.width*0.3,  y1 = r.top + r.height*0.4;
  var x2 = r.left + r.width*0.7,  y2 = r.top + r.height*0.6;
  ev('mousedown', x1, y1);
  document.dispatchEvent(new MouseEvent('mousemove',
    {clientX:x2, clientY:y2, bubbles:true}));
  document.dispatchEvent(new MouseEvent('mouseup',
    {clientX:x2, clientY:y2, bubbles:true}));
  var b = document.getElementById('rv-box');
  return JSON.stringify({box: b.style.cssText, cls: b.className,
                         zoomAfter: document.getElementById('rv-imgwrap').style.transform});
})()
"""

SCENARIOS = {
    "table":  [],
    "editor": [(2, "reviewerOpen()"), (13, "document.getElementById('rv-count').textContent")],
    "backlog": [(2, "reviewerOpen()"),
                (5, "document.getElementById('rv-count').textContent"),
                (1, "(rvCurrent? '#'+rvCurrent.id+' '+rvCurrent.plate : 'brak')"),
                (0.5, "rvVerdict('ok'); 'ocenione'"),
                (2.5, "(rvCurrent? 'nastepny: #'+rvCurrent.id+' '+rvCurrent.plate : 'brak')"),
                (0.5, "document.getElementById('rv-count').textContent")],
    "zoom":   [(2, "reviewerOpen()"), (13, "bboxStart('rv')"), (0.5, WHEEL % 4),
               (0.5, "document.getElementById('rv-imgwrap').style.transform")],
    "zoomdraw": [(2, "reviewerOpen()"), (13, "bboxStart('rv')"), (0.5, WHEEL % 4),
                 (0.5, DRAW)],
    "focus":  [(2, "openFocusModal()")],
    "camera": [(2, "openCameraModal()")],
    "usb": [(2, "openUsbModal()"), (2.5, "document.querySelector(String.fromCharCode(35)+'usb-body').textContent.slice(0,50)")],
    "bt": [(2, "openBtModal()"),
           (2, "document.querySelectorAll('.bt-row').length + ' urzadzen'")],
    "btmenu": [(2, "openBtModal()"),
               (3.5, "btMenu('AA:BB:CC:DD:EE:02'); 'menu otwarte'")],
    "wlbulk": [(2, "openWlModal()"),
               (1.5, "wlSelect('rodzina'); 'wybrana Rodzina'"),
               (1.5, "wlBulkToggle(); 'panel importu'")],
    "wlsearch": [(2, "openWlModal()"),
                 (2.5, "document.getElementById('wl-search').value='BMW 330i';"
                       "wlReloadList();"
                       "document.getElementById('wl-count').textContent"),
                 (1, "document.querySelectorAll('.wl-tab').length + ' zakladek'")],
    "wl": [(2, "openWlModal()"),
           (2, "document.getElementById('wl-list').textContent.slice(0,60)")],
    "log": [(2, "openLogModal()"),
            (2, "document.getElementById('log-pre').textContent.length + ' znakow'")],
    "camlive": [(2, "openCameraModal()"),
                (2, "document.getElementById('csi-now').textContent"),
                (0.5, "fetch('/api/camera',{method:'POST',headers:"
                      "{'Content-Type':'application/json'},"
                      "body:JSON.stringify({manual:true,exposure_us:2000,"
                      "gain:2})}); 'manual 1/500 wyslany'"),
                (6, "document.getElementById('csi-now').textContent"),
                (0.5, "fetch('/api/camera',{method:'POST',headers:"
                      "{'Content-Type':'application/json'},"
                      "body:JSON.stringify({reset_auto:1})}); 'auto przywrocone'")],
    "rec":    [(2, "openRecModal()")],
    "recfilter": [(2, "openRecModal()"),
                  (1.5, "document.getElementById('rec-fday').value='2026-07-15';"
                        "recDayChanged();"
                        "(function(){var fs=document.getElementById('rec-fses');"
                        "return fs.options.length+' opcji jazd, '+"
                        "document.getElementById('rec-fcount').textContent})()"),
                  (0.5, "document.getElementById('rec-fses').selectedIndex=2;"
                        "renderRecList();"
                        "document.getElementById('rec-fcount').textContent")],
    "recplay": [(2, "openRecModal()"),
                (2, "playRec('20260713_222224','jazda_00061.mkv')"),
                (8, "(function(){var v=document.getElementById('rec-video');"
                    "return JSON.stringify({src:v.src.slice(-40),dur:v.duration,"
                    "paused:v.paused,rate:v.playbackRate,t:v.currentTime,"
                    "stat:document.getElementById('rec-pstat').textContent})})()"),
                (0.5, "(function(){var v=document.getElementById('rec-video');"
                      "v.currentTime=30;v.playbackRate=2;setRecSpeed(2);"
                      "return 'seek+2x'})()")],
    "lightbox": [(2, "document.querySelector('#events-tbody tr').click()")],
    "viewer": [(2, "document.getElementById('f-day').value='2026-07-13';"
                   "reload(); 'dzien ustawiony'"),
               (1.5, "setView('duzy'); rows.length + ' wierszy'"),
               (1.5, "dVerdict('ok'); 'ocenione DOBRZE'"),
               (1.5, "document.getElementById('d-info').innerHTML")],
}

proc = subprocess.Popen([
    EDGE, "--headless=new", "--disable-gpu", "--remote-allow-origins=*",
    "--remote-debugging-port=%d" % PORT,
    "--user-data-dir=" + os.path.join(os.path.dirname(os.path.abspath(OUT)),
                                      "edgeprof_cdp"),
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

    ws = websocket.create_connection(ws_url, timeout=40)
    mid = [0]

    def cmd(method, **params):
        mid[0] += 1
        ws.send(json.dumps({"id": mid[0], "method": method, "params": params}))
        while True:
            msg = json.loads(ws.recv())
            if msg.get("id") == mid[0]:
                return msg.get("result", {})

    cmd("Emulation.setDeviceMetricsOverride", width=W, height=H,
        deviceScaleFactor=1, mobile=(W < 700))
    cmd("Page.enable")
    cmd("Page.navigate", url=URL)
    time.sleep(4)

    for delay, expr in SCENARIOS[SCEN]:
        time.sleep(delay)
        r = cmd("Runtime.evaluate", expression=expr, returnByValue=True)
        val = r.get("result", {}).get("value")
        exc = r.get("exceptionDetails")
        out = (json.dumps(val, ensure_ascii=True) if val is not None
               else exc and exc.get("text"))
        print("krok:", expr[:60].replace("\n", " "), "->", out)

    time.sleep(1)
    shot = cmd("Page.captureScreenshot", format="png")
    open(OUT, "wb").write(base64.b64decode(shot["data"]))
    chk = cmd("Runtime.evaluate", expression=(
        "JSON.stringify({scrollW:document.documentElement.scrollWidth,"
        "innerW:innerWidth,scrollH:document.documentElement.scrollHeight,"
        "innerH:innerHeight})"), returnByValue=True)
    print("OK", OUT, chk["result"]["value"])
    ws.close()
finally:
    proc.kill()
