from PIL import Image, ImageDraw, ImageFilter, ImageFont
import os
REPO=os.path.abspath(os.path.join(os.path.dirname(__file__),"..",".."))
I=REPO+"/docs/images/"
W,H=2400,1200
bg=Image.new("RGB",(W,H),(18,14,22)); d=ImageDraw.Draw(bg)
# delikatna siatka punktów jak na ekranie startowym
for x in range(0,W,40):
    for y in range(0,H,40): d.point((x,y),fill=(40,34,46))
# poświata
glow=Image.new("L",(W,H),0); ImageDraw.Draw(glow).ellipse([1050,80,2500,1150],fill=90)
glow=glow.filter(ImageFilter.GaussianBlur(220))
bg=Image.composite(Image.new("RGB",(W,H),(60,52,96)),bg,glow); d=ImageDraw.Draw(bg)
def shadow(box,r,blur=40,off=(0,30),alpha=170):
    sh=Image.new("L",(W,H),0); ImageDraw.Draw(sh).rounded_rectangle([box[0]+off[0],box[1]+off[1],box[2]+off[0],box[3]+off[1]],radius=r,fill=alpha)
    return sh.filter(ImageFilter.GaussianBlur(blur))
# urządzenie: ekran 800x480 w ramce
scr=Image.open(I+"ekran_jazda.png").convert("RGB").resize((1120,672),Image.LANCZOS)
bx,by=1080,230; pad=34
box=(bx-pad,by-pad,bx+1120+pad,by+672+pad)
bg.paste((0,0,0),(0,0),shadow(box,40))
d=ImageDraw.Draw(bg)
d.rounded_rectangle(box,radius=40,fill=(28,28,32),outline=(70,70,78),width=3)
d.rounded_rectangle((box[0]+8,box[1]+8,box[2]-8,box[3]-8),radius=34,outline=(44,44,50),width=2)
bg.paste(scr,(bx,by))
# telefon
ph=Image.open(I+"www_telefon.png").convert("RGB")
ph=ph.crop((0,0,780,1560)).resize((360,720),Image.LANCZOS)
px,py=1940,420; pp=16
pbox=(px-pp,py-pp,px+360+pp,py+720+pp)
bg.paste((0,0,0),(0,0),shadow(pbox,56,30,(0,24),200)); d=ImageDraw.Draw(bg)
d.rounded_rectangle(pbox,radius=56,fill=(14,14,16),outline=(80,80,90),width=3)
m=Image.new("L",(360,720),0); ImageDraw.Draw(m).rounded_rectangle([0,0,359,719],radius=42,fill=255)
bg.paste(ph,(px,py),m)
d.rounded_rectangle((px+130,py+10,px+230,py+30),radius=10,fill=(14,14,16))
# logo i tekst
logo=Image.open(REPO+"/pi/local_resources/lpr_splash_pilpr_pixel.png").convert("RGB")
lw,lh=logo.size; logo=logo.crop((int(lw*0.29),int(lh*0.32),int(lw*0.73),int(lh*0.54)))
logo=logo.resize((760,int(760*logo.height/logo.width)),Image.NEAREST)
lm=Image.eval(logo.convert("L"),lambda v:255 if v>70 else 0)
bg.paste(logo,(120,300),lm)
d=ImageDraw.Draw(bg)
F="/home/admin/hailo-apps/local_resources/web/VT323-Regular.ttf"
f1=ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",44)
f2=ImageFont.truetype(F,46)
y=300+logo.height+70
import sys
LANG=sys.argv[1] if len(sys.argv)>1 else "pl"
for line in (("Wideorejestrator, który czyta","tablice rejestracyjne.") if LANG=="pl" else ("A dashcam that reads","licence plates.")):
    d.text((124,y),line,font=f1,fill=(242,236,225)); y+=60
y+=40
for line in ("RASPBERRY PI 5  ·  HAILO-10H  ·  IMX708 HDR",("RADAR 24 GHZ  ·  WSZYSTKO OFFLINE" if LANG=="pl" else "24 GHZ RADAR  ·  FULLY OFFLINE")):
    d.text((126,y),line,font=f2,fill=(150,140,170)); y+=52
bg=bg.resize((1600,800),Image.LANCZOS)
bg.save(I+("hero.png" if LANG=="pl" else "hero_en.png"),optimize=True); print(bg.size)
