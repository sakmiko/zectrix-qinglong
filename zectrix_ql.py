#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ZECTRIX NOTE4 / 青龙面板单入口适配。

一次运行即退出；由 QingLong cron 调度三个页面及 OAuth 独立刷新。
图形数据 zectrix_bitmaps.json.gz 保留原 Rust 手绘点阵资源。
仅依赖 Pillow，其余 HTTP、OAuth、数据获取全部使用 Python 标准库。
"""
from __future__ import annotations

import argparse
import base64
import calendar as pycalendar
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
import gzip
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import statistics
import sys
import tempfile
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

try:
    from PIL import Image, ImageDraw
except ImportError as exc:
    raise SystemExit("缺少 Pillow，请在青龙依赖管理安装 Python 依赖 Pillow") from exc

ROOT = Path(__file__).resolve().parent
TZ8 = timezone(timedelta(hours=8))
SIZE = (400, 300)
DEFAULT_BASE = "https://cloud.zectrix.com"
USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
OAUTH_URL = "https://auth.openai.com/oauth/token"
SIWC_URL = "https://auth.openai.com/api/accounts/oauth/token"
OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"


def load_dotenv(path: Path):
    """Environment supplied by QingLong wins over optional local .env."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        val = val.strip()
        if (val.startswith("'") and val.endswith("'")) or (val.startswith('"') and val.endswith('"')):
            val = val[1:-1]
        os.environ.setdefault(key.strip().removeprefix("export "), val)


def env(key, default=""):
    return os.getenv(key, str(default)).strip()


def flag(key, default=False):
    return env(key, str(default)).lower() in ("1", "true", "yes", "on")


def integer(key, default, low=None, high=None):
    try:
        v = int(env(key, default))
    except ValueError:
        v = default
    return max(low, min(high, v) if high is not None else v) if low is not None else v


def get_json(url, headers=None, timeout=15, max_bytes=2_000_000):
    req = Request(url, headers={"User-Agent": "Zectrix-Qinglong/1.0", "Accept": "application/json",
                                **(headers or {})})
    with urlopen(req, timeout=timeout) as response:
        content = response.read(max_bytes + 1)
        if len(content) > max_bytes:
            raise ValueError("API 响应超过大小限制")
        return json.loads(content)


def post_form_json(url, body, headers=None, timeout=25):
    req = Request(url, data=urlencode(body).encode(), method="POST",
                  headers={"Content-Type": "application/x-www-form-urlencoded", **(headers or {})})
    with urlopen(req, timeout=timeout) as response:
        raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("OAuth 响应过大")
        return json.loads(raw)


def atomic_write(path, blob, private=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            if private:
                os.fchmod(f.fileno(), 0o600)
            f.write(blob)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        try:
            Path(tmp).unlink(missing_ok=True)
        except OSError:
            pass


def data_dir():
    p = Path(env("ZECTRIX_DATA_DIR", str(ROOT / "data"))).expanduser()
    p.mkdir(parents=True, exist_ok=True)
    return p


def assets():
    with gzip.open(ROOT / "zectrix_bitmaps.json.gz", "rt", encoding="utf-8") as f:
        return json.load(f)


ASSETS = assets()


class Frame:
    """Integer-pixel 1-bit compatible drawing surface (no antialias/dither)."""
    def __init__(self):
        self.image = Image.new("L", SIZE, 255)
        self.draw = ImageDraw.Draw(self.image)

    def px(self, x, y, on=True):
        if 0 <= x < 400 and 0 <= y < 300:
            self.draw.point((int(x), int(y)), fill=0 if on else 255)

    def rect(self, x, y, w, h, on=True):
        if w > 0 and h > 0:
            self.draw.rectangle((int(x), int(y), int(x + w - 1), int(y + h - 1)),
                                fill=0 if on else 255)

    def stroke(self, x, y, w, h, t=1, on=True):
        if w <= 0 or h <= 0:
            return
        t = min(t, w, h)
        self.rect(x, y, w, t, on)
        self.rect(x, y + h - t, w, t, on)
        self.rect(x, y, t, h, on)
        self.rect(x + w - t, y, t, h, on)

    def card(self, x, y, w, h, border=2, shadow=2):
        if shadow:
            self.rect(x + shadow, y + h, w, shadow)
            self.rect(x + w, y + shadow, shadow, h)
        self.rect(x, y, w, h, False)
        self.stroke(x, y, w, h, border)
        for cx, cy in ((x,y),(x+w-1,y),(x,y+h-1),(x+w-1,y+h-1)):
            self.px(cx, cy, False)

    def pill(self, x, y, w, h, filled, inverted=False):
        fg, bg = not inverted, inverted
        self.rect(x, y, w, h, fg if filled else bg)
        if not filled:
            self.stroke(x, y, w, h, 1, fg)
        for cx, cy in ((x,y),(x+w-1,y),(x,y+h-1),(x+w-1,y+h-1)):
            self.px(cx, cy, bg)

    def dots(self, x, y, w, h, pitch=2):
        for py in range(y, y + max(0,h)):
            if py % pitch:
                continue
            for px in range(x, x + max(0,w)):
                if px % pitch == 0:
                    self.px(px, py)

    def halftone(self, x, y, w, h, step=2):
        for py in range(y, y + max(0,h)):
            for px in range(x, x + max(0,w)):
                if (px + py) % max(1,step) == 0:
                    self.px(px, py)

    def dotted_h(self, x0, x1, y, step=2):
        for x in range(x0, x1 + 1):
            if x % step == 0: self.px(x, y)

    def dotted_v(self, x, y0, y1, step=2):
        for y in range(y0, y1 + 1):
            if y % step == 0: self.px(x, y)

    def line(self, x0, y0, x1, y1, thickness=1):
        dx, dy = abs(x1-x0), -abs(y1-y0)
        sx, sy = (1 if x0 < x1 else -1), (1 if y0 < y1 else -1)
        err, half = dx + dy, thickness // 2
        while True:
            if thickness <= 1: self.px(x0,y0)
            else: self.rect(x0-half,y0-half,thickness,thickness)
            if x0 == x1 and y0 == y1: break
            e2 = 2 * err
            if e2 >= dy: err += dy; x0 += sx
            if e2 <= dx: err += dx; y0 += sy

    def save(self, path):
        self.image.save(path, "PNG")
        return hashlib.sha256(self.image.tobytes()).hexdigest()

    def png(self):
        stream = io.BytesIO()
        self.image.save(stream, "PNG")
        return stream.getvalue()

    def bitmap(self, key, text, x, y, on=True, align="left", gap=2):
        """Draw original Rust hand-drawn glyphs, including calendar-specific 12px data."""
        glyphs = ASSETS[key]
        size = 16 if key == "weather16" else 12
        tw = len(text) * size + max(0, len(text)-1) * gap
        cx = x - (tw // 2 if align == "center" else tw if align == "right" else 0)
        fallback = ASSETS["calendar12"].get("□", [0xFFE] + [0x802]*9 + [0xFFE,0])
        for char in text:
            if char == " ":
                cx += size + gap
                continue
            if char in ASSETS.get("weather7",{}) and char not in glyphs:
                self.font7(char,cx+(size-7)//2,y+max(0,(size-11)//2),on)
                cx += size + gap
                continue
            if 33<=ord(char)<=126 and char not in glyphs:
                self.micro(char,cx+(size-5)//2,y+max(0,(size-7)//2),on)
                cx += size + gap
                continue
            rows = glyphs.get(char) or ASSETS["weather12"].get(char) or ASSETS["calendar12"].get(char) or fallback
            sz = size if len(rows) == size else len(rows)
            for ry, bits in enumerate(rows):
                for rx in range(min(sz, size)):
                    if bits & (1 << (sz-1-rx)):
                        self.px(cx + rx, y + ry, on)
            cx += size + gap
        return tw

    def c12(self, text, x, y, on=True, align="left", gap=2, calendar=False):
        return self.bitmap("calendar12" if calendar else "weather12", text, x, y, on, align, gap)

    def c16(self, text, x, y, on=True, align="left", gap=2):
        return self.bitmap("weather16", text, x, y, on, align, gap)

    def font7(self, text, x, y, on=True, align="left", gap=1):
        font=ASSETS.get("weather7",{})
        widths=[(4 if c==" " else font[c][0] if c in font else 6) for c in text]
        tw=sum(widths)+max(0,len(widths)-1)*gap
        cx=x-(tw//2 if align=="center" else tw if align=="right" else 0)
        for ch,width in zip(text,widths):
            if ch in font:
                for ry,bits in enumerate(font[ch][1]):
                    for rx in range(width):
                        if bits & (1<<(width-1-rx)):self.px(cx+rx,y+ry,on)
            elif ch!=" ":
                self.micro(ch,cx,y+2,on)
            cx+=width+gap
        return tw

    def micro(self, text, x, y, on=True, align="left", gap=1, sx=1, sy=1):
        tw = len(text)*(5*sx) + max(0,len(text)-1)*gap
        cx = x - (tw//2 if align == "center" else tw if align == "right" else 0)
        for char in text.upper():
            rows = MICRO.get(char, (0,)*7)
            for ry, bits in enumerate(rows):
                for rx in range(5):
                    if bits & (1 << (4-rx)):
                        self.rect(cx+rx*sx,y+ry*sy,sx,sy,on)
            cx += 5*sx+gap
        return tw

    def number(self, text, x, y, dw, dh, dt, gap=2, on=True, shadow=False):
        cx = x
        for ch in str(text):
            if ch.isdigit():
                self.digit(ch,cx,y,dw,dh,dt,on,shadow); cw=dw
            elif ch == ":":
                dot=max(2,dt)
                self.rect(cx+1,y+dh//4,dot,dot,on)
                self.rect(cx+1,y+dh*3//4-dot,dot,dot,on)
                cw=dt+2
            elif ch == "-":
                cw=max(dw-2,4);self.rect(cx,y+(dh-dt)//2,cw,dt,on)
            elif ch == "/":
                cw=max(dw-2,4)
                for i in range(max(0,dh-2)):
                    px=cx+cw-1-i*(cw-1)//max(1,dh-3)
                    self.rect(px,y+1+i,dt,dt,on)
            elif ch == "%":
                psize=max(dh-2,8)
                self.percent(cx,y+dh-psize,psize,max(2,dt-1),on)
                cw=psize
            else: cw=dw//2
            cx += cw + gap
        return max(0,cx-x-gap)

    def digit(self,ch,x,y,w,h,t,on=True,shadow=False):
        segments={
            "0":(1,1,1,1,1,1,0),"1":(0,1,1,0,0,0,0),
            "2":(1,1,0,1,1,0,1),"3":(1,1,1,1,0,0,1),
            "4":(0,1,1,0,0,1,1),"5":(1,0,1,1,0,1,1),
            "6":(1,0,1,1,1,1,1),"7":(1,1,1,0,0,0,0),
            "8":(1,1,1,1,1,1,1),"9":(1,1,1,1,0,1,1)}
        if ch not in segments:return
        a,b,c,d,e,f,g=segments[ch]
        if ch=="1":
            bar=x+(w-t)//2+int(w>=12);beak=max(2,t-1)
            rects=[(bar-beak,y+int(h>=14),beak,t),(bar,y,t,h),
                   (max(bar-beak,x),y+h-t,min(t+2*beak,w),t)]
        else:
            mid=y+(h-t)//2;top=(h-t)//2
            rects=[]
            if a:rects.append((x,y,w,t))
            if d:rects.append((x,y+h-t,w,t))
            if g:rects.append((x,mid,w,t))
            if f:rects.append((x,y,t,top+t))
            if b:rects.append((x+w-t,y,t,top+t))
            if e:rects.append((x,mid,t,h-(mid-y)))
            if c:rects.append((x+w-t,mid,t,h-(mid-y)))
        if shadow and on:
            for rx,ry,rw,rh in rects:self.halftone(rx+2,ry+2,rw,rh)
        for rx,ry,rw,rh in rects:self.rect(rx,ry,rw,rh,on)
        if w>=10 and ch!="1":
            if a and f:self.px(x,y,not on)
            if a and b:self.px(x+w-1,y,not on)
            if d and e:self.px(x,y+h-1,not on)
            if d and c:self.px(x+w-1,y+h-1,not on)

    def percent(self,x,y,size,t,on=True):
        dot=max(size//3,t+1)
        self.rect(x,y,dot,dot,on)
        self.rect(x+size-dot,y+size-dot,dot,dot,on)
        for i in range(max(0,size-2)):
            self.rect(x+1+i,y+size-2-i-t//2,t,t,on)

    def gauge(self, percent, x, y, w, h, count):
        self.stroke(x,y,w,h,1)
        inner_x,inner_y=x+2,y+2
        iw,ih=max(0,w-4),max(0,h-4)
        count=max(1,count)
        sw=(iw-(count-1))//count
        rem=iw-(sw*count+count-1)
        target=iw*max(0,min(100,percent))//100
        cursor=inner_x
        for i in range(count):
            width=sw+int(i<rem)
            begin=cursor-inner_x
            full=max(0,min(width,target-begin))
            if full:self.rect(cursor,inner_y,full,ih)
            if full<width:self.dots(cursor+full,inner_y,width-full,ih)
            cursor+=width+1


MICRO = {
    "0":(14,17,17,17,17,17,14),"1":(4,12,4,4,4,4,14),
    "2":(14,17,1,6,8,16,31),"3":(30,1,1,14,1,1,30),
    "4":(18,18,18,31,2,2,2),"5":(31,16,30,1,1,17,14),
    "6":(14,16,30,17,17,17,14),"7":(31,1,2,4,8,8,8),
    "8":(14,17,17,14,17,17,14),"9":(14,17,17,15,1,1,14),
    "A":(14,17,17,31,17,17,17),"C":(15,16,16,16,16,16,15),
    "D":(30,17,17,17,17,17,30),"E":(31,16,16,30,16,16,31),
    "H":(17,17,17,31,17,17,17),"I":(14,4,4,4,4,4,14),
    "K":(17,18,20,24,20,18,17),"L":(16,16,16,16,16,16,31),
    "M":(17,27,21,21,17,17,17),"N":(17,25,21,19,17,17,17),
    "O":(14,17,17,17,17,17,14),"P":(30,17,17,30,16,16,16),
    "Q":(14,17,17,17,21,18,13),"R":(30,17,17,30,20,18,17),
    "T":(31,4,4,4,4,4,4),"U":(17,17,17,17,17,17,14),
    "V":(17,17,17,17,17,10,4),"X":(17,17,10,4,10,17,17),
    "Y":(17,17,10,4,4,4,4),"Z":(31,1,2,4,8,16,31),
    "-":(0,0,0,31,0,0,0),":":(0,4,4,0,4,4,0),
    ".":(0,0,0,0,0,12,12),"/":(1,2,2,4,8,8,16),
    "%":(25,26,2,4,8,11,19),"!":(4,4,4,4,4,0,4),
    "+":(0,4,4,31,4,4,0),"~":(0,0,13,18,0,0,0),
}


def save_preview(frame, path):
    dest = Path(path).expanduser()
    dest.parent.mkdir(parents=True, exist_ok=True)
    return frame.save(dest)


def load_state(name):
    path = data_dir() / (name + ".json")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def store_state(name, state):
    atomic_write(data_dir()/(name+".json"), json.dumps(state, ensure_ascii=False).encode())


def publish(frame, page, task):
    if not 1 <= page <= 5:
        raise ValueError("页码必须在 1–5 之间")
    if task != "quota" and page in [int(n) for n in env("ZECTRIX_RESERVED_PAGES","3").split(",") if n.strip().isdigit()] and not (flag("ZECTRIX_ALLOW_RESERVED_OVERRIDE") or flag("ALLOW_PAGE_3_OVERRIDE")):
        raise ValueError(f"第 {page} 页已预留，拒绝覆盖")
    key = env("ZECTRIX_API_KEY")
    key_path = env("ZECTRIX_API_KEY_FILE")
    if not key and key_path:
        key = Path(key_path).read_text(encoding="utf-8").strip()
    device = env("ZECTRIX_DEVICE_ID")
    if not key or not device:
        raise ValueError("缺少 ZECTRIX_DEVICE_ID 或 ZECTRIX_API_KEY / ZECTRIX_API_KEY_FILE")
    boundary = "----zectrix" + os.urandom(12).hex()
    png = frame.png()
    if len(png) > integer("ZECTRIX_MAX_IMAGE_BYTES",2*1024*1024):
        raise ValueError("PNG 超过大小上限")
    parts=[]
    for name,value in [("pageId",str(page)),("dither",str(flag("ZECTRIX_DITHER",False)).lower())]:
        parts.append((f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n").encode())
    parts.append((f"--{boundary}\r\nContent-Disposition: form-data; name=\"images\"; filename=\"{task}.png\"\r\nContent-Type: image/png\r\n\r\n").encode()+png+b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    req=Request(env("ZECTRIX_API_BASE",DEFAULT_BASE).rstrip("/")+f"/open/v1/devices/{device}/display/image",
                method="POST",data=b"".join(parts),
                headers={"X-API-Key":key,"Content-Type":f"multipart/form-data; boundary={boundary}"})
    with urlopen(req,timeout=integer("ZECTRIX_HTTP_TIMEOUT_SECS",30,5)) as resp:
        data=json.loads(resp.read(200_000))
    if data.get("code") != 0:
        raise ValueError("设备上屏接口返回非零 code：" + str(data.get("message",data.get("code"))))
    print(f"[{task}] P{page} 上屏成功")


def deliver(frame, task, page, args):
    state=load_state("publish_"+task)
    raw_hash=hashlib.sha256(frame.image.tobytes()).hexdigest()
    preview=args.output or env(task.upper()+"_PREVIEW_PATH",str(data_dir()/f"{task}_preview.png"))
    save_preview(frame,preview)
    print(f"[{task}] 预览 {preview} 400x300 1-bit; sha256={raw_hash[:16]}")
    if args.preview: return
    if raw_hash==state.get("hash") and not args.force:
        print(f"[{task}] 图像未变化，跳过推送")
        return
    if not args.force and int(time.time()) - state.get("last_push",0) < integer("ZECTRIX_MIN_PUSH_SECONDS",60,1):
        print(f"[{task}] 设备最小推送间隔未到，跳过")
        return
    publish(frame,page,task)
    store_state("publish_"+task,{"hash":raw_hash,"last_push":int(time.time())})


# --- 1. 月历、农历与精确节气（移植原 Rust 201 年数据表） -----------------------
GAN="甲乙丙丁戊己庚辛壬癸"
ZHI="子丑寅卯辰巳午未申酉戌亥"
ANIMALS="鼠牛虎兔龙蛇马羊猴鸡狗猪"
LUNAR_MONTHS=["正","二","三","四","五","六","七","八","九","十","冬","腊"]
SOLAR_TERMS=[("小寒","大寒"),("立春","雨水"),("惊蛰","春分"),("清明","谷雨"),
             ("立夏","小满"),("芒种","夏至"),("小暑","大暑"),("立秋","处暑"),
             ("白露","秋分"),("寒露","霜降"),("立冬","小雪"),("大雪","冬至")]
LUNAR_FEST={(1,1):"春节",(1,15):"元宵",(2,2):"龙头",(5,5):"端午",
            (7,7):"七夕",(7,15):"中元",(8,15):"中秋",(9,9):"重阳",
            (12,8):"腊八",(12,23):"小年"}
SOLAR_FEST={(1,1):"元旦",(3,8):"妇女",(3,12):"植树",(5,1):"五一",
            (5,4):"青年",(6,1):"儿童",(7,1):"建党",(8,1):"建军",
            (9,10):"教师",(10,1):"国庆",(10,24):"编程",
            (12,24):"平安",(12,25):"圣诞"}


def lunar_month_days(year,month):
    index=max(0,min(200,year-1900))
    return 30 if ASSETS["lunar"][index] & (0x10000 >> month) else 29


def lunar_leap_month(year):
    return ASSETS["lunar"][max(0,min(200,year-1900))] & 15


def lunar_leap_days(year):
    if not lunar_leap_month(year):return 0
    return 30 if ASSETS["lunar"][max(0,min(200,year-1900))] & 0x10000 else 29


def lunar_year_days(year):
    info=ASSETS["lunar"][max(0,min(200,year-1900))]
    return 348+sum(bool(info & (0x8000>>i)) for i in range(12))+lunar_leap_days(year)


def lunar_day_text(day):
    if day==10:return "初十"
    if day==20:return "二十"
    if day==30:return "三十"
    return ("初","十","廿","三")[min(3,day//10)]+("","一","二","三","四","五","六","七","八","九","十")[day%10]


def lunar_info(target):
    offset=max(0,(target-date(1900,1,31)).days)
    original=offset
    year=1900
    while year<=2100 and offset>=lunar_year_days(year):
        offset-=lunar_year_days(year)
        year+=1
    leap=lunar_leap_month(year)
    month=1;is_leap=False;processed=False
    while month<=12:
        if leap and month==leap+1 and not processed:
            month-=1;is_leap=True;processed=True
            days=lunar_leap_days(year)
        else:
            is_leap=False;days=lunar_month_days(year,month)
        if offset<days:break
        offset-=days;month+=1
    day=offset+1
    yd=(year-1900+36)%60
    dd=(original+40)%60
    phase=next((name for test,name in (
        (day==1,"朔月"),(2<=day<=6,"蛾眉月"),(7<=day<=9,"上弦月"),
        (10<=day<=14,"盈凸月"),(15<=day<=16,"望月"),(17<=day<=21,"亏凸月"),
        (22<=day<=24,"下弦月"),(25<=day<=29,"残月")) if test),"晦月")
    month_text=("闰" if is_leap else "")+LUNAR_MONTHS[min(11,month-1)]+"月"
    return dict(year=year,month=month,day=day,leap=is_leap,big=days==30,
                month_text=month_text,day_text=lunar_day_text(day),phase=phase,
                ganzhi_year=GAN[yd%10]+ZHI[yd%12],
                zodiac=ANIMALS[(year-1900)%12],
                ganzhi_day=GAN[dd%10]+ZHI[dd%12]+"日")


def month_terms(year,month):
    n1,n2=SOLAR_TERMS[month-1]
    if 1900<=year<=2100:
        byte=ASSETS["solar"][(year-1900)*12+month-1]
        return ((byte>>4)+3,n1),((byte&15)+18,n2)
    y=year%100
    leap=(y-1)//4 if month<=2 else y//4
    return ((max(3,min(9,int(y*.2422+5.5)-leap)),n1),
            (max(18,min(24,int(y*.2422+21)-leap)),n2))


def cell_label(target):
    info=lunar_info(target)
    if not info["leap"]:
        fest=LUNAR_FEST.get((info["month"],info["day"]))
        if info["month"]==12 and info["day"]==lunar_month_days(info["year"],12) and lunar_leap_month(info["year"])!=12:
            fest="除夕"
        if fest:return fest,True
    (d1,t1),(d2,t2)=month_terms(target.year,target.month)
    if target.day==d1:return t1,True
    if target.day==d2:return t2,True
    fest=SOLAR_FEST.get((target.month,target.day))
    if fest:return fest,True
    if info["day"]==1:return info["month_text"][:2],True
    return info["day_text"],False


def calendar_highlights(year,month):
    total=pycalendar.monthrange(year,month)[1]
    all_days=[(d,*cell_label(date(year,month,d))) for d in range(1,total+1)]
    items=[(d,txt) for d,txt,special in all_days if special and not txt.endswith("月")]
    if len(items)<4:
        items.extend((d,txt) for d,txt,special in all_days
                     if special and txt.endswith("月") and d not in [x[0] for x in items])
    return sorted(items)[:4]


def render_calendar(target,page=1):
    if not (1900<=target.year<=2100):
        raise ValueError("农历数据支持 1900–2100 年")
    f=Frame()
    yy,mm,day=target.year,target.month,target.day
    total=pycalendar.monthrange(yy,mm)[1]
    weekday=target.weekday(); first=pycalendar.monthrange(yy,mm)[0]
    day_of_year=target.timetuple().tm_yday
    total_year=366 if pycalendar.isleap(yy) else 365
    lunar=lunar_info(target)
    lx,ly,lw=3,31,130
    # 原版：窄黑顶栏
    f.rect(3,3,394,24)
    for x,y in ((3,3),(396,3),(3,26),(396,26)):f.px(x,y,False)
    f.rect(7,6,62,18,False)
    f.micro("ZECTRIX",11,12,True,gap=1,sx=1,sy=1)
    f.c12("月度日历",78,9,False,gap=3,calendar=True)
    f.rect(138,14,2,2,False)
    f.c12("农历黄历",145,9,False,gap=3,calendar=True)
    f.stroke(295,6,98,18,1,False)
    f.rect(301,12,6,6,False)
    f.micro(f"P{page} UTC+8",313,12,False,gap=2)
    # 左：年月、农历、超大今天、周期进度
    f.card(lx,ly,lw,265,1,0)
    f.rect(lx+2,ly+2,lw-4,23)
    wy=f.number(f"{yy:04}",lx+6,ly+8,6,11,2,1,False)
    f.c12("年",lx+8+wy,ly+8,False,gap=2,calendar=True)
    wm=f.number(f"{mm:02}",lx+23+wy,ly+8,6,11,2,1,False)
    f.c12("月",lx+25+wy+wm,ly+8,False,gap=2,calendar=True)
    f.stroke(lx+lw-26,ly+6,20,15,1,False)
    f.micro(f"Q{(mm-1)//3+1}",lx+lw-21,ly+10,False,gap=1)
    f.rect(lx+2,ly+25,lw-4,19,False);f.rect(lx+2,ly+44,lw-4,1)
    f.c12(lunar["ganzhi_year"]+lunar["zodiac"]+"年",lx+6,ly+29,True,gap=2,calendar=True)
    f.c12(lunar["month_text"]+("大" if lunar["big"] else "小"),lx+lw-6,ly+29,
          True,"right",2,True)
    f.stroke(lx+7,ly+50,lw-14,65)
    for x,y in ((lx+9,ly+52),(lx+lw-12,ly+52),(lx+9,ly+111),(lx+lw-12,ly+111)):
        f.rect(x,y,3,3)
    f.rect(lx+33,ly+46,64,10);f.micro("TODAY",lx+48,ly+48,False,gap=2)
    x=lx+(lw-(2*28+8))//2
    for c in f"{day:02}":f.digit(c,x,ly+62,28,44,6,True,True);x+=36
    f.rect(lx+7,ly+119,lw-14,20)
    f.c12(lunar["month_text"]+lunar["day_text"],lx+12,ly+123,False,calendar=True)
    f.c12(lunar["phase"],lx+lw-12,ly+123,False,"right",2,True)
    f.stroke(lx+7,ly+143,lw-14,20)
    f.dotted_v(lx+65,ly+145,ly+160)
    f.c12(("星期一","星期二","星期三","星期四","星期五","星期六","星期日")[weekday],
          lx+36,ly+147,True,"center",2,True)
    f.c12(lunar["ganzhi_day"],lx+94,ly+147,True,"center",2,True)
    f.stroke(lx+7,ly+166,lw-14,20)
    f.dotted_v(lx+65,ly+168,ly+183)
    f.c12("第",lx+11,ly+170,calendar=True)
    ww=f.number(f"{target.isocalendar().week:02}",lx+24,ly+171,6,10,2,1)
    f.c12("周",lx+26+ww,ly+170,calendar=True)
    f.c12("余",lx+70,ly+170,calendar=True)
    ww=f.number(f"{total-day:02}",lx+83,ly+171,6,10,2,1)
    f.c12("天",lx+85+ww,ly+170,calendar=True)
    f.dotted_h(lx+7,lx+lw-8,ly+191)
    f.c12("本月流转",lx+7,ly+196,calendar=True)
    s=f"{day}/{total}"; width=f.number(s,-300,-300,5,10,1,2)
    f.number(s,lx+lw-7-width,ly+197,5,10,1,2)
    f.gauge(day*100//total,lx+7,ly+211,lw-14,10,10)
    f.c12("全年进度",lx+7,ly+227,calendar=True)
    s=f"{day_of_year*100//total_year}%"; width=f.number(s,-300,-300,5,10,1,2)
    f.number(s,lx+lw-7-width,ly+228,5,10,1,2)
    f.gauge(day_of_year*100//total_year,lx+7,ly+242,lw-14,10,10)
    # 右：原版 7x6 双层月历矩阵
    rx,ry,rw=137,31,259
    f.card(rx,ry,rw,265,1,0)
    gx,cell_w=rx+4,36
    hy=ry+4
    f.stroke(gx,hy,252,20)
    for col,label in enumerate("一二三四五六日"):
        cx=gx+col*36
        if col>=5:
            f.rect(cx,hy,36,20)
            f.c12(label,cx+18,hy+4,False,"center",2,True)
        else:
            if col>0:f.dotted_v(cx,hy+2,hy+17)
            f.c12(label,cx+18,hy+4,True,"center",2,True)
    cy0=ry+26
    f.stroke(gx,cy0,252,210)
    for col in range(1,7):f.dotted_v(gx+col*36,cy0+1,cy0+208)
    for row in range(1,6):f.dotted_h(gx+1,gx+250,cy0+row*35)
    for slot in range(42):
        row,col=divmod(slot,7)
        cx,cy=gx+col*36,cy0+row*35
        day_num=slot-first+1
        if not 1<=day_num<=total:
            f.rect(cx+17,cy+17,3,1)
            continue
        text,special=cell_label(date(yy,mm,day_num))
        s=str(day_num);nx=cx+(36-(len(s)*7+(len(s)-1)*2))//2
        if day_num==day:
            f.rect(cx+1,cy+1,35,34)
            for px,py in ((cx+2,cy+2),(cx+33,cy+2),(cx+2,cy+32),(cx+33,cy+32)):
                f.px(px,py,False)
            f.number(s,nx,cy+6,7,11,2,2,False)
            f.c12(text,cx+18,cy+20,False,"center",2,True)
        else:
            if day_num<day:
                for dy in range(4):f.rect(cx+2,cy+2+dy,4-dy,1)
            if special:
                f.rect(cx+31,cy+2,3,3);f.rect(cx+5,cy+33,27,1)
            f.number(s,nx,cy+6,7,11,2,2)
            f.c12(text,cx+18,cy+20,True,"center",2,True)
    leg_y=ry+239
    f.stroke(gx,leg_y,252,20)
    f.rect(gx,leg_y,36,20)
    f.c12("节候",gx+18,leg_y+4,False,"center",2,True)
    hx=gx+43
    for i,(hd,name) in enumerate(calendar_highlights(yy,mm)):
        if i:f.rect(hx,leg_y+9,2,2);hx+=6
        hx+=f.number(f"{hd:02}",hx,leg_y+5,5,10,1,1)+3
        hx+=f.c12(name,hx,leg_y+4,True,"left",2,True)+6
    return f


# --- 2. Codex 额度、独立 OAuth 刷新、公共重置雷达 -----------------------------
def timestamp(value):
    if isinstance(value,(int,float)):
        return int(value//1000 if value>10_000_000_000 else value)
    if isinstance(value,str):
        if value.isdigit():return timestamp(int(value))
        try:return int(datetime.fromisoformat(value.replace("Z","+00:00")).timestamp())
        except ValueError:pass
    raise ValueError("时间戳无法识别")


def extract_claims(jwt):
    try:
        payload=jwt.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload+"="*((-len(payload))%4)))
    except (IndexError,ValueError,TypeError):
        return {}


def account_id(auth):
    if auth.get("account_id"):return auth["account_id"]
    for jwt in (auth.get("id_token",""),auth.get("siwc",{}).get("id_token","")):
        claims=extract_claims(jwt)
        items=(claims.get("https://api.openai.com/auth") or {})
        value=items.get("chatgpt_account_id") or items.get("account_id") or claims.get("chatgpt_account_id")
        if value:return value
    raise ValueError("Codex 授权 JSON 未包含 ChatGPT account_id")


def auth_file():
    raw=env("CODEX_AUTH_PATH")
    if not raw:raise ValueError("需要配置 CODEX_AUTH_PATH 为 CPA Codex 登录 JSON 文件或只含一个 JSON 的目录")
    path=Path(raw).expanduser()
    if path.is_dir():
        paths=list(path.glob("*.json"))
        if len(paths)!=1:raise ValueError("CODEX_AUTH_PATH 目录必须恰好包含一个 *.json")
        path=paths[0]
    if path.is_symlink() or not path.is_file():
        raise ValueError("登录文件必须是普通文件，不能是软链接")
    if path.stat().st_size>1024*1024:raise ValueError("登录 JSON 大于 1MB")
    return path


def read_auth():
    path=auth_file()
    content=path.read_bytes()
    if content.startswith(b"ZXF1"):
        raise ValueError("这是 Rust 专用加密 ZXF1 登录文件，Python 版需独立的标准 CPA JSON 副本")
    obj=json.loads(content)
    if not isinstance(obj,dict) or obj.get("type","codex").lower()!="codex":
        raise ValueError("登录 JSON 类型必须是 codex")
    return path,content,obj


def active_auth(auth):
    if auth.get("use_siwc"):
        siwc=auth.get("siwc")
        if not isinstance(siwc,dict):raise ValueError("use_siwc=true，但未提供 siwc 对象")
        return siwc
    return auth


def expiry_soon(auth,now=None):
    now=int(time.time()) if now is None else now
    record=active_auth(auth)
    try:return timestamp(record.get("expired",0))<=now+300
    except ValueError:return True


def oauth_refresh(auth):
    """Refresh only. Never call quota or radar here. Preserve rotated refresh_token."""
    changed=json.loads(json.dumps(auth))
    target=active_auth(changed)
    refresh_token=target.get("refresh_token","")
    if not refresh_token:raise ValueError("缺少 refresh_token")
    if changed.get("use_siwc"):
        client_id=target.get("client_id","")
        if not client_id:raise ValueError("SIWC 缺少 client_id")
        result=post_form_json(env("CODEX_SIWC_TOKEN_URL",SIWC_URL),{
            "grant_type":"refresh_token","client_id":client_id,
            "refresh_token":refresh_token,"resource":"https://api.openai.com/v1"})
        if "chatgpt.tokens.use.direct" not in result.get("scope","").split():
            raise ValueError("SIWC 刷新结果不包含 chatgpt.tokens.use.direct 权限")
        if not result.get("refresh_token"):
            raise ValueError("SIWC 刷新结果缺少轮换后的 refresh_token")
        target["scope"]=result.get("scope","")
        target["scopes"]=target["scope"].split()
    else:
        result=post_form_json(env("CODEX_OAUTH_TOKEN_URL",OAUTH_URL),{
            "client_id":OAUTH_CLIENT_ID,"grant_type":"refresh_token",
            "refresh_token":refresh_token,"scope":"openid profile email"})
    if not result.get("access_token"):
        raise ValueError("OAuth 刷新响应缺少 access_token")
    for field in ("access_token","refresh_token","id_token"):
        if result.get(field):target[field]=result[field]
    now=datetime.now(timezone.utc)
    target["last_refresh"]=now.strftime("%Y-%m-%dT%H:%M:%SZ")
    expires_in=int(result.get("expires_in",3600))
    target["expired"]=(now+timedelta(seconds=expires_in)).strftime("%Y-%m-%dT%H:%M:%SZ")
    if result.get("id_token"):
        claims=extract_claims(result["id_token"])
        auth_claims=claims.get("https://api.openai.com/auth") or {}
        account=auth_claims.get("chatgpt_account_id") or claims.get("chatgpt_account_id")
        if account:changed["account_id"]=account
    return changed


def refresh_auth(force=True):
    path,original,auth=read_auth()
    if not force and not expiry_soon(auth):
        print("[oauth] 令牌尚未临期，跳过自动刷新")
        return auth
    updated=oauth_refresh(auth)
    if not flag("CODEX_AUTH_WRITE_BACK",True):
        raise ValueError("CODEX_AUTH_WRITE_BACK=false，刷新结果无法安全持久保存，已阻止执行")
    # 防止其他 CPA 实例刚刚轮换 refresh token，禁止覆盖并发写入。
    if path.read_bytes()!=original:
        raise RuntimeError("刷新期间登录 JSON 已被其他进程修改，不覆盖该文件")
    atomic_write(path,json.dumps(updated,ensure_ascii=False,indent=2).encode()+b"\n",private=True)
    print("[oauth] 登录 JSON 已原子更新，日志不显示 Token")
    return updated


def parse_usage(payload,now=None):
    now=int(time.time()) if now is None else now
    rate=payload.get("rate_limit",payload.get("rate_limits",{}))
    if not isinstance(rate,dict):raise ValueError("Codex 响应缺少 rate_limit")
    windows=[]
    for keys,default_min in ((("five_hour","primary_window","primary"),300),
                             (("weekly","secondary_window","secondary"),10080)):
        item=next((rate[k] for k in keys if isinstance(rate.get(k),dict)),None)
        if item is None:continue
        def num(keys):
            for key in keys:
                if item.get(key) is not None:return float(item[key])
            return None
        used=num(("used_percent","usedPercent"))
        if used is None:
            remain=num(("percent_left","percentLeft"))
            if remain is not None:used=100-remain
        if used is None or not 0<=used<=100:raise ValueError("Codex 额度窗口缺少有效百分比")
        minutes=num(("window_minutes","windowMinutes","window_duration_mins","windowDurationMins"))
        if minutes is None:
            seconds=num(("limit_window_seconds",))
            minutes=seconds/60 if seconds is not None else default_min
        minutes=round(minutes)
        if minutes<=0:raise ValueError("额度时间窗口无效")
        val=next((item[k] for k in ("reset_at","resetAt","resetsAt") if item.get(k) is not None),None)
        if val is None:
            after=num(("reset_after_seconds","resetAfterSeconds"))
            if after is None:raise ValueError("缺少额度重置时间")
            resets=now+round(after)
        else:resets=timestamp(val)
        name=("5小时" if minutes==300 else "7天" if minutes==10080 else
              f"{minutes//60}小时" if minutes%60==0 else f"{minutes}分钟")
        windows.append((minutes,dict(name=name,used_percent=round(used),reset_at=resets)))
    if not windows:raise ValueError("没有可用的额度窗口")
    credits=payload.get("rate_limit_reset_credits",payload.get("rateLimitResetCredits",{})) or {}
    credit=int(credits.get("available_count",credits.get("availableCount",0)) or 0)
    return dict(windows=[item for _,item in sorted(windows)],credits=max(0,credit),stale=False)


def fetch_quota(auth):
    def request(token):
        return get_json(env("CODEX_USAGE_URL",USAGE_URL),headers={
            "Authorization":"Bearer "+token,"ChatGPT-Account-Id":account_id(auth),
            "User-Agent":"codex_cli_rs/0.1.0"},timeout=35)
    token=active_auth(auth).get("access_token","")
    if not token:raise ValueError("Codex 缺少 access_token")
    return parse_usage(request(token))


def fetch_radar():
    raw=get_json(env("RADAR_STATUS_URL","https://codex-resets.com/api/v1/status"),
                 timeout=8,max_bytes=64*1024)
    info=raw.get("data")
    if not isinstance(info,dict) or "latest_reset" not in info or "active_watch" not in info:
        raise ValueError("重置雷达 API 数据格式不完整")
    if info["active_watch"] is not None:
        w=info["active_watch"]
        if not isinstance(w,dict) or w.get("level") not in ("strong","elevated"):
            raise ValueError("重置雷达预测数据无效")
    return dict(data=info,updated=int(time.time()),stale=False)


def radar_display(radar,now=None):
    now=int(time.time()) if now is None else now
    raw=radar.get("data",{})
    conf=raw.get("latest_reset")
    watch=raw.get("active_watch")
    ts=radar.get("updated",0)
    fresh=not radar.get("stale",True)
    if isinstance(conf,dict):
        try:
            if not 0<=now-timestamp(conf["announced_at"])<86400:conf=None
        except (ValueError,KeyError):conf=None
    if isinstance(watch,dict):
        try:
            if not timestamp(watch["observed_at"])<=now<timestamp(watch["expires_at"]):watch=None
            elif conf and timestamp(watch["observed_at"])<=timestamp(conf["announced_at"]):watch=None
        except (ValueError,KeyError):watch=None
    if watch:return "watch",watch,not fresh
    if conf:return "confirmed",conf,not fresh
    if "active_watch" in raw and raw["active_watch"] is None and now-ts<=7200 and fresh:
        return "empty",{},False
    return "unavailable",{},True


def quota_card(f,win,x,y,w=186,single=False):
    f.card(x,y,w,142,2,2)
    f.rect(x+2,y+2,w-4,24 if single else 23,False)
    f.rect(x+2,y+(26 if single else 25),w-4,2)
    tab=88 if single else 72
    f.rect(x+2,y+2,tab,24 if single else 23)
    for dy in range(24 if single else 23):
        cut=dy//3
        if cut:f.rect(x+2+tab,y+2+dy,cut,1)
    f.c12(win["name"],x+2+tab//2,y+7,False,"center",1)
    remaining=max(0,100-int(win["used_percent"]))
    date_local=datetime.fromtimestamp(win["reset_at"],TZ8)
    stamp=f"{date_local:%Y}年{date_local:%m}月{date_local:%d}日 {date_local:%H:%M}"
    if single:
        f.c12("配额实时状态看板",x+w-12,y+7,True,"right",1)
        xx=x+16
        for ch in str(remaining):f.digit(ch,xx,y+38,28,48,7);xx+=34
        f.percent(xx+4,y+62,22,4)
        f.c12("剩余额度 / 已用",x+16,y+96)
        f.number(str(win["used_percent"])+"%",x+118,y+96,7,13,2)
        f.dotted_v(x+176,y+32,y+134)
        f.c12("当前额度余量",x+190,y+32)
        f.gauge(remaining,x+190,y+52,178,14,10)
        f.rect(x+190,y+78,178,1)
        f.rect(x+190,y+86,36,16)
        f.c12("重置",x+208,y+88,False,"center")
        f.c12("精确重置时刻",x+232,y+86)
        draw_reset_stamp(f,stamp,x+190,y+110,8)
    else:
        f.c12("剩余额度",x+w-8,y+7,True,"right",1)
        xx=x+10
        for ch in str(remaining):f.digit(ch,xx,y+32,22,36,6);xx+=26
        f.percent(xx+2,y+48,18,3)
        f.dotted_v(x+w-62,y+31,y+69)
        f.c12("已用",x+w-34,y+30,True,"center")
        f.number(str(win["used_percent"])+"%",x+w-57,y+51,7,13,2)
        f.gauge(remaining,x+10,y+75,w-20,12,10)
        f.rect(x+2,y+96,w-4,1)
        f.rect(x+8,y+101,34,16)
        f.c12("重置",x+25,y+103,False,"center")
        f.c12("窗口重置时刻",x+46,y+101)
        draw_reset_stamp(f,stamp,x+10,y+122,7)


def draw_reset_stamp(f,stamp,x,y,dw):
    chunks=re.findall(r"\d+|[^\d]+",stamp)
    for chunk in chunks:
        if chunk[0].isdigit():
            x+=f.number(chunk,x,y,dw,13 if dw==7 else 15,2)+2
        else:
            f.c12(chunk,x,y+1,gap=1)
            x+=len(chunk)*13


def render_quota(quota,radar,now=None):
    now=int(time.time()) if now is None else now
    f=Frame()
    f.rect(8,6,382,26)
    for x,y in ((8,6),(389,6),(8,31),(389,31)):f.px(x,y,False)
    f.rect(12,10,66,18,False)
    f.micro("CODEX",16,12,True,gap=2,sx=2,sy=2)
    f.c16("配额监控",86,10,False)
    if quota.get("credits",0)>0:
        f.stroke(158,10,82,18,1,False)
        f.c12("重置券×",165,12,False)
        f.number(str(quota["credits"]),220,13,7,12,2,on=False)
    if quota.get("stale"):
        f.rect(276,9,110,20,False)
        f.rect(281,12,14,14);f.rect(287,14,2,6,False);f.rect(287,22,2,2,False)
        f.c12("额度未更新",300,12)
    else:
        f.stroke(308,10,78,18,1,False)
        f.rect(315,16,6,6,False)
        f.c12("实时同步",326,12,False)
    wins=quota["windows"]
    if len(wins)==1:quota_card(f,wins[0],8,38,382,True)
    elif len(wins)==2:
        quota_card(f,wins[0],8,38)
        quota_card(f,wins[1],204,38)
    else:raise ValueError("额度窗口必须是一项或两项")
    # 重置雷达控制台与原版共用布局 X8 Y188 W382 H102
    rx,ry=8,188
    f.card(rx,ry,382,102,2,2)
    f.rect(rx+2,ry+2,378,24,False);f.rect(rx+2,ry+26,378,2)
    f.stroke(rx+11,ry+9,14,14)
    f.rect(rx+17,ry+11,2,8)
    f.rect(rx+14,ry+16,8,2)
    f.micro("CODEX",rx+30,ry+8,sx=2,sy=2,gap=2)
    f.c16("重置雷达",rx+94,ry+7)
    mode,details,stale=radar_display(radar,now)
    statuses={"unavailable":"雷达不可用","empty":"暂无预测",
              "confirmed":"储备重置已记录" if details.get("reset_type")=="banked" else "普通重置已记录",
              "watch":"缓存预测" if stale else "有预测"}
    status=statuses[mode]
    badge_w=max(68,min(160,len(status)*13+(28 if mode=="watch" and not stale else 16)))
    bx=rx+382-badge_w-8
    active=(mode=="confirmed" or (mode=="watch" and not stale))
    if active:f.rect(bx,ry+5,badge_w,18)
    else:f.stroke(bx,ry+5,badge_w,18)
    f.c12(status,bx+badge_w//2,ry+8,not active,"center",1)
    if mode=="watch":
        msg="预测窗口："+str(details.get("forecast_window",""))
        f.c12(msg[:15],rx+12,ry+35)
        probability=details.get("reset_chance_percent")
        if isinstance(probability,int) and 0<=probability<=100:
            f.c12("重置概率",rx+240,ry+35)
            f.number(str(probability)+"%",rx+308,ry+33,9,15,2)
            f.gauge(probability,rx+12,ry+56,358,14,20)
            for label,xx in (("0%",rx+12),("50%",rx+174),("100%",rx+347)):
                f.micro(label,xx,ry+78)
        else:
            f.c12("概率未提供",rx+282,ry+35)
            f.gauge(0,rx+12,ry+56,358,14,20)
    else:
        f.stroke(rx+12,ry+36,358,54,1)
        f.dots(rx+14,ry+38,36,50)
        if mode=="confirmed":lines=("公开重置已记录","个人额度以实际查询为准")
        elif mode=="empty":lines=("当前无公共重置预警信号","额度按上方窗口时间重置")
        else:lines=("暂时无法取得公共雷达数据","个人额度以实际查询为准")
        f.c16(lines[0],rx+60,ry+42)
        f.c12(lines[1],rx+60,ry+66)
    return f


# --- 3. 全国区县天气源与 NOTE4 画廊气象布局 ----------------------------------
def weather_catalog():
    path=Path(env("WEATHER_LOCATIONS_FILE",str(ROOT/"weather_locations.json")))
    if not path.is_absolute():path=ROOT/path
    with path.open(encoding="utf-8") as f:
        obj=json.load(f)
    items=obj.get("locations",obj) if isinstance(obj,dict) else obj
    if not isinstance(items,list):raise ValueError("天气地址簿格式错误")
    return items


def resolve_location(query):
    items=weather_catalog()
    lon,lat=None,None
    try:
        raw=query.replace("，",",").split(",")
        if len(raw)==2:
            a,b=float(raw[0]),float(raw[1])
            if 70<=a<=140 and 0<=b<=60:lon,lat=a,b
            elif 70<=b<=140 and 0<=a<=60:lon,lat=b,a
    except (ValueError,TypeError):
        pass
    if env("WEATHER_LON") and env("WEATHER_LAT"):
        lon=float(env("WEATHER_LON"));lat=float(env("WEATHER_LAT"))
    if lon is not None and lat is not None:
        def dist(v):
            x1,y1=math.radians(lon),math.radians(lat)
            x2,y2=math.radians(float(v["lon"])),math.radians(float(v["lat"]))
            h=math.sin((y2-y1)/2)**2+math.cos(y1)*math.cos(y2)*math.sin((x2-x1)/2)**2
            return 2*6371*math.asin(min(1,math.sqrt(h)))
        chosen=min(items,key=dist)
        return dict(chosen,lon=lon,lat=lat)
    normalized=re.sub(r"[\s·•\-省市区县自治州]","",query.lower())
    ranked=[]
    for item in items:
        terms=[item.get("city","")+item.get("district",""),item.get("district",""),
               item.get("city",""),item.get("province","")]+item.get("aliases",[])
        score=0
        for term in terms:
            key=re.sub(r"[\s·•\-省市区县自治州]","",str(term).lower())
            if key and key==normalized:score=max(score,100+len(key))
            elif key and len(key)>1 and key in normalized:score=max(score,30+len(key))
            elif normalized and normalized in key:score=max(score,10+len(normalized))
        if score:ranked.append((score,item))
    if not ranked:raise ValueError(f"天气地址未匹配：{query}，使用 --locations 检索支持的别名或输入经纬度")
    return max(ranked,key=lambda v:v[0])[1]


def weather_icon(condition):
    raw=str(condition)
    if "雷" in raw:return "ThunderRain"
    if "雪" in raw:return "Snow"
    if "雨" in raw:return "Rain"
    if "雾" in raw or "霾" in raw:return "FogHaze"
    if "风" in raw and "多云" not in raw:return "Wind"
    if "阴" in raw:return "Cloudy"
    if "云" in raw:return "PartlyCloudy"
    return "Sunny"


def wmo_icon(code):
    code=int(code)
    if code==0:return "Sunny","晴"
    if code in (1,2):return "PartlyCloudy","多云"
    if code==3:return "Cloudy","阴"
    if code in (45,48):return "FogHaze","雾"
    if code in (71,73,75,77,85,86):return "Snow","雪"
    if code in (95,96,99):return "ThunderRain","雷阵雨"
    if code in (51,53,55,56,57,61,63,65,66,67,80,81,82):return "Rain","阵雨"
    return "PartlyCloudy","多云"


def wind_compass(deg):
    words=("北风","东北风","东风","东南风","南风","西南风","西风","西北风")
    return words[round((float(deg)%360)/45)%8]


def wind_beaufort(kmh):
    scale=(1,5,11,19,28,38,49,61,74,88,102,117)
    return sum(float(kmh)>=threshold for threshold in scale)


def aqi_grade(aqi):
    if aqi<=50:return "优"
    if aqi<=100:return "良"
    if aqi<=150:return "轻"
    if aqi<=200:return "中"
    return "差"


def weather_base(place,page,temp,cond,**kwargs):
    """Shared normalized weather scene; missing real observation is an error."""
    if temp is None or not -90<=float(temp)<=65:
        raise ValueError("天气源缺少有效实时温度")
    temp=round(float(temp))
    now=datetime.now(TZ8)
    defaults=dict(city=place["city"],district=place.get("district",""),
                  page_id=page,updated_at=now.strftime("%H:%M"),
                  hero_icon=weather_icon(cond),condition=cond,
                  hero_sub_cn="湿度",hero_sub_val="--",
                  temp_c=temp,feels_like=temp,low_c=temp,high_c=temp,
                  humidity=None,wind_dir="--",wind_scale=None,
                  aqi_grade="--",aqi=None,alert=None,
                  radar_summary="短临降水未知",minute_bars=[0.0]*24,
                  hourly_points=[],daily_forecast=[],source_cn="")
    defaults.update(kwargs)
    if defaults["humidity"] is not None:
        defaults["hero_sub_val"]=f'{defaults["humidity"]}%'
    if defaults["aqi"] is not None:
        defaults["aqi_grade"]=aqi_grade(defaults["aqi"])
    return defaults


def forecast_day(idx,when,icon,cond,low,high):
    try:day=date.fromisoformat(str(when)[:10])
    except ValueError:day=datetime.now(TZ8).date()+timedelta(days=idx)
    today=datetime.now(TZ8).date()
    diff=(day-today).days
    word="今天" if diff==0 else "明天" if diff==1 else ("周一","周二","周三","周四","周五","周六","周日")[day.weekday()]
    return dict(day_cn=word,date=day.strftime("%m/%d"),icon=icon,
                cond=cond[:2],low=round(float(low)),high=round(float(high)))


def fetch_open_meteo(place,page):
    host=env("OPEN_METEO_API_BASE","https://api.open-meteo.com").rstrip("/")
    query=urlencode({
      "latitude":round(float(place["lat"]),4),"longitude":round(float(place["lon"]),4),
      "current":"temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,weather_code,surface_pressure,wind_speed_10m,wind_direction_10m",
      "minutely_15":"precipitation","hourly":"temperature_2m",
      "daily":"weather_code,temperature_2m_max,temperature_2m_min",
      "timezone":"auto","forecast_days":5})
    obj=get_json(host+"/v1/forecast?"+query,timeout=integer("WEATHER_HTTP_TIMEOUT_SECS",15,3))
    cur=obj["current"];temp=cur.get("temperature_2m")
    icon,condition=wmo_icon(cur.get("weather_code",1))
    bars=[]
    for n in obj.get("minutely_15",{}).get("precipitation",[])[:8]:
        value=0.0 if not n or n<.02 else max(.12,min(1,n/3))
        bars.extend([value]*3)
    bars=(bars+[0.0]*24)[:24]
    rainy=sum(v>0 for v in bars)
    hourly=[]
    times=obj.get("hourly",{}).get("time",[])
    temps=obj.get("hourly",{}).get("temperature_2m",[])
    step=integer("WEATHER_HOURLY_STEP_HOURS",4,1,6)
    for i in range(0,min(len(times),len(temps)),step):
        if temps[i] is None:continue
        hourly.append((str(times[i])[-5:-3]+"H",round(temps[i])))
        if len(hourly)>=integer("WEATHER_HOURLY_POINTS",5,3,7):break
    daily=[]
    d=obj.get("daily",{})
    for idx,raw in enumerate(d.get("time",[])[:5]):
        try:
            low=d["temperature_2m_min"][idx];high=d["temperature_2m_max"][idx]
            wc=d["weather_code"][idx]
            di,dc=wmo_icon(wc)
            daily.append(forecast_day(idx,raw,di,dc,low,high))
        except (IndexError,TypeError,KeyError,ValueError):continue
    if len(daily)<3 or len(hourly)<2:raise ValueError("Open-Meteo 预报数据不完整")
    wind=cur.get("wind_speed_10m")
    return weather_base(place,page,temp,condition,source_cn="Open-Meteo",hero_icon=icon,
        feels_like=round(cur.get("apparent_temperature") or temp),
        humidity=round(cur["relative_humidity_2m"]) if cur.get("relative_humidity_2m") is not None else None,
        wind_dir=wind_compass(cur.get("wind_direction_10m",0)),
        wind_scale=wind_beaufort(wind or 0),
        low_c=daily[0]["low"],high_c=daily[0]["high"],
        radar_summary="未来两小时无雨" if not rainy else f"未来降水{rainy*5}分",
        minute_bars=bars,hourly_points=hourly,daily_forecast=daily)


def get_path(obj,*path):
    for name in path:
        if not isinstance(obj,dict):return None
        obj=obj.get(name)
    return obj


def valid_number(value):
    try:
        result=float(value)
        if math.isfinite(result) and -90<=result<=65:return result
    except (TypeError,ValueError):pass
    return None


def fetch_nmc(place,page):
    base=env("NMC_API_BASE","http://www.nmc.cn").rstrip("/")
    station=env("NMC_STATION_ID",place.get("nmc_station_id",""))
    if not station:raise ValueError("地区没有中央台气象站 ID")
    data=get_json(base+"/rest/weather?"+urlencode({"stationid":station}),
                  timeout=integer("WEATHER_HTTP_TIMEOUT_SECS",15,3)).get("data",{})
    real=get_path(data,"real","weather") or {}
    temp=valid_number(real.get("temperature"))
    if temp is None:raise ValueError("中央台缺少有效实时气温")
    cond=str(real.get("info") or "多云")
    humidity=real.get("humidity")
    try:humidity=max(0,min(100,round(float(humidity))))
    except (ValueError,TypeError):humidity=None
    aqi=get_path(data,"air","aqi")
    try:aqi=max(1,int(aqi))
    except (ValueError,TypeError):aqi=None
    wind=get_path(data,"real","wind") or {}
    power=re.search(r"\d+",str(wind.get("power","")))
    daily=[]
    for idx,item in enumerate(get_path(data,"predict","detail") or []):
        dday=item.get("day") or {};night=item.get("night") or {}
        high=valid_number(get_path(dday,"weather","temperature"))
        low=valid_number(get_path(night,"weather","temperature"))
        if low is None or high is None:continue
        dcond=get_path(dday,"weather","info") or get_path(night,"weather","info") or cond
        daily.append(forecast_day(idx,item.get("date",""),weather_icon(dcond),dcond,min(low,high),max(low,high)))
        if len(daily)>=5:break
    hourly=[]
    for item in reversed((data.get("passedchart") or [])[:6]):
        value=valid_number(item.get("temperature"))
        if value is not None:hourly.append((str(item.get("time",""))[-5:-3]+"H",round(value)))
    if len(daily)<3 or len(hourly)<2:
        raise ValueError("中央台有效预报不足，使用其他来源")
    alert=get_path(data,"real","warn","alert")
    if alert in ("9999","-",""):alert=None
    return weather_base(place,page,temp,cond,source_cn="中央气象台",alert=alert,
        feels_like=round(float(real.get("feelst") or temp)),
        humidity=humidity,wind_dir=str(wind.get("direct") or "--"),
        wind_scale=int(power.group()) if power else None,aqi=aqi,
        low_c=daily[0]["low"],high_c=daily[0]["high"],
        radar_summary="局地短时降水" if "雨" in cond or "雪" in cond else "短临降水未知",
        hourly_points=hourly,daily_forecast=daily)


def get_text(url,headers=None):
    req=Request(url,headers={"User-Agent":"Mozilla/5.0 ZectrixQinglong/1.0",**(headers or {})})
    with urlopen(req,timeout=integer("WEATHER_HTTP_TIMEOUT_SECS",15,3)) as resp:
        raw=resp.read(2_000_001)
        if len(raw)>2_000_000:raise ValueError("天气网页过大")
        try:return raw.decode("utf-8")
        except UnicodeDecodeError:return raw.decode("gb18030",errors="replace")


def extract_js_var(raw,name):
    match=re.search(r"\bvar\s+"+re.escape(name)+r"\s*=\s*(\{.*?\})\s*;",raw,re.S)
    if not match:return {}
    return json.loads(match.group(1))


def fetch_weathercn(place,page):
    code=env("WEATHER_CN_CODE",place.get("weather_cn_code",""))
    if not re.fullmatch(r"\d{9}",code):raise ValueError("缺少中国天气网代码")
    web=env("WEATHER_CN_WEB_BASE","http://www.weather.com.cn").rstrip("/")
    sk=env("WEATHER_CN_SK_BASE","http://d1.weather.com.cn").rstrip("/")
    html=get_text(f"{web}/weather1d/{code}.shtml")
    live=extract_js_var(get_text(f"{sk}/sk_2d/{code}.html",{"Referer":web+"/"}),"dataSK")
    temp=valid_number(live.get("temp"))
    if temp is None:raise ValueError("中国天气网实况温度缺失")
    humidity=re.search(r"\d+",str(live.get("SD","")))
    cond=live.get("weather") or "多云"
    hourly=[];daily=[]
    hdata=extract_js_var(html,"hour3data")
    for item in hdata.get("1d",[]):
        parts=str(item).split(",")
        if len(parts)<4:continue
        value=valid_number(re.sub(r"[℃°C]","",parts[3]))
        if value is not None:
            hr=re.search(r"(\d{1,2})时",parts[0])
            hourly.append((f"{int(hr.group(1)):02}H" if hr else "12H",round(value)))
        if len(hourly)>=7:break
    for idx,items in enumerate(hdata.get("7d",[])):
        vals=[];day_cond=cond
        for raw in items if isinstance(items,list) else []:
            cols=str(raw).split(",")
            if len(cols)<4:continue
            v=valid_number(re.sub(r"[℃°C]","",cols[3]))
            if v is not None:vals.append(v)
            if cols[2]:day_cond=cols[2]
        if vals:
            daily.append(forecast_day(idx,"",weather_icon(day_cond),day_cond,min(vals),max(vals)))
        if len(daily)>=5:break
    if len(daily)<3 or len(hourly)<2:raise ValueError("中国天气网完整预报不足")
    aqi=live.get("aqi")
    return weather_base(place,page,temp,cond,source_cn="中国天气网",
        humidity=int(humidity.group()) if humidity else None,
        wind_dir=str(live.get("WD","--")),wind_scale=None,
        aqi=int(aqi) if str(aqi).isdigit() else None,
        low_c=daily[0]["low"],high_c=daily[0]["high"],
        hourly_points=hourly,daily_forecast=daily)


def fetch_moji_web(place,page):
    slug=env("MOJI_WEB_SLUG",place.get("moji_slug",""))
    if not slug:raise ValueError("缺少墨迹网页版地区路径")
    html=get_text(env("MOJI_WEB_BASE","https://tianqi.moji.com").rstrip("/")+f"/weather/china/{slug}")
    def strip(raw):
        return re.sub(r"\s+"," ",re.sub(r"<[^>]+>"," ",raw)).strip()
    live=re.search(r'class=["\'][^"\']*wea_weather[^"\']*["\'][^>]*>(.*?)</div>',html,re.S|re.I)
    if not live:raise ValueError("墨迹网页版未找到实况区域")
    temp_match=re.search(r"(-?\d+)\s*°",strip(live.group(1)))
    if not temp_match:raise ValueError("墨迹网页缺少实时温度")
    temp=int(temp_match.group(1))
    cond_match=re.search(r"<b[^>]*>(.*?)</b>",live.group(1),re.S)
    cond=strip(cond_match.group(1)) if cond_match else "多云"
    days=re.findall(r"<li[^>]*>(.*?)</li>",html,re.S|re.I)
    daily=[]
    for idx,line in enumerate(days):
        txt=strip(line)
        nums=re.findall(r"(-?\d+)\s*[°℃]",txt)
        if len(nums)<2:continue
        lo,hi=sorted((int(nums[0]),int(nums[1])))
        daily.append(forecast_day(idx,"",weather_icon(txt),cond,lo,hi))
        if len(daily)>=5:break
    # 原站没有稳定的 24H 温度字段；以 Open-Meteo 的真实逐小时数据补齐。
    # 明确记录来源，不把合成时间曲线冒充墨迹天气的独立观测值。
    supplemental=fetch_open_meteo(place,page)
    result=dict(supplemental)
    result["temp_c"]=temp
    result["hero_icon"]=weather_icon(cond)
    result["condition"]=cond
    result["source_cn"]="墨迹实况 + Open-Meteo 预报"
    if len(daily)>=3:
        result["daily_forecast"]=daily
        result["low_c"]=daily[0]["low"]
        result["high_c"]=daily[0]["high"]
    return result


def fetch_qweather(place,page):
    key=env("QWEATHER_KEY")
    if not key:raise ValueError("未配置 QWEATHER_KEY")
    host=env("QWEATHER_HOST","https://devapi.qweather.com").rstrip("/")
    args=urlencode({"location":f'{place["lon"]},{place["lat"]}',"key":key})
    now=get_json(host+"/v7/weather/now?"+args)
    daily_json=get_json(host+"/v7/weather/7d?"+args)
    hourly_json=get_json(host+"/v7/weather/24h?"+args)
    if any(v.get("code")!="200" for v in (now,daily_json,hourly_json)):
        raise ValueError("和风天气 API 返回非 200 业务状态")
    current=now["now"]
    temperature=valid_number(current.get("temp"))
    if temperature is None:raise ValueError("和风天气缺少实况")
    cond=str(current.get("text","多云"))
    daily=[]
    for idx,item in enumerate(daily_json.get("daily",[])[:5]):
        low=valid_number(item.get("tempMin"));high=valid_number(item.get("tempMax"))
        if low is not None and high is not None:
            name=item.get("textDay") or cond
            daily.append(forecast_day(idx,item.get("fxDate",""),weather_icon(name),name,low,high))
    hourly=[]
    for item in hourly_json.get("hourly",[])[::4][:7]:
        v=valid_number(item.get("temp"))
        if v is not None:hourly.append((str(item.get("fxTime",""))[11:13]+"H",round(v)))
    if len(daily)<3 or len(hourly)<2:raise ValueError("和风预报不完整")
    return weather_base(place,page,temperature,cond,source_cn="和风天气",
        feels_like=round(float(current.get("feelsLike") or temperature)),
        humidity=int(current["humidity"]) if str(current.get("humidity","")).isdigit() else None,
        wind_dir=str(current.get("windDir") or "--"),
        wind_scale=int(current.get("windScale") or 0),
        low_c=daily[0]["low"],high_c=daily[0]["high"],
        hourly_points=hourly,daily_forecast=daily)


def fetch_caiyun(place,page):
    key=env("CAIYUN_TOKEN")
    if not key:raise ValueError("未配置 CAIYUN_TOKEN")
    host=env("CAIYUN_API_BASE","https://api.caiyunapp.com").rstrip("/")
    version=env("CAIYUN_API_VERSION","v2.6")
    url=f'{host}/{version}/{key}/{place["lon"]},{place["lat"]}/weather.json?'+urlencode({
        "alert":"true","dailysteps":5,"hourlysteps":24})
    data=get_json(url)
    if data.get("status")!="ok":raise ValueError("彩云 API 返回非 ok")
    result=data.get("result",{})
    now=result.get("realtime",{})
    temp=valid_number(now.get("temperature"))
    if temp is None:raise ValueError("彩云缺少实时温度")
    sky=str(now.get("skycon","CLOUDY"))
    kinds={"CLEAR_DAY":("Sunny","晴"),"CLEAR_NIGHT":("Sunny","晴"),
           "PARTLY_CLOUDY_DAY":("PartlyCloudy","多云"),"CLOUDY":("Cloudy","阴"),
           "LIGHT_RAIN":("Rain","小雨"),"MODERATE_RAIN":("Rain","中雨"),
           "HEAVY_RAIN":("Rain","大雨"),"LIGHT_SNOW":("Snow","小雪"),
           "FOG":("FogHaze","雾")}
    icon,cond=kinds.get(sky,("PartlyCloudy","多云"))
    daily=[]
    daily_result=result.get("daily",{})
    temps=daily_result.get("temperature",[])
    skycons=daily_result.get("skycon",[])
    for idx,item in enumerate(temps[:5]):
        low=valid_number(item.get("min"));high=valid_number(item.get("max"))
        if low is None or high is None:continue
        raw=skycons[idx].get("value","CLOUDY") if idx<len(skycons) else "CLOUDY"
        ic,cn=kinds.get(raw,("PartlyCloudy","多云"))
        daily.append(forecast_day(idx,item.get("date",""),ic,cn,low,high))
    hourly=[]
    for item in result.get("hourly",{}).get("temperature",[])[::4][:7]:
        value=valid_number(item.get("value"))
        if value is not None:hourly.append((str(item.get("datetime",""))[11:13]+"H",round(value)))
    if len(daily)<3 or len(hourly)<2:raise ValueError("彩云预报数据不足")
    humidity=now.get("humidity")
    humidity=round(float(humidity)*100) if humidity is not None else None
    return weather_base(place,page,temp,cond,source_cn="彩云天气",hero_icon=icon,
        feels_like=round(float(now.get("apparent_temperature") or temp)),
        humidity=humidity,low_c=daily[0]["low"],high_c=daily[0]["high"],
        hourly_points=hourly,daily_forecast=daily)


def fetch_moji_aliyun(place,page):
    appcode=env("MOJI_APPCODE")
    if not appcode:raise ValueError("未配置 MOJI_APPCODE")
    base=env("MOJI_API_BASE","http://aliv18.data.moji.com").rstrip("/")
    city_id=env("MOJI_CITY_ID","2")
    tokens={
      "condition":env("MOJI_TOKEN_CONDITION","50b53ff8dd7d9fa320d3d3ca32cf8ed1"),
      "forecast24hours":env("MOJI_TOKEN_24H","008d2ad9197090c5dddc76f583616606"),
      "forecast15days":env("MOJI_TOKEN_15D","f9f212e1996e79e0e602b08ea297ffb0"),
      "aqi":env("MOJI_TOKEN_AQI","8b36edf8e3444047812be3a59d27bab9")}
    def request(endpoint):
        return post_form_json(base+"/whapi/json/alicityweather/"+endpoint,
               {"cityId":city_id,"token":tokens[endpoint]},
               headers={"Authorization":"APPCODE "+appcode},
               timeout=integer("WEATHER_HTTP_TIMEOUT_SECS",15,3))
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures={name:pool.submit(request,name) for name in tokens}
        result={}
        for name,future in futures.items():
            try:result[name]=future.result()
            except Exception as exc:
                if name=="condition":raise ValueError("墨迹阿里云实况接口失败") from exc
                print(f"[weather] moji {name} 辅助接口失败：{exc}")
    live=get_path(result["condition"],"data","condition") or {}
    temp=valid_number(live.get("temp"))
    if temp is None:raise ValueError("墨迹阿里云缺少有效温度")
    cond=str(live.get("condition") or "多云")
    daily=[]
    for idx,item in enumerate((get_path(result.get("forecast15days"),"data","forecast") or [])[1:6]):
        low=valid_number(item.get("tempNight"));high=valid_number(item.get("tempDay"))
        if low is None or high is None:continue
        name=str(item.get("conditionDay") or cond)
        daily.append(forecast_day(idx,item.get("predictDate",""),weather_icon(name),name,low,high))
    hourly=[]
    hours=get_path(result.get("forecast24hours"),"data","hourly") or []
    step=integer("WEATHER_HOURLY_STEP_HOURS",4,1,6)
    for item in hours[::step][:7]:
        val=valid_number(item.get("temp"))
        if val is None:continue
        hourly.append((str(item.get("hour","12")).zfill(2)+"H",round(val)))
    if len(hourly)<2 or len(daily)<3:
        raise ValueError("墨迹阿里云预报不完整，不生成伪造曲线")
    aqi=get_path(result.get("aqi"),"data","aqi","value")
    try:aqi=max(1,int(aqi))
    except (TypeError,ValueError):aqi=None
    humidity=live.get("humidity")
    try:humidity=max(0,min(100,round(float(humidity))))
    except (TypeError,ValueError):humidity=None
    return weather_base(place,page,temp,cond,source_cn="墨迹阿里云",
        feels_like=round(float(live.get("realFeel") or temp)),
        humidity=humidity,wind_dir=str(live.get("windDir") or "--"),
        wind_scale=int(live.get("windLevel") or 0),
        aqi=aqi,low_c=daily[0]["low"],high_c=daily[0]["high"],
        hourly_points=hourly,daily_forecast=daily)


def fetch_source(name,place,page):
    aliases={"weather_cn_web":"weathercn","weather_cn":"weathercn",
             "open-meteo":"open_meteo","openmeteo":"open_meteo"}
    name=aliases.get(name.strip().lower(),name.strip().lower())
    sources={"open_meteo":fetch_open_meteo,"nmc":fetch_nmc,
             "weathercn":fetch_weathercn,"moji_web":fetch_moji_web,
             "caiyun":fetch_caiyun,"qweather":fetch_qweather,
             "moji":fetch_moji_aliyun}
    if name not in sources:raise ValueError(f"该 Python 适配尚未实现天气源 {name}")
    return sources[name](place,page)


def combine_weather(snaps):
    if not snaps:raise ValueError("没有有效天气源")
    if len(snaps)==1:return snaps[0]
    # 只对真实、存在的观测字段求中位数；不注入伪造常量。
    result=dict(snaps[0])
    for field in ("temp_c","feels_like","humidity","aqi"):
        valid=[s[field] for s in snaps if isinstance(s.get(field),(int,float))]
        result[field]=round(statistics.median(valid)) if valid else None
    for s in snaps:
        if s.get("alert"):result["alert"]=s["alert"];break
    rain=next((s for s in snaps if s["hero_icon"] in ("Rain","Snow","ThunderRain")),None)
    if rain:
        result["hero_icon"]=rain["hero_icon"];result["condition"]=rain["condition"]
    result["minute_bars"]=[max(s["minute_bars"][i] for s in snaps) for i in range(24)]
    best=next((s for s in snaps if any(x>0 for x in s["minute_bars"])),None)
    if best:result["radar_summary"]=best["radar_summary"]
    for idx,item in enumerate(result["hourly_points"]):
        vals=[s["hourly_points"][idx][1] for s in snaps if len(s["hourly_points"])>idx]
        result["hourly_points"][idx]=(item[0],round(statistics.median(vals)))
    for idx,item in enumerate(result["daily_forecast"]):
        lows=[s["daily_forecast"][idx]["low"] for s in snaps if len(s["daily_forecast"])>idx]
        highs=[s["daily_forecast"][idx]["high"] for s in snaps if len(s["daily_forecast"])>idx]
        item["low"]=round(statistics.median(lows))
        item["high"]=max(item["low"]+1,round(statistics.median(highs)))
    result["low_c"]=result["daily_forecast"][0]["low"]
    result["high_c"]=result["daily_forecast"][0]["high"]
    result["aqi_grade"]=aqi_grade(result["aqi"]) if result["aqi"] is not None else "--"
    result["hero_sub_val"]=f'{result["humidity"]}%' if result["humidity"] is not None else "--"
    result["source_cn"]="聚合："+"/".join(s["source_cn"] for s in snaps)
    return result


def fetch_weather(place,page):
    mode=env("WEATHER_PROVIDER","aggregate").lower()
    raw=(env("WEATHER_AGGREGATE_SOURCES","nmc,weathercn,moji_web,open_meteo,caiyun,qweather,moji")
         if mode=="aggregate" else
         env("WEATHER_FALLBACK_ORDER","nmc,weathercn,moji_web,open_meteo,caiyun,qweather,moji") if mode=="auto" else mode)
    names=list(dict.fromkeys(s.strip() for s in raw.split(",") if s.strip()))
    if mode=="aggregate":
        gathered={}
        with ThreadPoolExecutor(max_workers=min(6,len(names))) as pool:
            futures={pool.submit(fetch_source,name,place,page):name for name in names}
            for future in as_completed(futures):
                name=futures[future]
                try:
                    gathered[name]=future.result()
                    print(f"[weather] {name} 获取成功")
                except Exception as exc:
                    print(f"[weather] {name} 不可用：{exc}")
        return combine_weather([gathered[name] for name in names if name in gathered])
    for name in names:
        try:return fetch_source(name,place,page)
        except Exception as exc:print(f"[weather] {name} 不可用：{exc}")
    raise ValueError("所有天气源均未返回完整、可验证的数据")


def weather_demo(mode,page):
    place={"city":"北京","district":"海淀区"}
    icon={"rain":"ThunderRain","sunny":"PartlyCloudy","snow":"Snow"}.get(mode,"PartlyCloudy")
    condition={"rain":"阵雨转多云","sunny":"晴间多云","snow":"阵雪转晴"}.get(mode,"多云")
    temp={"rain":18,"sunny":23,"snow":-6}.get(mode,23)
    low=temp-7; high=temp+3
    days=[]
    start=date(2026,10,17)
    for i in range(4):
        daily_icon=icon if i==0 else ("Sunny" if i%2 else "PartlyCloudy")
        days.append(forecast_day(i,start+timedelta(days=i),daily_icon,
                                 condition if i==0 else "多云",low+i,high+i))
    for idx,d in enumerate(days):
        d["day_cn"]=("今天","明天","周一","周二")[idx]
    return weather_base(place,page,temp,condition,source_cn="样图示例",hero_icon=icon,
        updated_at="14:30",humidity=78,feels_like=temp-2,wind_dir="东南风",
        wind_scale=3,aqi=28,alert="雷电黄色预警" if mode=="rain" else None,
        low_c=low,high_c=high,
        radar_summary="35分后雨渐停" if mode=="rain" else "未来两小时无雨",
        minute_bars=([.72,.85,.92,.78,.62,.45,.28,.12]+[0]*16 if mode=="rain" else [0]*24),
        hourly_points=[("14H",temp),("18H",temp-3),("22H",temp-6),
                       ("02H",temp-7),("06H",temp-2)],daily_forecast=days)


def icon_bitmap(f,kind,x,y,big):
    key="hero_icons" if big else "mini_icons"
    sz=52 if big else 24
    rows=ASSETS[key].get(kind,ASSETS[key]["PartlyCloudy"])
    for py,bits in enumerate(rows):
        for px in range(sz):
            if bits & (1<<(sz-1-px)):f.px(x+px,y+py)


def temp_big(f,text,x,y,width=22,height=38,thick=5,gap=4,shadow=True):
    for ch in text:
        if ch=="-":
            f.rect(x+2,y+height//2,width-4,thick)
        else:f.digit(ch,x,y,width,height,thick,True,shadow)
        x+=width+gap


def render_weather(scene):
    f=Frame()
    m=integer("WEATHER_MARGIN_PX",4,2,8)
    border=integer("WEATHER_CARD_BORDER_PX",2,1,3)
    shadow=integer("WEATHER_CARD_SHADOW_PX",2,0,4)
    inv=flag("WEATHER_INVERT_MASTHEAD",True)
    inv_today=flag("WEATHER_INVERT_TODAY_HEADER",True)
    # 顶栏 V3 Gallery Card HUD
    if inv:
        f.rect(m,4,400-2*m,28)
        for x,y in ((m,4),(399-m,4),(m,31),(399-m,31)):
            f.px(x,y,False)
    else:f.card(m,4,400-2*m,28,border,0)
    mast_fg=not inv
    badge=env("WEATHER_HEADER_BADGE","气象站")
    bw=max(40,len(badge)*13+10)
    f.pill(m+4,8,bw,20,True,inv)
    f.c12(badge,m+4+bw//2,12,not mast_fg,"center",1)
    cx=m+4+bw+8
    city=str(scene["city"]); district=str(scene.get("district",""))
    f.c16(city,cx,10,mast_fg)
    dotx=cx+len(city)*18+5
    if district and district!=city:
        f.rect(dotx,17,2,2,mast_fg)
        f.c16(district,dotx+7,10,mast_fg)
    time_label=f'{scene.get("updated_at","--:--")} P{scene.get("page_id",2)}'
    tw=len(time_label)*6
    f.micro(time_label,396-m,15,mast_fg,"right",1)
    sep_x=396-m-tw-6
    f.rect(sep_x,10,1,16,mast_fg)
    if scene.get("alert"):
        alert="! "+scene["alert"]
        aw=len(alert)*13+10
        ax=sep_x-6-aw
        f.pill(ax,8,aw,20,True,inv)
        f.c12(alert,ax+aw//2,12,not mast_fg,"center",1)
    else:
        air=f'{scene.get("aqi_grade","--")} {scene.get("aqi") or "--"}'
        aw=len(air)*13+10
        ax=sep_x-6-aw
        f.pill(ax,9,aw,18,False,inv)
        f.c12(air,ax+aw//2,12,mast_fg,"center",1)
    # 左：实时天气
    f.card(4,37,188,156,border,shadow)
    icon_bitmap(f,scene["hero_icon"],14,46,True)
    temp=str(scene["temp_c"])
    dw=22 if len(temp)<=2 else 17
    gap=4 if len(temp)<=2 else 3
    temp_big(f,temp,76,46,dw,38,5 if dw==22 else 4,gap,
             flag("WEATHER_HERO_TEMP_SHADOW",True))
    tx=76+len(temp)*(dw+gap)-gap+5
    f.font7("°C",tx,48)
    f.font7(f'{scene["low_c"]}~{scene["high_c"]}°',tx,70)
    f.c16(str(scene["condition"]),16,100)
    f.c12(scene.get("hero_sub_cn","湿度")+" "+scene.get("hero_sub_val","--"),
          182,102,True,"right",1)
    f.rect(12,122,172,1)
    f.dotted_v(98,126,186)
    f.dotted_h(12,184,156)
    for k in range(-2,3):f.px(98+k,156);f.px(98,156+k)
    f.c12(env("WEATHER_LABEL_FEELS","体感"),14,134,gap=1)
    f.font7(f'{scene.get("feels_like", "--")}°C',90,135,True,"right")
    f.c12(env("WEATHER_LABEL_HUMIDITY","湿度"),106,134,gap=1)
    humidity=scene.get("humidity")
    f.font7(f'{humidity if humidity is not None else "--"}%',182,135,True,"right")
    f.c12(str(scene.get("wind_dir","--")),14,166,gap=1)
    f.c12("级",90,166,True,"right",1)
    f.font7(str(scene.get("wind_scale") if scene.get("wind_scale") is not None else "--"),
            76,167,True,"right")
    f.c12(env("WEATHER_LABEL_AQI","空气"),106,166,gap=1)
    f.pill(136,164,18,16,True,False)
    f.c12(str(scene.get("aqi_grade") or "--")[:1],145,166,False,"center",0)
    f.font7(str(scene.get("aqi") if scene.get("aqi") is not None else "--"),
            182,167,True,"right")
    # 右：时序趋势 + 短临降雨
    f.card(198,37,198,156,border,shadow)
    f.rect(200,62,194,1)
    f.pill(206,43,28,15,True,False)
    f.micro(env("WEATHER_TREND_BADGE","24H"),220,47,False,"center")
    f.c12(env("WEATHER_TREND_TITLE","趋势"),238,45)
    radar=str(scene.get("radar_summary","短临未知"))
    rw=len(radar)*13+10
    rainfall=any(v>.05 for v in scene.get("minute_bars",[]))
    rx=388-rw
    f.pill(rx,42,rw,17,rainfall,False)
    f.c12(radar,388-rw//2,45,not rainfall,"center",1)
    x0,y0,w,h=212,82,170,82
    base_y=y0+h
    f.rect(x0,base_y,w,1)
    f.dotted_h(x0,x0+w,y0+h//2,3)
    points=scene.get("hourly_points",[])[:integer("WEATHER_HOURLY_POINTS",5,3,7)]
    if len(points)>=2:
        tmin=min(v for _,v in points)-1
        tmax=max(v for _,v in points)+1
        span=max(1,tmax-tmin)
        step=144//(len(points)-1)
        nodes=[]
        for i,(t,v) in enumerate(points):
            px=x0+12+i*step
            py=base_y-10-round((v-tmin)/span*60)
            nodes.append((px,py,t,v))
        for (ax,ay,*_), (bx,by,*_) in zip(nodes,nodes[1:]):
            dx=max(1,bx-ax)
            for px in range(ax,bx+1):
                py=ay+(px-ax)*(by-ay)//dx
                for yy in range(py+3,base_y):
                    if px%2==0 and yy%2==0:f.px(px,yy)
        if rainfall:
            for i,b in enumerate(scene["minute_bars"]):
                if b>.02:
                    bh=max(2,round(b*14))
                    f.rect(x0+6+i*6,base_y-bh,3,bh)
        for (ax,ay,*_), (bx,by,*_) in zip(nodes,nodes[1:]):f.line(ax,ay,bx,by,2)
        for px,py,t,value in nodes:
            f.rect(px-3,py-3,7,7,False)
            f.stroke(px-2,py-2,5,5)
            f.px(px,py)
            vs=f"{value}°"
            tw=f.font7(vs,-300,-300)
            f.rect(px-tw//2-1,py-16,tw+2,13,False)
            f.font7(vs,px,py-15,True,"center")
            f.rect(px,base_y+1,1,3)
            label=(str(t).rstrip("H")+":00") if str(t).endswith("H") else str(t)
            f.micro(label,px,base_y+6,True,"center")
    # 下方未来四日画廊卡片
    days=scene.get("daily_forecast",[])[:integer("WEATHER_FORECAST_DAYS",4,3,5)]
    if not days:raise ValueError("没有天气逐日预报，不能渲染")
    lo=min(d["low"] for d in days);hi=max(d["high"] for d in days)
    span=max(1,hi-lo)
    count=len(days);gap=6
    cw=(390-(count-1)*gap)//count
    for idx,d in enumerate(days):
        cx=4+idx*(cw+gap);cy=199
        f.card(cx,cy,cw,95,border,shadow)
        hdr=idx==0 and inv_today
        if hdr:f.rect(cx+2,cy+2,cw-4,20)
        else:f.rect(cx+2,cy+21,cw-4,1)
        f.c12(str(d["day_cn"]),cx+8,cy+6,not hdr,gap=1)
        f.micro(str(d["date"]),cx+cw-8,cy+9,not hdr,"right")
        icon_bitmap(f,str(d["icon"]),cx+9,cy+27,False)
        f.c16(str(d["cond"]),cx+41,cy+31)
        f.font7(f'{d["low"]}°',cx+9,cy+58)
        f.font7("~",cx+cw//2,cy+58,True,"center")
        f.font7(f'{d["high"]}°',cx+cw-9,cy+58,True,"right")
        bx,bw,by=cx+9,cw-18,cy+76
        f.pill(bx,by,bw,7,False,False)
        f.dots(bx+2,by+2,bw-4,3)
        sx=bx+2+round((d["low"]-lo)/span*(bw-6))
        ex=bx+2+round((d["high"]-lo)/span*(bw-6))
        f.rect(sx,by+1,max(6,ex-sx+2),5)
        if idx==0:
            cur=bx+3+round((scene["temp_c"]-lo)/span*(bw-7))
            f.rect(cur-1,by+2,3,3,False);f.px(cur,by+3)
    return f


def in_pause_windows(raw,now=None):
    """Beijing wall clock windows, including those crossing midnight."""
    now=now or datetime.now(TZ8)
    moment=now.hour*60+now.minute
    for item in raw.split(","):
        if not item.strip() or "-" not in item:continue
        begin,end=item.strip().split("-",1)
        def minute(s):
            h,m=map(int,s.split(":"))
            if not 0<=h<=23 or not 0<=m<=59:raise ValueError()
            return h*60+m
        try:start,stop=minute(begin),minute(end)
        except (ValueError,TypeError):raise ValueError("暂停时间格式应为 00:00-06:00")
        if start==stop:continue
        if start<stop and start<=moment<stop:return True
        if start>stop and (moment>=start or moment<stop):return True
    return False


def quota_task(args):
    if not args.preview and in_pause_windows(env("QUOTA_PAUSE_WINDOWS")) and not args.force:
        print("[quota] 当前处于额度暂停时段；公共雷达请由独立 radar 任务刷新")
        return
    quota=None
    if args.preview:
        quota={"windows":[{"name":"5小时","used_percent":36,"reset_at":int(time.time())+3600},
                          {"name":"7天","used_percent":72,"reset_at":int(time.time())+86400*4}],
               "credits":2,"stale":False}
        radar={"data":{"latest_reset":None,"active_watch":{
            "level":"strong","reset_chance_percent":83,"forecast_window":"未来24小时",
            "observed_at":datetime.now(timezone.utc).isoformat(),
            "expires_at":(datetime.now(timezone.utc)+timedelta(hours=24)).isoformat()}},
            "updated":int(time.time()),"stale":False}
    else:
        try:
            _,_,auth=read_auth()
            if expiry_soon(auth):
                auth=refresh_auth(True)
            try:quota=fetch_quota(auth)
            except HTTPError as exc:
                if exc.code!=401:raise
                auth=refresh_auth(True)
                quota=fetch_quota(auth)
            store_state("quota_data",quota)
            print("[quota] Codex 额度查询成功")
        except Exception as exc:
            print(f"[quota] 本次额度查询失败：{exc}")
            quota=load_state("quota_data")
            quota["stale"]=True
        # 强制绑定：每次实际的额度轮询，雷达同步查询一次。
        try:
            radar=fetch_radar()
            store_state("radar_data",radar)
            print("[radar] 随额度轮询同步完成")
        except Exception as exc:
            print(f"[radar] 本次雷达同步失败：{exc}")
            radar=load_state("radar_data");radar["stale"]=True
    if not quota.get("windows"):
        raise ValueError("没有实时额度或上次有效额度缓存，拒绝发布模拟数据")
    deliver(render_quota(quota,radar),"quota",integer("CODEX_PAGE_ID",3,1,5),args)


def radar_task(args):
    """Separate fallback timer, even while quota pause window is active."""
    if args.preview:
        args2=argparse.Namespace(preview=True,output=args.output,force=args.force)
        quota_task(args2)
        return
    radar=fetch_radar()
    store_state("radar_data",radar)
    print("[radar] 独立雷达刷新成功")
    quota=load_state("quota_data")
    if not quota.get("windows"):
        print("[radar] 当前无额度缓存，仅更新雷达缓存，待额度任务首次成功")
        return
    quota["stale"]=True   # 此次没有重新查询额度，不标记为实时同步
    deliver(render_quota(quota,radar),"quota",integer("CODEX_PAGE_ID",3,1,5),args)


def auth_task(args):
    interval=integer("CODEX_AUTH_REFRESH_SECONDS",3600,0)
    if not args.force:
        if interval==0:
            print("[oauth] CODEX_AUTH_REFRESH_SECONDS=0，已关闭自动刷新；手动运行带 --force")
            return
        record=load_state("oauth_state")
        if int(time.time())-record.get("last_success",0)<max(60,interval):
            print("[oauth] 未到自动刷新间隔")
            return
    # Qinglong on Linux: a lock avoids concurrent manual/scheduled token rotation.
    import fcntl
    lock_path=data_dir()/"oauth_refresh.lock"
    with lock_path.open("ab") as lock:
        fcntl.flock(lock.fileno(),fcntl.LOCK_EX)
        if not args.force:
            record=load_state("oauth_state")
            if int(time.time())-record.get("last_success",0)<max(60,interval):
                print("[oauth] 其他任务已刷新，无需重复")
                return
        refresh_auth(True)
        store_state("oauth_state",{"last_success":int(time.time())})


def calendar_task(args):
    target=date.fromisoformat(args.date) if args.date else datetime.now(TZ8).date()
    deliver(render_calendar(target,integer("CALENDAR_PAGE_ID",1,1,5)),
            "calendar",integer("CALENDAR_PAGE_ID",1,1,5),args)


def weather_task(args):
    page=integer("WEATHER_PAGE_ID",2,1,5)
    if args.preview:
        scene=weather_demo(args.demo,page)
    else:
        try:
            place=resolve_location(args.location or env("WEATHER_LOCATION","北京海淀"))
            scene=fetch_weather(place,page)
            store_state("weather_data",scene)
            print("[weather] 来源："+scene["source_cn"])
        except Exception as exc:
            print(f"[weather] 网络/解析失败：{exc}")
            cache=load_state("weather_data")
            max_age=integer("WEATHER_CACHE_MAX_AGE_SECONDS",3600,0)
            # Cache age is intentionally checked against a saved monotonic wall-clock timestamp.
            if not cache or time.time()-cache.get("_cache_time",0)>max_age:
                raise
            scene=cache
            print("[weather] 使用时效内缓存（禁止生成伪造天气实况）")
        else:
            scene["_cache_time"]=int(time.time())
            store_state("weather_data",scene)
    deliver(render_weather(scene),"weather",page,args)


def main(argv=None):
    parser=argparse.ArgumentParser(
        description="ZECTRIX NOTE4：单脚本青龙适配（额度/雷达/农历/天气/OAuth）")
    parser.add_argument("--task",required=True,
                        choices=("quota","radar","auth-refresh","calendar","weather"))
    parser.add_argument("--env-file",help="可选 .env 文件路径；青龙环境变量始终优先")
    parser.add_argument("--preview",action="store_true",help="只生成离线模拟预览，不联网、不推送")
    parser.add_argument("--output",help="预览 PNG 路径")
    parser.add_argument("--date",help="日历预览日期，格式 2026-10-28")
    parser.add_argument("--demo",choices=("rain","sunny","snow"),default="rain")
    parser.add_argument("--force",action="store_true",
                        help="跳过最小推送间隔、额度暂停或 OAuth 自动刷新间隔")
    parser.add_argument("--location",help="天气地区/区县别名或经纬度")
    parser.add_argument("--locations",action="store_true",help="按 --location 查询地区地址簿")
    args=parser.parse_args(argv)
    load_dotenv(Path(args.env_file).expanduser() if args.env_file else ROOT/".env")
    if args.locations:
        keyword=(args.location or "").lower().strip()
        for item in weather_catalog():
            corpus=" ".join([item.get("city",""),item.get("district","")]
                             +item.get("aliases",[])).lower()
            if keyword in corpus:
                print(f'{item["city"]} · {item["district"]} '
                      f'({item["lon"]},{item["lat"]})')
        return 0
    actions={"quota":quota_task,"radar":radar_task,"auth-refresh":auth_task,
             "calendar":calendar_task,"weather":weather_task}
    try:
        actions[args.task](args)
    except Exception as exc:
        print(f"[{args.task}] 任务失败：{exc}",file=sys.stderr)
        return 1
    return 0


if __name__=="__main__":
    sys.exit(main())
