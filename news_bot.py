import os
import re
import json
import time
import asyncio
import subprocess
import requests
from g4f.client import Client
import edge_tts
from gtts import gTTS
from PIL import Image, ImageDraw, ImageFont
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
# Google API Imports
from google_auth_oauthlib.flow import InstalledAppFlow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
# Gemini Imports
from google import genai
from google.genai import types
# Groq Import
from groq import Groq

# ==========================================
# 1. Configuration & Setup
# ==========================================
client = Client()  # g4f (last fallback)

# ==========================================
# Gemini Setup (PRIMARY) — 3 API KEY ROTATION
# ==========================================
GEMINI_API_KEYS = [
    os.getenv("GEMINI_API_KEY_1", "").strip(),
    os.getenv("GEMINI_API_KEY_2", "").strip(),
    os.getenv("GEMINI_API_KEY_3", "").strip(),
]
GEMINI_API_KEYS = [k for k in GEMINI_API_KEYS if k]

if not GEMINI_API_KEYS:
    print("⚠️ Warning: Koi bhi GEMINI_API_KEY_1/2/3 nahi mila. GitHub Secrets check karo!")

GEMINI_MODELS = [os.getenv("GEMINI_MODEL_NAME", "gemini-3.6-flash")]

GEMINI_CLIENTS = []
for idx, key in enumerate(GEMINI_API_KEYS, start=1):
    try:
        GEMINI_CLIENTS.append((f"KEY_{idx}", genai.Client(api_key=key)))
    except Exception as e:
        print(f"⚠️ GEMINI_API_KEY_{idx} se client banane me error: {e}")

# ==========================================
# Groq Setup (SECONDARY FALLBACK)
# ==========================================
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
GROQ_CLIENT = None
GROQ_MODELS = ["openai/gpt-oss-120b", "qwen/qwen3.8-27b"]

if GROQ_API_KEY:
    try:
        GROQ_CLIENT = Groq(api_key=GROQ_API_KEY)
        print("✅ Groq client ready (fallback)")
    except Exception as e:
        print(f"⚠️ Groq client banane me error: {e}")
else:
    print("⚠️ GROQ_API_KEY nahi mila.")

SCOPES = ['https://www.googleapis.com/auth/youtube.upload']
MAX_CHUNK_CHARACTERS = 1000
MAX_RETRIES = 2

# ==========================================
# Telegram Notifier
# ==========================================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

def notify_telegram(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": False
        }, timeout=20)
    except Exception as e:
        print(f"⚠️ Telegram notify failed: {e}")

def check_ffmpeg_codecs():
    try:
        cmd = ["ffmpeg", "-codecs"]
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if "libx264" in result.stdout:
            print("✅ libx264 codec available")
        if "aac" in result.stdout:
            print("✅ AAC codec available")
        return True
    except:
        print("⚠️ FFmpeg not found! Please install FFmpeg.")
        return False

def get_youtube_service():
    creds = None
    token_file = 'token.json'
    
    if os.path.exists(token_file):
        creds = Credentials.from_authorized_user_file(token_file, SCOPES)
        
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists('client_secrets.json'):
                print("❌ Error: 'client_secrets.json' file missing in folder. Skipping YouTube Upload.")
                notify_telegram("❌ 'client_secrets.json' missing hai, YouTube upload skip ho gaya.")
                return None
            flow = InstalledAppFlow.from_client_secrets_file('client_secrets.json', SCOPES)
            creds = flow.run_local_server(port=0)
            
        with open(token_file, 'w') as token:
            token.write(creds.to_json())
            
    return build('youtube', 'v3', credentials=creds)

# ==========================================
# 2. GitHub Pages Dynamic Article Scraper
# ==========================================
def extract_github_article_content(url):
    print(f"🔍 Scraping dynamic content from GitHub Pages: {url}")
    
    title_text = ""
    content_text = ""
    
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            
            page.goto(url, wait_until="networkidle", timeout=30000)
            page.wait_for_timeout(2000)
            
            title_selectors = ['h1', 'h2', '.article-title', '#article-title', '.post-title', '#title']
            for selector in title_selectors:
                if page.locator(selector).count() > 0:
                    title_text = page.locator(selector).first.inner_text().strip()
                    if title_text:
                        break
            
            if not title_text:
                title_text = page.title() or "Important News Update"
                
            body_selectors = ['#article-content', '.article-content', '.post-body', 'article', 'main', '.content']
            found_body = False
            
            for selector in body_selectors:
                if page.locator(selector).count() > 0:
                    content_text = page.locator(selector).first.inner_text().strip()
                    if len(content_text) > 100:
                        found_body = True
                        break
            
            if not found_body:
                paragraphs = page.locator('p, li, td, h2, h3').all_inner_texts()
                content_text = "\n".join([p.strip() for p in paragraphs if len(p.strip()) > 10])
                
            browser.close()
            
            if content_text and len(content_text) > 50:
                print("✅ GitHub Article extracted successfully!")
                print(f"📌 Extracted Title: {title_text}")
                print(f"📊 Extracted Content Length: {len(content_text)} characters")
                return title_text, content_text[:6000]
            else:
                print("⚠️ Warning: Extracted content is very short or empty.")
                return title_text, content_text
                
    except Exception as e:
        print(f"❌ Error scraping GitHub Article: {e}")
        return None, None

# ==========================================
# 3. NEWS VIDEO SCRIPT GENERATOR
# ==========================================
def generate_news_script(title, content, max_retries=3):
    print("🎙️ Generating High-CTR Clickbait Title & Detailed News Script...")
    
    prompt = f"""
    You are a top viral YouTube Growth Expert and News Anchor for channel 'SG News18'.
    Create a complete viral YouTube news package based on this full article content:
    ARTICLE TITLE: {title}
    FULL ARTICLE CONTENT:
    {content}

    ====== SCRIPT SPECIFICITY INSTRUCTIONS (VERY IMPORTANT) ======
    - READ the provided article content carefully.
    - INCLUDE ALL SPECIFIC DATA from the content: dates, vacancy numbers, eligibility criteria, salary, selection process, fee details, and official link instructions.
    - DO NOT USE GENERIC STATEMENTS like "पात्रता की जानकारी जल्द दी जाएगी" or "अधिक जानकारी के लिए वेबसाइट देखें" IF THE DATA IS PRESENT IN THE ARTICLE.
    - Write a long, comprehensive, in-depth news breakdown as if reading a full TV news report.

    ====== 1. LANGUAGE RULE ======
    - The ENTIRE video script MUST be in HINDI language ONLY (Devanagari Script).
    - Use natural, energetic, conversational Hindi spoken by Indian news anchors.
    - NO pure English sentences in the video script.

    ====== 2. CLICKBAIT TITLE RULES ======
    - Generate 3 High-CTR Clickbait titles in Hinglish/Hindi.
    - Use psychological triggers: Official Notice, Shocking Rule, Final Date, Big Relief.

    ====== 3. VIRAL SEO DESCRIPTION RULES ======
    - Write a detailed YouTube description (300-400 words) summarizing all key aspects of the news.

    ====== 4. TAGS & HASHTAGS ======
    - 15-20 trending tags (mix of Hindi & English keywords)
    - 5-8 relevant hashtags

    ====== 5. SCRIPT FORMAT (VERY IMPORTANT) ======
    - Video length target: 3 to 5 minutes (approximately 500 to 750 words).
    - Start DIRECTLY with a strong HOOK + engaging opening (curiosity, shock, urgency, or big news style).
    - DO NOT start with "नमस्ते दोस्तों! SG News18 में आपका स्वागत है" or any similar greeting.
    - First 2-3 sentences must be powerful hook that stops the scroll.
    - Then continue with full detailed news in continuous paragraph format with logical flow.
    - NO section headings or timestamps.
    - Keep natural energetic news-anchor style throughout.

    ====== OUTPUT FORMAT (Strictly valid JSON) ======
    {{
      "seo_title": [
        "🔥 Title 1 (Extreme Clickbait)",  
        "⚡ Title 2 (Curiosity Trigger)",  
        "📌 Title 3 (SEO Targeted Title)"
      ],
      "video_script": "Strong hook + detailed 500-750 word script in pure Hindi Devanagari based on actual article details...",
      "seo_description": "Comprehensive YouTube SEO Description with summary and highlights...",
      "tags": ["tag1", "tag2", "tag3", "tag4", "tag5"],
      "hashtags": "#hashtag1 #hashtag2 #hashtag3",
      "gk_community_questions": [
        {{
          "question": "संबंधित सवाल...",
          "options": ["Option A", "Option B", "Option C", "Option D"]
        }}
      ]
    }}
    """

    # ========== 1. PRIMARY: Gemini ==========
    if GEMINI_CLIENTS:
        for model in GEMINI_MODELS:
            for key_label, gclient in GEMINI_CLIENTS:
                for attempt in range(1, max_retries + 1):
                    try:
                        print(f"🔄 AI Generation Attempt {attempt}/{max_retries} (Gemini {key_label} - {model})...")
                        response = gclient.models.generate_content(
                            model=model,
                            contents=prompt,
                            config=types.GenerateContentConfig(
                                response_mime_type="application/json"
                            )
                        )
                        raw_text = response.text.strip()
                        clean_json = re.sub(r'^```json\s*|\s*```$', '', raw_text, flags=re.MULTILINE)
                        data = json.loads(clean_json)
                        script = data.get("video_script", "")
                        if script and len(script) > 400 and any('\u0900' <= c <= '\u097F' for c in script):
                            print(f"✅ Script generated successfully with {key_label} ({model})!")
                            return data
                        else:
                            print(f"⚠️ {key_label} ({model}) script short/invalid language. Retrying...")
                    except Exception as e:
                        print(f"⚠️ Gemini {key_label} ({model}) Attempt {attempt} failed ({e}).")
                        time.sleep(2 ** attempt)
                print(f"➡️ {key_label} fail ho gayi, agli key try kar rahe hain...")
                notify_telegram(f"⚠️ Gemini {key_label} fail ho gayi, agli key try ho rahi hai...")
    else:
        print("⚠️ Koi Gemini client initialize nahi hua (keys missing). Skipping Gemini...")
        notify_telegram("⚠️ Gemini keys missing, Groq try ho raha hai...")

    # ========== 2. SECONDARY: Groq ==========
    if GROQ_CLIENT:
        print("🔄 Gemini failed. Switching to Groq...")
        notify_telegram("🔄 Gemini fail, ab Groq try ho raha hai...")
        for model in GROQ_MODELS:
            for attempt in range(1, max_retries + 1):
                try:
                    print(f"🔄 AI Generation Attempt {attempt}/{max_retries} (Groq - {model})...")
                    response = GROQ_CLIENT.chat.completions.create(
                        model=model,
                        messages=[{"role": "user", "content": prompt}],
                        temperature=0.7,
                        max_tokens=4096,
                        response_format={"type": "json_object"}
                    )
                    raw_text = response.choices[0].message.content.strip()
                    clean_json = re.sub(r'^```json\s*|\s*```$', '', raw_text, flags=re.MULTILINE)
                    data = json.loads(clean_json)
                    script = data.get("video_script", "")
                    if script and len(script) > 400 and any('\u0900' <= c <= '\u097F' for c in script):
                        print(f"✅ Script generated successfully with Groq ({model})!")
                        notify_telegram(f"✅ Script Groq se ban gaya ({model})")
                        return data
                    else:
                        print(f"⚠️ Groq ({model}) script short/invalid. Retrying...")
                except Exception as e:
                    print(f"⚠️ Groq ({model}) Attempt {attempt} failed ({e}).")
                    time.sleep(2 ** attempt)
            print(f"➡️ Groq model {model} fail, agli model try...")
        notify_telegram("⚠️ Groq bhi fail, ab g4f try ho raha hai...")
    else:
        print("⚠️ Groq client nahi hai. Skipping Groq...")

    # ========== 3. LAST FALLBACK: g4f ==========
    print("🔄 Gemini + Groq failed. Switching to g4f...")
    for attempt in range(1, max_retries + 1):
        try:
            print(f"🔄 AI Generation Attempt {attempt}/{max_retries} (g4f)...")
            response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.75
            )
            raw_text = response.choices[0].message.content.strip()
            clean_json = re.sub(r'^```json\s*|\s*```$', '', raw_text, flags=re.MULTILINE)
            data = json.loads(clean_json)
            script = data.get("video_script", "")
            if script and len(script) > 400 and any('\u0900' <= c <= '\u097F' for c in script):
                print("✅ Script generated successfully with g4f!")
                return data
            else:
                print(f"⚠️ g4f script short/invalid. Retrying...")
        except Exception as e:
            print(f"⚠️ g4f Attempt {attempt} failed ({e}).")
            time.sleep(2)
            
    print("❌ AI Generation Error: Gemini + Groq + g4f teeno fail.")
    notify_telegram("❌ <b>Script generation fail</b> (Gemini + Groq + g4f teeno fail).")
    return None

# ==========================================
# 4. AI VOICE GENERATOR
# ==========================================
def split_script_into_chunks(script, max_chars=MAX_CHUNK_CHARACTERS):
    paragraphs = [p.strip() for p in script.split('\n\n') if p.strip()]
    chunks = []
    current_chunk = ""
    for p in paragraphs:
        if len(current_chunk) + len(p) + 2 <= max_chars:
            current_chunk += (p + "\n\n")
        else:
            if current_chunk:
                chunks.append(current_chunk.strip())
                current_chunk = ""
            
            if len(p) > max_chars:
                sentences = re.split(r'([।!?\n])', p)
                sub_chunk = ""
                for i in range(0, len(sentences), 2):
                    sentence = sentences[i]
                    punct = sentences[i+1] if i+1 < len(sentences) else ""
                    full_sentence = sentence + punct
                    if len(sub_chunk) + len(full_sentence) <= max_chars:
                        sub_chunk += full_sentence
                    else:
                        if sub_chunk:
                            chunks.append(sub_chunk.strip())
                        sub_chunk = full_sentence
                if sub_chunk:
                    chunks.append(sub_chunk.strip())
            else:
                current_chunk = p + "\n\n"
    if current_chunk:
        chunks.append(current_chunk.strip())
    return chunks

def generate_fable_voice_openai_fm(text_chunk, output_path):
    try:
        url = "https://www.openai.fm/api/generate"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
            "Origin": "https://www.openai.fm",
            "Referer": "https://www.openai.fm/",
            "Accept": "*/*",
        }
        files = {
            "input": (None, text_chunk),
            "voice": (None, "fable"),
            "prompt": (None, "Speak clearly in a natural, friendly Indian Hindi male tone. Moderate pace, clear pronunciation."),
            "vibe": (None, "audio")
        }
        response = requests.post(url, files=files, headers=headers, timeout=90, stream=True)
        if response.status_code != 200:
            params = {
                "input": text_chunk,
                "voice": "fable",
                "prompt": "Speak clearly in a natural, friendly Indian Hindi male tone. Moderate pace, clear pronunciation."
            }
            response = requests.get(url, params=params, headers=headers, timeout=90, stream=True)
        response.raise_for_status()
        content_type = response.headers.get("content-type", "").lower()
        if not any(x in content_type for x in ("audio", "mpeg", "wav", "octet-stream")):
            raise Exception(f"Unexpected content-type: {content_type}")
        with open(output_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        file_size = os.path.getsize(output_path)
        if file_size < 8000:
            raise Exception(f"Downloaded audio too small ({file_size} bytes)")
        print("✅ OpenAI.fm audio saved successfully")
        return True
    except Exception as e:
        print(f"⚠️ OpenAI.fm Direct API Error: {e}")
        return False

def generate_male_voice(text, output_audio_path):
    print("🎙️ Generating Natural Hindi Voiceover (Fable Voice via OpenAI.fm)...")
    temp_dir = "temp_voice"
    os.makedirs(temp_dir, exist_ok=True)
  
    chunks = split_script_into_chunks(text, max_chars=MAX_CHUNK_CHARACTERS)
    print(f"🧩 Script split into {len(chunks)} part(s).")
  
    audio_parts = []
    fable_failed = False
    
    for idx, chunk in enumerate(chunks, start=1):
        part_filename = os.path.join(temp_dir, f"part_{idx:03d}.mp3")
        success = False
      
        if not fable_failed:
            for attempt in range(1, MAX_RETRIES + 1):
                print(f"🎙️ Generating Part {idx}/{len(chunks)} via OpenAI.fm - Attempt {attempt}...")
                if generate_fable_voice_openai_fm(chunk, part_filename):
                    if os.path.exists(part_filename) and os.path.getsize(part_filename) >= 8000:
                        success = True
                        break
                time.sleep(2)
          
            if not success:
                print("⚠️ OpenAI.fm failed. Falling back to Edge TTS (hi-IN-MadhurNeural).")
                fable_failed = True
        
        if fable_failed or not success:
            try:
                print(f"🔊 Generating Part {idx}/{len(chunks)} via Edge TTS Fallback...")
                async def _save():
                    communicate = edge_tts.Communicate(chunk, "hi-IN-MadhurNeural", rate="+0%")
                    await communicate.save(part_filename)
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(_save())
                loop.close()
                if os.path.exists(part_filename) and os.path.getsize(part_filename) >= 4000:
                    success = True
                else:
                    raise Exception("Edge TTS ne khali/chhoti audio di")
            except Exception as e:
                print(f"⚠️ Edge TTS Part {idx} fail ({e}). Ab gTTS try kar rahe hain...")
                try:
                    tts = gTTS(text=chunk, lang="hi")
                    tts.save(part_filename)
                    if os.path.exists(part_filename) and os.path.getsize(part_filename) >= 4000:
                        success = True
                        print(f"✅ Part {idx} gTTS se ban gaya.")
                    else:
                        raise Exception("gTTS ne bhi khali/chhoti audio di")
                except Exception as e2:
                    print(f"❌ Part {idx} completely failed: {e2}")
                    notify_telegram(f"❌ Awaaz fail — OpenAI.fm + Edge TTS + gTTS teeno fail (Part {idx}).")
                    return False
        
        audio_parts.append(part_filename)
    
    if audio_parts:
        print("🔗 Concatenating audio with FFmpeg...")
        list_file = os.path.join(temp_dir, "concat_list.txt")
      
        with open(list_file, "w", encoding="utf-8") as f:
            for p in audio_parts:
                clean_p = os.path.abspath(p).replace('\\', '/')
                f.write(f"file '{clean_p}'\n")
        
        try:
            cmd_concat = [
                'ffmpeg', '-f', 'concat', '-safe', '0', '-i', list_file,
                '-af', 'loudnorm=I=-16:LRA=11:TP=-1.5',
                '-ar', '44100', '-ac', '2', '-b:a', '128k',
                '-c:a', 'libmp3lame', '-write_xing', '0',
                '-y', output_audio_path
            ]
            subprocess.run(cmd_concat, capture_output=True, check=True)
            print(f"🔊 Final Audio merged: {output_audio_path}")
            return True
        except Exception as e:
            print(f"❌ Audio Joining Error: {e}")
            return False
        finally:
            for f in audio_parts + [list_file]:
                if os.path.exists(f):
                    try:
                        os.remove(f)
                    except:
                        pass
    return False

# ==========================================
# 5. Playwright Screen Recorder
# ==========================================
def record_website_video(url, output_clip_path, target_duration):
    print(f"📹 Auto-Recording GitHub Article Page for {target_duration:.1f}s...")
    temp_dir = "temp_rec"
    os.makedirs(temp_dir, exist_ok=True)
    
    keywords_to_highlight = [
        "Result", "Apprentice", "Apply", "Notification",  
        "Download", "Eligibility", "Vacancy", "Important Dates",
        "Age Limit", "Salary", "Selection Process"
    ]
    
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                viewport={'width': 1920, 'height': 1080},
                record_video_dir=temp_dir,
                record_video_size={'width': 1920, 'height': 1080}
            )
            
            page = context.new_page()
            print("⏳ Loading GitHub Article Page...")
            
            try:
                page.goto(url, wait_until="networkidle", timeout=30000)
            except:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
            
            page.wait_for_timeout(3000)
            page.evaluate("document.body.style.zoom = '1.4'")
            page.wait_for_timeout(1000)
            
            js_code = """
            (keywords) => {
                keywords.forEach(kw => {
                    const regex = new RegExp(`(${kw})`, 'gi');
                    const elements = document.querySelectorAll('p, li, span, td, h1, h2, h3, div, a, strong');
                    elements.forEach(el => {
                        if (el.children.length === 0 && el.innerText && regex.test(el.innerText)) {
                            el.innerHTML = el.innerText.replace(
                                regex,  
                                '<mark style="background-color: #fef08a; color: #000; text-decoration: underline 3px red; padding: 2px 4px; border-radius: 3px; font-weight: bold;">$1</mark>'
                            );
                        }
                    });
                });
            }
            """
            page.evaluate(js_code, keywords_to_highlight)
            
            print("🔴 Recording started...")
            start_time = time.time()
            max_duration = min(target_duration, 300)
            
            page_height = page.evaluate("document.body.scrollHeight")
            viewport_height = page.evaluate("window.innerHeight")
            total_scroll = max(page_height - viewport_height, 0)
            
            scroll_step = 2
            current_scroll = 0
            direction = 1
            wait_time = 3
            
            while time.time() - start_time < max_duration:
                if time.time() - start_time > wait_time and total_scroll > 0:
                    current_scroll += scroll_step * direction
                    if current_scroll > total_scroll:
                        current_scroll = total_scroll
                        direction = -1
                    elif current_scroll < 0:
                        current_scroll = 0
                        direction = 1
                    page.evaluate(f"window.scrollTo({{ top: {current_scroll}, behavior: 'auto' }})")
                time.sleep(0.03)
            print("⏹️ Recording finished")
            rec_path = page.video.path()
            context.close()
            browser.close()
            
            if os.path.exists(rec_path):
                if os.path.exists(output_clip_path):
                    os.remove(output_clip_path)
                os.rename(rec_path, output_clip_path)
                print(f"✅ Website Recording Saved: {output_clip_path}")
                return True
                
    except Exception as e:
        print(f"⚠️ Screen Recording Error: {e}")
        return False
    
    return False

# ==========================================
# 6. Helper Functions & FFmpeg Processing
# ==========================================
def get_audio_duration(audio_path):
    cmd = [
        "ffprobe", "-v", "error", "-show_entries",
        "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", audio_path
    ]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return float(result.stdout.strip())

def create_fallback_image(title, output_image_path):
    img = Image.new('RGB', (1920, 1080), color=(15, 23, 42))
    draw = ImageDraw.Draw(img)
    draw.rectangle([(60, 50), (1860, 1030)], outline=(234, 179, 8), width=5)
    
    try:
        font = ImageFont.truetype("arial.ttf", 40)
    except:
        font = ImageFont.load_default()
        
    draw.text((140, 150), "📢 SG NEWS18 - NEWS UPDATE", fill=(234, 179, 8), font=font)
    clean_title = title[:55] + "..." if len(title) > 55 else title
    draw.text((140, 250), clean_title, fill=(255, 255, 255), font=font)
    draw.text((140, 350), "🔗 Full Story Below", fill=(100, 200, 255), font=font)
    draw.text((140, 550), "✅ Subscribe for More Updates", fill=(255, 200, 100), font=font)
    img.save(output_image_path)

def build_video_ffmpeg(audio_path, video_clip_path, output_video_path, title):
    print("🎬 Processing Video with FFmpeg...")
    audio_dur = get_audio_duration(audio_path)
    
    if not (video_clip_path and os.path.exists(video_clip_path)):
        temp_img = "temp_fallback.png"
        create_fallback_image(title, temp_img)
        cmd = [
            "ffmpeg", "-y",
            "-loop", "1", "-i", temp_img,
            "-i", audio_path,
            "-c:v", "libx264", "-tune", "stillimage",
            "-c:a", "aac", "-b:a", "128k",
            "-pix_fmt", "yuv420p",
            "-shortest", output_video_path
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if os.path.exists(temp_img):
            os.remove(temp_img)
        return True
    raw_size_mb = os.path.getsize(video_clip_path) / (1024 * 1024)
    print(f"📊 Raw Recorded Video Size: {raw_size_mb:.2f} MB")
    if raw_size_mb <= 95:
        print("⚡ Size ≤ 95 MB: Attempting Direct Copy...")
        cmd1 = [
            "ffmpeg", "-y",
            "-stream_loop", "-1", "-i", video_clip_path,
            "-i", audio_path,
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", "128k",
            "-map", "0:v:0", "-map", "1:a:0",
            "-t", str(audio_dur),
            "-avoid_negative_ts", "make_zero",
            output_video_path
        ]
        res1 = subprocess.run(cmd1, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if res1.returncode == 0 and os.path.exists(output_video_path):
            return True
    print("📦 Encoding Video to 1080p...")
    cmd = [
        "ffmpeg", "-y",
        "-stream_loop", "-1", "-i", video_clip_path,
        "-i", audio_path,
        "-vf", "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2",
        "-c:v", "libx264", "-crf", "23", "-preset", "fast",
        "-c:a", "aac", "-b:a", "128k",
        "-t", str(audio_dur),
        "-pix_fmt", "yuv420p",
        output_video_path
    ]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return os.path.exists(output_video_path)

# ==========================================
# 7. YouTube Auto Uploader
# ==========================================
def upload_to_youtube(video_path, title, description, tags):
    print("📤 Uploading Video to YouTube as UNLISTED...")
    youtube = get_youtube_service()
    if not youtube:
        return
    tags_list = tags.split(',') if isinstance(tags, str) else tags
    body = {
        'snippet': {
            'title': title[:100],
            'description': description[:5000],
            'tags': [t.strip() for t in tags_list[:20]],
            'categoryId': '27'
        },
        'status': {
            'privacyStatus': 'unlisted',
            'selfDeclaredMadeForKids': False
        }
    }
    try:
        media = MediaFileUpload(video_path, chunksize=-1, resumable=True)
        request = youtube.videos().insert(
            part=','.join(body.keys()),
            body=body,
            media_body=media
        )
        
        response = None
        while response is None:
            status, response = request.next_chunk()
            if status:
                print(f"⏳ Upload Progress: {int(status.progress() * 100)}%")
        video_id = response.get('id')
        video_url = f"https://youtu.be/{video_id}"
        print(f"✅ Video Uploaded Successfully! Video URL: {video_url}")
        notify_telegram(f"✅ <b>Video ban kar YouTube pe upload ho gaya!</b>\n🎬 {title}\n🔗 {video_url}")
    except Exception as e:
        print(f"❌ YouTube Upload Failed: {e}")
        notify_telegram(f"❌ <b>YouTube upload fail</b> ho gaya.\n<code>{e}</code>")

# ==========================================
# 8. Main Automation Flow
# ==========================================
def main():
    print("=" * 50)
    print("🎬 SG News18 - GitHub Article News Video Automation")
    print("=" * 50)
    check_ffmpeg_codecs()
    
    import sys
    post_url = os.getenv("ARTICLE_URL", "").strip()
    if not post_url and len(sys.argv) > 1:
        post_url = sys.argv[1].strip()
    if not post_url:
        post_url = input("\n🔗 Enter GitHub Pages Article URL: ").strip()
    
    notify_telegram(f"🚀 <b>News automation shuru hua</b>\n🔗 {post_url}")
    
    if post_url:
        blog_title, blog_content = extract_github_article_content(post_url)
        if not blog_content or len(blog_content) <= 50:
            notify_telegram(f"❌ Content extract nahi hua ya bahut chhota tha.\n🔗 {post_url}")
        if blog_content and len(blog_content) > 50:
            ai_data = generate_news_script(blog_title, blog_content)
            
            if ai_data:
                output_dir = "bot_outputs"
                os.makedirs(output_dir, exist_ok=True)
                
                filename_base = re.sub(r'[^\w\s-]', '', blog_title)[:30].strip().replace(" ", "_")
                txt_file = os.path.join(output_dir, f"{filename_base}_news_package.txt")
                audio_file = os.path.join(output_dir, f"{filename_base}_audio.mp3")
                web_clip_path = "temp_website_clip.mp4"
                video_file = os.path.join(output_dir, f"{filename_base}_video.mp4")
                
                titles = ai_data.get("seo_title", [])
                selected_title = titles[0] if isinstance(titles, list) and titles else blog_title
                seo_desc = f"{ai_data.get('seo_description', '')}\n\n{ai_data.get('hashtags', '')}"
                tags = ai_data.get('tags', '')
                
                with open(txt_file, "w", encoding="utf-8") as f:
                    f.write("=== NEWS VIDEO CONTENT PACKAGE ===\n\n")
                    f.write(f"📌 SELECTED TITLE: {selected_title}\n\n")
                    f.write("--- 📝 VIDEO SCRIPT ---\n\n")
                    f.write(f"{ai_data.get('video_script')}\n\n")
                    f.write("--- 🔍 SEO DESCRIPTION ---\n")
                    f.write(f"{seo_desc}\n\n")
                
                print(f"🎉 Text Package Saved: {txt_file}")
                
                script_text = ai_data.get("video_script", "")
                audio_ok = False
                if script_text:
                    audio_ok = generate_male_voice(script_text, audio_file)
                    if audio_ok and os.path.exists(audio_file):
                        audio_duration = get_audio_duration(audio_file)
                        
                        record_website_video(post_url, web_clip_path, audio_duration)
                        
                        video_created = build_video_ffmpeg(audio_file, web_clip_path, video_file, selected_title)
                        
                        if os.path.exists(web_clip_path):
                            try:
                                os.remove(web_clip_path)
                            except:
                                pass
                        
                        if video_created and os.path.exists(video_file):
                            upload_to_youtube(video_file, selected_title, seo_desc, tags)
                        else:
                            notify_telegram("❌ Video build fail ho gaya, YouTube upload skip.")
                if audio_ok:
                    print("\n✅ Entire Automation Finished Successfully!")
                else:
                    print("\n⚠️ Automation ruk gaya: audio nahi ban paayi.")
            else:
                print("❌ Failed to generate AI script package.")
                notify_telegram("❌ AI script generation fail ho gaya.")
        else:
            print("❌ Content extraction failed or text too short.")
            notify_telegram(f"❌ Article scrape fail/short content.\n🔗 {post_url}")
    else:
        print("⚠️ No URL provided.")
        notify_telegram("⚠️ Koi article URL provide nahi hui.")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        err_text = traceback.format_exc()[-2500:]
        print(f"❌ FATAL ERROR: {e}\n{err_text}")
        notify_telegram(f"❌ <b>Automation CRASH ho gaya</b>\n<code>{e}</code>")
        raise
