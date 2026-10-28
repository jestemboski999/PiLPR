"""Wykresy działania radaru na danych symulowanych, w stylu tapety PiCamera."""
import io
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager
from PIL import Image
import vhs

font_manager.fontManager.addfont(vhs.VT323)
T, P, R = vhs.hexc(vhs.TEKST), vhs.hexc(vhs.PRZYGASZONY), vhs.hexc(vhs.RAMKA)
plt.rcParams.update({"font.family": "VT323", "font.size": 17, "axes.edgecolor": R, "axes.labelcolor": P,
                     "xtick.color": P, "ytick.color": P, "axes.spines.top": False, "axes.spines.right": False,
                     "axes.facecolor": "none", "figure.facecolor": "none", "axes.linewidth": 1.4,
                     "xtick.major.width": 1.4, "ytick.major.width": 1.4})
PROBKI, ODRZUT, PROG, ZAPIS = vhs.hexc(vhs.CYJAN), vhs.hexc(vhs.MAGENTA), vhs.hexc(vhs.CZERWONY), vhs.hexc(vhs.ZIELONY)
rng = np.random.default_rng(4)


def zapisz(fig, nazwa, seed):
    buf = io.BytesIO()
    fig.savefig(buf, format="png", transparent=True)
    plt.close(fig)
    wyk = Image.open(io.BytesIO(buf.getvalue())).convert("RGBA")
    tlo = vhs.tlo(wyk.width, wyk.height, seed=seed, smugi=3, gwiazdki=0)
    tlo.paste(wyk, (0, 0), wyk)
    vhs.linie(tlo, 4, 0.18).save(vhs.REPO + "/docs/images/" + nazwa, optimize=True)


# --- przebieg zdarzenia radaru ---
t = np.arange(0, 6.0, 0.05)
v = np.where((t > 0.8) & (t < 3.6), 58 + 6 * np.sin((t - 0.8) / 2.8 * np.pi) + rng.normal(0, 1.2, t.size), np.nan)
spike_i = np.argmin(abs(t - 3.0))
v[spike_i] = 118
fig, ax = plt.subplots(figsize=(11, 4.2), dpi=150)
ax.axvspan(0.8, 0.95, color=vhs.hexc(vhs.ZLOTY), alpha=.22)
ax.text(0.83, 7, "OTWARCIE\n3 PRÓBKI ≥ PRÓG", fontsize=15, color=vhs.hexc(vhs.ZLOTY))
ax.axvspan(3.6, 5.1, color=vhs.hexc(vhs.NIEBIESKI), alpha=.25)
ax.text(3.67, 7, "RADAR UCICHŁ NA 1,5 S\nZDARZENIE ZAMKNIĘTE", fontsize=15, color=vhs.hexc(vhs.CYJAN))
ax.axvspan(5.1, 6.0, color=ZAPIS, alpha=.12)
ax.text(5.16, 7, "CZEKANIE NA\nTABLICĘ Z KAMERY", fontsize=15, color=ZAPIS)
ax.axhline(50, color=PROG, lw=1.4, ls=(0, (4, 3)))
ax.text(5.95, 51.5, "PRÓG 50 KM/H", ha="right", color=PROG, fontsize=16)
good = ~np.isnan(v)
good[spike_i] = False
ax.scatter(t[good], v[good], s=22, marker="s", color=PROBKI, label="próbki radaru (V+)", zorder=3, linewidths=0)
ax.scatter([t[spike_i]], [v[spike_i]], s=90, marker="x", color=ODRZUT, zorder=4, linewidths=2.2, label="szpilka odrzucona (harmoniczna)")
med = np.median(np.sort(v[good])[-5:])
ax.axhline(med, color=ZAPIS, lw=1.8)
ax.text(0.05, med + 2, f"ZAPIS: MEDIANA 5 NAJWYŻSZYCH = {med:.0f} KM/H", color=ZAPIS, fontsize=16)
ax.set_xlim(0, 6)
ax.set_ylim(0, 125)
ax.set_xlabel("czas [s]")
ax.set_ylabel("prędkość [km/h]")
leg = ax.legend(loc="upper left", frameon=False, fontsize=16)
for tx in leg.get_texts():
    tx.set_color(T)
fig.tight_layout()
zapisz(fig, "radar_przebieg.png", 11)

# --- prędkość z kamery ---
fig, axs = plt.subplots(1, 2, figsize=(11, 3.8), dpi=150)
tt = np.linspace(0, 1.6, 26)
d = 60 - 55 / 3.6 * tt
K = 4640
px = K / d + rng.normal(0, 0.6, tt.size)
axs[0].plot(tt, px, "s", ms=5, color=PROBKI, mew=0)
axs[0].set_xlabel("czas [s]")
axs[0].set_ylabel("szerokość tablicy [px]")
axs[0].set_title("AUTO SIĘ ZBLIŻA, TABLICA ROŚNIE", fontsize=18, color=T, loc="left")
dd = K / px
sl, ic = np.polyfit(tt, dd, 1)
axs[1].plot(tt, dd, "s", ms=5, color=PROBKI, mew=0)
axs[1].plot(tt, sl * tt + ic, color=ZAPIS, lw=1.8)
axs[1].set_xlabel("czas [s]")
axs[1].set_ylabel("odległość d = K / px [m]")
axs[1].set_title(f"REGRESJA d(t): {-sl * 3.6:.0f} KM/H (RADAR: 55 KM/H)", fontsize=18, color=T, loc="left")
fig.tight_layout()
zapisz(fig, "predkosc_z_kamery.png", 12)
print("ok")
