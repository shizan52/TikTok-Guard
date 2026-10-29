"""Composes a clean "comment + suggested reply" demo card (Pillow).

The point is a client-presentable image: the real cropped screenshot of the
TikTok comment (proof it is live data) next to the AI-drafted reply, with a
clear "not posted yet, awaiting your approval" status -- since this project
currently only detects and drafts, it never posts automatically.
"""
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FONT_DIR = Path("C:/Windows/Fonts")
_BENGALI_RE = re.compile(r"[\u0980-\u09FF]")
# Plain TTFs (Segoe UI, Nirmala) have no color-emoji glyphs, so any emoji in
# AI-generated or scraped text would otherwise render as a missing-glyph box.
_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF\U0000FE0F\U00002700-\U000027BF]+"
)


def _strip_emoji(text: str) -> str:
    return _EMOJI_RE.sub("", text or "").strip()

WIDTH = 1000
PAD = 28
LINE_H = 30
COLOR_BG = (255, 255, 255)
COLOR_TEXT = (30, 30, 30)
COLOR_MUTED = (110, 110, 110)
COLOR_HEADER = (210, 40, 40)
COLOR_REPLY_BG = (232, 248, 237)
COLOR_REPLY_BORDER = (60, 170, 100)
COLOR_BADGE = (255, 243, 205)
COLOR_BADGE_TEXT = (133, 100, 4)

# Segoe UI / Nirmala are plain TTFs with no color-emoji table, so raw emoji
# characters render as missing-glyph boxes -- plain bracket labels instead.
LABEL_ALERT = "[!] Negative comment detected"
LABEL_AI = "[AI] Suggested reply (not posted -- awaiting your approval):"


def _font(size: int, *, bold: bool = False, text: str = "") -> ImageFont.FreeTypeFont:
    if _BENGALI_RE.search(text or ""):
        name = "NirmalaB.ttf" if bold else "Nirmala.ttf"
    else:
        name = "segoeuib.ttf" if bold else "segoeui.ttf"
    path = FONT_DIR / name
    if not path.exists():
        return ImageFont.load_default()
    return ImageFont.truetype(str(path), size)


def _wrap_lines(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    """Greedy word-wrap measured in actual pixel width (not character count --
    character-count wrapping assumes a monospace-ish font, which Bengali
    conjuncts/matras violate badly enough to overflow the card)."""
    lines: list[str] = []
    for paragraph in (text or "").splitlines() or [""]:
        words = paragraph.split(" ")
        current = ""
        for word in words:
            trial = f"{current} {word}".strip()
            if current and font.getlength(trial) > max_width:
                lines.append(current)
                current = word
            else:
                current = trial
        lines.append(current)
    return lines or [""]


def _draw_lines(draw: ImageDraw.ImageDraw, lines: list[str], *, x: int, y: int,
                 font: ImageFont.FreeTypeFont, fill, line_gap: int = 8) -> int:
    cursor_y = y
    for line in lines:
        draw.text((x, cursor_y), line, font=font, fill=fill)
        bbox = font.getbbox(line) or (0, 0, 0, font.size)
        cursor_y += (bbox[3] - bbox[1]) + line_gap
    return cursor_y


def compose_demo_card(*, comment: dict, language: str, reply: str, brand: str,
                       video_label: str, out_path: str) -> str:
    comment_text = _strip_emoji(comment.get("text", ""))
    reply = _strip_emoji(reply)
    username = comment.get("username", "?")
    screenshot_path = comment.get("screenshot")

    shot_img = None
    if screenshot_path and Path(screenshot_path).exists():
        try:
            shot_img = Image.open(screenshot_path).convert("RGB")
            max_w = WIDTH - 2 * PAD
            if shot_img.width > max_w:
                ratio = max_w / shot_img.width
                shot_img = shot_img.resize((max_w, max(int(shot_img.height * ratio), 1)))
        except Exception:  # noqa: BLE001
            shot_img = None

    reply_font = _font(22, text=reply)
    comment_font = _font(20, text=comment_text)
    reply_lines = _wrap_lines(reply or "(no reply generated)", reply_font, WIDTH - 2 * PAD - 36)
    comment_lines = _wrap_lines(comment_text, comment_font, WIDTH - 2 * PAD) if not shot_img else []

    # -- pass 1: measure height (uses the SAME wrapped line lists as the draw
    # pass below, so the canvas is never under-sized for what actually gets
    # drawn into it) ------------------------------------------------------
    height = PAD
    height += 46  # title
    height += 26  # subtitle
    height += 14
    if shot_img:
        height += shot_img.height + 14
    else:
        height += len(comment_lines) * LINE_H + 10
    height += 30  # meta line
    height += 36  # "AI suggests" label
    height += len(reply_lines) * 32 + 50  # reply bubble
    height += 60  # footer badge
    height += PAD

    img = Image.new("RGB", (WIDTH, height), COLOR_BG)
    draw = ImageDraw.Draw(img)

    y = PAD
    draw.text((PAD, y), LABEL_ALERT, font=_font(28, bold=True), fill=COLOR_HEADER)
    y += 42
    draw.text((PAD, y), f"{brand}  |  {video_label}", font=_font(17), fill=COLOR_MUTED)
    y += 36

    if shot_img:
        img.paste(shot_img, (PAD, y))
        y += shot_img.height + 14
    else:
        y = _draw_lines(draw, comment_lines, x=PAD, y=y, font=comment_font, fill=COLOR_TEXT)
        y += 10

    draw.text((PAD, y), f"@{username}  \u00b7  language: {language}  \u00b7  likes: {comment.get('digg_count', 0)}",
               font=_font(15), fill=COLOR_MUTED)
    y += 40

    draw.text((PAD, y), LABEL_AI, font=_font(17, bold=True), fill=COLOR_TEXT)
    y += 34

    bubble_top = y
    bubble_height = len(reply_lines) * 32 + 24
    draw.rounded_rectangle((PAD, bubble_top, WIDTH - PAD, bubble_top + bubble_height),
                            radius=14, fill=COLOR_REPLY_BG, outline=COLOR_REPLY_BORDER, width=2)
    _draw_lines(draw, reply_lines, x=PAD + 18, y=bubble_top + 14,
                font=reply_font, fill=(20, 80, 40), line_gap=6)
    y = bubble_top + bubble_height + 20

    badge_w, badge_h = 420, 40
    draw.rounded_rectangle((PAD, y, PAD + badge_w, y + badge_h), radius=10, fill=COLOR_BADGE)
    draw.text((PAD + 16, y + 10), "Status: PENDING human approval in Telegram",
               font=_font(14, bold=True), fill=COLOR_BADGE_TEXT)

    out_path = str(out_path)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path)
    return out_path
