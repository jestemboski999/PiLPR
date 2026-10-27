"""Syntetyczna scena z kamery w stylu malowanych szyb z szaf grających (lata 80.):
czarne szkło, płaskie kształty z czarnym konturem, palmy, latarnie-gwiazdy,
hotele z oświetlonymi oknami i fajerwerki. Fikcyjne tablice, bez prawdziwych danych.
Ten sam interfejs co scene.scene()."""
import math, random
import numpy as np
from PIL import Image, ImageDraw, ImageFilter
from scene import plate_img

K = (8, 8, 10)                       # kontur
KREM = (230, 223, 207)
OKNO = (246, 196, 67)
SZARY = (125, 128, 137)
DACHY = [(198, 58, 44), (42, 79, 156), (231, 181, 50)]
LISCIE = [(31, 90, 44), (44, 111, 55), (24, 71, 31)]


class Kamera:
    def __init__(s, W, H, hz=0.47, f=0.62, h=1.35, vx=0.5):
        s.W, s.H, s.hz, s.f, s.h, s.vx = W, H, H * hz, W * f, h, W * vx

    def p(s, X, Z, Y=0.0):
        """Punkt świata (X w bok, Z w głąb, Y w górę, metry) na obraz."""
        return (s.vx + s.f * X / Z, s.hz + s.f * (s.h - Y) / Z)

    def sk(s, Z):
        return s.f / Z


def fajerwerk(d, cx, cy, r, kol, n=26, sw=3):
    for i in range(n):
        a = 2 * math.pi * i / n + random.uniform(-.05, .05)
        rr = r * random.uniform(.75, 1)
        d.line([(cx + math.cos(a) * r * .18, cy + math.sin(a) * r * .18), (cx + math.cos(a) * rr, cy + math.sin(a) * rr)], fill=kol, width=sw)
        e = sw * 1.2
        d.ellipse([cx + math.cos(a) * rr - e, cy + math.sin(a) * rr - e, cx + math.cos(a) * rr + e, cy + math.sin(a) * rr + e], fill=(255, 244, 194))


def gwiazda(d, x, y, r, sw):
    """Latarnia-gwiazda: 16 promieni, co drugi krótszy."""
    for i in range(16):
        a = 2 * math.pi * i / 16
        rr = r if i % 2 == 0 else r * .58
        d.line([(x, y), (x + math.cos(a) * rr, y + math.sin(a) * rr)], fill=(255, 255, 255), width=max(1, int(sw)))
    e = r * .22
    d.ellipse([x - e, y - e, x + e, y + e], fill=(255, 251, 230))


def palma(d, x, y, s, rng):
    """Palma stojąca w (x, y), wysokość ~ 9 m * s px/m."""
    h = 9 * s
    sw = max(2, int(s * 0.12))
    pts_l = [(x - s * .22, y), (x - s * .05 - h * 0.04, y - h * .5), (x + s * .05, y - h)]
    pts_r = [(x + s * .2, y - h), (x + s * .22 - h * 0.035, y - h * .5), (x + s * .26, y)]
    d.polygon(pts_l + pts_r, fill=(107, 74, 43), outline=K, width=sw)
    for i in range(1, 9):
        yy = y - h * i / 9
        d.line([(x - s * .18, yy), (x + s * .22, yy - s * .08)], fill=(61, 40, 20), width=max(1, sw // 2))
    cx, cy = x + s * .08, y - h
    for k in range(10):
        a = math.radians(-90 + (k - 4.5) * 33 + rng.uniform(-8, 8))
        L = s * rng.uniform(3.6, 4.6)
        ex, ey = cx + math.cos(a) * L, cy + math.sin(a) * L + s * 1.3
        mx, my = cx + math.cos(a) * L * .5, cy + math.sin(a) * L * .5 - s * .9
        d.polygon([(cx, cy), (mx, my), (ex, ey), (mx + s * .25, my + s * .7)], fill=rng.choice(LISCIE), outline=K, width=sw)
    d.ellipse([cx - s * .3, cy - s * .3, cx + s * .3, cy + s * .3], fill=(90, 60, 31), outline=K, width=max(1, sw // 2))


def latarnia(d, x, y, s):
    h = 7 * s
    d.line([(x, y), (x, y - h)], fill=(233, 230, 221), width=max(2, int(s * .16)))
    gwiazda(d, x, y - h, s * 1.1, max(1, s * .09))


def hotel(d, x0, x1, ytop0, ytop1, ybot0, ybot1, rng):
    """Fasada w perspektywie (czworobok), z oknami i kolorowym gzymsem."""
    sw = 3
    d.polygon([(x0, ytop0), (x1, ytop1), (x1, ybot1), (x0, ybot0)], fill=KREM, outline=K, width=sw)
    g = 0.06
    d.polygon([(x0, ytop0), (x1, ytop1), (x1, ytop1 + (ybot1 - ytop1) * g), (x0, ytop0 + (ybot0 - ytop0) * g)], fill=rng.choice(DACHY), outline=K, width=sw)
    cols = max(2, int(abs(x1 - x0) / 26))
    rows = max(3, int((ybot0 - ytop0) / 34))
    for i in range(cols):
        u0, u1 = (i + .25) / cols, (i + .7) / cols
        for j in range(1, rows):
            if rng.random() < 0.62:
                v0, v1 = (j + .2) / rows, (j + .6) / rows
                def P(u, v):
                    yt = ytop0 + (ytop1 - ytop0) * u
                    yb = ybot0 + (ybot1 - ybot0) * u
                    return (x0 + (x1 - x0) * u, yt + (yb - yt) * v)
                d.polygon([P(u0, v0), P(u1, v0), P(u1, v1), P(u0, v1)], fill=OKNO if rng.random() < .9 else (255, 255, 255))


def car_rear(w, body=(42, 79, 156), plate="WX 1234A"):
    """Tył auta w stylu szyby: płaskie plamy koloru z grubym konturem."""
    ss = 2
    W, H = w * ss, int(w * 0.82) * ss
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    lw = max(3, int(W * 0.012))
    P = lambda x, y: (int(x * W), int(y * H))
    d.ellipse([P(0.0, 0.86), P(1.0, 1.0)], fill=(0, 0, 0, 150))
    for x in (0.08, 0.74):
        d.rounded_rectangle([P(x, 0.70), P(x + 0.18, 0.97)], radius=int(W * 0.03), fill=(14, 14, 16), outline=K, width=lw)
    d.rounded_rectangle([P(0.02, 0.42), P(0.98, 0.88)], radius=int(W * 0.07), fill=body, outline=K, width=lw)
    top = tuple(max(0, int(c * .85)) for c in body)
    d.polygon([P(0.13, 0.46), P(0.22, 0.07), P(0.78, 0.07), P(0.87, 0.46)], fill=top, outline=K, width=lw)
    d.polygon([P(0.19, 0.42), P(0.26, 0.13), P(0.74, 0.13), P(0.81, 0.42)], fill=(22, 26, 34), outline=K, width=lw)
    d.polygon([P(0.30, 0.40), P(0.40, 0.15), P(0.47, 0.15), P(0.39, 0.40)], fill=(150, 165, 190))
    d.rounded_rectangle([P(0.40, 0.085), P(0.60, 0.105)], radius=4, fill=(255, 59, 46))
    for x0, x1 in ((0.04, 0.24), (0.76, 0.96)):
        d.rounded_rectangle([P(x0, 0.47), P(x1, 0.58)], radius=int(W * 0.02), fill=(170, 20, 24), outline=K, width=lw)
        d.rounded_rectangle([P(x0 + 0.025, 0.495), P(x1 - 0.025, 0.535)], radius=int(W * 0.01), fill=(255, 92, 70))
    d.line([P(0.24, 0.60), P(0.76, 0.60)], fill=K, width=lw)
    d.rounded_rectangle([P(0.03, 0.74), P(0.97, 0.88)], radius=int(W * 0.05), fill=(26, 27, 31), outline=K, width=lw)
    d.ellipse([P(0.475, 0.50), P(0.525, 0.56)], outline=(220, 220, 225), width=lw)
    im = im.resize((w, int(w * 0.82)), Image.LANCZOS)
    pw = int(w * 0.36)
    ph = int(pw * 114 / 520)
    px, py = (w - pw) // 2, int(int(w * 0.82) * 0.625)
    im.paste(plate_img(plate, pw, ph), (px, py))
    ImageDraw.Draw(im).rectangle([px - 1, py - 1, px + pw, py + ph], outline=K, width=max(2, w // 220))
    hh = int(w * 0.82)
    return im, (px / w, py / hh, (px + pw) / w, (py + ph) / hh)


def scene(W=2304, H=1296, plate="WX 1234A", car_w=640, car_cx=0.52, car_by=0.80, body=(42, 79, 156), seed=3, extra_cars=True, **_):
    rng = random.Random(seed)
    random.seed(seed)
    k = Kamera(W, H)
    im = Image.new("RGB", (W, H), (6, 6, 9))
    d = ImageDraw.Draw(im)
    hz = k.hz
    # niebo: czarne szkło, kilka gwiazdek i fajerwerki
    for _ in range(8):
        x, y = rng.uniform(.05, .95) * W, rng.uniform(.03, .3) * H
        gwiazda(d, x, y, 7, 2)
    for (fx, fy, fr, fk) in ((.36, .14, 100, (240, 185, 58)), (.47, .07, 64, (226, 85, 63)), (.62, .12, 84, (111, 195, 224)), (.20, .08, 60, (226, 85, 63)), (.80, .06, 56, (240, 185, 58))):
        fajerwerk(d, fx * W, fy * H, fr, fk, sw=4)
    # lewa strona: morze z odbiciami, statek przy horyzoncie
    d.rectangle([0, hz - 40, W * .42, hz + 2], fill=(4, 4, 7))
    for _ in range(70):
        x, y = rng.uniform(0, W * .42), rng.uniform(hz - 36, hz)
        d.rectangle([x, y, x + rng.uniform(6, 30), y + 2], fill=(242, 196, 90))
    sx, sy, ss_ = W * .12, hz - 52, 0.9
    d.polygon([(sx, sy), (sx + 300 * ss_, sy), (sx + 270 * ss_, sy + 28 * ss_), (sx + 20 * ss_, sy + 28 * ss_)], fill=(232, 230, 224), outline=K, width=3)
    d.rectangle([sx + 40 * ss_, sy - 22 * ss_, sx + 250 * ss_, sy], fill=(239, 237, 231), outline=K, width=3)
    d.rectangle([sx + 120 * ss_, sy - 44 * ss_, sx + 150 * ss_, sy - 22 * ss_], fill=(198, 58, 44), outline=K, width=3)
    for i in range(14):
        d.rectangle([sx + (48 + i * 14) * ss_, sy - 15 * ss_, sx + (55 + i * 14) * ss_, sy - 8 * ss_], fill=OKNO)
    # prawa strona: rząd hoteli w perspektywie
    Zs = [16]
    while Zs[-1] < 260:
        Zs.append(Zs[-1] * rng.uniform(1.18, 1.32))
    for Z0, Z1 in reversed(list(zip(Zs[:-1], Zs[1:]))):
        hgt = rng.uniform(7, 15)
        xa, yb0 = k.p(13, Z0)
        xb, yb1 = k.p(13, Z1)
        _, yt0 = k.p(13, Z0, hgt)
        _, yt1 = k.p(13, Z1, hgt)
        hotel(d, xa, xb, yt0, yt1, yb0, yb1, rng)
    # teren: chodniki i jezdnia
    def quad(X0, X1, Z0, Z1, kol, ow=0):
        d.polygon([k.p(X0, Z0), k.p(X1, Z0), k.p(X1, Z1), k.p(X0, Z1)], fill=kol, outline=K if ow else None, width=ow)
    quad(-40, -6.2, 1.2, 400, (20, 20, 24))
    quad(-6.2, -4.6, 1.2, 400, (158, 160, 168))
    quad(4.6, 13, 1.2, 400, (158, 160, 168))
    quad(-4.6, 4.6, 1.2, 400, SZARY)
    for X in (-4.6, 4.6):
        d.line([k.p(X, 1.2), k.p(X, 400)], fill=(244, 242, 236), width=10)
    # zielone pasy z kwiatami po lewej
    for Z in (8, 20, 36, 60, 100):
        quad(-14, -7, Z, Z * 1.25, (47, 90, 52), 3)
        for _ in range(int(40 / (Z / 8))):
            X, ZZ = rng.uniform(-13.5, -7.5), rng.uniform(Z, Z * 1.25)
            x, y = k.p(X, ZZ)
            r = max(2, k.sk(ZZ) * .12)
            d.ellipse([x - r, y - r, x + r, y + r], fill=rng.choice([(226, 85, 63), (240, 185, 58), (244, 241, 232)]))
    # przerywana oś
    Z = 3.0
    while Z < 300:
        a, b = k.p(0, Z), k.p(0, Z * 1.12)
        w0, w1 = k.sk(Z) * .09, k.sk(Z * 1.12) * .09
        d.polygon([(a[0] - w0, a[1]), (a[0] + w0, a[1]), (b[0] + w1, b[1]), (b[0] - w1, b[1])], fill=(255, 255, 255))
        Z *= 1.35
    # palmy i latarnie, od najdalszych
    obj = [(17, "palma", -10.5), (19, "lampa", -5.6), (19, "lampa", 5.6)]
    Z = 30
    while Z < 300:
        obj.append((Z, "palma", -9))
        obj.append((Z * 1.12, "palma", -12.5))
        obj.append((Z * 0.85, "lampa", -5.4))
        obj.append((Z * 0.85, "lampa", 5.4))
        Z *= 1.28
    for Z, typ, X in sorted(obj, key=lambda o: -o[0]):
        x, y = k.p(X, Z)
        if typ == "palma":
            palma(d, x, y, k.sk(Z), rng)
        else:
            latarnia(d, x, y, k.sk(Z))
    # poświata latarni
    g = Image.new("L", (W, H), 0)
    dg = ImageDraw.Draw(g)
    for Z, typ, X in obj:
        if typ == "lampa":
            x, y = k.p(X, Z, 7)
            r = k.sk(Z) * 1.8
            dg.ellipse([x - r, y - r, x + r, y + r], fill=45)
    g = g.filter(ImageFilter.GaussianBlur(10))
    im.paste((255, 240, 200), (0, 0), g)
    boxes = []
    if extra_cars:
        for X, Z, kol, pl in ((3.1, 44, (231, 181, 50), "WX 11111"), (-3.1, 30, (242, 240, 232), "WX 50000")):
            cw = int(1.8 * k.sk(Z))
            c, _ = car_rear(cw, kol, pl)
            x, y = k.p(X, Z)
            x0, y0 = int(x - cw / 2), int(y - c.size[1])
            im.paste(c, (x0, y0), c)
            boxes.append(("car", (x0 / W, y0 / H, (x0 + cw) / W, (y0 + c.size[1]) / H), None))
    car, pb = car_rear(car_w, body, plate)
    ch = car.size[1]
    x, y = int(W * car_cx - car_w / 2), int(H * car_by - ch)
    im.paste(car, (x, y), car)
    car_box = (x / W, y / H, (x + car_w) / W, (y + ch) / H)
    plate_box = ((x + pb[0] * car_w) / W, (y + pb[1] * ch) / H, (x + pb[2] * car_w) / W, (y + pb[3] * ch) / H)
    boxes.append(("car", car_box, None))
    arr = np.asarray(im).astype(np.float32)
    arr += np.random.default_rng(seed).normal(0, 2.0, arr.shape)
    return np.clip(arr, 0, 255).astype(np.uint8), car_box, plate_box, boxes


if __name__ == "__main__":
    import cv2, sys
    a, cb, pb, _ = scene()
    cv2.imwrite(sys.argv[1] if len(sys.argv) > 1 else "scene_szafa_test.jpg", cv2.cvtColor(a, cv2.COLOR_RGB2BGR))
    print(cb, pb)
