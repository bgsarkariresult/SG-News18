import os
import re
import json
import time
import asyncio
import subprocess
import requests
from bs4 import BeautifulSoup
from urllib.parse import urlparse, parse_qs
from io import BytesIO
from google import genai

import reel_editing   # captions, scene sync, blurred website background, transitions

# YouTube Upload libraries
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.auth.transport.requests import Request

# ddgs library (DuckDuckGo)
try:
    from ddgs import DDGS
except ImportError:
    print("❌ ddgs library nahi mili. Pehle yeh chalao: pip install ddgs")
    exit()

# ==========================================
# 0. FFMPEG PATH
# ==========================================
FFMPEG_PATH = r"C:\ffmpeg\bin"   # sirf Windows PC ke liye; GitHub Actions par ffmpeg PATH me hota hai
if os.path.isdir(FFMPEG_PATH) and FFMPEG_PATH not in os.environ["PATH"]:
    os.environ["PATH"] += os.path.pathsep + FFMPEG_PATH

# ==========================================
# CONFIG
# ==========================================
# API keys code me nahi hoti: GitHub Secrets / environment variables se aati hain
# (SG News18 bot jaisi hi GEMINI_API_KEY_1/2/3). Local PC par: set GEMINI_API_KEY_1=...
GEMINI_API_KEYS = [os.getenv(k, "").strip() for k in
                   ("GEMINI_API_KEY_1", "GEMINI_API_KEY_2", "GEMINI_API_KEY_3", "GEMINI_API_KEY")]
GEMINI_API_KEYS = list(dict.fromkeys(k for k in GEMINI_API_KEYS if k))
if not GEMINI_API_KEYS:
    print("⚠️ Koi GEMINI_API_KEY nahi mili (GitHub Secrets / environment check karo).")

MAX_CHUNK_CHARACTERS = 160     # chhote chunks = captions ki timing zyada sahi
MAX_RETRIES = 2
VOICE_SPEED = 1.10             # final voice speed (captions/scene timing isi se nikalti hai)

# Peeche website ki recording (bina blur) chalegi: default me wahi article link jo aap dete ho
BG_SITE_URL = os.getenv("REEL_BG_URL", "").strip()   # khali = jo article link diya, wahi peeche record hoga

OUTPUT_DIR = "reel_outputs"
LAST_UPLOAD_ERROR = ""   # upload fail hone ki wajah (Telegram message me jaati hai)

# YouTube (SG News18 bot jaisi hi files: client_secrets.json + token.json)
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
CLIENT_SECRETS_FILE = "client_secrets.json"
TOKEN_FILE = "token.json"

GEMINI_CLIENTS = []
for _i, _k in enumerate(GEMINI_API_KEYS, start=1):
    try:
        GEMINI_CLIENTS.append((f"KEY_{_i}", genai.Client(api_key=_k)))
    except Exception as _e:
        print(f"⚠️ Gemini client {_i} nahi bana: {_e}")

# Telegram (optional: wahi secrets jo SG News18 bot use karta hai)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()


def notify_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                      data={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"},
                      timeout=20)
    except Exception as e:
        print(f"⚠️ Telegram notify failed: {e}")


# ==========================================
# 1. SMART SCRAPER
# ==========================================
def scrape_article_text(url):
    print(f"🌐 Scraping article from: {url}")
    
    article_image = None

    if "bgnewswab.github.io/sgnewswab" in url:
        try:
            parsed = urlparse(url)
            qs = parse_qs(parsed.query)
            article_id = qs.get("id", [None])[0]
            
            if article_id:
                print(f"🔑 Detected SG News ID: {article_id}")
                json_url = "https://bgnewswab.github.io/sgnewswab/data/news.json"
                headers = {'User-Agent': 'Mozilla/5.0'}
                resp = requests.get(json_url, headers=headers, timeout=15)
                data = resp.json()
                
                article = None
                for item in data:
                    if str(item.get("id")) == str(article_id):
                        article = item
                        break
                
                if article:
                    title = article.get("title", "")
                    summary = article.get("summary", "")
                    content_html = article.get("content", "")
                    article_image = article.get("image", None)
                    
                    soup = BeautifulSoup(content_html, "html.parser")
                    clean_content = soup.get_text(separator=" ", strip=True)
                    
                    full_text = f"{title}\n\n{summary}\n\n{clean_content}"
                    full_text = re.sub(r'\s+', ' ', full_text).strip()
                    
                    print(f"✅ Successfully extracted from news.json | Length: {len(full_text)} chars")
                    return full_text, article_image
                else:
                    print(f"⚠️ ID {article_id} news.json mein nahi mila")
        except Exception as e:
            print(f"⚠️ JSON scrape failed: {e}")
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }
    
    try:
        response = requests.get(url, headers=headers, timeout=20)
        soup = BeautifulSoup(response.content, 'html.parser')
        
        title = soup.title.get_text().strip() if soup.title else ""
        
        meta_desc = ""
        meta = soup.find('meta', attrs={'name': 'description'}) or soup.find('meta', property='og:description')
        if meta and meta.get('content'):
            meta_desc = meta.get('content').strip()
        
        paragraphs = [p.get_text().strip() for p in soup.find_all('p') if len(p.get_text().strip()) > 25]
        
        full_text = f"{title}\n{meta_desc}\n" + " ".join(paragraphs)
        full_text = re.sub(r'\s+', ' ', full_text).strip()
        
        print(f"📄 Scraped text length: {len(full_text)} chars")
        return full_text if full_text else "News content not available.", None
        
    except Exception as e:
        print(f"❌ Scrape Error: {e}")
        return "News article content not available.", None


def generate_teen_friendly_script(article_text):
    print("🤖 Generating Script, Title & Description (Gemini Primary -> g4f Valid Providers Fallback)...")
    
    prompt = f"""
    आपको नीचे दिए गए न्यूज़ आर्टिकल के EXACT facts और information का इस्तेमाल करके एक 28-32 सेकंड की वायरल Instagram/YouTube Shorts स्क्रिप्ट बनानी है। साथ ही इसके लिए Catchy Title और Description (Hashtags के साथ) भी बनाना है।

    STRICT RULES:
    1. सिर्फ आर्टिकल के exact facts इस्तेमाल करो। कुछ भी अतिरिक्त मत सोचो या ऐड मत करो।
    2. भाषा: Casual, high-energy, Gen-Z conversational हिंदी (थोड़ा Hinglish allowed)।
    3. स्ट्रक्चर (ज़रूर फॉलो करो):
       - 0-3 सेकंड: मजबूत HOOK (shocking / curiosity / bold statement जो लोगों को रोक दे)
       - बीच में: मुख्य facts तेज़ गति से, साफ और interesting
       - आखिरी 4-5 सेकंड: Soft CTA (जैसे "Full story के लिए कमेंट करो", "शेयर कर दो दोस्तों के साथ", "Follow for more updates")
    4. कुल लंबाई: Voiceover लगभग 28-32 सेकंड (लगभग 70-85 शब्द)।
    5. Scenes: voiceover को क्रम से 5 से 6 scenes में बाँटो। हर scene में (a) उस हिस्से का Hindi voiceover text और (b) उस वक़्त स्क्रीन पर दिखाने लायक image का 1 short English search keyword (सिर्फ real people, places, events) हो। सभी scenes के text को क्रम से जोड़ने पर पूरा voiceover बनना चाहिए: कुछ भी छूटे या दोहराया न जाए। Hook पहले scene में और CTA आख़िरी scene में हो। हर scene में (c) "hinglish" भी दो: वही वाक्य Roman अक्षरों में, जैसे लोग WhatsApp पर लिखते हैं (kya, hai, mein, nahi, aaj), शब्द-दर-शब्द उतने ही शब्द और उसी क्रम में, कोई शब्द जोड़ना या हटाना नहीं। साथ में \"keyword_alt\" दो: उसी scene का और सरल/चौड़ा backup search keyword (सिर्फ़ व्यक्ति, टीम या विषय का नाम, जैसे \"Gautam Gambhir\" या \"BCCI\"), ताकि पहला keyword फेल होने पर दूसरी इमेज मिल सके। नाम और English शब्द सामान्य spelling में लिखो (Smriti Mandhana, Team India, T20)।
    6. Video Title: Catchy & Clickworthy Title with Emojis (Max 80-90 chars).
    7. Video Description: Summary of the news + Call To Action + Relevant Trending Hashtags (#Shorts #Viral #News).

    Article Text:
    \"\"\"{article_text}\"\"\"

    Output STRICTLY in JSON (no markdown, no extra text):
    {{
        "title": "Viral Catchy Title with Emojis 😱🔥",
        "description": "Short engaging description of the news...\\n\\nFollow for more updates! 🚀\\n\\n#Shorts #News #Trending #Viral",
        "scenes": [
            {{"text": "Hook वाला हिस्सा (Hindi voiceover)", "hinglish": "same words in Roman Hinglish", "keyword": "english keyword 1", "keyword_alt": "simple backup keyword"}},
            {{"text": "अगला हिस्सा (Hindi voiceover)", "hinglish": "same words in Roman Hinglish", "keyword": "english keyword 2", "keyword_alt": "simple backup keyword"}},
            {{"text": "CTA वाला आख़िरी हिस्सा (Hindi voiceover)", "hinglish": "same words in Roman Hinglish", "keyword": "english keyword 5", "keyword_alt": "simple backup keyword"}}
        ]
    }}
    """
    
    raw_text = None

    # 1. Primary: Gemini (keys rotate: KEY_1 -> KEY_2 -> KEY_3)
    active_gemini_models = [os.getenv("GEMINI_MODEL_NAME", "gemini-3.6-flash"), 'gemini-3.7-flash']
    active_gemini_models = list(dict.fromkeys(m for m in active_gemini_models if m))

    for key_label, gclient in GEMINI_CLIENTS:
        for model_name in active_gemini_models:
            try:
                print(f"🔄 Trying Gemini {key_label} - {model_name}...")
                response = gclient.models.generate_content(model=model_name, contents=prompt)
                if response and response.text:
                    raw_text = response.text.strip()
                    print(f"✅ Success using Gemini {key_label} - {model_name}")
                    break
            except Exception as e:
                print(f"⚠️ Gemini {key_label} {model_name} Failed: {str(e)[:100]}...")
        if raw_text:
            break

    # 2. Secondary Fallback: Verifiable Working g4f Providers
    if not raw_text:
        print("⚠️ Gemini Failed! Triggering Verified g4f Providers...")
        import g4f
        
        working_providers = ['Pollinations', 'TeachAnything', 'DeepInfra', 'BlackboxPro', 'Custom']
        
        for p_name in working_providers:
            try:
                provider_cls = getattr(g4f.Provider, p_name, None)
                if not provider_cls:
                    continue

                print(f"🔄 Trying g4f Provider: {p_name}...")
                g4f_response = g4f.ChatCompletion.create(
                    model="",
                    provider=provider_cls,
                    messages=[{"role": "user", "content": prompt}]
                )
                if g4f_response and len(str(g4f_response)) > 20:
                    raw_text = str(g4f_response).strip()
                    print(f"✅ Success using g4f Provider: {p_name}!")
                    break
            except Exception as g_err:
                print(f"⚠️ g4f Provider {p_name} failed: {str(g_err)[:100]}")

    # 3. Last Resort: Direct REST Backup
    if not raw_text:
        print("⚠️ Attempting Direct Web Endpoint Fallback...")
        try:
            import urllib.parse
            encoded_prompt = urllib.parse.quote(prompt)
            url = f"https://text.pollinations.ai/{encoded_prompt}?model=openai&json=true"
            resp = requests.get(url, timeout=20)
            if resp.status_code == 200 and len(resp.text.strip()) > 20:
                raw_text = resp.text.strip()
                print("✅ Success using Direct Web Endpoint!")
        except Exception as e:
            print(f"⚠️ Direct Web Endpoint failed: {e}")

    # 4. JSON Extraction
    if raw_text:
        match = re.search(r'\{.*\}', raw_text, re.DOTALL)
        if match:
            data = json.loads(match.group(0))
            scenes = []
            for sc in data.get("scenes") or []:
                if isinstance(sc, dict) and str(sc.get("text", "")).strip() and str(sc.get("keyword", "")).strip():
                    scenes.append({"text": " ".join(str(sc["text"]).split()),
                                   "hinglish": " ".join(str(sc.get("hinglish", "")).split()),
                                   "keyword": str(sc["keyword"]).strip(),
                                   "keyword_alt": " ".join(str(sc.get("keyword_alt", "")).split())})
            if scenes:
                script_text = " ".join(sc["text"] for sc in scenes)
                keywords = [sc["keyword"] for sc in scenes]
            else:   # purana format (script_text + search_keywords)
                script_text = data.get("script_text", "")
                keywords = data.get("search_keywords", [])
            return data.get("title", ""), data.get("description", ""), script_text, keywords, scenes

    # 5. Offline fallback: never stop the Reel just because all AI APIs are down.
    # This uses only the scraped article text; it does not invent new facts.
    print("⚠️ All AI providers unavailable. Building a basic script locally from article text...")
    clean = re.sub(r"<[^>]+>", " ", str(article_text or ""))
    clean = re.sub(r"https?://\\S+", " ", clean)
    clean = re.sub(r"(पूरा विवरण पढ़ें|मुख्य बिंदु|Quick Summary|Follow for more updates)", " ", clean, flags=re.I)
    clean = re.sub(r"\\s+", " ", clean).strip()
    if len(clean) < 100:
        raise Exception("Article text is too short to create a safe fallback Reel.")

    # Prefer complete sentences; cap the voiceover so rendering stays short.
    sentences = [x.strip(" \\t\\n-•") for x in re.split(r"(?<=[।.!?])\\s+", clean)
                 if len(x.strip()) >= 20]
    if not sentences:
        sentences = [clean]
    selected = []
    count = 0
    for sentence in sentences:
        words = sentence.split()
        if count >= 82:
            break
        remaining = 82 - count
        selected.extend(words[:remaining])
        count += min(len(words), remaining)
    voice_words = " ".join(selected).strip()
    if len(voice_words.split()) < 20:
        voice_words = " ".join(clean.split()[:82])

    # Split the extracted facts into 5 sequential scenes; local transliteration is added downstream.
    words = voice_words.split()
    chunk_size = max(1, (len(words) + 4) // 5)
    scenes = []
    title_source = " ".join(clean.split()[:16]).strip(" ,।.!:")
    title = (title_source[:82].rstrip() + " 🔥") if title_source else "SG News18 की ताज़ा खबर 🔥"
    english_terms = re.findall(r"[A-Za-z][A-Za-z0-9'-]*", clean[:900])
    search_terms = " ".join(english_terms[:5]) or "India news"
    for i in range(0, len(words), chunk_size):
        chunk = " ".join(words[i:i + chunk_size]).strip()
        if chunk:
            scenes.append({
                "text": chunk,
                "hinglish": "",
                "keyword": search_terms,
                "keyword_alt": search_terms
            })
    description = title + "\\n\\nखबर की जानकारी के आधार पर तैयार की गई Reel.\\n\\n#SGNews18 #HindiNews #NewsReels #Shorts"
    print(f"✅ Local fallback created: {len(words)} words, {len(scenes)} scenes (no AI API required).")
    return title, description, voice_words, [search_terms], scenes

# ==========================================
# 2. REAL IMAGE DOWNLOADER (Strict Script Matching)
# ==========================================
def _ahash(path):
    """Chhota perceptual hash: ek jaisi dikhne wali images pakadne ke liye."""
    from PIL import Image
    g = Image.open(path).convert("L").resize((8, 8), Image.BILINEAR)
    px = list(g.getdata())
    avg = sum(px) / 64.0
    return sum(1 << i for i, v in enumerate(px) if v >= avg)


def _is_dup(h, hashes):
    return any(bin(h ^ x).count("1") <= 6 for x in hashes)


def pick_relevant_image(cand_paths, keyword, scene_text, context=""):
    """
    Gemini (vision) se poochho ki kaunsi image is scene se sabse zyada match karti hai.
    (index, match 0-10) lautata hai; index -1 = koi bhi sahi nahi; None = check nahi ho paya (Gemini fail).
    """
    if not GEMINI_CLIENTS or not cand_paths:
        return None
    try:
        from google.genai import types
        from PIL import Image
    except Exception:
        return None

    parts = []
    for i, path in enumerate(cand_paths):
        try:
            im = Image.open(path).convert("RGB")
            im.thumbnail((640, 640))
            buf = BytesIO()
            im.save(buf, "JPEG", quality=80)
            parts.append(f"Image {i}:")
            parts.append(types.Part.from_bytes(data=buf.getvalue(), mime_type="image/jpeg"))
        except Exception:
            continue
    if not parts:
        return None

    instruction = (
        "You are choosing the visual for ONE scene of a Hindi news reel.\n"
        f"News headline/context: {context}\n"
        f"Scene voiceover (Hindi): {scene_text}\n"
        f"Image search keyword: {keyword}\n\n"
        "Below are candidate images labelled Image 0, Image 1, ... Pick the ONE image that best shows "
        "the exact person / place / team / event this scene is about. Reject images of a different person, "
        "a different sport or topic, images with big text overlays, logos or watermarks, collages, memes, "
        "screenshots or cartoons. Always name the closest candidate, and rate how well it matches. "
        'Reply ONLY with JSON: {"best": <index of the closest candidate>, "match": <0-10, 10 = exactly right, '
        '0 = completely unrelated>, "reason": "short"}'
    )
    models = list(dict.fromkeys([os.getenv("GEMINI_MODEL_NAME", "gemini-3.6-flash"), "gemini-3.7-flash"]))
    for key_label, gclient in GEMINI_CLIENTS:
        for model_name in models:
            try:
                resp = gclient.models.generate_content(model=model_name, contents=[instruction] + parts)
                m = re.search(r"\{.*\}", (resp.text or ""), re.DOTALL)
                if not m:
                    continue
                data = json.loads(m.group(0))
                best = int(data.get("best", -1))
                match = float(data.get("match", 0))
                if best < 0 or best >= len(cand_paths):
                    best, match = -1, 0.0
                print(f"🧠 Gemini image check [{keyword}] -> {best} match={match:g} ({str(data.get('reason', ''))[:80]})")
                return best, match
            except Exception as e:
                print(f"⚠️ Image check {key_label} {model_name} failed: {str(e)[:80]}")
    return None


MIN_IMAGE_MATCH = 3      # isse kam match wali image nahi lagti
GOOD_IMAGE_MATCH = 6     # itni match mile to aage search nahi karte


def download_logo_free_images(keywords, output_folder="temp_images", article_image_url=None,
                              scene_texts=None, context="", alt_keywords=None):
    print("📸 Downloading relevant images strictly according to script keywords...")
    scene_texts = scene_texts or {}
    alt_keywords = alt_keywords or {}
    context_query = re.sub(r"[^A-Za-z0-9 ]+", " ", context or "")
    context_query = " ".join(context_query.split())

    if os.path.exists(output_folder):
        for f in os.listdir(output_folder):
            try:
                os.remove(os.path.join(output_folder, f))
            except Exception:
                pass
    os.makedirs(output_folder, exist_ok=True)

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    downloaded = []
    used_by_keyword = {}
    chosen_hashes = []
    counter = {"n": 0}

    def save_img(url, name):
        try:
            r = requests.get(url, headers=headers, timeout=12, stream=True)
            content_type = r.headers.get("Content-Type", "")
            if r.status_code != 200 or "image" not in content_type or len(r.content) < 20000:
                return None

            from PIL import Image
            img = Image.open(BytesIO(r.content))
            img.verify()
            img = Image.open(BytesIO(r.content))
            if img.width < 300 or img.height < 300:
                return None

            path = os.path.join(output_folder, name)
            with open(path, "wb") as f:
                f.write(r.content)
            return path
        except Exception as e:
            print(f"⚠️ Rejected {url[:60]}: {e}")
            return None

    def collect(ddgs, query, cands, cand_hashes, limit, max_results):
        try:
            results = list(ddgs.images(query, max_results=max_results, safesearch="moderate"))
        except Exception as e:          # rate limit: thoda ruk kar ek baar aur
            print(f"⚠️ Image search retry for '{query}': {str(e)[:60]}")
            time.sleep(4)
            results = list(ddgs.images(query, max_results=max_results, safesearch="moderate"))
        for r in results:
            if len(cands) >= limit:
                break
            img_url = r.get("image")
            if not img_url:
                continue
            counter["n"] += 1
            path = save_img(img_url, f"cand_{counter['n']:03d}.jpg")
            if not path:
                continue
            try:
                h = _ahash(path)
            except Exception:
                continue
            if _is_dup(h, chosen_hashes) or _is_dup(h, cand_hashes):
                os.remove(path)
                continue
            cands.append(path)
            cand_hashes.append(h)

    clean_keywords = [k.strip() for k in keywords if k and k.strip()]
    if not clean_keywords:
        return downloaded, used_by_keyword

    # Article ki apni image: sabse relevant candidate (ek hi scene me use hogi)
    article_path = None
    if article_image_url:
        article_path = save_img(article_image_url, "article.jpg")
        if article_path:
            print("🖼️ Article image mil gayi (candidate me judegi)")

    try:
        with DDGS() as ddgs:
            for kw in clean_keywords:
                try:
                    print(f"🔍 Searching image for: '{kw}'")
                    cands, cand_hashes = [], []
                    if article_path and article_path not in downloaded:
                        cands.append(article_path)
                        cand_hashes.append(_ahash(article_path))

                    alt = alt_keywords.get(kw, "").strip()
                    if not alt and len(kw.split()) > 2:
                        alt = " ".join(kw.split()[:2])
                    queries = [kw]
                    if alt and alt.lower() != kw.lower():
                        queries.append(alt)
                    if len(context_query) >= 6 and context_query.lower() not in [q.lower() for q in queries]:
                        queries.append(context_query)

                    best, best_score, start = None, -1.0, 0
                    for qi, q in enumerate(queries):
                        if qi > 0:
                            print(f"🔍 Backup search for '{kw}': '{q}'")
                        collect(ddgs, q, cands, cand_hashes, limit=len(cands) + 5, max_results=12)
                        new = cands[start:]
                        start = len(cands)
                        if not new:
                            continue
                        res = pick_relevant_image(new, kw, scene_texts.get(kw, ""), context)
                        if res is None:                 # Gemini check nahi ho paya: pehli valid image
                            best, best_score = new[0], GOOD_IMAGE_MATCH
                            break
                        idx, score = res
                        if idx >= 0 and score > best_score:
                            best, best_score = new[idx], score
                        if best_score >= GOOD_IMAGE_MATCH:
                            break
                    if best is not None and best_score < MIN_IMAGE_MATCH:
                        print(f"⚠️ '{kw}' ke liye koi image script se match nahi hui (best match {best_score:g})")
                        best = None

                    if best:
                        downloaded.append(best)
                        used_by_keyword[kw] = best
                        chosen_hashes.append(_ahash(best))
                        print(f"✅ Selected for '{kw}': {os.path.basename(best)} (match {best_score:g})")

                    for c in cands:        # chune hue ke alawa candidates hata do
                        if c not in downloaded and c != article_path and os.path.exists(c):
                            os.remove(c)
                    time.sleep(1.2)
                except Exception as e:
                    print(f"⚠️ Search failed for '{kw}': {e}")
                    time.sleep(1.5)

    except Exception as e:
        print(f"⚠️ DDGS main error: {e}")

    # Article image kisi scene me na chuni gayi ho to bhi spare ki tarah rakho (galat image se behtar)
    if article_path and article_path not in downloaded:
        downloaded.append(article_path)

    print(f"✅ Total script matched images ready: {len(downloaded)} | Keywords mapped: {list(used_by_keyword.keys())}")
    return downloaded, used_by_keyword


# ==========================================
# 3. VOICE GENERATOR
# ==========================================
def split_script_into_chunks(text, max_chars=400):
    sentences = re.split(r'(?<=[.!?|।])\s+', text)
    chunks = []
    current = ""
    for sentence in sentences:
        if len(current) + len(sentence) <= max_chars:
            current += (" " if current else "") + sentence
        else:
            if current:
                chunks.append(current)
            current = sentence
    if current:
        chunks.append(current)
    return chunks


def generate_fable_voice_openai_fm(text_chunk, output_path):
    try:
        url = "https://www.openai.fm/api/generate"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Origin": "https://www.openai.fm",
            "Referer": "https://www.openai.fm/",
        }
        files = {
            "input": (None, text_chunk),
            "voice": (None, "fable"),
            "prompt": (None, "Speak clearly in a natural, friendly Indian Hindi male tone. Moderate pace."),
            "vibe": (None, "audio")
        }
        response = requests.post(url, files=files, headers=headers, timeout=90, stream=True)
        if response.status_code != 200:
            params = {"input": text_chunk, "voice": "fable", "prompt": "Speak clearly in natural friendly Indian Hindi male tone."}
            response = requests.get(url, params=params, headers=headers, timeout=90, stream=True)
        
        response.raise_for_status()
        with open(output_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
                    
        if os.path.getsize(output_path) < 8000:
            raise Exception("Audio too small")
        return True
    except Exception as e:
        print(f"⚠️ OpenAI.fm Error: {e}")
        return False


def generate_edge_tts_voice(text, output_audio_path):
    voice = "hi-IN-MadhurNeural"
    async def _save():
        import edge_tts
        communicate = edge_tts.Communicate(text, voice, rate="+0%")
        await communicate.save(output_audio_path)
        
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(_save())
    loop.close()


def generate_male_voice(text, output_audio_path):
    print("🎙️ Generating Hindi Voiceover...")
    temp_dir = "temp_voice"
    os.makedirs(temp_dir, exist_ok=True)
    
    chunks = split_script_into_chunks(text, max_chars=MAX_CHUNK_CHARACTERS)
    audio_parts = []
    chunk_texts = []
    fable_failed = False
    
    for idx, chunk in enumerate(chunks, start=1):
        part_filename = os.path.join(temp_dir, f"part_{idx:03d}.mp3")
        success = False
        
        if not fable_failed:
            for attempt in range(1, MAX_RETRIES + 1):
                print(f"🎙️ Part {idx}/{len(chunks)} - OpenAI.fm Attempt {attempt}...")
                if generate_fable_voice_openai_fm(chunk, part_filename):
                    if os.path.getsize(part_filename) >= 8000:
                        success = True
                        break
                time.sleep(2)
            if not success:
                fable_failed = True
        
        if fable_failed or not success:
            print(f"🔊 Part {idx}/{len(chunks)} - Edge TTS Fallback...")
            generate_edge_tts_voice(chunk, part_filename)
            
        audio_parts.append(part_filename)
        chunk_texts.append(chunk)
    
    if audio_parts:
        list_file = os.path.join(temp_dir, "concat_list.txt")
        with open(list_file, "w", encoding="utf-8") as f:
            for p in audio_parts:
                clean_p = os.path.abspath(p).replace('\\', '/')
                f.write(f"file '{clean_p}'\n")
        
        ffmpeg_exe = os.path.join(FFMPEG_PATH, "ffmpeg.exe") if os.path.exists(os.path.join(FFMPEG_PATH, "ffmpeg.exe")) else "ffmpeg"
        
        cmd = [
            ffmpeg_exe, '-f', 'concat', '-safe', '0', '-i', list_file,
            '-af', 'loudnorm=I=-16:LRA=11:TP=-1.5',
            '-ar', '44100', '-ac', '2', '-b:a', '128k',
            '-c:a', 'libmp3lame', '-write_xing', '0',
            '-y', output_audio_path
        ]
        subprocess.run(cmd, capture_output=True, check=True)
        print(f"🔊 Audio Merged: {output_audio_path}")
        # captions/scene timing ke liye har chunk ki asli length
        return reel_editing.measure_chunks(chunk_texts, audio_parts)
    return []


# ==========================================
# 4. VIDEO RENDERER  ->  reel_editing.py
# ==========================================
# Captions, script-synced images, blurred website-recording background aur
# transitions sab reel_editing.build_reel() me hain (settings wahin ke upar hain).


# ==========================================
# 5. YOUTUBE UPLOAD (Unlisted Mode)
# ==========================================
def get_youtube_service():
    credentials = None
    if os.path.exists(TOKEN_FILE):
        credentials = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    if not credentials or not credentials.valid:
        if credentials and credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
        else:
            if os.getenv("GITHUB_ACTIONS") == "true":
                print("❌ GitHub Actions par token.json valid nahi (YT_TOKEN_JSON_B64 secret check karo). Upload skip.")
                global LAST_UPLOAD_ERROR
                LAST_UPLOAD_ERROR = "token.json valid nahi (YT_TOKEN_JSON_B64 secret khali/purana)"
                return None
            if not os.path.exists(CLIENT_SECRETS_FILE):
                print("❌ client_secrets.json nahi mili. Upload skip.")
                LAST_UPLOAD_ERROR = "client_secrets.json nahi mili"
                return None
            flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRETS_FILE, SCOPES)
            credentials = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as token:
            token.write(credentials.to_json())

    return build("youtube", "v3", credentials=credentials)


def upload_to_youtube(video_path, title, description=""):
    print("\n📤 YouTube पर upload शुरू हो रहा है...")
    try:
        youtube = get_youtube_service()
        if not youtube:
            return None

        body = {
            "snippet": {
                "title": title[:100],
                "description": description[:5000],
                "tags": ["Shorts", "News", "Hindi", "Breaking News"],
                "categoryId": "25"  # News & Politics
            },
            "status": {
                "privacyStatus": "unlisted",  # ★ Video set to Unlisted
                "selfDeclaredMadeForKids": False
            }
        }

        media = MediaFileUpload(video_path, mimetype="video/mp4", resumable=True)
        request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

        response = None
        while response is None:
            status, response = request.next_chunk()
            if status:
                print(f"⏳ Upload progress: {int(status.progress() * 100)}%")

        video_id = response.get("id")
        print(f"\n✅ YouTube Upload Successful (Unlisted)!")
        print(f"🔗 Shorts Link: https://youtube.com/shorts/{video_id}")
        print(f"🔗 Normal Link: https://youtu.be/{video_id}")
        return video_id

    except Exception as e:
        global LAST_UPLOAD_ERROR
        LAST_UPLOAD_ERROR = f"{type(e).__name__}: {e}"
        print(f"❌ YouTube Upload Error: {LAST_UPLOAD_ERROR}")
        return None


# ==========================================
# MAIN
# ==========================================
def main():
    import sys
    url = os.getenv("ARTICLE_URL", "").strip()
    if not url and len(sys.argv) > 1:
        url = sys.argv[1].strip()
    if not url:
        url = input("🔗 Enter News Article Link: ").strip()
    if not url:
        print("❌ URL khali nahi ho sakta!")
        return

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    notify_telegram(f"🎞️ <b>Reel maker shuru hua</b>\n🔗 {url}")

    article_text, article_img = scrape_article_text(url)
    print(f"\n📄 Article Preview:\n{article_text[:500]}...\n")

    # AI: Title, Description, Script + scenes (har scene ka text aur image keyword)
    yt_title, yt_desc, script_text, keywords, scenes = generate_teen_friendly_script(article_text)
    if not script_text.strip():
        raise Exception("AI ne khali script di.")
    if not scenes:   # AI ne scenes nahi diye: script ko keywords me barabar baant do
        scenes = reel_editing.scenes_from_script(script_text, keywords)

    print(f"\n📌 Generated Title:\n{yt_title}\n")
    print(f"📝 Generated Description:\n{yt_desc}\n")
    print(f"📜 Generated Script:\n{script_text}\n")
    for i, sc in enumerate(scenes, start=1):
        print(f"🎞️ Scene {i} [{sc['keyword']}]: {sc['text']}")

    for sc in scenes:   # Hinglish na mila ho to local transliteration
        if not sc.get("hinglish"):
            sc["hinglish"] = reel_editing.devanagari_to_hinglish(sc["text"])
        print(f"   ↳ Hinglish: {sc['hinglish']}")

    unique_kws = list(dict.fromkeys(sc["keyword"] for sc in scenes))
    scene_texts = {}
    for sc in scenes:
        scene_texts.setdefault(sc["keyword"], sc["text"])
    alt_kws = {}
    for sc in scenes:
        alt_kws.setdefault(sc["keyword"], sc.get("keyword_alt", ""))
    images, kw_to_path = download_logo_free_images(
        unique_kws, article_image_url=article_img, scene_texts=scene_texts,
        context=yt_title, alt_keywords=alt_kws)
    got = len(set(kw_to_path.values()))
    print(f"🖼️ Scenes ke liye alag images mili: {got}/{len(unique_kws)}")
    if not images:
        raise Exception("Koi image nahi mili.")

    audio_file = os.path.join(OUTPUT_DIR, "final_voiceover.mp3")
    chunk_infos = generate_male_voice(script_text, audio_file)

    output_mp4 = os.path.join(OUTPUT_DIR, f"News_Reel_{time.strftime('%Y%m%d_%H%M%S')}.mp4")
    reel_video = reel_editing.build_reel(
        audio_file, chunk_infos, scenes, images, kw_to_path, output_mp4,
        bg_url=(BG_SITE_URL or url), speed=VOICE_SPEED)

    print("\n🎉 SUCCESS! Reel ban gayi.")
    print(f"Reel: {os.path.abspath(reel_video)}")

    if not yt_title:
        yt_title = script_text[:90]
    if not yt_desc:
        yt_desc = script_text + "\n\n#Shorts #News #Hindi #BreakingNews"

    video_id = upload_to_youtube(reel_video, yt_title, yt_desc)
    if video_id:
        notify_telegram(f"✅ <b>Reel upload ho gaya (Unlisted)</b>\n🎬 {yt_title}\n🖼️ Images: {got}/{len(unique_kws)} scenes\n🔗 https://youtube.com/shorts/{video_id}")
    else:
        notify_telegram(f"✅ Reel ban gaya (YouTube upload nahi hua): {os.path.basename(reel_video)}\n⚠️ Wajah: {LAST_UPLOAD_ERROR[:300] or 'pata nahi'}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import sys
        import traceback
        print(f"\n❌ Pipeline Error: {e}")
        traceback.print_exc()
        notify_telegram(f"❌ <b>Reel maker fail</b>\n<code>{str(e)[:300]}</code>")
        sys.exit(1)
