"""
video_scenes ke tests.   Chalane ke liye (repo root se):

    python -m unittest discover -s tests -v

Logic tests offline chalte hain. Render/integration tests ke liye ffmpeg + Playwright Chromium +
Devanagari font chahiye; na ho to wo tests skip ho jaate hain.
"""
import http.server
import json
import os
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image  # noqa: E402

import video_scenes as vs  # noqa: E402

# ----------------------------------------------------------------------------
# Sample scripts (Hindi) — har category ke liye
# ----------------------------------------------------------------------------
SPORTS = """भारत और ऑस्ट्रेलिया के बीच खेले गए रोमांचक क्रिकेट मैच में टीम इंडिया ने शानदार जीत दर्ज की। कप्तान ने टॉस जीतकर पहले गेंदबाजी का फैसला किया। गेंदबाजों ने शुरुआत से ही विकेट चटकाने शुरू कर दिए। ऑस्ट्रेलिया की पूरी टीम सिर्फ 210 रन पर सिमट गई।

जवाब में भारतीय बल्लेबाजों ने तेज शुरुआत की। सलामी बल्लेबाज ने 78 रन की पारी खेली और स्टेडियम में मौजूद दर्शकों ने जमकर तालियां बजाईं। मध्यक्रम ने भी जिम्मेदारी से बल्लेबाजी की। टीम ने चार विकेट खोकर यह लक्ष्य आसानी से हासिल कर लिया।

इस जीत के साथ भारत ने सीरीज में 2-1 की बढ़त बना ली है। अगला मैच रविवार को खेला जाएगा। कोच ने खिलाड़ियों की जमकर तारीफ की। क्रिकेट प्रेमियों के लिए यह मुकाबला यादगार रहा। फाइनल मैच से पहले टीम को अब आराम मिलेगा।"""

POLITICS = """चुनाव आयोग ने विधानसभा चुनाव की तारीखों का ऐलान कर दिया है। राज्य में दो चरणों में मतदान कराया जाएगा। मुख्यमंत्री ने कहा कि सरकार ने पिछले पांच साल में बड़े काम किए हैं। विपक्ष ने इन दावों को सिरे से खारिज कर दिया।

कांग्रेस और भाजपा दोनों दलों ने उम्मीदवारों की सूची पर मंथन शुरू कर दिया है। पार्टी के नेता रैली की तैयारियों में जुट गए हैं। चुनाव आयोग ने सभी दलों को आचार संहिता का पालन करने के निर्देश दिए हैं।

मतगणना की तारीख अलग से घोषित की जाएगी। राजनीतिक गलियारों में गठबंधन को लेकर चर्चा तेज है। विधायक और सांसद अपने क्षेत्रों में सक्रिय हो गए हैं। आने वाले दिनों में चुनावी माहौल और गरमाने की उम्मीद है।"""

TECH = """नया स्मार्टफोन आज भारत में लॉन्च हो गया है और इसमें एआई वाले कई फीचर दिए गए हैं। फोन में 200 मेगापिक्सल का कैमरा और 5000 एमएएच की बैटरी मिलती है। कंपनी का दावा है कि यह प्रोसेसर पिछले मॉडल से तीस प्रतिशत तेज है।

इस डिवाइस की कीमत 34999 रुपये से शुरू होती है। एंड्रॉयड का नया वर्जन पहले दिन से मिलेगा। एआई असिस्टेंट अब हिंदी में भी काम करेगा। फोन की बिक्री अगले हफ्ते से ऑनलाइन शुरू होगी।

टेक विशेषज्ञों का कहना है कि इस सेगमेंट में मुकाबला कड़ा होने वाला है। गूगल और सैमसंग के फोन से इसकी सीधी टक्कर होगी। लॉन्च ऑफर में बैंक कार्ड पर छूट भी दी जा रही है।"""

TRENDING = """सोशल मीडिया पर एक वायरल वीडियो ने सबका ध्यान खींच लिया है। यह वीडियो कुछ ही घंटों में लाखों लोगों तक पहुंच गया। लोग इसे ट्रेंड करा रहे हैं और अपनी राय दे रहे हैं।

वीडियो में एक छोटी बच्ची गाना गाती दिख रही है। उसकी आवाज सुनकर लोग हैरान रह गए। कई लोगों ने कमेंट करके उसकी तारीफ की है। रील बनाने वालों ने भी इस गाने पर कई वीडियो बना डाले हैं।

अभी तक इस वीडियो को दस लाख से ज्यादा बार देखा जा चुका है। परिवार ने सभी का आभार जताया है। आगे भी ऐसे वीडियो आने की उम्मीद है।"""

JOBS = """रेलवे में भर्ती का नोटिफिकेशन जारी हो गया है। कुल 5000 पदों पर आवेदन मांगे गए हैं। आवेदन की अंतिम तारीख 30 नवंबर है। उम्मीदवार का दसवीं पास होना जरूरी है। आयु सीमा अठारह से तैंतीस साल रखी गई है। चयन परीक्षा और दस्तावेज सत्यापन के आधार पर होगा। आवेदन शुल्क सामान्य वर्ग के लिए सौ रुपये है।"""


def _ffmpeg_ok():
    return shutil.which("ffmpeg") and shutil.which("ffprobe")


def _chromium_ok():
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            return os.path.exists(p.chromium.executable_path)
    except Exception:
        return False


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


class LocalServer:
    """Folder ko 127.0.0.1 par serve karta hai (images + test article page)."""

    def __init__(self, root):
        handler = lambda *a, **k: _QuietHandler(*a, directory=root, **k)  # noqa: E731
        socketserver.TCPServer.allow_reuse_address = True
        self.httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        os.environ["NO_PROXY"] = "127.0.0.1,localhost"
        os.environ["no_proxy"] = "127.0.0.1,localhost"

    def url(self, name):
        return f"http://127.0.0.1:{self.port}/{name}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def _make_image(path, color, size=(1200, 700), fmt="JPEG"):
    Image.new("RGB", size, color).save(path, fmt)


# ----------------------------------------------------------------------------
# 1. Category classification
# ----------------------------------------------------------------------------
class TestCategory(unittest.TestCase):
    def test_four_categories(self):
        self.assertEqual(vs.classify_category("भारत ने ऑस्ट्रेलिया को हराया", SPORTS), "sports")
        self.assertEqual(vs.classify_category("विधानसभा चुनाव की तारीखें घोषित", POLITICS), "politics")
        self.assertEqual(vs.classify_category("नया स्मार्टफोन लॉन्च", TECH), "technology")
        self.assertEqual(vs.classify_category("वायरल वीडियो ने मचाई धूम", TRENDING), "trending")

    def test_jobs_never_default(self):
        cat = vs.classify_category("रेलवे भर्ती 2026", JOBS)
        self.assertIn(cat, vs.CATEGORIES)
        self.assertEqual(cat, "trending")          # weak signal -> Trending, not jobs

    def test_empty_input(self):
        self.assertEqual(vs.classify_category("", ""), "trending")

    def test_english_keywords_need_exact_match(self):
        # "said" ke andar "ai" hai, par ye technology nahi banna chahiye
        self.assertEqual(vs.classify_category("He said she said", "said said said said"), "trending")


# ----------------------------------------------------------------------------
# 2. Scene planning / timing
# ----------------------------------------------------------------------------
class TestScenePlanning(unittest.TestCase):
    def _check_contiguous(self, scenes, dur):
        self.assertTrue(scenes)
        self.assertAlmostEqual(scenes[0]["start"], 0.0, places=3)
        self.assertAlmostEqual(scenes[-1]["end"], dur, places=2)
        for a, b in zip(scenes, scenes[1:]):
            self.assertAlmostEqual(a["end"], b["start"], places=2)
            self.assertGreater(a["end"], a["start"])

    def test_all_categories_plan(self):
        for script, dur in ((SPORTS, 75.0), (POLITICS, 70.0), (TECH, 66.0), (TRENDING, 58.0)):
            scenes = vs.plan_scenes(script, dur, title="टेस्ट शीर्षक")
            self._check_contiguous(scenes, dur)
            self.assertGreaterEqual(len(scenes), 3)
            for s in scenes[:-1]:
                self.assertGreaterEqual(s["end"] - s["start"], vs.SCENE_MIN * 0.8)
                self.assertLessEqual(s["end"] - s["start"], vs.SCENE_MAX + 12)  # ek lambi sentence ki chhoot

    def test_first_scene_uses_article_title(self):
        scenes = vs.plan_scenes(SPORTS, 75.0, title="भारत ने ऑस्ट्रेलिया को हराया")
        self.assertEqual(scenes[0]["headline"], "भारत ने ऑस्ट्रेलिया को हराया")

    def test_headline_is_verbatim_prefix_of_script(self):
        scenes = vs.plan_scenes(TECH, 66.0, title="")
        for s in scenes:
            core = s["headline"].rstrip("…")
            self.assertIn(core, " ".join(TECH.split()), "headline script se hi aani chahiye (koi invented text nahi)")
            self.assertLessEqual(len(s["headline"]), 90)

    def test_headline_truncates_on_word_boundary(self):
        long = "यह एक बहुत लंबा वाक्य है " * 10
        h = vs.make_headline(long, 40)
        self.assertTrue(h.endswith("…"))
        self.assertLessEqual(len(h), 41)
        self.assertNotIn("  ", h)

    def test_tiny_audio_gives_single_scene(self):
        scenes = vs.plan_scenes("यह एक छोटी खबर है। इसमें बस दो वाक्य हैं।", 5.0)
        self.assertEqual(len(scenes), 1)
        self._check_contiguous(scenes, 5.0)

    def test_empty_script(self):
        self.assertEqual(vs.plan_scenes("", 30.0), [])
        self.assertEqual(vs.plan_scenes("कुछ", 0), [])

    def test_script_without_danda(self):
        script = " ".join(f"Sentence number {i} about the cricket match today." for i in range(30))
        scenes = vs.plan_scenes(script, 90.0)
        self.assertGreater(len(scenes), 2)
        self._check_contiguous(scenes, 90.0)

    def test_chunk_anchored_timing(self):
        """Voice parts ki asli length se scene timing: part boundary par sahi time."""
        paras = [p.strip() for p in SPORTS.split("\n\n")]
        durs = [30.0, 10.0, 30.0]          # jaan-boojh kar chars ke anupaat se alag
        info = list(zip(paras, durs))
        sents = vs.split_sentences(SPORTS)
        times = vs._sentence_times(sents, SPORTS, 70.0, info)
        first_of_part2 = next(i for i, s in enumerate(sents) if s["text"].startswith("जवाब में"))
        first_of_part3 = next(i for i, s in enumerate(sents) if s["text"].startswith("इस जीत"))
        self.assertAlmostEqual(times[first_of_part2][0], 30.0, places=2)
        self.assertAlmostEqual(times[first_of_part3][0], 40.0, places=2)
        self.assertAlmostEqual(times[-1][1], 70.0, places=2)
        # bina anchor ke estimate alag hota hai (anchor sach me kaam kar raha hai)
        plain = vs._sentence_times(sents, SPORTS, 70.0, None)
        self.assertGreater(abs(plain[first_of_part2][0] - 30.0), 1.0)

    def test_bad_chunk_info_falls_back(self):
        scenes = vs.plan_scenes(SPORTS, 70.0, chunk_info=[("ye text script me nahi hai", 70.0)])
        self._check_contiguous(scenes, 70.0)

    def test_audio_duration_scaling(self):
        """Parts ka total audio se thoda alag ho (loudnorm/mp3 padding) to bhi end = audio_dur."""
        paras = [p.strip() for p in SPORTS.split("\n\n")]
        scenes = vs.plan_scenes(SPORTS, 71.3, chunk_info=list(zip(paras, [25.0, 21.0, 24.0])))
        self._check_contiguous(scenes, 71.3)

    def test_chips_are_verbatim_words(self):
        scenes = vs.plan_scenes(SPORTS, 75.0, title="भारत ऑस्ट्रेलिया क्रिकेट मैच", category="sports")
        words = set(vs.TOKEN_RE.findall(SPORTS))
        lower_words = {w.lower() for w in words}
        for s in scenes:
            self.assertLessEqual(len(s["chips"]), 3)
            for c in s["chips"]:
                self.assertIn(c.lower(), lower_words)


# ----------------------------------------------------------------------------
# 3. Image candidate filtering
# ----------------------------------------------------------------------------
class TestImageCandidates(unittest.TestCase):
    def test_filters(self):
        data = {
            "title": "खबर",
            "metas": [
                "https://cdn.example.com/photos/ipl-final-2026.jpg",
                "https://images.unsplash.com/photo-1585829365295-ab7cd400c167?w=800",  # generic stock
            ],
            "imgs": [
                {"url": "https://cdn.example.com/photos/ipl-final-2026.jpg?w=1200", "alt": "", "w": 1200, "h": 700, "inArticle": True},  # dup
                {"url": "https://cdn.example.com/img/site-logo.png", "alt": "", "w": 800, "h": 400, "inArticle": True},
                {"url": "https://cdn.example.com/img/pic.svg", "alt": "", "w": 800, "h": 400, "inArticle": True},
                {"url": "https://cdn.example.com/img/tiny.jpg", "alt": "", "w": 80, "h": 80, "inArticle": True},
                {"url": "https://cdn.example.com/img/strip.jpg", "alt": "", "w": 3000, "h": 200, "inArticle": True},
                {"url": "https://cdn.example.com/img/other-page.jpg", "alt": "", "w": 900, "h": 600, "inArticle": False},
                {"url": "data:image/png;base64,AAAA", "alt": "", "w": 900, "h": 600, "inArticle": True},
                {"url": "ftp://cdn.example.com/a.jpg", "alt": "", "w": 900, "h": 600, "inArticle": True},
                {"url": "https://cdn.example.com/img/kohli-century.jpg", "alt": "Virat Kohli century", "caption": "शतक", "w": 1000, "h": 600, "inArticle": True},
                {"url": "https://cdn.example.com/img/ad-banner.jpg", "alt": "advert logo", "w": 900, "h": 600, "inArticle": True},
            ],
        }
        out = vs.parse_extracted_images(data)
        urls = [c["url"] for c in out]
        self.assertEqual(urls, ["https://cdn.example.com/photos/ipl-final-2026.jpg",
                                "https://cdn.example.com/img/kohli-century.jpg"])
        self.assertEqual(out[0]["kind"], "meta")                      # lead image pehle

    def test_cap(self):
        imgs = [{"url": f"https://cdn.example.com/p/a{i}.jpg", "w": 900, "h": 600, "inArticle": True} for i in range(30)]
        self.assertLessEqual(len(vs.parse_extracted_images({"imgs": imgs})), vs.MAX_IMAGES)


# ----------------------------------------------------------------------------
# 4. Image <-> scene matching
# ----------------------------------------------------------------------------
class TestAssignImages(unittest.TestCase):
    def _scenes(self):
        return vs.plan_scenes(SPORTS, 75.0, title="भारत ने ऑस्ट्रेलिया को हराया", category="sports")

    def test_metadata_match_beats_order(self):
        scenes = self._scenes()
        # scenes[2..] me 'सीरीज/कोच' wali baatein; ek image sirf 'coach' se match kare
        images = [{"path": "/x/cricket.jpg", "url": "https://c.example.com/ipl-cricket-match-batsman.jpg",
                   "alt": "", "caption": "", "title": "", "kind": "img"}]
        vs.assign_images(scenes, images)
        assigned = [s for s in scenes if s["image_path"]]
        self.assertTrue(assigned)
        self.assertTrue(all(s["image_mode"] in ("matched", "article-lead") for s in assigned))

    def test_unrelated_image_not_claimed_as_matched(self):
        scenes = self._scenes()
        images = [{"path": "/x/farm.jpg", "url": "https://c.example.com/farm-tractor-wheat.jpg",
                   "alt": "", "caption": "", "title": "", "kind": "img"}]
        vs.assign_images(scenes, images)
        self.assertNotIn("matched", [s["image_mode"] for s in scenes])
        # lead image opening scene par aati hai (article ki apni image)
        self.assertEqual(scenes[0]["image_mode"], "article-lead")

    def test_cross_script_concept_match(self):
        """Hindi scene (क्रिकेट/बल्लेबाज) <-> English filename (cricket-batsman)."""
        scenes = vs.plan_scenes(SPORTS, 75.0, category="sports")
        target = max(range(len(scenes)), key=lambda i: len(scenes[i]["concepts"] & {"cricket"}))
        self.assertIn("cricket", scenes[target]["concepts"])
        tokens, concepts = vs._image_context({"url": "https://x.com/media/cricket-batsman.jpg"})
        self.assertIn("cricket", concepts)
        self.assertGreaterEqual(vs._match_score(scenes[target], tokens, concepts), 3)

    def test_no_images_all_cards(self):
        scenes = self._scenes()
        vs.assign_images(scenes, [])
        self.assertTrue(all(s["image_path"] is None and s["image_mode"] == "card" for s in scenes))

    def test_use_limits_and_no_adjacent_repeat(self):
        scenes = self._scenes()
        images = [{"path": "/x/a.jpg", "url": "https://c.example.com/cricket-match.jpg",
                   "alt": "", "caption": "", "title": "", "kind": "img"}]
        vs.assign_images(scenes, images)
        self.assertLessEqual(sum(1 for s in scenes if s["image_path"] == "/x/a.jpg"), 2)
        for a, b in zip(scenes, scenes[1:]):
            if a["image_path"]:
                self.assertNotEqual(a["image_path"], b["image_path"])

    def test_spread_unmatched_images_evenly(self):
        scenes = vs.plan_scenes(SPORTS, 150.0, category="sports")          # kai scenes
        self.assertGreaterEqual(len(scenes), 6)
        images = [{"path": f"/x/{i}.jpg", "url": f"https://c.example.com/zzz{i}.jpg",
                   "alt": "", "caption": "", "title": "", "kind": "img"} for i in range(3)]
        vs.assign_images(scenes, images)
        used = [s["image_path"] for s in scenes if s["image_path"]]
        self.assertEqual(sorted(used), sorted(i["path"] for i in images))   # har image ek baar
        self.assertEqual(scenes[0]["image_path"], "/x/0.jpg")               # lead -> opening
        self.assertGreater(sum(1 for s in scenes if s["image_mode"] == "card"), 0)  # baaki topic cards


# ----------------------------------------------------------------------------
# 5. Image download / validation
# ----------------------------------------------------------------------------
class TestFetchImage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        _make_image(os.path.join(cls.tmp, "good.jpg"), (200, 30, 30))
        _make_image(os.path.join(cls.tmp, "good.png"), (30, 200, 30), fmt="PNG")
        _make_image(os.path.join(cls.tmp, "good.webp"), (30, 30, 200), fmt="WEBP")
        _make_image(os.path.join(cls.tmp, "tiny.jpg"), (1, 2, 3), size=(50, 50))
        with open(os.path.join(cls.tmp, "fake.jpg"), "wb") as f:
            f.write(b"this is not an image at all" * 100)
        with open(os.path.join(cls.tmp, "page.html"), "w") as f:
            f.write("<html></html>")
        cls.server = LocalServer(cls.tmp)
        cls.out = tempfile.mkdtemp()

    @classmethod
    def tearDownClass(cls):
        cls.server.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)
        shutil.rmtree(cls.out, ignore_errors=True)

    def _fetch(self, name):
        return vs.fetch_image(self.server.url(name), os.path.join(self.out, "o.jpg"), timeout=5)

    def test_valid_formats_normalised_to_jpeg(self):
        for name in ("good.jpg", "good.png", "good.webp"):
            path = self._fetch(name)
            self.assertIsNotNone(path, name)
            with Image.open(path) as im:
                self.assertEqual(im.format, "JPEG")
                self.assertEqual(im.mode, "RGB")

    def test_failures_return_none(self):
        self.assertIsNone(self._fetch("missing.jpg"))      # 404
        self.assertIsNone(self._fetch("fake.jpg"))         # image nahi hai
        self.assertIsNone(self._fetch("tiny.jpg"))         # bahut chhoti
        self.assertIsNone(self._fetch("page.html"))        # content-type html
        self.assertIsNone(vs.fetch_image("http://127.0.0.1:9/none.jpg", os.path.join(self.out, "x.jpg"), timeout=2))
        self.assertIsNone(vs.fetch_image("file:///etc/passwd", os.path.join(self.out, "x.jpg")))
        self.assertIsNone(vs.fetch_image("ftp://example.com/a.jpg", os.path.join(self.out, "x.jpg")))


# ----------------------------------------------------------------------------
# 6. Live-page extraction (Chromium)
# ----------------------------------------------------------------------------
@unittest.skipUnless(_chromium_ok(), "Playwright Chromium nahi mila")
class TestExtractArticleImages(unittest.TestCase):
    def test_dynamic_page(self):
        tmp = tempfile.mkdtemp()
        try:
            _make_image(os.path.join(tmp, "ipl-final.jpg"), (10, 120, 200), size=(1000, 600))
            _make_image(os.path.join(tmp, "site-logo.png"), (255, 0, 0), size=(600, 300), fmt="PNG")
            _make_image(os.path.join(tmp, "sidebar-promo.jpg"), (0, 255, 0), size=(800, 500))
            _make_image(os.path.join(tmp, "late.jpg"), (90, 90, 90), size=(900, 600))
            with open(os.path.join(tmp, "article.html"), "w", encoding="utf-8") as f:
                f.write("""<!doctype html><html><head><meta charset="utf-8">
<meta property="og:title" content="IPL फाइनल"><meta property="og:image" content="ipl-final.jpg"></head>
<body><header><img src="site-logo.png"></header>
<article id="article-content"><h1>IPL फाइनल</h1><p>टेक्स्ट</p><div id="slot"></div></article>
<aside class="sidebar"><img src="sidebar-promo.jpg"></aside>
<script>setTimeout(()=>{const i=document.createElement('img');i.src='late.jpg';i.alt='Final match trophy';
document.getElementById('slot').appendChild(i);},400);</script></body></html>""")
            server = LocalServer(tmp)
            try:
                found = vs.extract_article_images(server.url("article.html"))
            finally:
                server.close()
            names = [os.path.basename(c["url"]) for c in found]
            self.assertIn("ipl-final.jpg", names)          # og:image
            self.assertIn("late.jpg", names)               # JS se baad me aayi article image
            self.assertNotIn("site-logo.png", names)       # header logo
            self.assertNotIn("sidebar-promo.jpg", names)   # sidebar
            late = next(c for c in found if c["url"].endswith("late.jpg"))
            self.assertEqual(late["alt"], "Final match trophy")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_unreachable_page_returns_empty(self):
        self.assertEqual(vs.extract_article_images("http://127.0.0.1:9/nothing.html"), [])


# ----------------------------------------------------------------------------
# 7. Full render (FFmpeg + Chromium)
# ----------------------------------------------------------------------------
def _make_background(path, seconds, tmpdir):
    """Scrolling 'website' clip: lambi page par crop se scroll, VP8 webm ko .mp4 naam (Playwright jaisa)."""
    from PIL import ImageDraw
    page = Image.new("RGB", (1920, 6000), (250, 250, 250))
    d = ImageDraw.Draw(page)
    for y in range(0, 6000, 120):
        d.rectangle([(300, y + 20), (1620, y + 36)], fill=(40 + (y // 120) % 6 * 30, 60, 90))
        d.rectangle([(300, y + 56), (1400, y + 66)], fill=(150, 150, 150))
    png = os.path.join(tmpdir, "tallpage.png")
    page.save(png)
    webm = os.path.join(tmpdir, "bg.webm")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-loop", "1", "-framerate", "25", "-i", png,
                    "-vf", "crop=1920:1080:0:'min(t*80,4900)'", "-t", str(seconds),
                    "-c:v", "libvpx", "-b:v", "1M", "-an", webm], check=True)
    os.replace(webm, path)


def _make_audio(path, seconds):
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    f"sine=frequency=330:sample_rate=44100:duration={seconds}",
                    "-ac", "2", "-c:a", "libmp3lame", "-b:a", "128k", path], check=True)


def _frame(video, t, out):
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{t:.3f}", "-i", video,
                    "-frames:v", "1", out], check=True)
    return Image.open(out).convert("RGB")


@unittest.skipUnless(_ffmpeg_ok() and _chromium_ok() and vs._has_devanagari_font(),
                     "ffmpeg / Chromium / Devanagari font chahiye")
class TestFullRender(unittest.TestCase):
    AUDIO = 52.0

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.web = os.path.join(cls.tmp, "web")
        os.makedirs(cls.web)
        _make_image(os.path.join(cls.web, "lead-photo.jpg"), (20, 90, 220))           # unmatched -> lead
        _make_image(os.path.join(cls.web, "ipl-cricket-batsman.jpg"), (220, 150, 20))  # matches cricket scene
        cls.server = LocalServer(cls.web)
        cls.audio = os.path.join(cls.tmp, "a.mp3")
        cls.bg = os.path.join(cls.tmp, "bg.mp4")
        _make_audio(cls.audio, cls.AUDIO)
        _make_background(cls.bg, 20, cls.tmp)           # audio se chhoti -> loop path bhi test hoga
        cls.cwd = os.getcwd()
        os.chdir(cls.tmp)

    @classmethod
    def tearDownClass(cls):
        os.chdir(cls.cwd)
        cls.server.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _build(self, images, out_name, **kw):
        out = os.path.join(self.tmp, out_name)
        ok = vs.build_video_with_visuals(
            audio_path=self.audio, video_clip_path=kw.pop("clip", self.bg), output_video_path=out,
            title="भारत ने ऑस्ट्रेलिया को हराया", script=SPORTS, article_images=images,
            page_text="", **kw)
        return ok, out

    def test_render_with_images(self):
        images = [
            {"url": self.server.url("lead-photo.jpg"), "alt": "", "caption": "", "title": "", "kind": "meta"},
            {"url": self.server.url("ipl-cricket-batsman.jpg"), "alt": "", "caption": "", "title": "", "kind": "img"},
        ]
        ok, out = self._build(images, "with_images.mp4")
        self.assertTrue(ok)
        info = json.loads(subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", out],
            capture_output=True, text=True).stdout)
        v = next(s for s in info["streams"] if s["codec_type"] == "video")
        a = next(s for s in info["streams"] if s["codec_type"] == "audio")
        self.assertEqual((v["codec_name"], v["pix_fmt"], v["width"], v["height"]), ("h264", "yuv420p", 1920, 1080))
        self.assertEqual(a["codec_name"], "aac")
        self.assertAlmostEqual(float(info["format"]["duration"]), self.AUDIO, delta=0.6)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, vs.TEMP_DIR)), "temp folder saaf hona chahiye")

        # --- foreground: scene mid-points par panel ka rang (image change at scene boundaries)
        scenes = vs.plan_scenes(SPORTS, self.AUDIO, title="भारत ने ऑस्ट्रेलिया को हराया", category="sports")
        vs.assign_images(scenes, [
            {**images[0], "path": "/p/lead.jpg"}, {**images[1], "path": "/p/cricket.jpg"}])
        panel_center = (1920 - 56 - 420, 64 + 236)
        seen = []
        for sc in scenes:
            mid = (sc["start"] + sc["end"]) / 2
            px = _frame(out, mid, os.path.join(self.tmp, f"f{sc['idx']}.png")).getpixel(panel_center)
            seen.append((sc["image_path"], px))
        by_path = {p: px for p, px in seen if p}
        if "/p/lead.jpg" in by_path:
            r, g, b = by_path["/p/lead.jpg"]
            self.assertTrue(b > 150 and r < 80, f"lead image rang galat: {by_path['/p/lead.jpg']}")
        if "/p/cricket.jpg" in by_path:
            r, g, b = by_path["/p/cricket.jpg"]
            self.assertTrue(r > 170 and b < 80, f"cricket image rang galat: {by_path['/p/cricket.jpg']}")
        for p, (r, g, b) in seen:
            if p is None:                      # topic card: dark navy
                self.assertTrue(r < 90 and g < 90 and b < 130, f"card ka rang galat: {(r, g, b)}")
        self.assertGreaterEqual(len({tuple(px) for _, px in seen}), 2, "scenes me visuals badalne chahiye")

        # --- background scrolling ab bhi visible: left area (panel ke bahar) time ke saath badalta hai
        left = (100, 150, 800, 800)
        f1 = _frame(out, 5.0, os.path.join(self.tmp, "l1.png")).crop(left)
        f2 = _frame(out, 12.0, os.path.join(self.tmp, "l2.png")).crop(left)
        diff = sum(abs(a - b) for p, q in zip(f1.getdata(), f2.getdata()) for a, b in zip(p, q))
        self.assertGreater(diff, 1_000_000, "background scrolling nahi dikh rahi")

        # --- end par overlay saaf fade-out ho chuka hai (panel area bg jaisa)
        from PIL import ImageStat
        lt_box = (200, 1080 - 48 - 150, 1700, 1080 - 48)          # lower-third area
        pn_box = (1920 - 56 - 830, 64 + 6, 1920 - 56 - 10, 64 + 460)  # panel area
        mid = _frame(out, scenes[1]["start"] + 3, os.path.join(self.tmp, "mid.png"))
        end = _frame(out, self.AUDIO - 0.02, os.path.join(self.tmp, "end.png"))

        def lum(img, box):
            return ImageStat.Stat(img.crop(box).convert("L")).mean[0]

        self.assertLess(lum(mid, lt_box), 90, "scene ke beech lower-third dikhna chahiye")
        self.assertGreater(lum(end, lt_box), 190, "audio ke end par lower-third saaf fade-out nahi hua")
        self.assertGreater(lum(end, pn_box), 170, "audio ke end par panel saaf fade-out nahi hua")

    def test_all_images_fail_still_renders_with_cards(self):
        images = [{"url": self.server.url("nope.jpg"), "alt": "", "caption": "", "title": "", "kind": "meta"},
                  {"url": "http://127.0.0.1:9/never.jpg", "alt": "", "caption": "", "title": "", "kind": "img"}]
        ok, out = self._build(images, "cards_only.mp4")
        self.assertTrue(ok)
        mid = _frame(out, 10.0, os.path.join(self.tmp, "c.png"))
        r, g, b = mid.getpixel((1920 - 56 - 420, 64 + 236))
        self.assertTrue(r < 90 and g < 90 and b < 130, f"topic card nahi dikha: {(r, g, b)}")

    def test_no_article_images_at_all(self):
        ok, out = self._build(None, "no_images.mp4")
        self.assertTrue(ok)

    def test_missing_background_returns_false(self):
        ok, out = self._build([], "nobg.mp4", clip=os.path.join(self.tmp, "does_not_exist.mp4"))
        self.assertFalse(ok)
        self.assertFalse(os.path.exists(out))

    def test_broken_background_returns_false_and_cleans_up(self):
        bad = os.path.join(self.tmp, "bad.mp4")
        with open(bad, "wb") as f:
            f.write(b"not a video" * 500)
        ok, out = self._build([], "badbg.mp4", clip=bad)
        self.assertFalse(ok)                                  # caller purane build par girega
        self.assertFalse(os.path.exists(out))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, vs.TEMP_DIR)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
