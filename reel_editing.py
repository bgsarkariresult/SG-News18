# -*- coding: utf-8 -*-
"""
reel_editing.py  —  SG News18 Reel Editing Engine
=================================================
reels_maker.py is module ko import karta hai. Isme 4 cheezein hain:

1. CAPTIONS      : bole ja rahe shabd ko highlight karne wale bade Hindi captions (PIL se,
                   ImageMagick ki zaroorat nahi).
2. SCENE SYNC    : har image tab dikhti hai jab script me uska zikr ho (word timings se).
3. WEBSITE BG    : jo article link diya jaata hai uski live screen recording (Playwright)
                   peeche saaf (bina blur) scroll hoti hai. Recording fail ho to image ki
                   blurred copy fallback me lagti hai.
4. TRANSITIONS   : fade / slide / flash / zoom — har scene ke beech.

Layout (1080x1920):
    peeche  -> website recording (saaf, blur nahi)
    upar    -> image "card" (poori image dikhti hai, crop nahi hoti, halka zoom)
    beech   -> captions (Hinglish, bolte hue shabd highlight)
"""
import os
import re
import time
import shutil
import subprocess
import unicodedata

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
CARD_BOX_W, CARD_BOX_H = 940, 860      # image is box ke andar poori fit hoti hai
CARD_CENTER_X = W // 2
CARD_CENTER_Y = 600
CARD_RADIUS = 38
CARD_BORDER = 8
CARD_ZOOM = 0.13                       # 13% slow zoom for long-video foreground images

# Captions
CAPTION_CENTER_Y = 1300
CAPTION_FONT_SIZE = 80
CAPTION_WORDS_PER_LINE = 3
CAPTION_MARGIN = 60
CAPTION_STROKE = 7
CAPTION_ACTIVE_BG = (255, 214, 0, 255)   # bola ja raha shabd: peela box
CAPTION_ACTIVE_FG = (0, 0, 0, 255)       # ... us par kala text
CAPTION_TEXT = (255, 255, 255, 255)
# Caption ki lipi: "hinglish" (Roman, default) ya "hindi" (Devanagari)
CAPTION_SCRIPT = os.getenv("REEL_CAPTION_SCRIPT", "hinglish").strip().lower()

# Transitions (cycle me chalte hain)
TRANSITIONS = ["fade", "slide", "flash", "zoom"]
TRANSITION_SEC = 0.45
MIN_SCENE_SEC = 1.2

# Background website recording (saaf, bina blur)
BG_REC_W, BG_REC_H = 540, 960           # mobile layout
# NOTE: recording ka size hamesha viewport ke barabar rakhna hai (Playwright video ko bada nahi karta;
# bada size dene par page kone me chhota dikhta hai aur baaki jagah gray rehti hai)
BG_SCROLL_PX_PER_SEC = 120

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

# Kai sites ki max-width hoti hai (beech me patla column, dono taraf khali). Pehle us column ki
# chaudai napte hain, phir recording usi chaudai ke viewport me karte hain taaki page poora 9:16 bhare.
MEASURE_JS = """
() => {
  const vw = window.innerWidth;
  let best = 0;
  for (const el of document.querySelectorAll('body *')) {
    const r = el.getBoundingClientRect();
    if (r.height > 300 && r.width > 200 && r.width < vw - 12 && r.width > best) best = r.width;
  }
  return best;
}
"""

# Page chhota ho to uska main column neeche tak lamba kar do (neeche ka khali ग्रे हिस्सा na dikhe)
FILL_JS = """
() => {
  const vw = window.innerWidth, vh = window.innerHeight;
  let best = null, area = 0;
  for (const el of document.querySelectorAll('body *')) {
    const r = el.getBoundingClientRect();
    if (r.height > 200 && r.width >= vw * 0.9 && r.width * r.height > area) { best = el; area = r.width * r.height; }
  }
  if (best && best.getBoundingClientRect().height < vh) best.style.minHeight = vh + 'px';
  document.documentElement.style.minHeight = '100vh';
  return !!best;
}
"""

HIDE_SCROLLBAR_CSS = "::-webkit-scrollbar{display:none!important} html{scrollbar-width:none!important}"

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
    sr = 8000
    try:
        proc = subprocess.run(
            [_ffmpeg(), "-v", "error", "-i", path, "-f", "s16le", "-ac", "1", "-ar", str(sr), "-"],
            capture_output=True, check=True)
        arr = np.frombuffer(proc.stdout, dtype=np.int16).astype("float32")
        if arr.size == 0:
            raise ValueError("empty audio")
        dur = arr.size / float(sr)
        env = np.convolve(np.abs(arr), np.ones(80, dtype="float32") / 80.0, mode="same")
        lead = trail = 0.0
        if env.max() > 0:
            idx = np.where(env > env.max() * 0.05)[0]
            if idx.size:
                lead = idx[0] / float(sr)
                trail = max(dur - (idx[-1] + 1) / float(sr), 0.0)
        return dur, min(lead, 1.0), min(trail, 1.0)
    except Exception as e:
        print(f"⚠️ Silence measure skip ({e})")
        from moviepy.editor import AudioFileClip
        clip = AudioFileClip(path)
        dur = float(clip.duration)
        clip.close()
        return dur, 0.0, 0.0


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
# 1b. HINGLISH (Devanagari -> Roman)
# ==========================================
# Gemini har scene ka "hinglish" text deta hai. Wo na mile ya galat nikle to
# neeche ka simple rule-based transliterator fallback ki tarah chalta hai.
_HI_DICT_RAW = {
    "है": "hai", "हैं": "hain", "में": "mein", "की": "ki", "का": "ka", "के": "ke", "को": "ko",
    "से": "se", "पर": "par", "और": "aur", "यह": "yeh", "ये": "ye", "वो": "woh", "वह": "woh",
    "क्या": "kya", "कि": "ki", "तो": "toh", "भी": "bhi", "ही": "hi", "नहीं": "nahi", "हो": "ho",
    "था": "tha", "थी": "thi", "थे": "the", "रहा": "raha", "रही": "rahi", "रहे": "rahe",
    "आज": "aaj", "कल": "kal", "अब": "ab", "जब": "jab", "तब": "tab", "हम": "hum", "आप": "aap",
    "मैं": "main", "एक": "ek", "दो": "do", "तीन": "teen", "इस": "is", "उस": "us",
    "लिए": "liye", "साथ": "saath", "बाद": "baad", "पहले": "pehle", "ज़रूर": "zaroor",
    "फिर": "phir", "कर": "kar", "करो": "karo", "करें": "karein", "दोस्तों": "doston",
    "फॉलो": "follow", "कमेंट": "comment", "शेयर": "share", "लाइक": "like", "टीम": "team",
    "इंडिया": "India", "भारत": "Bharat", "भारतीय": "Bharatiya", "ज़िम्बाब्वे": "Zimbabwe",
    "सब": "sab", "बहुत": "bahut", "सिर्फ": "sirf", "कुछ": "kuch", "कोई": "koi", "जो": "jo",
    "अपनी": "apni", "अपना": "apna", "आपकी": "aapki", "आपका": "aapka", "आपके": "aapke",
    "ज़्यादा": "zyada", "खबर": "khabar", "ख़बर": "khabar", "न्यूज़": "news", "मैच": "match",
    "सीरीज़": "series", "क्लीन": "clean", "स्वीप": "sweep", "राय": "raay",
}
_HI_DICT = {unicodedata.normalize("NFC", k): v for k, v in _HI_DICT_RAW.items()}

_IND_VOW = {"अ": "a", "आ": "aa", "इ": "i", "ई": "ee", "उ": "u", "ऊ": "oo", "ऋ": "ri",
            "ए": "e", "ऐ": "ai", "ओ": "o", "औ": "au", "ऑ": "o", "ऍ": "e"}
_MATRA = {"ा": "aa", "ि": "i", "ी": "ee", "ु": "u", "ू": "oo", "ृ": "ri", "े": "e", "ै": "ai",
          "ो": "o", "ौ": "au", "ॉ": "o", "ॅ": "e"}
_CONS = {"क": "k", "ख": "kh", "ग": "g", "घ": "gh", "ङ": "n", "च": "ch", "छ": "chh", "ज": "j",
         "झ": "jh", "ञ": "n", "ट": "t", "ठ": "th", "ड": "d", "ढ": "dh", "ण": "n", "त": "t",
         "थ": "th", "द": "d", "ध": "dh", "न": "n", "प": "p", "फ": "ph", "ब": "b", "भ": "bh",
         "म": "m", "य": "y", "र": "r", "ल": "l", "व": "v", "श": "sh", "ष": "sh", "स": "s",
         "ह": "h", "ळ": "l"}
_NUKTA_MAP = {"क": "q", "ख": "kh", "ग": "g", "ज": "z", "ड": "r", "ढ": "rh", "फ": "f", "य": "y"}
_DIGITS = {c: str(i) for i, c in enumerate("०१२३४५६७८९")}
_VIRAMA, _NUKTA = "\u094d", "\u093c"
_DEV_RE = re.compile(r"[\u0900-\u097F]")


def _translit_word(word):
    m = re.match(r"^([^\w\u0900-\u0963\u0966-\u097F]*)(.*?)([^\w\u0900-\u0963\u0966-\u097F]*)$", word, re.S)
    pre, core, post = m.groups() if m else ("", word, "")
    pre, post = pre.replace("।", "."), post.replace("।", ".")
    if not _DEV_RE.search(core):
        return word
    core = unicodedata.normalize("NFD", core)
    nfc = unicodedata.normalize("NFC", core)
    if nfc in _HI_DICT:
        return pre + _HI_DICT[nfc] + post
    core = core.replace("ज" + _VIRAMA + "ञ", "ग" + _VIRAMA + "य")      # ज्ञ -> gya

    syl, i, ch = [], 0, list(core)          # syl: [consonant, vowel(None = inherent a), nasal]
    while i < len(ch):
        c = ch[i]
        if c in _IND_VOW:
            syl.append(["", _IND_VOW[c], ""])
            i += 1
        elif c in _CONS:
            cons = _CONS[c]
            i += 1
            if i < len(ch) and ch[i] == _NUKTA:
                cons = _NUKTA_MAP.get(c, cons)
                i += 1
            vowel = None
            if i < len(ch) and ch[i] == _VIRAMA:
                vowel = ""
                i += 1
            elif i < len(ch) and ch[i] in _MATRA:
                vowel = _MATRA[ch[i]]
                i += 1
            syl.append([cons, vowel, ""])
        elif c in ("\u0902", "\u0901"):      # anusvara / chandrabindu
            if syl:
                syl[-1][2] = "n"
            i += 1
        elif c == "\u0903":                  # visarga
            if syl:
                syl[-1][2] = "h"
            i += 1
        elif c == _NUKTA:
            i += 1
        elif c in _DIGITS:
            syl.append([_DIGITS[c], "", ""])
            i += 1
        elif c == "।":
            syl.append([".", "", ""])
            i += 1
        else:
            syl.append([c, "", ""])
            i += 1

    n, out, prev_vowel = len(syl), [], False
    for idx, (cons, vowel, nasal) in enumerate(syl):
        if vowel is None:
            nxt = syl[idx + 1] if idx + 1 < n else None
            if idx == n - 1:
                v = ""                                   # shabd ke ant ka 'a' nahi bolte
            elif idx > 0 and prev_vowel and nxt is not None and nxt[1] not in (None, ""):
                v = ""                                   # sarkaar: beech ka 'a' gira do
            else:
                v = "a"
        else:
            v = vowel
        if idx == n - 1:
            if v == "aa" and not nasal:
                v = "a"
            elif v == "ee" and not nasal:
                v = "i"
            elif v == "e" and nasal:
                v = "ei"
        if nasal == "n" and idx + 1 < n and syl[idx + 1][0][:1] in ("b", "p", "m"):
            nasal = "m"
        out.append(cons + v + nasal)
        prev_vowel = (v != "")
    return pre + "".join(out) + post


def devanagari_to_hinglish(text):
    return " ".join(_translit_word(w) for w in text.split())


def scene_word_ranges(scenes, n_words):
    """Har scene ke shabdon ka [start, end) index (plan_scenes jaisa hi hisaab)."""
    counts = [max(len(s["text"].split()), 1) for s in scenes]
    tot = float(sum(counts)) or 1.0
    starts, cum = [], 0
    for c in counts:
        starts.append(min(int(round(cum / tot * n_words)), max(n_words - 1, 0)))
        cum += c
    return [(a, starts[i + 1] if i + 1 < len(starts) else n_words) for i, a in enumerate(starts)]


def hinglish_words(words, scenes):
    """Devanagari timed words ko Hinglish tokens me badlo (timing wahi rehti hai)."""
    out = []
    for sc, (a, b) in zip(scenes, scene_word_ranges(scenes, len(words))):
        dev = words[a:b]
        if not dev:
            continue
        tokens = (sc.get("hinglish") or "").split() or devanagari_to_hinglish(sc["text"]).split()
        if not tokens:
            tokens = [w["word"] for w in dev]
        if len(tokens) == len(dev):          # shabd-dar-shabd match: asli timing
            out += [{"word": t, "start": w["start"], "end": w["end"]} for t, w in zip(tokens, dev)]
        else:                                # ginti alag: scene ke andar lambai ke hisaab se baanto
            t0, t1 = dev[0]["start"], dev[-1]["end"]
            wts = [_word_weight(t) for t in tokens]
            tot, t = float(sum(wts)), t0
            for tok, wt in zip(tokens, wts):
                span = (t1 - t0) * wt / tot
                out.append({"word": tok, "start": t, "end": t + span})
                t += span
    return out


# ==========================================
# 2. CARD (image poori dikhe, crop na ho)
# ==========================================
# Ek hi image dobara aaye to alag hissa dikhao (pura / bayan / dayan / beech ka zoom)
CROP_VARIANTS = [None, (0.0, 0.0, 0.58, 1.0), (0.42, 0.0, 1.0, 1.0), (0.18, 0.12, 0.82, 0.88)]


def make_card(img_path, variant=0):
    img = Image.open(img_path).convert("RGB")
    crop = CROP_VARIANTS[variant % len(CROP_VARIANTS)]
    if crop:
        w0, h0 = img.size
        box = (int(crop[0] * w0), int(crop[1] * h0), int(crop[2] * w0), int(crop[3] * h0))
        if box[2] - box[0] >= 300 and box[3] - box[1] >= 300:
            img = img.crop(box)
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
def _find_font(script):
    env = os.getenv("CAPTION_FONT", "")
    if script == "hindi":
        cands = [env,
                 "fonts/NotoSansDevanagari-Bold.ttf",
                 "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Bold.ttf",
                 "/usr/share/fonts/truetype/noto/NotoSansDevanagariUI-Bold.ttf",
                 "/usr/share/fonts/opentype/noto/NotoSansDevanagari-Bold.ttf",
                 "/usr/share/fonts/truetype/lohit-devanagari/Lohit-Devanagari.ttf",
                 "C:/Windows/Fonts/NirmalaB.ttf", "C:/Windows/Fonts/Nirmala.ttf",
                 "C:/Windows/Fonts/mangalb.ttf", "/System/Library/Fonts/Kohinoor.ttc"]
        fc_name = "Noto Sans Devanagari:bold"
    else:
        cands = [env,
                 "fonts/NotoSans-Bold.ttf",
                 "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
                 "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                 "C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/segoeuib.ttf",
                 "/System/Library/Fonts/Supplemental/Arial Bold.ttf"]
        fc_name = "sans-serif:bold"
    for p in cands:
        if p and os.path.exists(p):
            return p
    try:   # Linux: fontconfig se pucho
        out = subprocess.run(["fc-match", "-f", "%{file}", fc_name],
                             capture_output=True, text=True, timeout=10).stdout.strip()
        if out and os.path.exists(out) and (script != "hindi" or "evanagari" in out):
            return out
    except Exception:
        pass
    return None


_FONT_CACHE = {}


def _load_font(size, script="latin"):
    key = (size, script)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    path = _find_font(script)
    if not path:
        raise RuntimeError(
            "Caption font nahi mila. GitHub Actions me 'fonts-noto-core' install karo, ya font "
            "fonts/ folder me rakho, ya CAPTION_FONT env var me font ka path do.")
    if script == "hindi":
        if features.check("raqm"):
            font = ImageFont.truetype(path, size, layout_engine=ImageFont.Layout.RAQM)
        else:
            print("⚠️ Pillow me RAQM nahi hai: Hindi matras/conjuncts galat dikh sakte hain "
                  "(Linux par 'libraqm0' install karo).")
            font = ImageFont.truetype(path, size)
    else:
        font = ImageFont.truetype(path, size)
    _FONT_CACHE[key] = font
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
    font = _load_font(CAPTION_FONT_SIZE, "hindi" if CAPTION_SCRIPT == "hindi" else "latin")
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
        int(round(CARD_CENTER_X + slide_dx(t) - cw * scale(t) / 2)),
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


def _open_page(ctx, url):
    page = ctx.new_page()
    try:
        page.goto(url, wait_until="networkidle", timeout=40000)
    except Exception:
        page.goto(url, wait_until="domcontentloaded", timeout=40000)
    try:   # scrollbar recording me na dikhe
        page.add_style_tag(content=HIDE_SCROLLBAR_CSS)
    except Exception:
        pass
    return page


def record_site_background(url, duration, work_dir):
    """Playwright se site ko 9:16 me record karo. (raw_path, shuru ka load time) lautata hai."""
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

        # 1) page ke content column ki chaudai nap lo
        rec_w = BG_REC_W
        env_w = os.getenv("REEL_BG_WIDTH", "").strip()
        if env_w.isdigit():
            rec_w = int(env_w)
        else:
            try:
                mctx = browser.new_context(viewport={"width": BG_REC_W, "height": BG_REC_H})
                mpage = _open_page(mctx, url)
                mpage.wait_for_timeout(1500)
                measured = float(mpage.evaluate(MEASURE_JS) or 0)
                mctx.close()
                if 320 <= measured < BG_REC_W - 12:
                    rec_w = int(round(measured))
                print(f"📐 Page column width: {measured:.0f}px")
            except Exception as e:
                print(f"⚠️ Page width measure fail ({e}); default {BG_REC_W}px")
        rec_w -= rec_w % 2
        rec_h = int(round(rec_w * 16 / 9))
        rec_h -= rec_h % 2
        print(f"📐 Recording viewport = video size = {rec_w}x{rec_h} (9:16)")

        # 2) us chaudai ke 9:16 viewport me record karo
        ctx = browser.new_context(
            viewport={"width": rec_w, "height": rec_h},
            record_video_dir=work_dir,
            record_video_size={"width": rec_w, "height": rec_h})
        t_open = time.time()
        page = _open_page(ctx, url)
        try:
            page.evaluate(FILL_JS)
        except Exception:
            pass
        page.wait_for_timeout(2500)
        skip = time.time() - t_open
        page.evaluate(SCROLL_JS, [int(total * 1000), BG_SCROLL_PX_PER_SEC])
        raw_path = page.video.path()
        ctx.close()
        browser.close()
    if not os.path.exists(raw_path) or os.path.getsize(raw_path) < 5000:
        raise Exception("Recording file nahi bani.")
    return raw_path, skip


def prepare_background_video(raw_path, out_path, start_sec, duration):
    """Recording ko 1080x1920 30fps me badlo (blur nahi: website saaf dikhegi)."""
    vf = (f"fps={FPS},scale={W}:{H}:force_original_aspect_ratio=increase:flags=lanczos,"
          f"crop={W}:{H},unsharp=5:5:0.9:3:3:0.4,format=yuv420p")
    cmd = [_ffmpeg(), "-y", "-ss", f"{max(start_sec - 0.1, 0):.2f}", "-i", raw_path,
           "-t", f"{duration:.2f}", "-vf", vf, "-an",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", out_path]
    subprocess.run(cmd, capture_output=True, check=True)
    if not os.path.exists(out_path) or os.path.getsize(out_path) < 5000:
        raise Exception("Background video nahi bani.")


# ==========================================
# 6. MAIN BUILDER
# ==========================================
def build_reel(audio_path, chunk_infos, scenes, images, kw_to_path, output_mp4,
               bg_url=None, speed=1.10, work_dir="reel_work", bg_video=None, bg_skip=0.0):
    """bg_video: pehle se bani website recording ka path (to bg_url record nahi hota).
    bg_skip: us recording ke shuru ke itne second (page load) kaat do."""
    from moviepy.editor import (AudioFileClip, VideoFileClip, ImageClip,
                                CompositeVideoClip, vfx)
    os.makedirs(work_dir, exist_ok=True)

    audio_src = AudioFileClip(audio_path)
    src_total = float(audio_src.duration)
    audio = audio_src.fx(vfx.speedx, speed) if abs(speed - 1.0) > 1e-3 else audio_src
    total = float(audio.duration)
    print(f"🔊 Audio: {src_total:.2f}s -> {total:.2f}s after {speed}x")

    if not chunk_infos:
        chunk_infos = [{"text": " ".join(s["text"] for s in scenes),
                        "dur": src_total, "lead": 0.0, "trail": 0.0}]
    words = build_word_timings(chunk_infos, src_total, speed)
    plan = plan_scenes(scenes, words, total)
    paths = assign_images(plan, kw_to_path, images)
    seen, cards = {}, []
    for pth in paths:           # same image repeat ho to har baar alag crop
        cards.append(make_card(pth, seen.get(pth, 0)))
        seen[pth] = seen.get(pth, 0) + 1
    if len(seen) < len(paths):
        print(f"⚠️ {len(paths)} scenes me sirf {len(seen)} alag images; baaki me alag crop use hua")
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
    if bg_url or bg_video:
        try:
            if bg_video:
                raw, skip = bg_video, float(bg_skip)
            else:
                raw, skip = record_site_background(bg_url, total, work_dir)
            prepared = os.path.join(work_dir, "bg_site.mp4")
            prepare_background_video(raw, prepared, skip, total)
            bg = VideoFileClip(prepared).without_audio()
            bg_layers = [bg.fx(vfx.loop, duration=total).set_start(0)]
            print("✅ Website recording background ready (saaf, bina blur)")
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
        cap_words = hinglish_words(words, scenes) if CAPTION_SCRIPT != "hindi" else words
        caption_clips = build_caption_clips(cap_words, total)
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


# ==========================================
# 7. LAYOUTS  (shorts = "vertical", lamba video = "wide")
# ==========================================
LAYOUTS = {
    "vertical": dict(W=1080, H=1920, FPS=30,
                     CARD_BOX_W=940, CARD_BOX_H=860, CARD_CENTER_X=540, CARD_CENTER_Y=600,
                     CAPTION_CENTER_Y=1300, CAPTION_FONT_SIZE=80, CAPTION_MARGIN=60,
                     CAPTION_WORDS_PER_LINE=3, CAPTION_STROKE=7),
    "wide": dict(W=1920, H=1080, FPS=25,
                 CARD_BOX_W=1160, CARD_BOX_H=650, CARD_CENTER_X=960, CARD_CENTER_Y=385,
                 CAPTION_CENTER_Y=930, CAPTION_FONT_SIZE=64, CAPTION_MARGIN=140,
                 CAPTION_WORDS_PER_LINE=4, CAPTION_STROKE=6),
}


def set_layout(name):
    """Sab size/position settings ek saath badal do. build_reel se pehle ek baar bulao."""
    globals().update(LAYOUTS[name])
    _FONT_CACHE.clear()
