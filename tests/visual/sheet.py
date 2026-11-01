"""把整页截图拼成联系表（contact sheet），便于一次浏览多页。

用法：
    python tests/visual/sheet.py .shot/pages .shot/sheets --cols 4 --rows 3

每张缩略图下方标注序号与页面路径，序号与 .shot/sheet-index.txt 对应，
发现问题时按序号回查对应 URL。
"""

import json
import re
import os
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

THUMB_W = 440
LABEL_H = 26
PAD = 10
BG = (246, 247, 249)
LABEL_BG = (255, 255, 255)
TEXT = (32, 36, 44)
BORDER = (208, 213, 221)


def load_font(size=13):
    for name in ("msyh.ttc", "msyhbd.ttc", "simhei.ttf", "arial.ttf"):
        for root in (r"C:\Windows\Fonts", "/usr/share/fonts"):
            p = Path(root) / name
            if p.exists():
                try:
                    return ImageFont.truetype(str(p), size)
                except Exception:
                    pass
    return ImageFont.load_default()


def main():
    src = Path(sys.argv[1] if len(sys.argv) > 1 else ".shot/pages")
    out = Path(sys.argv[2] if len(sys.argv) > 2 else ".shot/sheets")
    cols = int(sys.argv[3]) if len(sys.argv) > 3 else 4
    rows = int(sys.argv[4]) if len(sys.argv) > 4 else 3
    unique = "--unique" in sys.argv   # 按「模板形态」去重：数字段统一成 {n}
    no_admin = "--no-admin" in sys.argv  # 跳过 Django 后台（不是本项目模板）
    out.mkdir(parents=True, exist_ok=True)

    # 序号 + URL 的映射由 crawl.js 的报告提供；没有就按文件名
    report = src.parent / "crawl-report.json"
    names = []
    if report.exists():
        data = json.loads(report.read_text(encoding="utf-8"))
        seen_shape = set()
        for p in data.get("pages", []):
            if not p.get("shot") or not (src / p["shot"]).exists():
                continue
            url = p["url"]
            if no_admin and url.startswith("/admin/"):
                continue
            if unique:
                # /daily_record/detail/12/ → /daily_record/detail/{n}/
                shape = re.sub(r"/\d+(?=/|$|\?)", "/{n}", url)
                if shape in seen_shape:
                    continue
                seen_shape.add(shape)
            names.append((url, src / p["shot"]))
    if not names:
        names = [(f.stem, f) for f in sorted(src.glob("*.png"))]

    font = load_font(13)
    per = cols * rows
    index_lines = []
    for si in range(0, len(names), per):
        batch = names[si:si + per]
        cell_h = 0
        thumbs = []
        for url, fp in batch:
            try:
                im = Image.open(fp).convert("RGB")
            except Exception:
                continue
            h = max(1, int(im.height * THUMB_W / im.width))
            cap_h = 900  # 超长页面只取上半部分，避免一张图占满整版
            if h > cap_h:
                im = im.crop((0, 0, im.width, int(im.width * cap_h / THUMB_W)))
                h = cap_h
            im = im.resize((THUMB_W, h), Image.LANCZOS)
            thumbs.append((url, im, h, fp))
            cell_h = max(cell_h, h)

        if not thumbs:
            continue
        sheet_w = cols * (THUMB_W + PAD) + PAD
        sheet_h = rows * (cell_h + LABEL_H + PAD) + PAD
        sheet = Image.new("RGB", (sheet_w, sheet_h), BG)
        draw = ImageDraw.Draw(sheet)

        for i, (url, im, h, fp) in enumerate(thumbs):
            r, c = divmod(i, cols)
            x = PAD + c * (THUMB_W + PAD)
            y = PAD + r * (cell_h + LABEL_H + PAD)
            sheet.paste(im, (x, y + (cell_h - h)))  # 底部对齐，短页下沿整齐
            draw.rectangle([x, y + cell_h, x + THUMB_W, y + cell_h + LABEL_H], fill=LABEL_BG, outline=BORDER)
            no = si + i + 1
            label = f"#{no:03d} {url}"
            draw.text((x + 6, y + cell_h + 6), label[:58], fill=TEXT, font=font)
            index_lines.append(f"#{no:03d}\t{url}\t{fp.name}")

        path = out / f"sheet-{si // per + 1:02d}.png"
        sheet.save(path)
        print(f"{path}  {len(thumbs)} 页  {sheet_w}x{sheet_h}")

    (out.parent / "sheet-index.txt").write_text("\n".join(index_lines), encoding="utf-8")
    print(f"共 {len(names)} 页 → {len(index_lines)} 条索引")


if __name__ == "__main__":
    main()
