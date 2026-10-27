import os as _os
REPO = _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..", ".."))
import harness, time, types, numpy as np, cv2
from datetime import datetime
from scene import scene, plate_img
L = harness.lpr
OUT = REPO + "/docs/images/"
def fake_user():
    u = types.SimpleNamespace(web_server=None, frame=None)
    u.set_frame = lambda f: setattr(u, "frame", f)
    return u
def crop_of(rgb, pb, mw=0.2, mh=0.16):
    H, W = rgb.shape[:2]
    x1,y1,x2,y2 = pb; dw=(x2-x1)*mw; dh=(y2-y1)*mh
    c = rgb[int((y1-dh)*H):int((y2+dh)*H), int((x1-dw)*W):int((x2+dw)*W)]
    return cv2.cvtColor(c, cv2.COLOR_RGB2BGR)

def device_frame(rgb, rys, notifs=(), hud=(1, 37, ("", 0.0)), radar=False, stats=True):
    L._podglad_wh = (800, 480)
    L.RADAR_CFG["tryb"] = radar
    u = fake_user()
    now = time.monotonic()
    item = {"frame": rgb, "w": rgb.shape[1], "h": rgb.shape[0], "now": now, "user_data": u,
            "rys": rys, "notifs": list(notifs), "hud": hud, "web_ip": "", "web_start": 0}
    L._nakladka_rysuj(item)
    f = u.frame
    if stats:
        with L._stat_lock:
            L._stat_fps, L._stat_disp_fps, L._stat_cpu, L._stat_ram = 30.0, 30.0, 27.0, 1.1
            L._stat_cpu_temp, L._stat_hailo_temp = 54.0, 48.0
        L.LprApp._draw_stats_bar(types.SimpleNamespace(), f)
    return f

now = time.monotonic()
# 1. jazda: potwierdzona tablica + dymek
rgb, cb, pb, boxes = scene(plate="WX 1234A", car_cx=0.60)
crop = crop_of(rgb, pb)
rys = [("vehicle",)+b[1]+(False,"") for b in boxes] + [("plate",)+pb+(True,"WX1234A")]
notif = {"crop": crop, "text": "WX1234A", "t0": now - 1.0}
f = device_frame(rgb, rys, [notif], hud=(1, 37, ("WX1234A", now - 2)))
cv2.imwrite(OUT+"ekran_jazda.png", f)

# 2. trafienie z bazy znajomych
L.WATCH_GROUPS.clear(); L.WATCH_GROUPS["znajomi"] = {"name":"Znajomi","alert":"Mijasz znajomych. {tablica}.","enabled":True,"color":"green","nota":True}
L.WATCHLIST.clear(); L.WATCHLIST["WX5678K"] = {"group":"znajomi","note":"Ania i Tomek"}
L.SPEECH.say = lambda *a, **k: None
rgb2, cb2, pb2, boxes2 = scene(plate="WX 5678K", car_cx=0.60, body=(150,40,40), seed=5)
H,W = rgb2.shape[:2]
veh = cv2.cvtColor(rgb2[int(cb2[1]*H):int(cb2[3]*H), int(cb2[0]*W):int(cb2[2]*W)], cv2.COLOR_RGB2BGR)
rys2 = [("vehicle",)+b[1]+(False,"") for b in boxes2] + [("plate",)+pb2+(True,"WX5678K")]
f2 = device_frame(rgb2, rys2, [], hud=(1, 38, ("WX5678K", now)))
L._alert_trigger("WX5678K", veh, True)
L.LprApp._draw_alert(types.SimpleNamespace(), f2)
cv2.imwrite(OUT+"ekran_znajomi.png", f2)

# 3. tryb radar
L._radar_stan.update({"ok": True, "v": 63.0, "t": time.monotonic(), "zdarzen": 4, "otwarte": True})
L.RADAR_CFG["prog_kmh"] = 40.0
rgb3, cb3, pb3, boxes3 = scene(plate="WX 9021C", car_cx=0.58, body=(200,200,205), seed=8, car_w=720, car_by=0.86)
rys3 = [("vehicle",)+b[1]+(False,"") for b in boxes3] + [("plate",)+pb3+(False,"3/3")]
L._radar_zdarzenie = {"t0": time.monotonic(), "t_ost": time.monotonic(), "czas": datetime.now(), "vmax": 63.0, "znak": "+", "probek": 9, "v_lista":[63], "zamkniete": False, "zapisane": False, "kandydaci": []}
f3 = device_frame(rgb3, rys3, [], hud=(1, 41, ("", 0.0)), radar=True)
cv2.imwrite(OUT+"ekran_radar.png", f3)

# 4. zdjęcie z fotoradaru (pełna rozdzielczość)
ev = {"czas": datetime(2026, 10, 2, 17, 41, 7), "vmax": 63.0}
kand = {"tablica": "WX9021C", "bbox": pb3, "pojazd": cb3, "crop": crop_of(rgb3, pb3)}
zdj = L._radar_render_zdjecie(cv2.cvtColor(rgb3, cv2.COLOR_RGB2BGR), ev, kand)
cv2.imwrite(OUT+"radar_zdjecie.jpg", zdj, [cv2.IMWRITE_JPEG_QUALITY, 88])
print("ok")
