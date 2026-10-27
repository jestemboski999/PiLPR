import os as _os
REPO = _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..", ".."))
import sys, json, re, functools, cv2, numpy as np
sys.path.insert(0, REPO + "/pi/narzedzia_testowe")
import lpr_stub as S
from scene import scene
from http.server import ThreadingHTTPServer
BODIES=[(52,70,98),(150,40,40),(200,200,205),(40,40,44),(70,110,80),(180,150,90)]
@functools.lru_cache(maxsize=64)
def _sc(txt):
    i=abs(hash(txt))%len(BODIES)
    t=txt if txt!="UNKNOWN" else "WX ????"
    t=(t[:2]+" "+t[2:]) if len(t)>2 and " " not in t else t
    rgb,cb,pb,_=scene(plate=t,car_cx=0.6,body=BODIES[i],seed=3+i)
    return cv2.cvtColor(rgb,cv2.COLOR_RGB2BGR),pb
def full_jpg(txt):
    img,pb=_sc(txt); S.BBOX[:]=list(pb)
    return cv2.imencode(".jpg",cv2.resize(img,(1280,720)),[cv2.IMWRITE_JPEG_QUALITY,82])[1].tobytes()
def crop_jpg(txt,blur=0):
    img,pb=_sc(txt); H,W=img.shape[:2]
    x1,y1,x2,y2=pb; dw=(x2-x1)*.2; dh=(y2-y1)*.16
    c=img[int((y1-dh)*H):int((y2+dh)*H),int((x1-dw)*W):int((x2+dw)*W)]
    if blur: c=cv2.GaussianBlur(c,(2*blur+1,2*blur+1),0)
    return cv2.imencode(".jpg",c)[1].tobytes()
S.full_jpg=full_jpg; S.crop_jpg=crop_jpg
_sc("WA1234A"); S.BBOX[:]=list(_sc("WA1234A")[1])
RADAR=[(63,"WX9021C",64,1),(58,"KX8821C",55,1),(71,None,None,None),(52,"SX4451E",49,1),(66,"PX55XYZ",70,1)]
orig=S.H.do_GET
def do_GET(self):
    p=self.path.split("?")[0]
    if p=="/api/radar":
        z=[]
        for i,(v,t,vk,pw) in enumerate(RADAR):
            nm=f"r{i}.jpg"
            z.append({"id":40-i,"czas":f"2026-10-02 17:{41-i*7:02d}:0{i}","v":v,"kierunek":"+","probek":9+i,
                      "tablica":t,"ocr":0.95 if t else None,"v_kam":vk,"pewne":pw,"pojazd":1,"uwagi":None,"debug":None,
                      "zdjecie":f"/api/radar/plik/2026-10-02/{nm}","mini":f"/api/radar/plik/2026-10-02/{nm}?mini=1",
                      "crop":None,"klip":None})
        cfg={"tryb":True,"port":"/dev/ttyAMA0","prog_kmh":40.0,"kierunek":"oba","min_probek":3,"okno_s":15.0,
             "klip_przed_s":4.0,"klip_po_s":6.0,"zapis_zwykly":False,"glos":True,"kam_K":4640.0,"kam_tol":0.35}
        st={"ok":True,"blad":"","v":0.0,"t":0,"probek":18234,"zdarzen":5,"ostatnie":None,"otwarte":False,"swieza":False}
        return self._send(json.dumps({"cfg":cfg,"stan":st,"zdarzenia":z}).encode())
    m=re.match(r"^/api/radar/plik/[\d-]+/r(\d)\.jpg$",p)
    if m:
        img=cv2.imread(REPO + "/docs/images/radar_zdjecie.jpg")
        if "mini=1" in self.path: img=cv2.resize(img,(480,270))
        return self._send(cv2.imencode(".jpg",img)[1].tobytes(),"image/jpeg")
    if p in ("/stream","/api/snapshot","/api/focus-view"):
        img=cv2.imread(REPO + "/docs/images/ekran_jazda.png")[40:440]
        return self._send(cv2.imencode(".jpg",img)[1].tobytes(),"image/jpeg")
    if p=="/api/watchlist":
        zn=[("WX5678K","Ania i Tomek"),("WX1020A","Tata · Octavia"),("KX4410M","Kuba"),("WX7733P","Babcia")]
        po=[("WX2730A","Wujek Marek"),("SX2A000","Kasia · Kia Ceed"),("BX416AA","Dziadkowie")]
        return self._send(json.dumps({"ok":True,"tts_all":False,"tts_brand":True,"spell_mode":"letters","tts_gain":1.0,
          "tts_gains":{"alert":2.4,"plate":1.4,"system":1.6},
          "spell_example":"wu iks, pięć sześć siedem osiem ka",
          "groups":[{"gid":"znajomi","name":"Znajomi","alert":"Mijasz znajomych. {tablica}.","enabled":True,"color":"green","count":len(zn),"nota":True},
                    {"gid":"rodzina","name":"Rodzina","alert":"Rodzina na drodze. {tablica}.","enabled":True,"color":"blue","count":len(po),"nota":True}],
          "brand_examples":[{"gid":"znajomi","plate":"WX5678K","note":"Ania i Tomek","text":"Mijasz znajomych. Wu iks, pięć sześć siedem osiem ka."}],
          "plates":[{"plate":t,"group":"znajomi","note":n} for t,n in zn]+[{"plate":t,"group":"rodzina","note":n} for t,n in po]}).encode())
    if p=="/api/usb":
        return self._send(json.dumps({"ok":True,"mounted":True,"free_gb":23.4,"total_gb":58.6}).encode())
    return orig(self)
S.H.do_GET=do_GET
ThreadingHTTPServer(("127.0.0.1",8765),S.H).serve_forever()
