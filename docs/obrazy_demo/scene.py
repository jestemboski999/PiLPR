"""Syntetyczna scena drogowa (ilustracja) z fikcyjną tablicą. Bez prawdziwych danych."""
import numpy as np, cv2, random
from PIL import Image, ImageDraw, ImageFont, ImageFilter
BOLD="/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

def plate_img(text, w=520, h=114):
    ss=4; W,H=w*ss,h*ss; u=H/456
    im=Image.new("RGB",(W,H),(246,246,242)); d=ImageDraw.Draw(im)
    d.rounded_rectangle([0,0,W-1,H-1],radius=int(40*u),outline=(25,25,25),width=max(2,int(20*u)),fill=(246,246,242))
    bw=int(W*0.075)
    d.rounded_rectangle([int(24*u),int(24*u),bw,H-int(24*u)],radius=int(24*u),fill=(0,51,153))
    f=ImageFont.truetype(BOLD,int(H*0.22))
    d.text(((int(24*u)+bw)//2,H*0.74),"PL",font=f,fill=(255,255,255),anchor="mm")
    for i in range(12):
        import math
        a=2*math.pi*i/12; cx=(int(24*u)+bw)/2+math.cos(a)*H*0.13; cy=H*0.32+math.sin(a)*H*0.13
        d.ellipse([cx-H*0.018,cy-H*0.018,cx+H*0.018,cy+H*0.018],fill=(255,204,0))
    # tekst: szeroki krój zwężony w poziomie (jak czcionka tablic)
    ft=ImageFont.truetype(BOLD,int(H*0.82))
    tw=int(ft.getlength(text)); tim=Image.new("L",(tw+40,int(H*1.1)),0)
    ImageDraw.Draw(tim).text((20,int(H*0.55)),text,font=ft,fill=255,anchor="lm")
    tim=tim.crop(tim.getbbox())
    tw2=int(W-bw-240*u); th2=int(H*0.66)
    tim=tim.resize((tw2,th2),Image.LANCZOS)
    im.paste((20,20,20),(bw+int(112*u),int((H-th2)/2)),tim)
    return im.resize((w,h),Image.LANCZOS)

def car_rear(w, body=(52,70,98), plate="WX 1234A"):
    """Tył hatchbacka, szerokość w px. Zwraca RGBA i bbox tablicy (względny)."""
    ss=2; W=w*ss; H=int(w*0.82)*ss
    im=Image.new("RGBA",(W,H),(0,0,0,0)); d=ImageDraw.Draw(im)
    def P(x,y): return (int(x*W),int(y*H))
    # cień
    d.ellipse([P(0.02,0.86),P(0.98,1.0)],fill=(0,0,0,110))
    # koła
    for x in (0.08,0.74):
        d.rounded_rectangle([P(x,0.70),P(x+0.18,0.97)],radius=int(W*0.03),fill=(18,18,20))
    # nadwozie dolne
    d.rounded_rectangle([P(0.02,0.42),P(0.98,0.88)],radius=int(W*0.07),fill=body)
    # kabina
    top=(tuple(max(0,c-8) for c in body))
    d.polygon([P(0.13,0.46),P(0.22,0.07),P(0.78,0.07),P(0.87,0.46)],fill=top)
    d.rounded_rectangle([P(0.20,0.05),P(0.80,0.16)],radius=int(W*0.04),fill=top)
    # szyba tylna
    d.polygon([P(0.19,0.42),P(0.26,0.13),P(0.74,0.13),P(0.81,0.42)],fill=(28,34,44))
    d.polygon([P(0.30,0.40),P(0.40,0.15),P(0.48,0.15),P(0.40,0.40)],fill=(60,70,86))
    # spojler / światło stop
    d.rounded_rectangle([P(0.40,0.085),P(0.60,0.105)],radius=4,fill=(200,30,30))
    # lampy
    for x0,x1 in ((0.04,0.24),(0.76,0.96)):
        d.rounded_rectangle([P(x0,0.47),P(x1,0.58)],radius=int(W*0.02),fill=(150,18,22))
        d.rounded_rectangle([P(x0+0.02,0.49),P(x1-0.02,0.53)],radius=int(W*0.01),fill=(235,60,55))
    # klapa – linia
    d.line([P(0.24,0.60),P(0.76,0.60)],fill=tuple(max(0,c-18) for c in body)+(255,),width=ss*2)
    # zderzak
    d.rounded_rectangle([P(0.03,0.74),P(0.97,0.88)],radius=int(W*0.05),fill=(30,32,36))
    # logo
    d.ellipse([P(0.475,0.50),P(0.525,0.56)],outline=(190,190,195),width=ss*2)
    im=im.resize((w,int(w*0.82)),Image.LANCZOS)
    pw=int(w*0.36); ph=int(pw*114/520)
    px=(w-pw)//2; py=int(int(w*0.82)*0.625)
    im.paste(plate_img(plate,pw,ph),(px,py))
    hh=int(w*0.82)
    return im,(px/w,py/hh,(px+pw)/w,(py+ph)/hh)

def scene(W=2304,H=1296,plate="WX 1234A",car_w=640,car_cx=0.52,car_by=0.80,body=(52,70,98),seed=3,night=False,extra_cars=True):
    random.seed(seed)
    im=Image.new("RGB",(W,H)); d=ImageDraw.Draw(im)
    hz=int(H*0.47)
    for y in range(hz):
        t=y/hz
        c=(int(150+60*t),int(185+45*t),int(222+20*t)) if not night else (int(25+40*t),int(35+45*t),int(60+50*t))
        d.line([(0,y),(W,y)],fill=c)
    # bloki osiedla
    def blok(x0,x1,ytop,ybot,col):
        d.rectangle([x0,ytop,x1,ybot],fill=col)
        nx=max(2,int((x1-x0)/42)); ny=max(2,int((ybot-ytop)/48))
        for i in range(nx):
            for j in range(ny):
                wx=x0+(i+0.25)*(x1-x0)/nx; wy=ytop+(j+0.3)*(ybot-ytop)/ny
                lit = night and random.random()<0.35
                d.rectangle([wx,wy,wx+(x1-x0)/nx*0.5,wy+(ybot-ytop)/ny*0.45],
                            fill=(255,214,140) if lit else tuple(int(c*0.72) for c in col))
    for x0,x1,yt,col in ((-40,520,140,(206,200,188)),(470,820,300,(196,182,170)),(1520,1880,280,(200,196,186)),(1830,2360,110,(214,206,196)),(800,1010,380,(186,186,182)),(1320,1530,370,(190,184,176))):
        blok(x0,x1,yt,hz+40,col if not night else tuple(int(c*0.35) for c in col))
    # drzewa
    for x,r in ((620,120),(330,150),(1700,140),(2050,170),(1040,70),(1290,75)):
        g=(70,110,62) if not night else (22,36,26)
        d.ellipse([x-r,hz-r*1.6,x+r,hz+r*0.2],fill=g); d.rectangle([x-10,hz-20,x+10,hz+60],fill=(70,55,45))
    # chodniki i jezdnia
    vx=W*0.5
    d.polygon([(0,hz+30),(W,hz+30),(W,H),(0,H)],fill=(150,148,142) if not night else (52,52,56))
    d.polygon([(vx-120,hz+30),(vx+120,hz+30),(W*1.02,H),(-W*0.02,H)],fill=(78,80,84) if not night else (34,35,38))
    for k in range(9):
        t0=(k/9)**2; t1=((k+0.45)/9)**2
        y0=hz+30+(H-hz-30)*t0; y1=hz+30+(H-hz-30)*t1
        w0=3+30*t0; w1=3+30*t1
        d.polygon([(vx-w0,y0),(vx+w0,y0),(vx+w1,y1),(vx-w1,y1)],fill=(235,235,230) if not night else (170,170,165))
    # krawężniki
    d.line([(vx-120,hz+30),(-W*0.02,H)],fill=(190,190,186),width=10)
    d.line([(vx+120,hz+30),(W*1.02,H)],fill=(190,190,186),width=10)
    im=im.filter(ImageFilter.GaussianBlur(1.2))
    boxes=[]
    if extra_cars:
        c2,pb2=car_rear(150,(150,150,152),"WX 50000")
        x2,y2=int(W*0.555),int(hz+40)
        im.paste(c2,(x2,y2),c2)
        boxes.append(("car",(x2/W,y2/H,(x2+150)/W,(y2+123)/H),None))
    cw=car_w; car,pb=car_rear(cw,body,plate)
    ch=car.size[1]; x=int(W*car_cx-cw/2); y=int(H*car_by-ch)
    im.paste(car,(x,y),car)
    car_box=(x/W,y/H,(x+cw)/W,(y+ch)/H)
    plate_box=((x+pb[0]*cw)/W,(y+pb[1]*ch)/H,(x+pb[2]*cw)/W,(y+pb[3]*ch)/H)
    arr=np.array(im).astype(np.float32)
    arr+=np.random.default_rng(seed).normal(0,3.0,arr.shape)
    arr=np.clip(arr,0,255).astype(np.uint8)
    boxes.append(("car",car_box,None))
    return arr, car_box, plate_box, boxes   # RGB

if __name__=="__main__":
    a,cb,pb,_=scene()
    cv2.imwrite("scene_test.jpg",cv2.cvtColor(a,cv2.COLOR_RGB2BGR))
    print(cb,pb)
