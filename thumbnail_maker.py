"""
SG News18 - Auto Thumbnail Maker
- 3 designs, used one after another (rotation saved in .thumb_index)
- Correct Hindi (Devanagari) text: uses Noto Sans Devanagari / Mukta (auto-download)
- Uses assets/anchor.png (transparent PNG) + news image taken from the article page
"""
import io
import os
import re
import random
from urllib.parse import urljoin

import numpy as np
import requests
from PIL import Image, ImageDraw, ImageFont, ImageFilter

HERE = os.path.dirname(os.path.abspath(__file__))
ANCHOR_PATH = os.path.join(HERE, "assets", "anchor.png")
FONT_DIR = os.path.join(HERE, "fonts")
STATE_FILE = os.path.join(HERE, ".thumb_index")

W, H, S = 1280, 720, 2            # final size, supersampling factor
YEL, RED, NAVY, WHITE = (255, 205, 25), (215, 20, 35), (10, 14, 38), (255, 255, 255)

# ------------------------------------------------------------------
# Fonts
# ------------------------------------------------------------------
HINDI_FONT_URLS = [
    ("NotoSansDevanagari-Bold.ttf",
     "https://github.com/notofonts/notofonts.github.io/raw/main/fonts/NotoSansDevanagari/hinted/ttf/NotoSansDevanagari-Bold.ttf"),
    ("Mukta-ExtraBold.ttf",
     "https://github.com/google/fonts/raw/main/ofl/mukta/Mukta-ExtraBold.ttf"),
    ("Hind-Bold.ttf",
     "https://github.com/google/fonts/raw/main/ofl/hind/Hind-Bold.ttf"),
]
HINDI_SYSTEM_FONTS = [
    "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansDevanagariUI-Bold.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansDevanagari-Bold.ttf",
    "/usr/share/fonts/truetype/lohit-devanagari/Lohit-Devanagari.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",   # last resort (has Devanagari)
]
LATIN_FONTS = [
    "/usr/share/fonts/truetype/google-fonts/Poppins-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
]
_font_cache = {}


def _valid_font(path):
    try:
        ImageFont.truetype(path, 20)
        return True
    except Exception:
        return False


def get_hindi_font_path():
    if "hi" in _font_cache:
        return _font_cache["hi"]
    os.makedirs(FONT_DIR, exist_ok=True)
    # 1) already downloaded
    for name, _ in HINDI_FONT_URLS:
        p = os.path.join(FONT_DIR, name)
        if os.path.exists(p) and _valid_font(p):
            _font_cache["hi"] = p
            return p
    # 2) download (works on GitHub Actions)
    for name, url in HINDI_FONT_URLS:
        try:
            r = requests.get(url, timeout=40)
            if r.status_code == 200 and len(r.content) > 20000:
                p = os.path.join(FONT_DIR, name)
                with open(p, "wb") as f:
                    f.write(r.content)
                if _valid_font(p):
                    print(f"✅ Hindi font downloaded: {name}")
                    _font_cache["hi"] = p
                    return p
        except Exception as e:
            print(f"⚠️ Font download failed ({name}): {e}")
    # 3) system fonts
    for p in HINDI_SYSTEM_FONTS:
        if os.path.exists(p) and _valid_font(p):
            print(f"ℹ️ Using system Hindi font: {p}")
            _font_cache["hi"] = p
            return p
    raise RuntimeError("Koi Devanagari font nahi mila!")


def get_latin_font_path():
    if "en" in _font_cache:
        return _font_cache["en"]
    for p in LATIN_FONTS:
        if os.path.exists(p) and _valid_font(p):
            _font_cache["en"] = p
            return p
    _font_cache["en"] = get_hindi_font_path()
    return _font_cache["en"]


def F(path, size):
    try:
        return ImageFont.truetype(path, int(size * S), layout_engine=ImageFont.Layout.RAQM)
    except Exception:
        return ImageFont.truetype(path, int(size * S))   # basic layout (matras may look off)


# ------------------------------------------------------------------
# Title helpers
# ------------------------------------------------------------------
_EMOJI = re.compile(
    "[\U00010000-\U0010ffff☀-➿⬀-⯿️‍←-⇿⌀-⏿]+",
    flags=re.UNICODE)


def clean_title(title):
    t = _EMOJI.sub("", title or "")
    t = re.sub(r"\s+", " ", t).strip(" -|:•")
    return t or "ताज़ा खबर"


def find_number_badge(title):
    """Returns (number, label) like ('5000', 'पद') or (None, None)."""
    m = re.search(r"(\d[\d,]{2,})\s*([^\s\d,।!?:\-]+)?", title)
    if not m:
        return None, None
    num = m.group(1).replace(",", "")
    label = (m.group(2) or "").strip()
    if label and not any("ऀ" <= c <= "ॿ" for c in label):
        label = ""
    return num, label[:8]


def wrap_text(draw, text, font, max_w):
    lines, cur = [], ""
    for w in text.split():
        t = (cur + " " + w).strip()
        if draw.textlength(t, font=font) <= max_w * S:
            cur = t
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def fit_text(draw, text, path, max_w, max_h, max_size, min_size, max_lines, spacing=1.25):
    """Largest font size where the wrapped text fits in the box. Returns (font, lines, size)."""
    for size in range(max_size, min_size - 1, -2):
        f = F(path, size)
        lines = wrap_text(draw, text, f, max_w)
        if len(lines) <= max_lines and len(lines) * size * spacing <= max_h:
            return f, lines, size
    f = F(path, min_size)
    lines = wrap_text(draw, text, f, max_w)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip(" ,.:;-") + "…"
    return f, lines, min_size


# ------------------------------------------------------------------
# Drawing helpers
# ------------------------------------------------------------------
def grad(w, h, c1, c2, mode="diag"):
    xs = np.linspace(0, 1, w)[None, :]
    ys = np.linspace(0, 1, h)[:, None]
    t = (xs + ys) / 2 if mode == "diag" else ys
    t = np.broadcast_to(np.clip(t, 0, 1), (h, w))[..., None]
    a, b = np.array(c1, float), np.array(c2, float)
    return Image.fromarray((a + (b - a) * t).astype("uint8"), "RGB")


def radial(w, h, cx, cy, r, color, alpha=1.0):
    ys, xs = np.mgrid[0:h, 0:w]
    d = np.sqrt((xs - cx) ** 2 + (ys - cy) ** 2) / r
    a = np.clip(1 - d, 0, 1) ** 1.6 * alpha
    layer = Image.new("RGBA", (w, h), color + (0,))
    layer.putalpha(Image.fromarray((a * 255).astype("uint8"), "L"))
    return layer


def bokeh(w, h, colors, n=26, seed=3):
    rnd = random.Random(seed)
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for _ in range(n):
        r = rnd.randint(25, 110) * S
        x, y = rnd.randint(0, w), rnd.randint(0, h)
        d.ellipse((x - r, y - r, x + r, y + r), fill=rnd.choice(colors) + (rnd.randint(25, 70),))
    return layer.filter(ImageFilter.GaussianBlur(14 * S))


def generated_photo(w, h, seed=1):
    """Used when no news image could be found on the article page."""
    im = grad(w, h, (20, 40, 90), (150, 60, 50)).convert("RGBA")
    im.alpha_composite(bokeh(w, h, [(255, 190, 60), (80, 160, 255), (255, 90, 90), (255, 255, 255)], 30, seed))
    im.alpha_composite(radial(w, h, w * .5, h * .55, max(w, h) * .6, (255, 220, 160), .45))
    return im


def cover_crop(img, w, h):
    img = img.convert("RGBA")
    r = max(w / img.width, h / img.height)
    img = img.resize((int(img.width * r) + 1, int(img.height * r) + 1), Image.LANCZOS)
    x, y = (img.width - w) // 2, (img.height - h) // 2
    return img.crop((x, y, x + w, y + h))


def shadow_paste(canvas, im, pos, blur=22, off=(0, 14), op=.55):
    sh = Image.new("RGBA", im.size, (0, 0, 0, 0))
    sh.putalpha(im.split()[3].point(lambda v: int(v * op)))
    big = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    big.paste(sh, (pos[0] + off[0] * S, pos[1] + off[1] * S))
    canvas.alpha_composite(big.filter(ImageFilter.GaussianBlur(blur * S)))
    canvas.alpha_composite(im, pos)


def glow_cutout(canvas, im, pos, color, blur=18, op=.9):
    g = Image.new("RGBA", im.size, color + (0,))
    g.putalpha(im.split()[3].point(lambda v: int(v * op)))
    big = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    big.paste(g, pos)
    canvas.alpha_composite(big.filter(ImageFilter.GaussianBlur(blur * S)))


def load_anchor(height):
    a = Image.open(ANCHOR_PATH).convert("RGBA")
    bb = a.split()[3].getbbox()
    if bb:
        a = a.crop(bb)
    r = height * S / a.height
    a = a.resize((int(a.width * r), int(a.height * r)), Image.LANCZOS)
    return a.filter(ImageFilter.UnsharpMask(radius=2, percent=60, threshold=2))


def rrect_mask(size, rad):
    m = Image.new("L", size, 0)
    ImageDraw.Draw(m).rounded_rectangle((0, 0, size[0] - 1, size[1] - 1), rad, fill=255)
    return m


def stroke_text(d, xy, t, f, fill, stroke=0, sfill=(0, 0, 0), anchor="la", shadow=None):
    x, y = xy
    if shadow:
        d.text((x + shadow[0] * S, y + shadow[1] * S), t, font=f, fill=shadow[2], anchor=anchor,
               stroke_width=int(stroke * S), stroke_fill=shadow[2])
    d.text((x, y), t, font=f, fill=fill, anchor=anchor, stroke_width=int(stroke * S), stroke_fill=sfill)


def finish(canvas, out_path):
    im = canvas.convert("RGB").resize((W, H), Image.LANCZOS)
    q = 92
    while True:                                  # YouTube limit: 2 MB
        im.save(out_path, "JPEG", quality=q, optimize=True)
        if os.path.getsize(out_path) <= 1_900_000 or q <= 60:
            break
        q -= 8
    return out_path


# ------------------------------------------------------------------
# Design 1 - Classic Breaking
# ------------------------------------------------------------------
def design1(title, photo, out):
    HI, EN = get_hindi_font_path(), get_latin_font_path()
    c = grad(W * S, H * S, (60, 0, 20), (14, 0, 8)).convert("RGBA")
    c.alpha_composite(radial(W * S, H * S, 1000 * S, 380 * S, 520 * S, (255, 60, 70), .55))
    c.alpha_composite(bokeh(W * S, H * S, [(255, 190, 60), (255, 70, 80)], 14, 8))
    d = ImageDraw.Draw(c)
    d.rectangle((0, 0, W * S, 12 * S), fill=YEL)
    d.rounded_rectangle((40 * S, 34 * S, 290 * S, 88 * S), 10 * S, fill=RED)
    d.text((165 * S, 61 * S), "SG NEWS18", font=F(EN, 32), fill=WHITE, anchor="mm")
    d.rounded_rectangle((300 * S, 34 * S, 470 * S, 88 * S), 10 * S, fill=YEL)
    d.text((385 * S, 61 * S), "ताज़ा खबर", font=F(HI, 32), fill=NAVY, anchor="mm")

    f, lines, size = fit_text(d, title, HI, 790, 200, 80, 44, 2, 1.22)
    y = 108 * S
    for i, ln in enumerate(lines):
        stroke_text(d, (40 * S, y), ln, f, WHITE if i == 0 else YEL, 2, (0, 0, 0), shadow=(3, 4, (0, 0, 0, 200)))
        y += int(size * 1.22 * S)

    pw, ph = 600, 330
    pic = cover_crop(photo, pw * S, ph * S)
    pic.putalpha(rrect_mask(pic.size, 18 * S))
    fr = Image.new("RGBA", ((pw + 12) * S, (ph + 12) * S), (0, 0, 0, 0))
    ImageDraw.Draw(fr).rounded_rectangle((0, 0, fr.width - 1, fr.height - 1), 22 * S, fill=YEL + (255,))
    fr.alpha_composite(pic, (6 * S, 6 * S))
    shadow_paste(c, fr, (40 * S, 318 * S))

    a = load_anchor(640)
    pos = (W * S - a.width - 30 * S, (H - 70) * S - a.height)
    glow_cutout(c, a, pos, (255, 80, 90), 22, .6)
    shadow_paste(c, a, pos, 10, (-6, 0), .5)

    d = ImageDraw.Draw(c)
    d.rectangle((0, 650 * S, W * S, H * S), fill=(18, 0, 10))
    d.rectangle((0, 650 * S, 300 * S, H * S), fill=RED)
    d.text((150 * S, 685 * S), "BREAKING", font=F(EN, 34), fill=WHITE, anchor="mm")
    d.text((325 * S, 685 * S), "पूरी जानकारी वीडियो में", font=F(HI, 30), fill=YEL, anchor="lm")
    d.rectangle((0, 646 * S, W * S, 650 * S), fill=YEL)
    return finish(c, out)


# ------------------------------------------------------------------
# Design 2 - Highlight bars
# ------------------------------------------------------------------
def design2(title, photo, out):
    HI, EN = get_hindi_font_path(), get_latin_font_path()
    c = grad(W * S, H * S, (8, 12, 40), (4, 6, 20), "v").convert("RGBA")
    c.alpha_composite(radial(W * S, H * S, 1010 * S, 360 * S, 520 * S, (60, 120, 255), .65))
    # faint news photo behind everything
    bg = cover_crop(photo, W * S, H * S)
    bg.putalpha(Image.new("L", bg.size, 38))
    c.alpha_composite(bg.filter(ImageFilter.GaussianBlur(6 * S)))
    c.alpha_composite(bokeh(W * S, H * S, [(80, 160, 255), (255, 205, 25)], 16, 5))
    st = Image.new("RGBA", c.size, (0, 0, 0, 0))
    sd = ImageDraw.Draw(st)
    for i in range(5):
        x = 700 + i * 80
        sd.polygon([(x * S, 0), ((x + 26) * S, 0), ((x - 120 + 26) * S, H * S), ((x - 120) * S, H * S)], fill=(255, 255, 255, 18))
    c.alpha_composite(st.filter(ImageFilter.GaussianBlur(2 * S)))

    a = load_anchor(680)
    pos = (W * S - a.width - 20 * S, H * S - a.height)
    glow_cutout(c, a, pos, (90, 170, 255), 26, .75)
    shadow_paste(c, a, pos, 10, (-8, 0), .5)

    d = ImageDraw.Draw(c)
    f, lines, size = fit_text(d, title, HI, 690, 590, 68, 40, 5, 1.55)
    bar_h = int(size * 1.5 * S)
    gap = 10 * S
    y = (44 * S) if len(lines) * (bar_h + gap) < 560 * S else 20 * S
    for i, ln in enumerate(lines):
        fg, bg_col = (WHITE, RED) if i % 2 == 0 else (NAVY, YEL)
        w = d.textlength(ln, font=f) + 44 * S
        sk = 22 * S
        d.polygon([(0, y), (w + sk, y), (w, y + bar_h), (0, y + bar_h)], fill=bg_col)
        d.text((24 * S, y + bar_h / 2 + 3 * S), ln, font=f, fill=fg, anchor="lm", stroke_width=1 * S, stroke_fill=fg)
        y += bar_h + gap

    d.rectangle((0, 650 * S, W * S, H * S), fill=YEL)
    d.text((40 * S, 685 * S), "SG NEWS18", font=F(EN, 36), fill=NAVY, anchor="lm")
    d.text((W * S - 330 * S, 685 * S), "पूरी जानकारी वीडियो में", font=F(HI, 30), fill=NAVY, anchor="rm")
    return finish(c, out)


# ------------------------------------------------------------------
# Design 3 - Photo left (diagonal) + number badge
# ------------------------------------------------------------------
def design3(title, photo, out):
    HI, EN = get_hindi_font_path(), get_latin_font_path()
    c = grad(W * S, H * S, (120, 8, 30), (30, 0, 60)).convert("RGBA")
    ph = cover_crop(photo, 760 * S, H * S)
    m = Image.new("L", ph.size, 0)
    ImageDraw.Draw(m).polygon([(0, 0), (ph.width, 0), (ph.width - 150 * S, ph.height), (0, ph.height)], fill=255)
    ph.putalpha(m)
    c.alpha_composite(ph, (0, 0))
    d = ImageDraw.Draw(c)
    d.polygon([(760 * S, 0), (790 * S, 0), (640 * S, H * S), (610 * S, H * S)], fill=YEL)
    c.alpha_composite(radial(W * S, H * S, 1020 * S, 400 * S, 420 * S, (255, 90, 120), .5))

    a = load_anchor(650)
    pos = (W * S - a.width - 10 * S, H * S - a.height)
    glow_cutout(c, a, pos, (255, 120, 150), 22, .65)
    shadow_paste(c, a, pos, 10, (-6, 0), .5)

    ov = Image.new("RGBA", c.size, (0, 0, 0, 0))
    ys = np.linspace(0, 1, H * S)[:, None]
    od = np.broadcast_to((np.clip((ys - .34) / .66, 0, 1) ** 1.3 * 225).astype("uint8"), (H * S, W * S)).copy()
    ov.putalpha(Image.fromarray(od, "L"))
    c.alpha_composite(ov)

    d = ImageDraw.Draw(c)
    d.rounded_rectangle((36 * S, 34 * S, 270 * S, 90 * S), 12 * S, fill=RED)
    d.text((153 * S, 62 * S), "BREAKING", font=F(EN, 30), fill=WHITE, anchor="mm")

    num, label = find_number_badge(title)
    cx, cy, r = 700 * S, 150 * S, 92 * S
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=YEL, outline=WHITE, width=5 * S)
    if num:
        nf = F(EN, 50 if len(num) <= 4 else 40)
        d.text((cx, cy - (16 * S if label else 0)), num, font=nf, fill=NAVY, anchor="mm")
        if label:
            d.text((cx, cy + 36 * S), label, font=F(HI, 38), fill=RED, anchor="mm", stroke_width=1 * S, stroke_fill=RED)
    else:
        d.text((cx, cy - 4 * S), "ताज़ा", font=F(HI, 40), fill=RED, anchor="mm", stroke_width=1 * S, stroke_fill=RED)
        d.text((cx, cy + 36 * S), "खबर", font=F(HI, 40), fill=NAVY, anchor="mm", stroke_width=1 * S, stroke_fill=NAVY)

    f, lines, size = fit_text(d, title, HI, 740, 290, 72, 42, 3, 1.3)
    total = len(lines) * size * 1.3
    y = int((650 - 18 - total) * S)
    for i, ln in enumerate(lines):
        stroke_text(d, (36 * S, y), ln, f, WHITE if i % 2 == 0 else YEL, 2, (0, 0, 0), shadow=(3, 4, (0, 0, 0, 220)))
        y += int(size * 1.3 * S)

    d.rectangle((0, 650 * S, W * S, H * S), fill=(14, 0, 10))
    d.text((36 * S, 685 * S), "SG NEWS18", font=F(EN, 34), fill=YEL, anchor="lm")
    d.text((330 * S, 685 * S), "आख़िरी तारीख मत भूलिए — पूरी जानकारी देखें", font=F(HI, 28), fill=WHITE, anchor="lm")
    return finish(c, out)


DESIGNS = [design1, design2, design3]


# ------------------------------------------------------------------
# Rotation (1 -> 2 -> 3 -> 1 ...)
# ------------------------------------------------------------------
def next_design_index(advance=True):
    idx = 0
    try:
        with open(STATE_FILE) as f:
            idx = int(f.read().strip()) % len(DESIGNS)
    except Exception:
        idx = 0
    if advance:
        try:
            with open(STATE_FILE, "w") as f:
                f.write(str((idx + 1) % len(DESIGNS)))
        except Exception as e:
            print(f"⚠️ Thumbnail state save nahi hui: {e}")
    return idx


# ------------------------------------------------------------------
# News image from the article page
# ------------------------------------------------------------------
def fetch_news_image(article_url):
    """Article page se best image nikalta hai (og:image ya sabse badi <img>). Fail hone par None."""
    if not article_url:
        return None
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1400, "height": 900})
            page.goto(article_url, wait_until="networkidle", timeout=30000)
            page.wait_for_timeout(1500)
            src = page.evaluate("""() => {
                const og = document.querySelector('meta[property="og:image"], meta[name="twitter:image"]');
                if (og && og.content) return og.content;
                let best = null, area = 0;
                document.querySelectorAll('img').forEach(i => {
                    const a = (i.naturalWidth || 0) * (i.naturalHeight || 0);
                    if (i.naturalWidth >= 300 && i.naturalHeight >= 180 && a > area) { area = a; best = i.currentSrc || i.src; }
                });
                return best;
            }""")
            browser.close()
        if not src:
            return None
        src = urljoin(article_url, src)
        r = requests.get(src, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        img = Image.open(io.BytesIO(r.content))
        img.load()
        if img.width < 200 or img.height < 120:
            return None
        print(f"🖼️ News image mili: {src}")
        return img.convert("RGB")
    except Exception as e:
        print(f"⚠️ News image nahi mili ({e}) - generated background use hoga")
        return None


# ------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------
def make_thumbnail(title, article_url=None, out_dir="bot_outputs", filename_base="thumb",
                   news_image=None, design=None):
    """Thumbnail banata hai (JPEG, 1280x720) aur path return karta hai.
    design: 1/2/3 force karne ke liye, warna rotation (bari-bari)."""
    os.makedirs(out_dir, exist_ok=True)
    title = clean_title(title)
    idx = (design - 1) if design else next_design_index()
    photo = news_image if news_image is not None else fetch_news_image(article_url)
    if photo is None:
        photo = generated_photo(W * S, H * S, seed=len(title))
    out = os.path.join(out_dir, f"{filename_base}_thumbnail.jpg")
    DESIGNS[idx](title, photo, out)
    print(f"🖼️ Thumbnail ready (Design {idx + 1}): {out}")
    return out


if __name__ == "__main__":
    import sys
    t = " ".join(sys.argv[1:]) or "बड़ी खबर: 10वीं पास के लिए 5000 पदों पर भर्ती, आख़िरी तारीख नज़दीक!"
    for n in (1, 2, 3):
        make_thumbnail(t, out_dir="thumb_test", filename_base=f"design{n}", design=n)
