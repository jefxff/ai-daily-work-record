"""把 before/after 拼成带标注的左右对比图。"""
import os

from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.abspath(__file__))
AB = os.path.join(ROOT, "ab")
OUT = os.path.join(ROOT, "compare")
os.makedirs(OUT, exist_ok=True)

PAIRS = [
    ("P1_1180_before.png", "P1_1180_after.png",
     "CMP1_1180px_诉讼列表_整页横向溢出", "现状：整页横向溢出 294px", "优化：溢出 0px，表格回到轨道宽度"),
    ("P2_1440_before.png", "P2_1440_after.png",
     "CMP2_1440px_诉讼列表_列被压扁", "现状：溢出 34px，关键列截断", "优化：溢出 0px，金额/类型/进度可读"),
    ("P3_390_before.png", "P3_390_after.png",
     "CMP3_390px_每日记录_卡片模式", "现状：徽标压住正文、序号占整行、图标热区 20px", "优化：标签独占行、序号为角标、热区 44px"),
    ("P4_390_before_drawer.png", "P4_390_after_drawer.png",
     "CMP4_390px_侧栏抽屉_遮罩", "现状：遮罩不显示，点外部无法关闭", "优化：遮罩正常显示，点外部可关闭"),
]

BAR = 46
PAD = 16
BG = (245, 246, 248)


def font(size):
    for name in ("msyh.ttc", "msyhbd.ttc", "simhei.ttf", "arial.ttf"):
        p = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", name)
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default()


for b_name, a_name, out_name, b_cap, a_cap in PAIRS:
    b = Image.open(os.path.join(AB, b_name)).convert("RGB")
    a = Image.open(os.path.join(AB, a_name)).convert("RGB")
    h = max(b.height, a.height)
    w = b.width + a.width + PAD * 3
    canvas = Image.new("RGB", (w, h + BAR + PAD * 2), BG)
    d = ImageDraw.Draw(canvas)
    canvas.paste(b, (PAD, BAR + PAD))
    canvas.paste(a, (PAD * 2 + b.width, BAR + PAD))

    f = font(20)
    d.text((PAD, 12), "BEFORE  " + b_cap, fill=(190, 45, 45), font=f)
    d.text((PAD * 2 + b.width, 12), "AFTER  " + a_cap, fill=(23, 120, 60), font=f)
    d.rectangle([PAD, BAR + PAD, PAD + b.width - 1, BAR + PAD + b.height - 1],
                outline=(190, 45, 45), width=2)
    d.rectangle([PAD * 2 + b.width, BAR + PAD, PAD * 2 + b.width + a.width - 1, BAR + PAD + a.height - 1],
                outline=(23, 120, 60), width=2)
    out = os.path.join(OUT, out_name + ".png")
    canvas.save(out)
    print("saved", out, canvas.size)
