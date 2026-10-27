import os as _os
REPO = _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..", ".."))
import numpy as np, matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager
plt.rcParams.update({"font.family":"DejaVu Sans","font.size":11,"axes.edgecolor":"#9aa3b5","axes.labelcolor":"#4a5366",
  "xtick.color":"#4a5366","ytick.color":"#4a5366","axes.spines.top":False,"axes.spines.right":False})
rng=np.random.default_rng(4)
# --- przebieg zdarzenia radaru ---
t=np.arange(0,6.0,0.05)
v=np.where((t>0.8)&(t<3.6), 58+6*np.sin((t-0.8)/2.8*np.pi)+rng.normal(0,1.2,t.size), rng.normal(0,0.8,t.size))
v=np.where((t>0.8)&(t<3.6), v, np.nan)
spike_i=np.argmin(abs(t-3.0)); v[spike_i]=118
fig,ax=plt.subplots(figsize=(11,4.2),dpi=150)
fig.patch.set_facecolor("#f7f8fa"); ax.set_facecolor("#ffffff")
ax.axhline(50,color="#d14b52",lw=1.2,ls="--"); ax.text(5.95,51.5,"próg 50 km/h",ha="right",color="#d14b52",fontsize=10)
ok=~np.isnan(v); good=ok.copy(); good[spike_i]=False
ax.scatter(t[good],v[good],s=14,color="#4a6fd0",label="próbki radaru (V+)",zorder=3)
ax.scatter([t[spike_i]],[v[spike_i]],s=60,marker="x",color="#d14b52",zorder=4,label="szpilka odrzucona (harmoniczna)")
top5=np.sort(v[good])[-5:]; med=np.median(top5)
ax.axhline(med,color="#2e7d4f",lw=1.4); ax.text(0.05,med+1.5,f"zapis: mediana 5 najwyższych = {med:.0f} km/h",color="#2e7d4f",fontsize=10)
ax.axvspan(0.8,0.95,color="#eab295",alpha=.35); ax.text(0.83,8,"otwarcie\n(3 próbki ≥ próg)",fontsize=9,color="#7a4a30")
ax.axvspan(3.6,5.1,color="#c4cfea",alpha=.35); ax.text(3.65,8,"radar ucichł 1,5 s →\nzdarzenie zamknięte",fontsize=9,color="#34446e")
ax.axvspan(5.1,6.0,color="#a9d1ad",alpha=.35); ax.text(5.13,8,"czekanie na\ntablicę z kamery",fontsize=9,color="#2e5a36")
ax.set_xlim(0,6); ax.set_ylim(0,125); ax.set_xlabel("czas [s]"); ax.set_ylabel("prędkość [km/h]")
ax.legend(loc="upper left",frameon=False,fontsize=10)
fig.tight_layout(); fig.savefig(REPO + "/docs/images/radar_przebieg.png",facecolor=fig.get_facecolor())

# --- prędkość z kamery ---
fig,axs=plt.subplots(1,2,figsize=(11,3.8),dpi=150); fig.patch.set_facecolor("#f7f8fa")
tt=np.linspace(0,1.6,26); vtrue=55/3.6; d=60-vtrue*tt; K=4640; px=K/d+rng.normal(0,0.6,tt.size)
for a in axs: a.set_facecolor("#ffffff")
axs[0].plot(tt,px,"o",ms=4,color="#4a6fd0"); axs[0].set_xlabel("czas [s]"); axs[0].set_ylabel("szerokość tablicy [px]")
axs[0].set_title("Auto się zbliża: tablica rośnie",fontsize=11,color="#1c2230",loc="left")
dd=K/px; sl,ic=np.polyfit(tt,dd,1)
axs[1].plot(tt,dd,"o",ms=4,color="#4a6fd0"); axs[1].plot(tt,sl*tt+ic,color="#2e7d4f",lw=1.6)
axs[1].set_xlabel("czas [s]"); axs[1].set_ylabel("odległość d = K / px [m]")
axs[1].set_title(f"Regresja d(t): {-sl*3.6:.0f} km/h (radar: 55 km/h)",fontsize=11,color="#1c2230",loc="left")
fig.tight_layout(); fig.savefig(REPO + "/docs/images/predkosc_z_kamery.png",facecolor=fig.get_facecolor())
print("ok")
