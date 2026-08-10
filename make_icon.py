#!/usr/bin/env python3
"""アイコン生成（icon-512.png / icon-180.png）— サバ州旗

州旗の形状は Wikimedia Commons の Flag_of_Sabah.svg の path をそのまま使っている
（1200x600。canton 0,0-600,400 / キナバル山 / 青・白・赤の帯）。
正方形アイコンへの収め方は下の LAYOUT で切り替える。デザイン変更時だけ再実行すればよい。
"""
import os
import re

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))

#   square  = 正方形に再構成（採用）。5色すべてを残しつつ、キナバル山は縦横比を保つ
#   hoist   = 掲揚側 x=0..600 をそのまま切り出し（青帯・白帯が入らない）
#   squeeze = 旗全体を横半分に圧縮（山が潰れる）
#   full    = 旗全体をレターボックス（ホーム画面サイズだと小さすぎる）
LAYOUT = "lens"

# ---- 州旗の色（SVG のまま） -------------------------------------------------
ICICLE = "#77CCFF"      # canton の空色
ROYAL  = "#002B7F"      # キナバル山
ZIRCON = "#0484D6"      # 上の青帯
WHITE  = "#FFFFFF"
CHILLI = "#F5362F"      # 赤帯

FLAG_W, FLAG_H = 1200, 600

# Flag_of_Sabah.svg の path826（キナバル山の稜線）
KINABALU_D = (
 "m600 300h-600v-60c25.415516-3.88906 48.820143-24.03652 58.612112-39.66176 21.527093 0 "
 "41.041179-23.47236 62.500538-48.82014 3.95849-7.84755 9.79176-9.79176 19.58394-1.94449 "
 "7.7779 9.72233 9.72233 1.94449 13.68081-11.73639 1.94449-3.95849 5.83327-7.84755 "
 "9.72233-3.95849 5.90291 7.84755 7.84755 2.0139 13.68082-1.94448 9.79176-1.94449 "
 "23.47236-1.94449 33.26433 1.94448 5.83348-3.88906 15.62545-3.88906 25.34778-3.88906 "
 "7.84755 3.88906 15.62524 3.88906 23.47236 0 3.88906-1.94448 9.79176-3.88905 "
 "17.56988-3.88905 11.73639-9.79176 21.52709-19.51409 29.30605-15.62524 7.84755 1.94448 "
 "17.63931 3.88905 27.36291 3.88905 21.52709 0 33.26433-1.94448 39.09802-3.88905 "
 "3.88906 9.79175 5.83327 9.79175 7.77791 0 2.01389-5.90291 2.01389-9.79176 "
 "11.73639-11.736402 0-9.79175 3.95849-11.736392 11.73639-3.88906 11.7364 9.722332 "
 "25.41552 11.736402 39.09803 9.722332 0 11.7364 1.94449 11.7364 7.77791 0 "
 "1.94446-11.666962 9.79175-7.777902 21.52709 9.79176 1.94448 3.88906 3.88906 7.84755 "
 "3.88906 11.73639 42.98856 68.40409 76.38046 98.2062 123.25534 113.8996z"
)


def parse_path(d, steps=14):
    """SVG path を折れ線に平坦化する。m/h/v/c/l/z（相対・絶対）だけ対応すれば足りる"""
    tokens = re.findall(r"[MmHhVvCcLlZz]|-?\d*\.?\d+(?:e-?\d+)?", d)
    pts, i = [], 0
    x = y = 0.0
    cmd = None
    start = (0.0, 0.0)
    while i < len(tokens):
        t = tokens[i]
        if re.match(r"[A-Za-z]", t):
            cmd = t
            i += 1
            if cmd in "Zz":
                pts.append(start)
                continue
        rel = cmd.islower()
        num = lambda k: float(tokens[i + k])                       # noqa: E731

        if cmd in "Mm":
            nx, ny = num(0), num(1)
            x, y = (x + nx, y + ny) if (rel and pts) else (nx, ny)
            start = (x, y); pts.append((x, y)); i += 2
            cmd = "l" if rel else "L"                              # 以降の座標は lineto 扱い
        elif cmd in "Ll":
            nx, ny = num(0), num(1)
            x, y = (x + nx, y + ny) if rel else (nx, ny)
            pts.append((x, y)); i += 2
        elif cmd in "Hh":
            nx = num(0); x = x + nx if rel else nx
            pts.append((x, y)); i += 1
        elif cmd in "Vv":
            ny = num(0); y = y + ny if rel else ny
            pts.append((x, y)); i += 1
        elif cmd in "Cc":
            c1 = (num(0), num(1)); c2 = (num(2), num(3)); e = (num(4), num(5))
            if rel:
                c1 = (x + c1[0], y + c1[1]); c2 = (x + c2[0], y + c2[1]); e = (x + e[0], y + e[1])
            for s in range(1, steps + 1):
                t_ = s / steps; u = 1 - t_
                pts.append((
                    u**3 * x + 3 * u**2 * t_ * c1[0] + 3 * u * t_**2 * c2[0] + t_**3 * e[0],
                    u**3 * y + 3 * u**2 * t_ * c1[1] + 3 * u * t_**2 * c2[1] + t_**3 * e[1],
                ))
            x, y = e; i += 6
        else:
            i += 1
    return pts


def draw_flag(scale):
    """州旗を 1200x600 の比率で描く（scale 倍）"""
    W, H = int(FLAG_W * scale), int(FLAG_H * scale)
    img = Image.new("RGB", (W, H), ICICLE)
    d = ImageDraw.Draw(img)
    s = lambda p: (p[0] * scale, p[1] * scale)                     # noqa: E731

    d.rectangle([s((600, 0)), s((1200, 200))], fill=ZIRCON)
    d.rectangle([s((600, 200)), s((1200, 400))], fill=WHITE)
    d.rectangle([s((0, 400)), s((1200, 600))], fill=CHILLI)
    d.polygon([s(p) for p in parse_path(KINABALU_D)], fill=ROYAL)
    return img


def draw_square_flag(S):
    """州旗を正方形に再構成する。

    帯の順序と5色は州旗のまま。canton を横に広げ（1/2 → 2/3）、赤帯をやや薄くして
    正方形に収める。キナバル山は canton 幅に等比縮小するので形は崩れない。
    """
    img = Image.new("RGB", (S, S), ICICLE)
    d = ImageDraw.Draw(img)
    u = S / 600.0
    canton_w, red_top = 400 * u, 420 * u

    d.rectangle([canton_w, 0, S, 210 * u], fill=ZIRCON)
    d.rectangle([canton_w, 210 * u, S, red_top], fill=WHITE)
    d.rectangle([0, red_top, S, S], fill=CHILLI)

    k = canton_w / 600.0                     # 山（原寸は x0..600, 裾 y=300）の縮小率
    base = 330 * u                           # 山の裾を置く高さ
    d.polygon([(p[0] * k, base - (300 - p[1]) * k) for p in parse_path(KINABALU_D)], fill=ROYAL)
    return img


def draw_lens(S):
    """州旗を丸く切り抜いてレンズに見立てる。

    ホーム画面に sabah-watch（角丸の正方形旗）と並ぶので、ひと目で別アプリと
    分かる必要がある。濃紺の地に旗の円盤を置き、外側に細いリングを回して
    カメラのレンズらしくした。中身の旗は square 版と同じ構成。
    """
    img = Image.new("RGB", (S, S), "#0B1020")
    disc = draw_square_flag(S)

    # 円マスク（アンチエイリアスのため4倍で描いて縮小する）
    m = Image.new("L", (S * 4, S * 4), 0)
    pad = int(S * 4 * 0.085)
    ImageDraw.Draw(m).ellipse([pad, pad, S * 4 - pad, S * 4 - pad], fill=255)
    img.paste(disc, (0, 0), m.resize((S, S), Image.LANCZOS))

    ring = Image.new("L", (S * 4, S * 4), 0)
    rpad = int(S * 4 * 0.052)
    ImageDraw.Draw(ring).ellipse(
        [rpad, rpad, S * 4 - rpad, S * 4 - rpad], outline=255, width=int(S * 4 * 0.016))
    img.paste(Image.new("RGB", (S, S), "#77CCFF"), (0, 0), ring.resize((S, S), Image.LANCZOS))
    return img


def make(layout, size=512, ss=4):
    """正方形アイコンを作る"""
    S = size * ss
    if layout == "lens":
        icon = draw_lens(S)
    elif layout == "square":
        icon = draw_square_flag(S)
    elif layout == "hoist":
        # 旗の掲揚側 x=0..600 はちょうど正方形。canton＋キナバル山＋赤帯が入り小さくても読める
        flag = draw_flag(S / 600)
        icon = flag.crop((0, 0, S, S))
    elif layout == "squeeze":
        icon = draw_flag(S / 600).resize((S, S), Image.LANCZOS)    # 横を半分に圧縮
    else:                                                          # "full"
        flag = draw_flag(S / 1200)                                 # S x S/2
        icon = Image.new("RGB", (S, S), ROYAL)
        icon.paste(flag, (0, (S - flag.height) // 2))
    return icon.resize((size, size), Image.LANCZOS)


if __name__ == "__main__":
    icon = make(LAYOUT)
    icon.save(os.path.join(HERE, "icon-512.png"))
    icon.resize((192, 192), Image.LANCZOS).save(os.path.join(HERE, "icon-192.png"))
    icon.resize((180, 180), Image.LANCZOS).save(os.path.join(HERE, "icon-180.png"))
    print(f"wrote icon-512.png / icon-192.png / icon-180.png  (layout={LAYOUT})")
