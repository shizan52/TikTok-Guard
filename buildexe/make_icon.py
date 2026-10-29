"""Draws the app's icon (the shield of its own logo on the brand gradient) -> packaging/icon.ico, no external image needed."""
import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw

HERE = Path(__file__).resolve().parent
SIZE = 1024                                         # drawn large, shrunk by the ICO writer (smooth edges at 16 px)
SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
BRAND, BRAND2 = (0x6C, 0x5C, 0xE7), (0xFF, 0x4D, 0x8D)


def bezier(p0, p1, p2, p3, steps=40):
    out = []
    for i in range(steps + 1):
        t = i / steps
        out.append(tuple((1 - t) ** 3 * a + 3 * (1 - t) ** 2 * t * b + 3 * (1 - t) * t ** 2 * c + t ** 3 * d
                         for a, b, c, d in zip(p0, p1, p2, p3)))
    return out


def shield_points():
    """The path of the logo's shield (24 x 24 units): M12 22 s8-4 8-10 V5 l-8-3 -8 3 v7 c0 6 8 10 8 10 z"""
    pts = bezier((12, 22), (12, 22), (20, 18), (20, 12))
    pts += [(20, 5), (12, 2), (4, 5), (4, 12)]
    pts += bezier((4, 12), (4, 18), (12, 22), (12, 22))
    return pts


def render() -> Image.Image:
    gradient = Image.new("RGB", (SIZE, SIZE))
    px = gradient.load()
    for y in range(SIZE):
        for x in range(SIZE):
            t = (x + y) / (2 * SIZE - 2)             # 135 degrees, like the app's logo
            px[x, y] = tuple(round(a + (b - a) * t) for a, b in zip(BRAND, BRAND2))
    mask = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, SIZE - 1, SIZE - 1), radius=int(SIZE * 0.24), fill=255)
    icon = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    icon.paste(gradient, (0, 0), mask)

    scale, off = SIZE * 0.60 / 24, SIZE * 0.20      # the shield fills 60 % of the tile, centred
    to_px = lambda p: (off + p[0] * scale, off + p[1] * scale)
    width = int(1.9 * scale * 1.15)
    draw = ImageDraw.Draw(icon)
    outline = [to_px(p) for p in shield_points()]
    draw.line(outline + [outline[0]], fill=(255, 255, 255, 255), width=width, joint="curve")
    tick = [to_px(p) for p in ((9, 12), (11, 14), (15, 10))]
    draw.line(tick, fill=(255, 255, 255, 255), width=width, joint="curve")
    for x, y in outline[::3] + tick:                 # round the line ends / joins
        draw.ellipse((x - width / 2, y - width / 2, x + width / 2, y + width / 2), fill=(255, 255, 255, 255))
    return icon


def wizard_images(folder: Path = HERE) -> list:
    """The pictures of the installer wizard (Inno Setup): the big one on the first/last page, the small one in the page headers.
    Each in three sizes (100 %, 150 %, 200 % screen scaling) - Inno picks the one that fits."""
    from PIL import ImageFont
    icon = render()
    try:
        bold = ImageFont.truetype(r"C:\Windows\Fonts\segoeuib.ttf", 30)
        small = ImageFont.truetype(r"C:\Windows\Fonts\segoeui.ttf", 15)
    except OSError:
        bold = small = ImageFont.load_default()
    made = []
    for scale, tag in ((1, ""), (1.5, "-150"), (2, "-200")):
        w, h = round(164 * scale), round(314 * scale)
        big = Image.new("RGB", (328, 628))
        px = big.load()
        for y in range(628):
            for x in range(328):
                t = (x * 0.35 + y) / (328 * 0.35 + 628)
                px[x, y] = tuple(round(a + (b - a) * t) for a, b in zip(BRAND, BRAND2))
        big.paste(icon.resize((200, 200), Image.LANCZOS), (64, 150), icon.resize((200, 200), Image.LANCZOS))
        draw = ImageDraw.Draw(big)
        for text, font, y in (("TikTok Guard", bold, 380), ("Comment desk", small, 430)):
            width = draw.textlength(text, font=font)
            draw.text(((328 - width) / 2, y), text, font=font, fill=(255, 255, 255))
        path = folder / f"wizard-large{tag}.bmp"
        big.resize((w, h), Image.LANCZOS).save(path)
        small_img = Image.new("RGB", (110, 110), (255, 255, 255))
        small_img.paste(icon.resize((100, 100), Image.LANCZOS), (5, 5), icon.resize((100, 100), Image.LANCZOS))
        side = round(55 * scale)
        small_path = folder / f"wizard-small{tag}.bmp"
        small_img.resize((side, side), Image.LANCZOS).save(small_path)
        made += [path, small_path]
    return made


def main(target: Path = HERE / "icon.ico") -> Path:
    icon = render()
    icon.save(target, format="ICO", sizes=SIZES)
    icon.resize((256, 256), Image.LANCZOS).save(HERE / "icon-preview.png")
    wizard_images()
    return target


if __name__ == "__main__":
    print(main(Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "icon.ico"))
