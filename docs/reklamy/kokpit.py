"""Ujęcie „z kabiny”: ekran PiLPR przy lusterku, za szybą ta sama scena,
którą widzi kamera. Wszystko rysowane, w kolorach tapety PiCamera."""
import numpy as np, cv2
from PIL import Image, ImageDraw, ImageFilter
import vhs


def _tlo_szyba(rgb, W, H, zoom=1.0, dx=0.0, dy=0.0, blur=2.2, horyzont=0.40):
    """Widok przez szybę: scena z kamery na szerokość ujęcia (z zapasem),
    niebo i jezdnia dociągnięte kolorem krawędzi."""
    sh, sw = rgb.shape[:2]
    sk = W * 1.25 * zoom / sw
    nw, nh = int(sw * sk), int(sh * sk)
    big = cv2.resize(rgb, (nw, nh), interpolation=cv2.INTER_AREA if sk < 1 else cv2.INTER_LINEAR)
    pad = max(H, nh) * 2
    top = np.repeat(big[:1], pad, axis=0); bot = np.repeat(big[-1:], pad, axis=0)
    full = np.concatenate([top, big, bot], axis=0)
    # horyzont sceny (0.47 wys.) ma wypaść na `horyzont` ujęcia
    y0 = int(pad + nh * 0.47 - H * horyzont + dy * H)
    x0 = int((nw - W) / 2 + dx * W)
    x0 = max(0, min(nw - W, x0))
    im = np.ascontiguousarray(full[y0:y0 + H, x0:x0 + W])
    if blur:
        im = cv2.GaussianBlur(im, (0, 0), blur)
    return Image.fromarray(im)


def _ekran(dev_bgr, szer):
    """Ekran urządzenia w ramce (RGBA), 800×480 → szer px."""
    dev = Image.fromarray(cv2.cvtColor(dev_bgr, cv2.COLOR_BGR2RGB))
    r = szer / 800
    ram = int(22 * r)
    w, h = szer + 2 * ram, int(480 * r) + 2 * ram + int(10 * r)
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([0, 0, w - 1, h - 1], radius=int(26 * r), fill=(14, 11, 16, 255),
                        outline=(70, 60, 78, 255), width=max(2, int(3 * r)))
    im.paste(dev.resize((szer, int(480 * r)), Image.LANCZOS), (ram, ram))
    # odblask na szkle
    gl = Image.new("L", (w, h), 0)
    ImageDraw.Draw(gl).polygon([(int(w * 0.55), 0), (int(w * 0.75), 0), (int(w * 0.45), h), (int(w * 0.25), h)], fill=20)
    gl = gl.filter(ImageFilter.GaussianBlur(int(18 * r)))
    im.paste(Image.new("RGBA", (w, h), (255, 240, 250, 255)), (0, 0), gl)
    return im, ram


def _perspektywa(rgba, kat=0.10, skos=0.03):
    """Lekkie obrócenie ekranu w stronę kierowcy (prawa krawędź bliżej)."""
    a = np.array(rgba)
    h, w = a.shape[:2]
    src = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
    dst = np.float32([[0, h * kat], [w, 0], [w, h], [0, h * (1 - kat)]]) + np.float32([[0, h * skos]] * 4)
    M = cv2.getPerspectiveTransform(src, dst)
    out = cv2.warpPerspective(a, M, (w, int(h * (1 + skos))), flags=cv2.INTER_LINEAR,
                              borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0, 0))
    return Image.fromarray(out, "RGBA")


def kokpit(scena_rgb, dev_bgr, W=1080, H=1920, ekran_x=0.5, ekran_y=0.66, ekran_szer=0.74,
           zoom=1.0, dx=0.0, dy=0.0, blur=2.2, kat=0.08, seed=1, swiatlo=1.0, horyzont=None):
    """Zwraca PIL RGB W×H: kabina nocą, ekran urządzenia na środku szyby.

    scena_rgb: RGB z kit.scena()[0] (to, co widać przez szybę),
    dev_bgr:   klatka 800×480 z kit.device_frame()/kit.menu().
    zoom/dx/dy pozwalają animować najazd kamery."""
    rng = np.random.default_rng(seed)
    if horyzont is None:
        horyzont = 0.36 if H > W else 0.30
    szyba = _tlo_szyba(scena_rgb, W, H, zoom, dx, dy, blur, horyzont)
    im = szyba.convert("RGB")
    # lekkie przyciemnienie i fiolet szyby
    a = np.asarray(im).astype(np.float32) * 0.78 + np.array([10, 4, 16], np.float32)
    im = Image.fromarray(np.clip(a, 0, 255).astype(np.uint8))

    # krople / refleksy na szybie
    ref = Image.new("L", (W, H), 0); dr = ImageDraw.Draw(ref)
    for _ in range(40):
        x, y, r = rng.uniform(0, W), rng.uniform(0, H * 0.6), rng.uniform(2, 6) * W / 1080
        dr.ellipse([x - r, y - r, x + r, y + r], fill=int(rng.uniform(15, 40)))
    im.paste(Image.new("RGB", (W, H), (230, 210, 230)), (0, 0), ref.filter(ImageFilter.GaussianBlur(1.5)))

    sil = Image.new("RGBA", (W, H), (0, 0, 0, 0)); d = ImageDraw.Draw(sil)
    ciem = (9, 7, 11, 255)
    # słupki A
    d.polygon([(0, 0), (int(W * 0.10), 0), (int(W * 0.02), int(H * 0.80)), (0, int(H * 0.82))], fill=ciem)
    d.polygon([(W, 0), (int(W * 0.90), 0), (int(W * 0.98), int(H * 0.80)), (W, int(H * 0.82))], fill=ciem)
    # podsufitka
    d.rectangle([0, 0, W, int(H * 0.035)], fill=ciem)
    d.ellipse([int(-W * 0.2), int(-H * 0.06), int(W * 1.2), int(H * 0.07)], fill=ciem)
    # deska rozdzielcza
    yb = int(H * 0.78)
    d.polygon([(0, yb + int(H * 0.03)), (int(W * 0.25), yb), (int(W * 0.75), yb),
               (W, yb + int(H * 0.03)), (W, H), (0, H)], fill=(12, 9, 15, 255))
    # górna krawędź deski podświetlona fioletowo (odbicie ekranu)
    d.line([(0, yb + int(H * 0.03)), (int(W * 0.25), yb), (int(W * 0.75), yb), (W, yb + int(H * 0.03))],
           fill=(70, 50, 86, 255), width=max(2, W // 360))
    # kierownica (z lewej, ruch prawostronny)
    cx, cy, R = int(W * 0.16), int(yb + H * 0.30), int(min(W, H) * 0.46)
    d.ellipse([cx - R, cy - R, cx + R, cy + R], outline=(16, 12, 19, 255), width=int(W * 0.06))
    d.ellipse([cx - R + int(W * 0.03), cy - R + int(W * 0.03), cx + R - int(W * 0.03), cy + R - int(W * 0.03)],
              outline=(40, 30, 48, 255), width=max(2, W // 400))
    # nawiewy i zegary (subtelnie)
    for k in range(3):
        x = int(W * (0.62 + k * 0.1)); y = int(H * 0.87)
        d.rounded_rectangle([x, y, x + int(W * 0.07), y + int(H * 0.025)], radius=8, outline=(46, 36, 56, 255), width=2)
    # lusterko wsteczne + noga
    mx = int(W * 0.5)
    d.rectangle([mx - W // 90, int(H * 0.03), mx + W // 90, int(H * 0.10)], fill=ciem)
    d.rounded_rectangle([mx - int(W * 0.22), int(H * 0.085), mx + int(W * 0.22), int(H * 0.145)],
                        radius=int(W * 0.03), fill=(16, 12, 20, 255), outline=(52, 42, 60, 255), width=2)
    # kamera IMX708 przy szybie
    kx, ky = mx + int(W * 0.25), int(H * 0.09)
    d.rounded_rectangle([kx - W // 30, ky - W // 30, kx + W // 30, ky + W // 30], radius=6, fill=(20, 16, 24, 255))
    d.ellipse([kx - W // 70, ky - W // 70, kx + W // 70, ky + W // 70], fill=(36, 40, 70, 255), outline=(90, 80, 110, 255))
    d.ellipse([kx - W // 300, ky - W // 300 - W // 200, kx + W // 300, ky - W // 200 + W // 300], fill=(200, 200, 240, 255))
    im.paste(sil, (0, 0), sil)

    # ekran urządzenia
    szer = int(W * ekran_szer * 800 / (800 + 44))
    ek, ram = _ekran(dev_bgr, szer)
    ek = _perspektywa(ek, kat)
    ex = int(W * ekran_x - ek.width / 2); ey = int(H * ekran_y - ek.height / 2)
    # poświata ekranu na kabinę
    gl = Image.new("L", (W, H), 0)
    ImageDraw.Draw(gl).rounded_rectangle([ex - 20, ey - 20, ex + ek.width + 20, ey + ek.height + 20], radius=40, fill=int(110 * swiatlo))
    gl = gl.filter(ImageFilter.GaussianBlur(W * 0.06))
    im.paste(Image.new("RGB", (W, H), (120, 80, 150)), (0, 0), gl)
    # uchwyt (ramię do deski)
    d = ImageDraw.Draw(im)
    d.polygon([(ex + ek.width * 0.42, ey + ek.height * 0.9), (ex + ek.width * 0.58, ey + ek.height * 0.9),
               (int(W * 0.56), yb + 6), (int(W * 0.44), yb + 6)], fill=(18, 14, 22))
    # cień pod ekranem
    sh = Image.new("L", (W, H), 0)
    ImageDraw.Draw(sh).rounded_rectangle([ex + 10, ey + 24, ex + ek.width + 10, ey + ek.height + 30], radius=30, fill=170)
    im.paste((0, 0, 0), (0, 0), sh.filter(ImageFilter.GaussianBlur(18)))
    im.paste(ek, (ex, ey), ek)

    # winieta + linie VHS
    yy, xx = np.mgrid[0:H, 0:W]
    r = np.sqrt(((xx - W / 2) / (W * 0.75)) ** 2 + ((yy - H / 2) / (H * 0.7)) ** 2)
    a = np.asarray(im).astype(np.float32) * np.clip(1.15 - r * 0.55, 0.35, 1)[..., None]
    im = Image.fromarray(np.clip(a, 0, 255).astype(np.uint8))
    return vhs.linie(im, okres=max(3, W // 360), sila=0.12)


def ekran_box(W=1080, H=1920, ekran_x=0.5, ekran_y=0.66, ekran_szer=0.74):
    """Przybliżone położenie obrazu ekranu (x0, y0, x1, y1) w ujęciu."""
    szer = int(W * ekran_szer * 800 / 844)
    r = szer / 800
    w, h = szer + 44 * r, 480 * r + 54 * r
    return (W * ekran_x - w / 2, H * ekran_y - h / 2, W * ekran_x + w / 2, H * ekran_y + h / 2)
