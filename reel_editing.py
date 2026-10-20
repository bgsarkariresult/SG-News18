# -*- coding: utf-8 -*-
"""
reel_editing.py  —  SG News18 Reel Editing Engine
=================================================
reels_maker.py is module ko import karta hai. Isme 4 cheezein hain:

1. CAPTIONS      : bole ja rahe shabd ko highlight karne wale bade Hindi captions (PIL se,
                   ImageMagick ki zaroorat nahi).
2. SCENE SYNC    : har image tab dikhti hai jab script me uska zikr ho (word timings se).
3. BLUR BG       : https://bgnewswab.github.io/sgnewswab/index.html ki live screen
                   recording (Playwright) peeche blur hokar chalti hai. Recording fail ho
                   to image ki blurred copy fallback me lagti hai.
4. TRANSITIONS   : fade / slide / flash / zoom — har scene ke beech.

Layout (1080x1920):
    peeche  -> blurred website recording
    beech   -> image "card" (poori image dikhti hai, crop nahi hoti, halka zoom)
    neeche  -> captions
"""
import os
import re
import time
import shutil
import subprocess

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter, features

# moviepy 1.0.3 + Pillow>=10 compatibility (Image.ANTIALIAS hata diya gaya hai)
if not hasattr(Image, "ANTIALIAS"):
    Image.ANTIALIAS = Image.LANCZOS

# ==========================================
# SETTINGS  (yahan se look badal sakte ho)
# ==========================================
W, H = 1080, 1920
FPS = 30

# Image card
CARD_BOX_W, CARD_BOX_H = 960, 1100     # image is box ke andar poori fit hoti hai
CARD_CENTER_Y = 760
CARD_RADIUS = 38
CARD_BORDER = 8
CARD_ZOOM = 0.07                       # 7% slow zoom

# Captions
CAPTION_CENTER_Y = 1560
CAPTION_FONT_SIZE = 86
CAPTION_WORDS_PER_LINE = 3
CAPTION_MARGIN = 80
CAPTION_STROKE = 7
CAPTION_ACTIVE_BG = (255, 214, 0, 255)   # bola ja raha shabd: peela box
CAPTION_ACTIVE_FG = (0, 0, 0, 255)       # ... us par kala text
CAPTION_TEXT = (255, 255, 255, 255)

# Transitions (cycle me chalte hain)
TRANSITIONS = ["fade", "slide", "flash", "zoom"]
TRANSITION_SEC = 0.45
MIN_SCENE_SEC = 1.2

# Background website recording
BG_REC_W, BG_REC_H = 540, 960           # mobile layout me record hota hai, phir 2x upscale + blur
BG_SCROLL_PX_PER_SEC = 140
BG_BLUR_SIGMA = 10
BG_BRIGHTNESS = -0.10

SCROLL_JS = """
([durMs, speed]) => new Promise(resolve => {
  const t0 = performance.now();
  let y = 0, dir = 1, last = t0;
  function step(now) {
    const el = now - t0;
    if (el >= durMs) { resolve(); return; }
    const dt = (now - last) / 1000; last = now;
    const maxY = Math.max(document.documentElement.scrollHeight - window.innerHeight, 0);
    if (el > 1500 && maxY > 0) {
      y += dir * speed * dt;
      if (y >= maxY) { y = maxY; dir = -1; }
      else if (y <= 0) { y = 0; dir = 1; }
      window.scrollTo(0, y);
    }
    requestAnimationFrame(step);
  }
  requestAnimationFrame(step);
})
"""

SENTENCE_END = re.compile(r"[.!?।|]$")
SOFT_PAUSE = re.compile(r"[,;:–—-]$")


# ==========================================
# 1. TIMING (pure python — moviepy ki zaroorat nahi)
# ==========================================
def _word_weight(word):
    """Shabd ko bolne me lagne wale samay ka andaza (lambai + viraam)."""
    core = re.sub(r"[^\w\u0900-\u097F]", "", word)
    n = max(len(core), 1)
    if SENTENCE_END.search(word):
        n += 6
    elif SOFT_PAUSE.search(word):
        n += 3
    return n + 1


def measure_audio_part(path):
    """(duration, shuru ki khamoshi, ant ki khamoshi) seconds me, speed lagne se pehle."""
    from moviepy.editor import AudioFileClip
    clip = AudioFileClip(path)
    dur = float(clip.duration)
    lead = trail = 0.0
    try:
        arr = clip.to_soundarray(fps=8000)
        env = np.abs(arr).max(axis=1) if arr.ndim == 2 else np.abs(arr)
        if env.size and env.max() > 0:
            idx = np.where(env > env.max() * 0.04)[0]
            if idx.size:
                lead = idx[0] / 8000.0
                trail = max(dur - (idx[-1] + 1) / 8000.0, 0.0)
    except Exception as e:
        print(f"⚠️ Silence measure skip ({e})")
    finally:
        clip.close()
    return dur, min(lead, 1.0), min(trail, 1.0)


def measure_chunks(chunk_texts, part_paths):
    infos = []
    for text, path in zip(chunk_texts, part_paths):
        dur, lead, trail = measure_audio_part(path)
        infos.append({"text": text, "dur": dur, "lead": lead, "trail": trail})
    return infos


def build_word_timings(chunk_infos, src_total, speed=1.0):
    """
    Har chunk ki asli audio length pata hai; chunk ke andar shabdon ko lambai ke hisaab se
    baanta jaata hai. Final timeline (speed lagne ke baad) me [{word,start,end}] deta hai.
    """
    sum_dur = sum(c["dur"] for c in chunk_infos) or 1.0
    k = src_total / sum_dur
    words, cursor = [], 0.0
    for c in chunk_infos:
        toks = c["text"].split()
        if not toks:
            continue
        d = c["dur"] * k
        s0 = cursor + c["lead"] * k
        s1 = cursor + d - c["trail"] * k
        if s1 - s0 < 0.2:
            s0, s1 = cursor, cursor + d
        weights = [_word_weight(t) for t in toks]
        tot = float(sum(weights)) or 1.0
        t = s0
        for tok, wgt in zip(toks, weights):
            span = (s1 - s0) * wgt / tot
            words.append({"word": tok, "start": t / speed, "end": (t + span) / speed})
            t += span
        cursor += d
    return words


def scenes_from_script(script_text, keywords):
    """AI ne scenes nahi diye to script ko keywords me barabar baant do."""
    words = script_text.split()
    kws = [k.strip() for k in keywords if k and k.strip()] or ["news"]
    n = len(kws)
    scenes = []
    for i, kw in enumerate(kws):
        a = int(round(i * len(words) / n))
        b = int(round((i + 1) * len(words) / n))
        text = " ".join(words[a:b])
        if text:
            scenes.append({"text": text, "keyword": kw})
    return scenes


def plan_scenes(scenes, words, total_dur):
    """Har scene ka start/end time, us scene ka pehla shabd bolne ke waqt se."""
    n_words = len(words)
    counts = [max(len(s["text"].split()), 1) for s in scenes]
    tot = float(sum(counts))
    plan, cum = [], 0
    for sc, c in zip(scenes, counts):
        idx = min(int(round(cum / tot * n_words)), n_words - 1) if n_words else 0
        start = 0.0 if not plan else (words[idx]["start"] if n_words else 0.0)
        cum += c
        if plan:
            start = max(start, plan[-1]["start"] + MIN_SCENE_SEC)
            if start > total_dur - MIN_SCENE_SEC:
                break                      # bachhe hue scenes pichle me mil jaate hain
        plan.append({"keyword": sc["keyword"], "start": start})
    for i, p in enumerate(plan):
        p["end"] = plan[i + 1]["start"] if i + 1 < len(plan) else total_dur
    return plan


def assign_images(plan, kw_to_path, all_paths):
    """Har scene ko image do. Jis keyword ki image nahi mili use bachi hui image ya pichli image."""
    if not all_paths:
        raise Exception("Koi image nahi mili.")
    used = set(kw_to_path.values())
    spare = [p for p in all_paths if p not in used]
    out, last = [], None
    for sc in plan:
        p = kw_to_path.get(sc["keyword"])
        if not p:
            p = spare.pop(0) if spare else (last or all_paths[0])
        out.append(p)
        last = p
    return out


# ==========================================
# 2. CARD (image poori dikhe, crop na ho)
# ==========================================
def make_card(img_path):
    img = Image.open(img_path).convert("RGB")
    iw, ih = img.size
    s = min(CARD_BOX_W / iw, CARD_BOX_H / ih, 2.5)
    nw, nh = max(int(iw * s), 2), max(int(ih * s), 2)
    img = img.resize((nw, nh), Image.LANCZOS)

    b, pad = CARD_BORDER, 40
    cw, ch = nw + 2 * b, nh + 2 * b
    canvas = Image.new("RGBA", (cw + 2 * pad, ch + 2 * pad), (0, 0, 0, 0))

    shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        [pad, pad + 10, pad + cw, pad + ch + 10], CARD_RADIUS, fill=(0, 0, 0, 150))
    canvas.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(16)))

    ImageDraw.Draw(canvas).rounded_rectangle(
        [pad, pad, pad + cw, pad + ch], CARD_RADIUS, fill=(255, 255, 255, 255))

    mask = Image.new("L", (nw, nh), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, nw, nh], max(CARD_RADIUS - b, 8), fill=255)
    canvas.paste(img, (pad + b, pad + b), mask)
    return np.array(canvas, dtype="uint8")


def blurred_image_bg(img_path):
    """Fallback: image ki hi blurred, dark copy poori screen par."""
    img = Image.open(img_path).convert("RGB")
    s = max(W / img.width, H / img.height)
    img = img.resize((int(img.width * s) + 1, int(img.height * s) + 1), Image.BILINEAR)
    left, top = (img.width - W) // 2, (img.height - H) // 2
    img = img.crop((left, top, left + W, top + H))
    small = img.resize((W // 4, H // 4), Image.BILINEAR).filter(ImageFilter.GaussianBlur(9))
    arr = np.array(small.resize((W, H), Image.BILINEAR), dtype="float32") * 0.55
    return arr.astype("uint8")


# ==========================================
# 3. CAPTIONS
# ==========================================
def _find_devanagari_font():
    cands = [
        os.getenv("CAPTION_FONT", ""),
        "fonts/NotoSansDevanagari-Bold.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Bold.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansDevanagariUI-Bold.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansDevanagari-Bold.ttf",
        "/usr/share/fonts/truetype/lohit-devanagari/Lohit-Devanagari.ttf",
        "C:/Windows/Fonts/NirmalaB.ttf",
        "C:/Windows/Fonts/Nirmala.ttf",
        "C:/Windows/Fonts/mangalb.ttf",
        "/System/Library/Fonts/Kohinoor.ttc",
    ]
    for p in cands:
        if p and os.path.exists(p):
            return p
    try:   # Linux: fontconfig se pucho
        out = subprocess.run(
            ["fc-match", "-f", "%{file}", "Noto Sans Devanagari:bold"],
            capture_output=True, text=True, timeout=10).stdout.strip()
        if out and os.path.exists(out) and "evanagari" in out:
            return out
    except Exception:
        pass
    return None


_FONT_CACHE = {}


def _load_font(size):
    if size in _FONT_CACHE:
        return _FONT_CACHE[size]
    path = _find_devanagari_font()
    if not path:
        raise RuntimeError(
            "Hindi font nahi mila. GitHub Actions me 'fonts-noto-core' install karo, ya "
            "Noto Sans Devanagari Bold ko fonts/NotoSansDevanagari-Bold.ttf me rakho, "
            "ya CAPTION_FONT env var me font ka path do.")
    if features.check("raqm"):
        font = ImageFont.truetype(path, size, layout_engine=ImageFont.Layout.RAQM)
    else:
        print("⚠️ Pillow me RAQM nahi hai: Hindi matras/conjuncts galat dikh sakte hain "
              "(Linux par 'libraqm0' install karo).")
        font = ImageFont.truetype(path, size)
    _FONT_CACHE[size] = font
    return font


def _line_width(tokens, font):
    space = font.getlength(" ")
    return sum(font.getlength(t) for t in tokens) + space * max(len(tokens) - 1, 0)


def build_caption_chunks(words, font, max_w):
    """Shabdon ko 2-3 ke group me todo; vakya khatam hone par naya group."""
    chunks, cur = [], []
    for i, w in enumerate(words):
        trial = cur + [i]
        too_wide = _line_width([words[j]["word"] for j in trial], font) > max_w
        if cur and (len(trial) > CAPTION_WORDS_PER_LINE or too_wide):
            chunks.append(cur)
            cur = [i]
        else:
            cur = trial
        if SENTENCE_END.search(w["word"]):
            chunks.append(cur)
            cur = []
    if cur:
        chunks.append(cur)
    return chunks


def render_caption(tokens, active, font):
    pad_x, pad_y, st = 24, 12, CAPTION_STROKE
    space = font.getlength(" ")
    widths = [font.getlength(t) for t in tokens]
    asc, desc = font.getmetrics()
    line_h = asc + desc
    total_w = int(sum(widths) + space * (len(tokens) - 1)) + 2 * (pad_x + st)
    total_h = line_h + 2 * (pad_y + st)
    img = Image.new("RGBA", (total_w, total_h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    x, y = pad_x + st, pad_y + st
    for i, t in enumerate(tokens):
        if i == active:
            d.rounded_rectangle([x - 12, y - 4, x + widths[i] + 12, y + line_h + 4],
                                radius=24, fill=CAPTION_ACTIVE_BG)
            d.text((x, y), t, font=font, fill=CAPTION_ACTIVE_FG)
        else:
            d.text((x, y), t, font=font, fill=CAPTION_TEXT,
                   stroke_width=st, stroke_fill=(0, 0, 0, 255))
        x += widths[i] + space
    return np.array(img, dtype="uint8")


def build_caption_clips(words, total_dur):
    from moviepy.editor import ImageClip
    font = _load_font(CAPTION_FONT_SIZE)
    chunks = build_caption_chunks(words, font, W - 2 * CAPTION_MARGIN)
    clips = []
    for chunk in chunks:
        tokens = [words[i]["word"] for i in chunk]
        for k, wi in enumerate(chunk):
            t0 = words[wi]["start"]
            nxt = words[wi + 1]["start"] if wi + 1 < len(words) else words[wi]["end"]
            t1 = max(nxt, t0 + 0.12)
            if k == len(chunk) - 1 and wi + 1 < len(words):
                t1 = min(t1, words[wi]["end"] + 0.45)       # lambe viraam me caption na atke
            t1 = min(t1, total_dur)
            if t1 - t0 < 0.05:
                continue
            arr = render_caption(tokens, k, font)
            clip = (ImageClip(arr).set_start(t0).set_duration(t1 - t0)
                    .set_position(("center", int(CAPTION_CENTER_Y - arr.shape[0] / 2))))
            clips.append(clip)
    print(f"💬 Captions ready: {len(clips)} frames, {len(chunks)} groups")
    return clips


# ==========================================
# 4. TRANSITIONS + SCENE CLIPS
# ==========================================
def _ease(x):
    x = min(max(x, 0.0), 1.0)
    return 1 - (1 - x) ** 3


def _scene_clip(card_arr, start, dur, idx, ttype, ov):
    from moviepy.editor import ImageClip
    ch, cw = card_arr.shape[:2]
    zoom_in = (idx % 2 == 0)

    def scale(t):
        p = min(max(t / dur, 0.0), 1.0)
        base = 1 + CARD_ZOOM * p if zoom_in else 1 + CARD_ZOOM * (1 - p)
        if ttype == "zoom" and ov > 0 and t < ov:
            base *= 1 + 0.30 * (1 - _ease(t / ov))
        return base

    def slide_dx(t):
        if ttype == "slide" and ov > 0 and t < ov:
            return (1 - _ease(t / ov)) * W * 0.8
        return 0.0

    clip = ImageClip(card_arr).set_duration(dur)
    clip = clip.resize(lambda t: scale(t))
    clip = clip.set_position(lambda t: (
        int(round(W / 2 + slide_dx(t) - cw * scale(t) / 2)),
        int(round(CARD_CENTER_Y - ch * scale(t) / 2))))
    if idx > 0 and ov > 0:
        if ttype in ("fade", "zoom"):
            clip = clip.crossfadein(ov)
        elif ttype == "slide":
            clip = clip.crossfadein(min(ov, 0.2))
    return clip.set_start(start)


def _flash_clip(at):
    from moviepy.editor import VideoClip
    d = 0.32
    white = np.full((H, W, 3), 255, dtype="uint8")

    def mask_frame(t):
        a = 0.85 * (1 - abs(t - d / 2) / (d / 2))
        return np.full((H, W), max(a, 0.0), dtype="float64")

    mask = VideoClip(mask_frame, ismask=True, duration=d)
    clip = VideoClip(lambda t: white, duration=d).set_mask(mask)
    return clip.set_start(max(at - d / 2, 0))


# ==========================================
# 5. BACKGROUND: website screen recording (blurred)
# ==========================================
def _ffmpeg():
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def record_site_background(url, duration, work_dir):
    """Playwright se site ko portrait me record karo. (raw_path, shuru ka load time) lautata hai."""
    from playwright.sync_api import sync_playwright
    os.makedirs(work_dir, exist_ok=True)
    for f in os.listdir(work_dir):
        if f.endswith(".webm"):
            try:
                os.remove(os.path.join(work_dir, f))
            except OSError:
                pass

    total = duration + 2.0
    print(f"📹 Recording background site for ~{total:.0f}s: {url}")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            viewport={"width": BG_REC_W, "height": BG_REC_H},
            record_video_dir=work_dir,
            record_video_size={"width": BG_REC_W, "height": BG_REC_H})
        t_open = time.time()
        page = ctx.new_page()
        try:
            page.goto(url, wait_until="networkidle", timeout=40000)
        except Exception:
            page.goto(url, wait_until="domcontentloaded", timeout=40000)
        page.wait_for_timeout(2500)
        skip = time.time() - t_open
        page.evaluate(SCROLL_JS, [int(total * 1000), BG_SCROLL_PX_PER_SEC])
        raw_path = page.video.path()
        ctx.close()
        browser.close()
    if not os.path.exists(raw_path) or os.path.getsize(raw_path) < 5000:
        raise Exception("Recording file nahi bani.")
    return raw_path, skip


def blur_background_video(raw_path, out_path, start_sec, duration):
    vf = (f"fps={FPS},gblur=sigma={BG_BLUR_SIGMA},"
          f"scale={W}:{H}:force_original_aspect_ratio=increase:flags=bicubic,crop={W}:{H},"
          f"eq=brightness={BG_BRIGHTNESS}:saturation=1.15,format=yuv420p")
    cmd = [_ffmpeg(), "-y", "-ss", f"{max(start_sec - 0.1, 0):.2f}", "-i", raw_path,
           "-t", f"{duration:.2f}", "-vf", vf, "-an",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "24", out_path]
    subprocess.run(cmd, capture_output=True, check=True)
    if not os.path.exists(out_path) or os.path.getsize(out_path) < 5000:
        raise Exception("Blur background nahi bana.")


# ==========================================
# 6. MAIN BUILDER
# ==========================================
def build_reel(audio_path, chunk_infos, scenes, images, kw_to_path, output_mp4,
               bg_url=None, speed=1.10, work_dir="reel_work"):
    from moviepy.editor import (AudioFileClip, VideoFileClip, ImageClip,
                                CompositeVideoClip, vfx)
    os.makedirs(work_dir, exist_ok=True)

    audio_src = AudioFileClip(audio_path)
    src_total = float(audio_src.duration)
    audio = audio_src.fx(vfx.speedx, speed)
    total = float(audio.duration)
    print(f"🔊 Audio: {src_total:.2f}s -> {total:.2f}s after {speed}x")

    if not chunk_infos:
        chunk_infos = [{"text": " ".join(s["text"] for s in scenes),
                        "dur": src_total, "lead": 0.0, "trail": 0.0}]
    words = build_word_timings(chunk_infos, src_total, speed)
    plan = plan_scenes(scenes, words, total)
    paths = assign_images(plan, kw_to_path, images)
    cards = [make_card(p) for p in paths]
    n = len(plan)
    for i, p in enumerate(plan):
        print(f"🎞️ Scene {i + 1}/{n}: {p['start']:.1f}s -> {p['end']:.1f}s | {p['keyword']}")

    # transitions
    b = [p["start"] for p in plan] + [total]
    ov, types = [0.0] * n, [None] * n
    for i in range(1, n):
        t = TRANSITIONS[(i - 1) % len(TRANSITIONS)]
        seg = min(b[i] - b[i - 1], b[i + 1] - b[i])
        ov[i] = 0.0 if t == "flash" else min(TRANSITION_SEC, seg * 0.5)
        types[i] = t

    scene_clips, flashes, spans = [], [], []
    for i in range(n):
        s_eff = b[i] - ov[i] / 2
        e_eff = b[i + 1] + (ov[i + 1] / 2 if i + 1 < n else 0.0)
        spans.append((s_eff, e_eff - s_eff))
        scene_clips.append(_scene_clip(cards[i], s_eff, e_eff - s_eff, i, types[i], ov[i]))
        if types[i] == "flash":
            flashes.append(_flash_clip(b[i]))

    # background
    bg_layers = []
    if bg_url:
        try:
            raw, skip = record_site_background(bg_url, total, work_dir)
            blurred = os.path.join(work_dir, "bg_blur.mp4")
            blur_background_video(raw, blurred, skip, total)
            bg = VideoFileClip(blurred).without_audio()
            bg_layers = [bg.fx(vfx.loop, duration=total).set_start(0)]
            print("✅ Website recording background ready")
        except Exception as e:
            print(f"⚠️ Website background fail ({e}) -> blurred image fallback")
    if not bg_layers:
        for i in range(n):
            c = ImageClip(blurred_image_bg(paths[i])).set_duration(spans[i][1])
            if i > 0 and ov[i] > 0:
                c = c.add_mask().crossfadein(ov[i])
            bg_layers.append(c.set_start(spans[i][0]))

    # captions (fail ho to bina caption ke bhi reel ban jaye)
    try:
        caption_clips = build_caption_clips(words, total)
    except Exception as e:
        print(f"⚠️ Captions skip: {e}")
        caption_clips = []

    layers = bg_layers + scene_clips + flashes + caption_clips
    video = (CompositeVideoClip(layers, size=(W, H), bg_color=(0, 0, 0))
             .set_duration(total).set_audio(audio))
    print("🎬 Rendering 9:16 reel...")
    video.write_videofile(
        output_mp4, fps=FPS, codec="libx264", audio_codec="aac",
        preset="veryfast", threads=4,
        ffmpeg_params=["-pix_fmt", "yuv420p", "-crf", "23"],
        temp_audiofile=os.path.join(work_dir, "temp_audio.m4a"), remove_temp=True)
    video.close()
    audio_src.close()
    print(f"🚀 Final Reel Saved: {output_mp4}")
    return output_mp4
