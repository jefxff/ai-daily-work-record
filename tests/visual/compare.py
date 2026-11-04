"""把修复前/后两张截图拼成并排对比图（可选裁剪聚焦区域）。

    python compare.py <before.png> <after.png> <out.png> [x,y,w,h]

标签统一用英文，避免不同机器的中文字体缺失导致方框字。
"""
import sys
from PIL import Image, ImageDraw

before_path, after_path, out_path = sys.argv[1:4]
crop = None
if len(sys.argv) > 4:
    crop = tuple(int(v) for v in sys.argv[4].split(','))

before = Image.open(before_path).convert('RGB')
after = Image.open(after_path).convert('RGB')
if crop:
    before = before.crop((crop[0], crop[1], crop[0] + crop[2], crop[1] + crop[3]))
    after = after.crop((crop[0], crop[1], crop[0] + crop[2], crop[1] + crop[3]))

# 统一到同一尺寸，单侧宽度上限 720px
side_w = 720
scale = min(1.0, side_w / before.width)
size = (int(before.width * scale), int(before.height * scale))
before = before.resize(size, Image.LANCZOS)
after = after.resize(size, Image.LANCZOS)

bar = 30
canvas = Image.new('RGB', (size[0] * 2 + 12, size[1] + bar), (238, 240, 244))
draw = ImageDraw.Draw(canvas)
draw.rectangle((0, 0, size[0], bar), fill=(38, 45, 58))
draw.rectangle((size[0] + 12, 0, canvas.width, bar), fill=(20, 96, 72))
draw.text((10, 8), 'BEFORE', fill=(255, 255, 255))
draw.text((size[0] + 22, 8), 'AFTER', fill=(255, 255, 255))
canvas.paste(before, (0, bar))
canvas.paste(after, (size[0] + 12, bar))
canvas.save(out_path)
print(out_path, canvas.size)
