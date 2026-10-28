"""Zestaw do materiałów reklamowych: prawdziwe klatki z ekranu PiLPR.

Ładuje pi/lpr.py przez docs/obrazy_demo/harness.py (Hailo i GStreamer
podmienione atrapami) i woła jego własne funkcje rysujące. Każda klatka
800×480 to dokładnie to, co rysuje ekran w aucie. Sceny drogowe są
syntetyczne, tablice fikcyjne.
"""
import os, sys, time, types
from datetime import datetime
import numpy as np, cv2

TU = os.path.dirname(os.path.abspath(__file__))
DEMO = os.path.join(TU, "..", "obrazy_demo")
sys.path.insert(0, DEMO)
import harness  # noqa: E402
from scene import scene  # noqa: E402
import vhs  # noqa: E402,F401

L = harness.lpr
L.SPEECH.say = lambda *a, **k: None
L.WATCH_GROUPS.clear()
L.WATCH_GROUPS.update({
    "znajomi": {"name": "Znajomi", "alert": "Mijasz znajomych. {tablica}.",
                "enabled": True, "color": "green"},
    "sluzbowe": {"name": "Pojazdy służbowe", "alert": "Uwaga, pojazd służbowy.",
                 "enabled": True, "color": "blue"},
})
L.WATCHLIST.clear()


def _user():
    u = types.SimpleNamespace(web_server=None, frame=None)
    u.set_frame = lambda f: setattr(u, "frame", f)
    return u


def _crop(rgb, box, mw=0.2, mh=0.16, bgr=True):
    H, W = rgb.shape[:2]
    x1, y1, x2, y2 = box
    dw, dh = (x2 - x1) * mw, (y2 - y1) * mh
    c = rgb[max(0, int((y1 - dh) * H)):int((y2 + dh) * H),
            max(0, int((x1 - dw) * W)):int((x2 + dw) * W)]
    return cv2.cvtColor(c, cv2.COLOR_RGB2BGR) if bgr else c


def _stats(f):
    with L._stat_lock:
        L._stat_fps, L._stat_disp_fps, L._stat_cpu, L._stat_ram = 30.0, 30.0, 27.0, 1.1
        L._stat_cpu_temp, L._stat_hailo_temp = 54.0, 48.0
    L.LprApp._draw_stats_bar(types.SimpleNamespace(), f)


def device_frame(sc, stan="szukam", tablica="", notif_wiek=None, ostatnia="",
                 radar=False, radar_v=None, liczba=37, stats=True):
    """Klatka ekranu urządzenia (BGR 800×480) dla sceny `sc` z `scena()`.

    stan: "nic" (bez ramek), "szukam" (ramka pojazdu + tablica bez odczytu),
          "czytam" (tablica z odczytem w trakcie), "pewne" (potwierdzona)."""
    rgb, cb, pb, boxes = sc
    L._podglad_wh = (800, 480)
    L.RADAR_CFG["tryb"] = radar
    now = time.monotonic()
    rys = []
    if stan != "nic":
        rys = [("vehicle",) + b[1] + (False, "") for b in boxes]
        if stan == "szukam":
            rys.append(("plate",) + pb + (False, ""))
        elif stan == "czytam":
            rys.append(("plate",) + pb + (False, tablica[:3] + "…"))
        else:
            rys.append(("plate",) + pb + (True, tablica))
    notifs = []
    if notif_wiek is not None and tablica:
        notifs.append({"crop": _crop(rgb, pb), "text": tablica, "t0": now - notif_wiek})
    hud = (1, liczba, (ostatnia, now - 2) if ostatnia else ("", 0.0))
    if radar:
        L.RADAR_CFG["prog_kmh"] = 40.0
        v = radar_v or 0.0
        L._radar_stan.update({"ok": True, "v": v, "t": now, "zdarzen": 4, "otwarte": v > 40})
        if v > 40:
            L._radar_zdarzenie = {"t0": now, "t_ost": now, "czas": datetime.now(), "vmax": v,
                                  "znak": "+", "probek": 9, "v_lista": [v], "zamkniete": False,
                                  "zapisane": False, "kandydaci": []}
        else:
            L._radar_zdarzenie = None
    u = _user()
    item = {"frame": rgb, "w": rgb.shape[1], "h": rgb.shape[0], "now": now, "user_data": u,
            "rys": rys, "notifs": notifs, "hud": hud, "web_ip": "", "web_start": 0}
    L._nakladka_rysuj(item)
    f = u.frame
    if stats:
        _stats(f)
    return f


def alert(f, sc, tablica, grupa, notatka=""):
    """Dorysowuje baner trafienia z bazy (prawdziwe `_draw_alert`)."""
    rgb, cb, pb, _ = sc
    L.WATCHLIST[tablica] = {"group": grupa, "note": notatka}
    L._alert_trigger(tablica, _crop(rgb, cb, 0.02, 0.02), True)
    L.LprApp._draw_alert(types.SimpleNamespace(), f)
    return f


def _fake_app(view="main", sel=0):
    a = types.SimpleNamespace(_menu_confirm=None, _menu_confirm_until=0.0,
                              _menu_view=view, _is_fullscreen=True, _disp_zoom=1)
    a._bt_label = lambda: "Radio w aucie"
    return a


def menu(f, view="main", sel=0, edit=False):
    """Nakłada prawdziwe menu ekranowe (kafle) na klatkę `f` (BGR 800×480)."""
    f = f.copy()
    h, w = f.shape[:2]
    L._radar_stan["ok"] = True          # moduł radaru podłączony
    items = L.LprApp._menu_def(_fake_app(view, sel))
    tytul = "Narzędzia deweloperskie" if view == "tools" else "Ustawienia"
    stopka = ("m — menu  ·  p — zdjęcie  ·  z — zoom  ·  f — pełny ekran" if view == "main"
              else "ustawienia zaawansowane — w codziennej jeździe niepotrzebne")
    pre, inv = L._menu_surface(w, h, items, sel, -1, edit, tytul, stopka)
    cv2.add(cv2.multiply(f, inv, scale=1.0 / 255.0), pre, dst=f)
    return f


def radar_foto(sc, tablica, v, czas=None):
    rgb, cb, pb, _ = sc
    ev = {"czas": czas or datetime(2026, 10, 2, 17, 41, 7), "vmax": v}
    kand = {"tablica": tablica, "bbox": pb, "pojazd": cb, "crop": _crop(rgb, pb)}
    return L._radar_render_zdjecie(cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR), ev, kand)


def scena(plate="WX 1234A", **kw):
    """Scena drogowa nocą (RGB 2304×1296) + ramki. Parametry jak scene.scene:
    car_w (szer. auta, im większa tym bliżej), car_cx, car_by, body, seed."""
    return scene(plate=plate, **kw)


def splash():
    from PIL import Image
    return Image.open(vhs.TAPETA).convert("RGB")
