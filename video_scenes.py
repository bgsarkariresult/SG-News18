"""
SG News18 — voice-synchronised foreground visuals
==================================================
news_bot.py ke purane flow ko badle bina, scrolling website recording ke UPAR
scene-wise picture-in-picture visuals + lower-third lagata hai.

Pipeline (build_video_with_visuals):
  1. classify_category   -> Trending / Politics / Sports / Technology (default: Trending)
  2. plan_scenes         -> Hindi script ko scenes me todna + audio length se timing nikalna
  3. fetch_image         -> article ki apni images validate + download (koi paid API nahi)
  4. assign_images       -> har scene ko relevant image, warna topic-card
  5. _render_frames      -> Chromium (Playwright) se transparent overlay PNG (Hindi text sahi dikhe)
  6. _run_ffmpeg         -> background clip loop + overlays + original audio, H.264/AAC

Koi bhi step fail ho to build_video_with_visuals False return karta hai aur
news_bot.py purana build_video_ffmpeg() chala deta hai (video kabhi nahi rukta).
"""

import html
import io
import json
import os
import re
import shutil
import subprocess
import traceback
from bisect import bisect_right
from collections import Counter
from urllib.parse import urlparse, unquote

import requests
from PIL import Image, ImageOps

# ==========================================
# Constants
# ==========================================
W, H = 1920, 1080
FPS = 30
FADE = 0.4                      # overlay fade in/out seconds (subtle crossfade)
TEMP_DIR = "temp_scenes"        # deterministic temp folder, always cleaned up
SCENE_TARGET = 15.0             # preferred scene length (seconds)
SCENE_MIN = 7.0
SCENE_MAX = 24.0
MAX_IMAGES = 10
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
MIN_IMG_W, MIN_IMG_H = 300, 180
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36")

# Accent colours picked so that white text stays readable on them.
CATEGORIES = {
    "trending":   {"label": "ट्रेंडिंग",   "accent": "#dc2626"},
    "politics":   {"label": "राजनीति",    "accent": "#ea580c"},
    "sports":     {"label": "खेल",        "accent": "#16a34a"},
    "technology": {"label": "टेक्नोलॉजी", "accent": "#0284c7"},
}
DEFAULT_CATEGORY = "trending"
CATEGORY_PRIORITY = ["politics", "sports", "technology", "trending"]  # tie-break order

# Hindi + English keywords (lower-case). Hindi tokens of 4+ chars also match by prefix
# (e.g. "चुनाव" matches "चुनावों"). Short/English tokens must match exactly.
CATEGORY_KEYWORDS = {
    "politics": [
        "चुनाव", "मतदान", "सरकार", "मंत्री", "प्रधानमंत्री", "मुख्यमंत्री", "विधानसभा",
        "लोकसभा", "राज्यसभा", "संसद", "पार्टी", "भाजपा", "कांग्रेस", "विपक्ष", "नेता",
        "विधायक", "सांसद", "राजनीति", "राजनीतिक", "गठबंधन", "चुनाव आयोग", "उम्मीदवार",
        "रैली", "सीएम", "पीएम", "राष्ट्रपति", "राज्यपाल", "विधेयक", "अध्यादेश",
        "election", "vote", "voting", "minister", "bjp", "congress", "parliament",
        "assembly", "government", "party", "politics", "political", "cm", "pm",
        "candidate", "opposition", "lok", "sabha",
    ],
    "sports": [
        "क्रिकेट", "मैच", "खिलाड़ी", "टीम", "आईपीएल", "विश्व कप", "वर्ल्ड", "टेस्ट", "वनडे",
        "टी20", "बल्लेबाज", "गेंदबाज", "कप्तान", "कोच", "फुटबॉल", "हॉकी", "ओलंपिक",
        "टेनिस", "बैडमिंटन", "कबड्डी", "मेडल", "स्टेडियम", "विकेट", "छक्का", "चौका",
        "टूर्नामेंट", "सीरीज", "पारी", "ओवर", "खेल", "एशिया कप", "फाइनल",
        "cricket", "match", "ipl", "football", "hockey", "olympic", "tournament",
        "player", "wicket", "stadium", "captain", "t20", "odi", "final", "series",
    ],
    "technology": [
        "मोबाइल", "स्मार्टफोन", "फोन", "ऐप", "एआई", "आर्टिफिशियल", "इंटेलिजेंस",
        "टेक्नोलॉजी", "गैजेट", "लॉन्च", "प्रोसेसर", "कैमरा", "बैटरी", "सॉफ्टवेयर",
        "इंटरनेट", "5जी", "लैपटॉप", "चिप", "ऐपल", "सैमसंग", "गूगल", "एंड्रॉयड", "आईफोन",
        "चैटजीपीटी", "जेमिनी", "डिवाइस", "वनप्लस", "रियलमी", "शाओमी", "टेक", "साइबर",
        "ai", "smartphone", "phone", "mobile", "launch", "app", "android", "iphone",
        "chatgpt", "gemini", "tech", "software", "5g", "chip", "gadget", "device",
        "laptop", "google", "samsung", "apple",
    ],
    "trending": [
        "वायरल", "ट्रेंड", "ट्रेंडिंग", "सोशल", "मीडिया", "बॉलीवुड", "सेलिब्रिटी", "रील",
        "viral", "trending", "trend", "reel", "bollywood", "celebrity",
    ],
}

# Cross-script concepts so a Hindi scene can match an English image alt/filename
# (e.g. "क्रिकेट" narration <-> "ipl-match.jpg").
CONCEPTS = {
    "cricket": ["क्रिकेट", "cricket", "आईपीएल", "ipl", "बल्लेबाज", "batsman", "बल्ला", "bat",
                "विकेट", "wicket", "टेस्ट", "test", "वनडे", "odi", "टी20", "t20", "कप्तान",
                "captain", "गेंदबाज", "bowler", "स्टेडियम", "stadium", "मैच", "match"],
    "football": ["फुटबॉल", "football", "soccer", "गोल", "goal", "फीफा", "fifa"],
    "hockey": ["हॉकी", "hockey"],
    "olympic": ["ओलंपिक", "olympic", "olympics", "मेडल", "medal", "गोल्ड", "gold"],
    "election": ["चुनाव", "election", "मतदान", "vote", "voting", "ballot", "ईवीएम", "evm",
                 "रैली", "rally", "उम्मीदवार", "candidate"],
    "parliament": ["संसद", "parliament", "लोकसभा", "राज्यसभा", "विधानसभा", "assembly"],
    "leader": ["प्रधानमंत्री", "मुख्यमंत्री", "मंत्री", "नेता", "minister", "leader", "cm", "pm"],
    "court": ["कोर्ट", "court", "अदालत", "सुप्रीम", "supreme", "हाईकोर्ट", "न्यायालय"],
    "phone": ["फोन", "मोबाइल", "स्मार्टफोन", "phone", "mobile", "smartphone", "iphone",
              "आईफोन", "android", "एंड्रॉयड"],
    "ai": ["एआई", "ai", "artificial", "आर्टिफिशियल", "चैटजीपीटी", "chatgpt", "gemini", "जेमिनी"],
    "laptop": ["लैपटॉप", "laptop", "कंप्यूटर", "computer", "pc"],
    "chip": ["चिप", "chip", "प्रोसेसर", "processor", "सेमीकंडक्टर", "semiconductor"],
    "money": ["बजट", "budget", "रुपये", "rupee", "टैक्स", "tax", "बैंक", "bank", "आरबीआई", "rbi"],
    "india": ["भारत", "india", "indian", "भारतीय"],
    "film": ["फिल्म", "film", "movie", "बॉलीवुड", "bollywood", "अभिनेता", "actor"],
}

STOPWORDS = set("""
है हैं था थी थे हो होगा होगी होंगे होता होती होते हुआ हुई हुए का की के को में से पर और या भी ही तो
कि जो यह ये वह वो इस उस इन उन एक दो कर करने करें किया करता गया गई गए जाता जाती जाते जाएगा जाएगी
रहा रही रहे लिए साथ बाद पहले अब जब तब कुछ सभी सब अपने अपनी अपना उनके उनकी इसके इसकी इसमें उसके
उसकी बताया कहा कहना वाले वाली वाला लेकिन मगर क्योंकि तक द्वारा बहुत बड़ी बड़ा नहीं न क्या कैसे कौन कब
कहां अगर तथा एवं सकते सकता सकती चाहिए आप हम हमें आपको दोस्तों खबर खबरें जानकारी लिंक वीडियो
the a an of to in and for on is are with by at from this that it as be
""".split())

# Image names/hosts that are logos, UI furniture or generic stock — never shown as news photos.
_GENERIC_NAME_RE = re.compile(
    r"(logo|icon|avatar|sprite|favicon|placeholder|default|spacer|pixel|emoji|badge|"
    r"banner-ad|adsense|/ads?/|loader|loading|thumb-default|no-?image)", re.I)
_GENERIC_HOSTS = ("unsplash.com", "pexels.com", "pixabay.com", "picsum.photos",
                  "placehold.co", "placeholder.com", "lorempixel.com", "dummyimage.com")
_BAD_EXT = (".svg", ".ico", ".cur", ".mp4", ".webm", ".pdf")

TOKEN_RE = re.compile(r"[A-Za-z0-9ऀ-ॣ०-ॿ]+")   # excludes danda ।
SENT_RE = re.compile(r"[^।!?\n]+(?:[।!?]+|(?=\n)|$)")


# ==========================================
# Text helpers
# ==========================================
def tokenize(text):
    """Lower-cased tokens (Devanagari matras stay attached to their word)."""
    return [t.lower() for t in TOKEN_RE.findall(text or "")]


def _content_tokens(text, min_len=3):
    out = []
    for t in tokenize(text):
        if t in STOPWORDS or t.isdigit():
            continue
        if len(t) >= min_len:
            out.append(t)
    return out


def _kw_hits(tokens, keywords):
    hits = 0
    for t in tokens:
        for kw in keywords:
            if t == kw or (len(kw) >= 4 and t.startswith(kw)):
                hits += 1
                break
    return hits


def concepts_in(tokens):
    found = set()
    for name, words in CONCEPTS.items():
        for t in tokens:
            if any(t == w or (len(w) >= 4 and t.startswith(w)) for w in words):
                found.add(name)
                break
    return found


# ==========================================
# 1. Category classification
# ==========================================
def classify_category(title, script, hints=""):
    """
    Article ke asli subject se category (title ka wazan zyada). Weak signal -> 'trending'.
    Government jobs/recruitment kabhi default nahi hai.
    """
    title_t, script_t, hint_t = tokenize(title), tokenize(script), tokenize(hints)
    scores = {}
    for cat, kws in CATEGORY_KEYWORDS.items():
        scores[cat] = _kw_hits(script_t, kws) + 3 * _kw_hits(title_t, kws) + _kw_hits(hint_t, kws)
    best = max(scores.values()) if scores else 0
    if best < 2:
        return DEFAULT_CATEGORY
    for cat in CATEGORY_PRIORITY:
        if scores.get(cat, 0) == best:
            return cat
    return DEFAULT_CATEGORY


# ==========================================
# 2. Scene planning
# ==========================================
def _weight(sentence):
    core = re.sub(r"\s+", "", sentence)
    return max(1, len(core) + 6 * len(re.findall(r"[।!?]", sentence))
               + 3 * len(re.findall(r"[,;:]", sentence)))


def split_sentences(script):
    """Sentences with char offset + 'para_break' flag (blank line before it)."""
    script = script or ""
    sents, prev_end = [], 0
    for m in SENT_RE.finditer(script):
        text = m.group().strip()
        if len(re.findall(r"[A-Za-z0-9ऀ-ॿ]", text)) < 2:
            continue
        gap = script[prev_end:m.start()]
        sents.append({
            "text": text,
            "start": m.start() + (len(m.group()) - len(m.group().lstrip())),
            "para_break": bool(re.search(r"\n\s*\n", gap)) and bool(sents),
            "w": _weight(text),
        })
        prev_end = m.end()
    # Script bina danda ('.' se likhi ho) to '.' par bhi todo
    if len(sents) < 3 and len(script) > 400:
        sents, pos = [], 0
        for part in re.split(r"(?<=\.)\s+", script):
            idx = script.find(part, pos)
            pos = max(pos, idx + len(part)) if idx >= 0 else pos
            if len(part.strip()) > 1:
                sents.append({"text": part.strip(), "start": max(idx, 0),
                              "para_break": False, "w": _weight(part)})
    return sents


def _times_with_chunks(sents, script, audio_dur, chunk_info):
    """Voice parts ki asli length ko anchor bana kar sentence timing (zyada sahi)."""
    chunks = [c for c, _ in chunk_info]
    durs = [float(d) for _, d in chunk_info]
    if not chunks or any(d <= 0 for d in durs):
        raise ValueError("bad chunk info")
    starts, pos = [], 0
    for c in chunks:
        first_line = c.strip().split("\n")[0][:30]
        i = script.find(first_line, pos)
        if i < 0:
            raise ValueError("chunk not found in script")
        starts.append(i)
        pos = i + 1
    starts[0] = 0
    scale = audio_dur / sum(durs)
    bounds = [0.0]
    for d in durs:
        bounds.append(bounds[-1] + d * scale)
    chunk_of = [max(0, bisect_right(starts, s["start"]) - 1) for s in sents]
    est = [None] * len(sents)
    for ci in range(len(chunks)):
        idxs = [i for i, c in enumerate(chunk_of) if c == ci]
        if not idxs:
            continue
        tw = sum(sents[i]["w"] for i in idxs)
        t = bounds[ci]
        span = bounds[ci + 1] - bounds[ci]
        for i in idxs:
            dt = span * sents[i]["w"] / tw
            est[i] = (t, t + dt)
            t += dt
    if any(e is None for e in est):
        raise ValueError("sentence without time")
    return est


def _sentence_times(sents, script, audio_dur, chunk_info):
    if chunk_info:
        try:
            return _times_with_chunks(sents, script, audio_dur, chunk_info)
        except Exception as e:
            print(f"ℹ️ Chunk-anchored timing nahi bani ({e}); length-based estimate use hoga.")
    total = sum(s["w"] for s in sents) or 1
    cum, est = 0, []
    for s in sents:
        est.append((audio_dur * cum / total, audio_dur * (cum + s["w"]) / total))
        cum += s["w"]
    return est


def _topic_shift(sents, cur, j):
    prev = set(_content_tokens(" ".join(sents[i]["text"] for i in cur[-2:])))
    nxt = set(_content_tokens(" ".join(s["text"] for s in sents[j:j + 2])))
    if len(prev) < 3 or len(nxt) < 3:
        return False
    return len(prev & nxt) / float(len(prev | nxt)) < 0.06


def _group_sentences(sents, times, audio_dur, target, min_len, max_len):
    groups, cur = [], [0]
    for j in range(1, len(sents)):
        cur_len = times[j][0] - times[cur[0]][0]
        brk = (cur_len >= max_len or cur_len >= target
               or (cur_len >= min_len and sents[j]["para_break"] and cur_len >= 0.6 * target)
               or (cur_len >= min_len and _topic_shift(sents, cur, j)))
        if brk:
            groups.append(cur)
            cur = [j]
        else:
            cur.append(j)
    groups.append(cur)
    if len(groups) > 1 and audio_dur - times[groups[-1][0]][0] < 0.8 * min_len:
        groups[-2].extend(groups.pop())
    return groups


def make_headline(text, limit=84):
    """Script ki asli line se chhota headline (word boundary par, '…' agar kata ho)."""
    t = re.sub(r"\s+", " ", (text or "")).strip().strip("\"'“”‘’")
    t = re.sub(r"[।!?.]+$", "", t).strip()
    if len(t) <= limit:
        return t
    cut = t[:limit]
    if " " in cut:
        cut = cut[:cut.rfind(" ")]
    return cut.rstrip(" ,;:-–—") + "…"


def _display_token(tok):
    return tok.upper() if tok.isascii() and len(tok) <= 4 else (tok.title() if tok.isascii() else tok)


def _scene_chips(text, title_tokens, cat_key, limit=3):
    """Scene ke verbatim shabd: title me aaye, category/concept wale, ya sankhya."""
    seen, ranked = set(), []
    for orig in TOKEN_RE.findall(text or ""):
        low = orig.lower()
        if low in seen or low in STOPWORDS:
            continue
        seen.add(low)
        if low in title_tokens and len(low) >= 3:
            score = 3
        elif _kw_hits([low], CATEGORY_KEYWORDS.get(cat_key, [])) or concepts_in([low]):
            score = 2
        elif low.isdigit() and len(low) >= 2:
            score = 1
        else:
            continue
        ranked.append((score, len(ranked), orig))
    ranked.sort(key=lambda r: (-r[0], r[1]))
    return [_display_token(o.lower()) if o.isascii() else o for _, _, o in ranked[:limit]]


def plan_scenes(script, audio_dur, chunk_info=None, title="", category=DEFAULT_CATEGORY,
                target=SCENE_TARGET, min_len=SCENE_MIN, max_len=SCENE_MAX):
    """
    Script -> contiguous scenes covering [0, audio_dur].
    Har scene: idx, start, end, text, headline, chips, tokens, concepts.
    """
    audio_dur = float(audio_dur)
    sents = split_sentences(script)
    if not sents or audio_dur <= 0:
        return []
    times = _sentence_times(sents, script, audio_dur, chunk_info)
    groups = _group_sentences(sents, times, audio_dur, target, min_len, max_len)

    title_tokens = set(_content_tokens(title))
    scenes = []
    for k, g in enumerate(groups):
        start = 0.0 if k == 0 else times[g[0]][0]
        end = audio_dur if k == len(groups) - 1 else times[groups[k + 1][0]][0]
        text = " ".join(sents[i]["text"] for i in g)
        toks = _content_tokens(text)
        scenes.append({
            "idx": k, "start": round(start, 3), "end": round(max(end, start + 0.5), 3),
            "text": text,
            "headline": make_headline(title, 90) if (k == 0 and title) else make_headline(sents[g[0]]["text"]),
            "chips": _scene_chips(text, title_tokens, category),
            "tokens": set(toks), "concepts": concepts_in(toks),
            "image_path": None, "image_mode": "card",
        })
    scenes[-1]["end"] = round(audio_dur, 3)
    return scenes


# ==========================================
# 3. Article image discovery + download
# ==========================================
_EXTRACT_JS = r"""
() => {
  const abs = (u) => { try { return new URL(u, document.baseURI).href; } catch (e) { return ""; } };
  const out = {metas: [], imgs: [], title: "", description: "", section: ""};
  const meta = (sel) => { const el = document.querySelector(sel); return el ? (el.getAttribute('content') || '') : ''; };
  const h1 = document.querySelector('h1');
  out.title = meta('meta[property="og:title"]') || (h1 ? h1.innerText.trim() : '');
  out.description = meta('meta[property="og:description"]') || meta('meta[name="description"]');
  out.section = meta('meta[property="article:section"]');
  ['meta[property="og:image"]', 'meta[property="og:image:secure_url"]',
   'meta[name="twitter:image"]', 'meta[name="twitter:image:src"]'].forEach(s => {
    const v = meta(s); if (v) out.metas.push(abs(v)); });
  const link = document.querySelector('link[rel="image_src"]');
  if (link && link.href) out.metas.push(abs(link.href));
  document.querySelectorAll('script[type="application/ld+json"]').forEach(sc => {
    try {
      const walk = (o) => {
        if (!o) return;
        if (Array.isArray(o)) { o.forEach(walk); return; }
        if (typeof o === 'object') {
          if (o.image) { (Array.isArray(o.image) ? o.image : [o.image]).forEach(x => {
            const u = typeof x === 'string' ? x : (x && (x.url || x.contentUrl)); if (u) out.metas.push(abs(u)); }); }
          Object.values(o).forEach(v => { if (v && typeof v === 'object') walk(v); });
        }
      };
      walk(JSON.parse(sc.textContent));
    } catch (e) {}
  });
  let root = null;
  for (const s of ['#article-content', '.article-content', '.post-body', 'article', 'main', '.content']) {
    const el = document.querySelector(s); if (el) { root = el; break; }
  }
  document.querySelectorAll('img').forEach(im => {
    if (im.closest('header, nav, footer, aside, .ad, .ads, [class*="sidebar"], [class*="advert"]')) return;
    const src = im.currentSrc || im.src || im.getAttribute('data-src') || im.getAttribute('data-lazy-src') || '';
    if (!src || src.startsWith('data:')) return;
    const fig = im.closest('figure');
    const cap = fig && fig.querySelector('figcaption') ? fig.querySelector('figcaption').innerText.trim() : '';
    out.imgs.push({url: abs(src), alt: im.alt || '', title: im.title || '', caption: cap,
                   w: im.naturalWidth || 0, h: im.naturalHeight || 0,
                   inArticle: root ? root.contains(im) : true});
  });
  return out;
}
"""


def _norm_url_key(u):
    p = urlparse(u)
    return f"{p.netloc.lower()}{p.path}"


def _is_candidate_image(c):
    """Cheap pre-download filter: logos/icons/stock/tiny/odd-ratio images are dropped."""
    url = c.get("url", "")
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.netloc:
        return False
    path = unquote(p.path).lower()
    if path.endswith(_BAD_EXT):
        return False
    if any(h in p.netloc.lower() for h in _GENERIC_HOSTS):
        return False                       # generic stock photo != news photo
    if _GENERIC_NAME_RE.search(path) or _GENERIC_NAME_RE.search(c.get("alt", "") or ""):
        return False
    w, h = c.get("w", 0) or 0, c.get("h", 0) or 0
    if w and h:
        if w < MIN_IMG_W or h < MIN_IMG_H:
            return False
        if not (0.4 <= w / float(h) <= 3.8):
            return False
    return True


def parse_extracted_images(data, max_images=MAX_IMAGES):
    """Playwright ke raw dict -> filtered, de-duplicated candidate list (lead image pehle)."""
    cands, seen = [], set()

    def add(c):
        key = _norm_url_key(c["url"])
        if key in seen or not _is_candidate_image(c):
            return
        seen.add(key)
        cands.append(c)

    for u in data.get("metas", []) or []:
        add({"url": u, "alt": "", "caption": "", "title": data.get("title", ""),
             "w": 0, "h": 0, "kind": "meta"})
    imgs = [i for i in (data.get("imgs", []) or []) if i.get("inArticle", True)]
    imgs.sort(key=lambda i: -((i.get("w") or 0) * (i.get("h") or 0)))
    for i in imgs:
        add({"url": i["url"], "alt": i.get("alt", ""), "caption": i.get("caption", ""),
             "title": i.get("title", ""), "w": i.get("w", 0), "h": i.get("h", 0), "kind": "img"})
    return cands[:max_images]


def extract_article_images(url, max_images=MAX_IMAGES):
    """
    Article page (JS se render hota hai) se uski apni images + metadata. Fail ho to [].
    Sirf og/twitter/ld+json aur article container ki <img> li jaati hain.
    """
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page(viewport={"width": 1280, "height": 900})
                try:
                    page.goto(url, wait_until="networkidle", timeout=30000)
                except Exception:
                    page.goto(url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(2000)
                try:                                   # lazy-loaded images ke liye
                    height = page.evaluate("document.body.scrollHeight")
                    for y in range(0, min(int(height), 9600), 800):
                        page.evaluate(f"window.scrollTo(0, {y})")
                        page.wait_for_timeout(150)
                    page.evaluate("window.scrollTo(0, 0)")
                except Exception:
                    pass
                data = page.evaluate(_EXTRACT_JS)
            finally:
                browser.close()
        found = parse_extracted_images(data, max_images)
        print(f"🖼️ Article images mili: {len(found)} (candidates after filtering)")
        return found
    except Exception as e:
        print(f"⚠️ Article image extraction fail ({e}). Topic cards use honge.")
        return []


def fetch_image(url, dest_path, timeout=15):
    """
    Image download + Pillow se validate; 1600px tak JPEG me normalize. Koi retry/bypass nahi —
    401/403/429 ya koi bhi error ho to None (source ki restriction ka samman).
    """
    try:
        if urlparse(url).scheme not in ("http", "https"):
            return None
        r = requests.get(url, headers={"User-Agent": USER_AGENT, "Accept": "image/*"},
                         timeout=timeout, stream=True)
        if r.status_code != 200:
            print(f"ℹ️ Image skip (HTTP {r.status_code}): {url[:90]}")
            return None
        ctype = r.headers.get("content-type", "").lower()
        if ctype and not (ctype.startswith("image/") or "octet-stream" in ctype):
            print(f"ℹ️ Image skip (content-type {ctype}): {url[:90]}")
            return None
        data = b""
        for chunk in r.iter_content(chunk_size=65536):
            data += chunk
            if len(data) > MAX_IMAGE_BYTES:
                print(f"ℹ️ Image skip (too large): {url[:90]}")
                return None
        img = Image.open(io.BytesIO(data))
        if img.width * img.height > MAX_IMAGE_PIXELS:
            return None
        img.load()
        if img.width < MIN_IMG_W or img.height < MIN_IMG_H:
            print(f"ℹ️ Image skip (too small {img.width}x{img.height}): {url[:90]}")
            return None
        img = ImageOps.exif_transpose(img)
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGBA")
            bg = Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[-1])
            img = bg
        else:
            img = img.convert("RGB")
        img.thumbnail((1600, 1600))
        img.save(dest_path, "JPEG", quality=88)
        return dest_path
    except Exception as e:
        print(f"ℹ️ Image skip ({type(e).__name__}): {url[:90]}")
        return None


# ==========================================
# 4. Image <-> scene matching
# ==========================================
def _image_context(img):
    parts = [img.get("alt", ""), img.get("caption", ""), img.get("title", "")]
    path = unquote(urlparse(img.get("url", "")).path)
    parts.append(re.sub(r"[-_./+]+", " ", path))
    toks = _content_tokens(" ".join(parts))
    return set(toks), concepts_in(toks)


def _match_score(scene, img_tokens, img_concepts):
    return 3 * len(scene["concepts"] & img_concepts) + len(scene["tokens"] & img_tokens)


def assign_images(scenes, images, threshold=3, max_uses=2):
    """
    images: [{'path','url','alt','caption','title','kind'}] (sirf downloaded/valid).
      1) metadata se match (alt/caption/filename/URL vs scene text) — score >= threshold
      2) lead image bachi ho to scene 0 (opening) par
      3) baaki article images bache scenes me barabar baant kar (article ki apni images)
      4) jis scene ko image na mile -> topic card
    Koi image kisi scene par max_uses se zyada ya lagataar do scenes par nahi.
    """
    for s in scenes:
        s["image_path"], s["image_mode"] = None, "card"
    if not scenes or not images:
        return scenes

    ctx = [_image_context(im) for im in images]
    uses = Counter()
    pairs = []
    for si, s in enumerate(scenes):
        for ii, (it, ic) in enumerate(ctx):
            sc = _match_score(s, it, ic)
            if sc >= threshold:
                pairs.append((sc, -si, ii, si))
    pairs.sort(reverse=True)
    for sc, _, ii, si in pairs:
        s = scenes[si]
        if s["image_path"] or uses[ii] >= max_uses:
            continue
        if any(scenes[n]["image_path"] == images[ii]["path"] for n in (si - 1, si + 1) if 0 <= n < len(scenes)):
            continue
        s["image_path"], s["image_mode"] = images[ii]["path"], "matched"
        uses[ii] += 1

    unused = [i for i in range(len(images)) if uses[i] == 0]
    free = [si for si, s in enumerate(scenes) if not s["image_path"]]
    if unused and free:
        if free[0] == 0:                       # lead image -> opening scene
            ii = unused.pop(0)
            scenes[0]["image_path"], scenes[0]["image_mode"] = images[ii]["path"], "article-lead"
            uses[ii] += 1
            free.pop(0)
        if unused and free:
            n = min(len(unused), len(free))
            picks = sorted({min(len(free) - 1, int((k + 0.5) * len(free) / n)) for k in range(n)})
            for slot, ii in zip(picks, unused):
                si = free[slot]
                scenes[si]["image_path"], scenes[si]["image_mode"] = images[ii]["path"], "article-order"
                uses[ii] += 1
    return scenes


# ==========================================
# 5. Overlay frames (Chromium renders Hindi correctly)
# ==========================================
_FRAME_CSS = """
*{box-sizing:border-box;margin:0;padding:0}
html,body{width:1920px;height:1080px;background:transparent;overflow:hidden;
 font-family:'Noto Sans Devanagari','Noto Sans','Lohit Devanagari','Mukta','FreeSans','Nirmala UI',sans-serif}
.panel{position:absolute;right:56px;top:64px;width:840px;height:472px;border-radius:24px;overflow:hidden;
 background:#0b1226;border:4px solid rgba(255,255,255,.92);box-shadow:0 22px 64px rgba(0,0,0,.55)}
.blur{position:absolute;inset:-40px;background-size:cover;background-position:center;filter:blur(26px) brightness(.55)}
.photo{position:absolute;inset:0;width:100%;height:100%;object-fit:contain}
.card{background:linear-gradient(135deg,#0b1226 0%,#16213f 60%,#0f1a33 100%)}
.ring{position:absolute;right:-120px;top:-120px;width:420px;height:420px;border-radius:50%;
 border:34px solid var(--accent);opacity:.30}
.disc{position:absolute;right:-110px;bottom:-170px;width:360px;height:360px;border-radius:50%;
 background:var(--accent);opacity:.16}
.bar{position:absolute;left:56px;top:120px;width:96px;height:10px;border-radius:6px;background:var(--accent)}
.label{position:absolute;left:56px;top:148px;font-size:108px;font-weight:800;color:#fff;line-height:1.3}
.keys{position:absolute;left:56px;right:56px;bottom:40px}
.sub{font-size:28px;color:rgba(255,255,255,.72);margin-bottom:10px}
.chips{display:flex;flex-wrap:wrap;gap:14px}
.chip{padding:6px 22px;border-radius:999px;border:2px solid rgba(255,255,255,.55);
 background:rgba(255,255,255,.08);color:#fff;font-size:34px;font-weight:600;line-height:1.35}
.lt{position:absolute;left:56px;right:56px;bottom:48px;height:152px;display:flex;align-items:center;gap:30px;
 padding:0 36px 0 30px;border-radius:20px;background:linear-gradient(90deg,rgba(8,13,28,.98),rgba(14,22,46,.96));
 border-left:14px solid var(--accent);box-shadow:0 14px 44px rgba(0,0,0,.5)}
.cat{flex:none;padding:8px 24px;border-radius:12px;background:var(--accent);color:#fff;font-size:34px;
 font-weight:800;line-height:1.4}
.hl{flex:1;color:#fff;font-size:44px;font-weight:700;line-height:1.3;display:-webkit-box;
 -webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
"""


def _frame_html(scene, cat_key, image_file=None):
    cat = CATEGORIES[cat_key]
    esc = html.escape
    if image_file:
        src = esc(os.path.basename(image_file), quote=True)
        panel = (f'<div class="panel"><div class="blur" style="background-image:url(\'{src}\')"></div>'
                 f'<img class="photo" src="{src}"></div>')
    else:
        chips = "".join(f'<span class="chip">{esc(c)}</span>' for c in scene.get("chips", []))
        sub = '<div class="sub">मुख्य शब्द</div>' if chips else ""
        panel = (f'<div class="panel card"><div class="ring"></div><div class="disc"></div>'
                 f'<div class="bar"></div><div class="label">{esc(cat["label"])}</div>'
                 f'<div class="keys">{sub}<div class="chips">{chips}</div></div></div>')
    return (f'<!doctype html><html lang="hi"><head><meta charset="utf-8"><style>{_FRAME_CSS}</style></head>'
            f'<body style="--accent:{cat["accent"]}">{panel}'
            f'<div class="lt"><div class="cat">{esc(cat["label"])}</div>'
            f'<div class="hl">{esc(scene["headline"])}</div></div></body></html>')


def _has_devanagari_font():
    """True agar system me Devanagari font hai (ya check hi nahi ho sakta)."""
    try:
        out = subprocess.run(["fc-list", ":lang=hi", "family"], capture_output=True, text=True, timeout=20)
        return bool(out.stdout.strip())
    except Exception:
        return True


def _render_frames(scenes, cat_key, workdir):
    """Har scene ke liye transparent 1920x1080 PNG. Image load na ho to wo scene card ban jata hai."""
    from playwright.sync_api import sync_playwright
    pngs = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_context(viewport={"width": W, "height": H}, device_scale_factor=1).new_page()
            for sc in scenes:
                png = os.path.join(workdir, f"frame_{sc['idx']:03d}.png")
                attempts = [sc["image_path"], None] if sc.get("image_path") else [None]
                done = False
                for img in attempts:
                    html_path = os.path.join(workdir, f"frame_{sc['idx']:03d}.html")
                    if img and os.path.abspath(os.path.dirname(img)) != os.path.abspath(workdir):
                        shutil.copyfile(img, os.path.join(workdir, os.path.basename(img)))
                    with open(html_path, "w", encoding="utf-8") as f:
                        f.write(_frame_html(sc, cat_key, img))
                    page.goto("file://" + os.path.abspath(html_path), wait_until="load", timeout=20000)
                    page.evaluate("document.fonts.ready")
                    if img and not page.evaluate(
                            "() => { const i = document.querySelector('img.photo'); return !!i && i.complete && i.naturalWidth > 0; }"):
                        print(f"ℹ️ Scene {sc['idx']}: image render nahi hui, topic card use hoga.")
                        sc["image_path"], sc["image_mode"] = None, "card"
                        continue
                    page.screenshot(path=png, omit_background=True)
                    done = True
                    break
                if not done:
                    raise RuntimeError(f"scene {sc['idx']} render fail")
                pngs.append(png)
        finally:
            browser.close()
    return pngs


# ==========================================
# 6. FFmpeg composition
# ==========================================
def _probe(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", path],
        capture_output=True, text=True, timeout=120)
    return json.loads(out.stdout or "{}")


def _probe_duration(path):
    return float(_probe(path)["format"]["duration"])


def build_ffmpeg_command(audio_path, clip_path, output_path, scenes, frame_pngs, audio_dur):
    """Background (loop) + per-scene overlay inputs. Overlay frames sirf apni window ke liye decode hote hain."""
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
           "-stream_loop", "-1", "-i", clip_path, "-i", audio_path]
    fl = [f"[0:v]fps={FPS},scale={W}:{H}:force_original_aspect_ratio=decrease,"
          f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,format=yuv420p[bg]"]
    last = "bg"
    n = len(scenes)
    for k, (sc, png) in enumerate(zip(scenes, frame_pngs)):
        t0 = sc["start"]
        last_scene = k == n - 1
        # Scene k fade-in hota hai pichhle (thos) scene ke UPAR; pichhla tab tak rukta hai jab tak naya
        # poora opaque na ho -> beech me background/text jhalakta nahi. Sirf aakhri scene fade-out hota hai.
        t1 = min(sc["end"] + (0.0 if last_scene else FADE), audio_dur)
        dur = max(t1 - t0, 0.6)
        fd = min(FADE, dur / 2.0)
        fade = f"fade=t=in:st=0:d={fd:.3f}:alpha=1"
        if last_scene:
            fade += f",fade=t=out:st={dur - fd:.3f}:d={fd:.3f}:alpha=1"
        cmd += ["-loop", "1", "-framerate", str(FPS), "-t", f"{dur:.3f}", "-i", png]
        fl.append(f"[{k + 2}:v]format=rgba,{fade},setpts=PTS-STARTPTS+{t0:.3f}/TB[o{k}]")
        fl.append(f"[{last}][o{k}]overlay=0:0:eof_action=pass:enable='between(t,{t0:.3f},{t0 + dur:.3f})'[v{k}]")
        last = f"v{k}"
    fl.append(f"[{last}]format=yuv420p[vout]")
    cmd += ["-filter_complex", ";".join(fl), "-map", "[vout]", "-map", "1:a:0",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
            "-profile:v", "high", "-r", str(FPS),
            "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
            "-t", f"{audio_dur:.3f}", "-movflags", "+faststart", output_path]
    return cmd


def verify_output(path, audio_dur, tol=0.6):
    """YouTube-compatible output? (h264/aac, yuv420p, 1920x1080, duration ~ audio). -> (ok, message)"""
    try:
        info = _probe(path)
        streams = info.get("streams", [])
        v = next((s for s in streams if s.get("codec_type") == "video"), None)
        a = next((s for s in streams if s.get("codec_type") == "audio"), None)
        if not v or not a:
            return False, "video/audio stream missing"
        dur = float(info["format"]["duration"])
        problems = []
        if v.get("codec_name") != "h264":
            problems.append(f"video codec {v.get('codec_name')}")
        if v.get("pix_fmt") != "yuv420p":
            problems.append(f"pix_fmt {v.get('pix_fmt')}")
        if (v.get("width"), v.get("height")) != (W, H):
            problems.append(f"size {v.get('width')}x{v.get('height')}")
        if a.get("codec_name") != "aac":
            problems.append(f"audio codec {a.get('codec_name')}")
        if abs(dur - audio_dur) > tol:
            problems.append(f"duration {dur:.2f}s vs audio {audio_dur:.2f}s")
        return (not problems), (", ".join(problems) or f"ok ({dur:.2f}s, {W}x{H}, h264/aac)")
    except Exception as e:
        return False, f"probe error: {e}"


def _run_ffmpeg(cmd):
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    if res.returncode != 0:
        print(f"❌ FFmpeg (visuals) fail, exit {res.returncode}:\n{(res.stderr or '')[-1800:]}")
        return False
    return True


# ==========================================
# Orchestrator
# ==========================================
def _safe_cleanup(workdir):
    try:
        if os.path.basename(os.path.abspath(workdir)) == TEMP_DIR and os.path.isdir(workdir):
            shutil.rmtree(workdir, ignore_errors=True)
    except Exception:
        pass


def build_video_with_visuals(audio_path, video_clip_path, output_video_path, title, script,
                             article_images=None, chunk_info=None, page_text="", workdir=TEMP_DIR):
    """
    Scrolling background + voice-synced foreground visuals. True = output ready & verified.
    False = kuch bhi fail hua (caller purana build_video_ffmpeg chalaye). Kabhi exception nahi uthata.
    """
    try:
        if not (video_clip_path and os.path.exists(video_clip_path)):
            print("ℹ️ Background recording nahi mili — visuals skip.")
            return False
        if not (audio_path and os.path.exists(audio_path)):
            return False
        if not _has_devanagari_font():
            print("⚠️ System me Devanagari font nahi mila (apt: fonts-noto-core) — visuals skip.")
            return False

        audio_dur = _probe_duration(audio_path)
        category = classify_category(title, script, (page_text or "")[:3000])
        print(f"🏷️ Category: {category} ({CATEGORIES[category]['label']})")

        scenes = plan_scenes(script, audio_dur, chunk_info, title, category)
        if not scenes:
            print("⚠️ Scene plan nahi ban paya — visuals skip.")
            return False

        _safe_cleanup(workdir)
        os.makedirs(workdir, exist_ok=True)

        ready = []
        for i, cand in enumerate(article_images or []):
            dest = os.path.join(workdir, f"img_{i:02d}.jpg")
            if fetch_image(cand["url"], dest):
                ready.append({**cand, "path": dest})
        assign_images(scenes, ready)

        print(f"🎞️ {len(scenes)} scenes | {len(ready)}/{len(article_images or [])} images usable | audio {audio_dur:.1f}s")
        for sc in scenes:
            print(f"   #{sc['idx']:02d} {sc['start']:7.2f}-{sc['end']:7.2f}s [{sc['image_mode']:13s}] {sc['headline'][:60]}")

        frames = _render_frames(scenes, category, workdir)
        if os.path.exists(output_video_path):
            os.remove(output_video_path)
        cmd = build_ffmpeg_command(audio_path, video_clip_path, output_video_path, scenes, frames, audio_dur)
        if not _run_ffmpeg(cmd):
            _discard(output_video_path)
            return False
        ok, msg = verify_output(output_video_path, audio_dur)
        print(("✅ " if ok else "❌ ") + f"Visual video check: {msg}")
        if not ok:
            _discard(output_video_path)
        return ok
    except Exception as e:
        print(f"❌ Visual pipeline error: {e}\n{traceback.format_exc()[-1500:]}")
        _discard(output_video_path)
        return False
    finally:
        _safe_cleanup(workdir)


def _discard(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception:
        pass
