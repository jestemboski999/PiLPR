# -*- coding: utf-8 -*-
"""Kalibracja prędkości z kamery (kam_K) i zgodność znaku radar/kamera.

Wejście: katalogi radar/<data>/ z plikami kalibracja_<data>.csv (jeden wiersz
na zakończony track w trybie RADAR: czas;tablica;track;v_kamera;v_radar_max3s;
prog;tryb) i probki_<data>.csv (WSZYSTKIE próbki radaru: czas z ms;v ze znakiem).

Dlaczego nie wprost z kolumny v_radar_max3s: to MAKSIMUM z 3 s, więc gdy w kadrze
są dwa auta, bierze szybsze z nich (np. WX1234S: kamera 21, radar 64). Tu dla
każdego tracku bierzemy próbki radaru z okna [t−2,5 s, t+0,5 s] i uznajemy parę
tylko, gdy próbki tworzą JEDNO skupisko (rozstęp międzykwartylowy < 6 km/h) —
wtedy mediana próbek to prędkość tego auta. K_nowe = K × mediana(v_radar/|v_kam|).

Użycie:  python analiza_kalibracji_radaru.py <katalog_radar> [--K 4640]
         (katalog_radar = np. ...\logs\radar albo \media\lpr\radar)
"""
import sys, glob, os, csv, statistics as st
from datetime import datetime, timedelta

K = 4640.0
args = [a for a in sys.argv[1:] if not a.startswith("--")]
if "--K" in sys.argv:
    K = float(sys.argv[sys.argv.index("--K") + 1])
root = args[0] if args else "."

probki = []
for f in sorted(glob.glob(os.path.join(root, "*", "probki_*.csv"))):
    with open(f, encoding="utf-8") as fh:
        for row in csv.reader(fh, delimiter=";"):
            if len(row) < 2 or not row[0][:1].isdigit():
                continue
            try:
                probki.append((datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S.%f"), float(row[1])))
            except ValueError:
                pass
probki.sort()
ts = [t for t, _ in probki]
import bisect

tracki = []
for f in sorted(glob.glob(os.path.join(root, "*", "kalibracja_*.csv"))):
    with open(f, encoding="utf-8") as fh:
        for row in csv.reader(fh, delimiter=";"):
            if len(row) < 5 or not row[0][:1].isdigit():
                continue
            try:
                t = datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")
                vk = float(row[3]) if row[3] else None
                vr3 = float(row[4]) if row[4] else None
            except ValueError:
                continue
            tracki.append((t, row[1], row[2], vk, vr3))

print(f"próbek radaru: {len(probki)}, tracków: {len(tracki)}, K wejściowe: {K:.0f}")
pary, znaki = [], []
print(f"{'czas':19} {'tablica':9} {'v_kam':>6} {'radar_med':>9} {'n':>3} {'IQR':>5} {'znak_r':>6} ocena")
for t, tabl, tid, vk, vr3 in tracki:
    if vk is None or abs(vk) < 12:
        continue
    a, b = t - timedelta(seconds=2.5), t + timedelta(seconds=0.5)
    i0, i1 = bisect.bisect_left(ts, a), bisect.bisect_right(ts, b)
    okno = [v for _, v in probki[i0:i1]]
    if len(okno) < 4:
        print(f"{t:%Y-%m-%d %H:%M:%S} {tabl:9} {vk:6.1f} {'-':>9} {len(okno):3} {'-':>5} {'-':>6} za mało próbek")
        continue
    absv = sorted(abs(v) for v in okno)
    q1, q3 = absv[len(absv) // 4], absv[(3 * len(absv)) // 4]
    med = st.median(absv)
    plus = sum(1 for v in okno if v > 0)
    znak = "+" if plus > len(okno) / 2 else "-"
    iqr = q3 - q1
    ok = iqr < 6.0 and med >= 12
    uw = "OK" if ok else "rozrzut (2 auta?)"
    if ok:
        pary.append((abs(vk), med, tabl))
        znaki.append((1 if vk > 0 else -1, 1 if znak == "+" else -1, tabl))
    print(f"{t:%Y-%m-%d %H:%M:%S} {tabl:9} {vk:6.1f} {med:9.1f} {len(okno):3} {iqr:5.1f} {znak:>6} {uw}")

if pary:
    ratios = sorted(r / k for k, r, _ in pary)
    med_r = st.median(ratios)
    # regresja przez zero: v_radar = a·|v_kam|
    sxx = sum(k * k for k, _, _ in pary)
    a = sum(k * r for k, r, _ in pary) / sxx
    print()
    print(f"par OK: {len(pary)}   mediana v_radar/|v_kam| = {med_r:.3f}   "
          f"regresja przez zero a = {a:.3f}   (Q1 {ratios[len(ratios)//4]:.3f}, Q3 {ratios[3*len(ratios)//4]:.3f})")
    print(f"ZALECANE kam_K = {K * med_r:.0f}  (z mediany)   /  {K * a:.0f} (z regresji)")
    resid = [r - med_r * k for k, r, _ in pary]
    print(f"po korekcie: błąd medianowy {st.median(abs(x) for x in resid):.1f} km/h, "
          f"maks {max(abs(x) for x in resid):.1f} km/h")
if znaki:
    zg = sum(1 for sk, sr, _ in znaki if sk == sr)
    print(f"znak kamery == znak radaru (+ = zbliża wg karty) w {zg}/{len(znaki)} parach; "
          f"kamera '+' (zbliża) w {sum(1 for sk,_,_ in znaki if sk>0)}, radar '+' w {sum(1 for _,sr,_ in znaki if sr>0)}")
    print("  → jeśli zgodnych jest ~0 %, radar i kamera mają ODWROTNY znak (odwrócić interpretację);"
          " ~50 % = znak radaru nie dotyczy tego auta (dwa kierunki ruchu w wiązce).")
