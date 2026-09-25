# SG News18 — GitHub पर पूरी तरह ऑटोमैटिक वीडियो बॉट

अब आपका PC या मोबाइल बंद होने पर भी काम रुकेगा नहीं — सब कुछ GitHub के सर्वर (runner) पर चलेगा।

## नई फाइलें इस रिपो में डालनी हैं
```
news_bot.py                          (मौजूदा फाइल की जगह यह नई वाली रखें)
requirements.txt
.github/workflows/news-bot.yml
telegram_check.py
```

## स्टेप 1 — Gemini की 3 API Key तैयार करें
Google AI Studio से 3 अलग-अलग API key बना लें (या मौजूदा 3 key इस्तेमाल करें)।
इन्हें GitHub Secrets में डालना है (नीचे बताया गया है) — कोड में कहीं भी hardcode नहीं करना।

## स्टेप 2 — Telegram Bot बनाएं
1. Telegram खोलें, **@BotFather** को मैसेज करें → `/newbot` भेजें → नाम दें।
2. BotFather आपको एक **Bot Token** देगा — इसे सेव कर लें।
3. अपने बॉट को एक बार `/start` भेजें (जरूरी है, वरना बॉट आपको मैसेज नहीं भेज पाएगा)।
4. अपनी **Chat ID** निकालने के लिए यह URL ब्राउज़र में खोलें:
   `https://api.telegram.org/bot<आपका-token>/getUpdates`
   `/start` भेजने के बाद वहाँ `"chat":{"id": 123456789}` जैसा कुछ दिखेगा — यही आपकी Chat ID है।

## स्टेप 3 — YouTube Upload के लिए client_secrets.json और token.json base64 में बदलें
आपके पास पहले से `client_secrets.json` और एक बार लॉगिन करने के बाद बना `token.json` होगा।
इन दोनों को base64 में बदलें (टर्मिनल में):
```bash
base64 -w 0 client_secrets.json
base64 -w 0 token.json
```
जो टेक्स्ट मिले, उसे आगे secrets में डालना है।

## स्टेप 4 — GitHub Repo Secrets जोड़ें
Repo → **Settings → Secrets and variables → Actions → New repository secret** में ये सब बनाएं:

| Secret Name              | Value                                   |
|---------------------------|------------------------------------------|
| `GEMINI_API_KEY_1`         | पहली Gemini key                         |
| `GEMINI_API_KEY_2`         | दूसरी Gemini key                        |
| `GEMINI_API_KEY_3`         | तीसरी Gemini key                        |
| `TELEGRAM_BOT_TOKEN`       | BotFather से मिला token                 |
| `TELEGRAM_CHAT_ID`         | आपकी chat id                            |
| `YT_CLIENT_SECRETS_B64`    | स्टेप 3 वाला base64 (client_secrets.json)|
| `YT_TOKEN_JSON_B64`        | स्टेप 3 वाला base64 (token.json)        |

## स्टेप 5 — इस्तेमाल कैसे करें (2 तरीके)

**तरीका 1 — सीधे GitHub पेज से**
1. Repo → **Actions** टैब → **"SG News18 - Auto Video Bot"** workflow खोलें।
2. **Run workflow** बटन दबाएं → `article_url` वाले बॉक्स में लिंक पेस्ट करें → **Run** दबाएं।
3. बस — अब PC/मोबाइल बंद कर दें, GitHub अपने आप वीडियो बनाकर YouTube पर डाल देगा।

**तरीका 2 — Telegram से (सबसे आसान)**
1. अपने Telegram बॉट को सीधे article का लिंक भेज दें (जैसे: `https://bgnewswab.github.io/sgnewswab/article.html?id=112`)।
2. GitHub हर 5 मिनट में खुद चेक करता है — लिंक मिलते ही वीडियो बनना शुरू हो जाएगा।
3. बॉट आपको Telegram पर ही रिप्लाई करेगा: "✅ Link मिल गया!" और फिर हर स्टेज पर अपडेट देगा।

## एरर और सफलता की सूचना
- शुरू होने पर, हर बड़े स्टेप (scraping, script, TTS, video, upload) में गड़बड़ी होने पर, और आखिर में सफल होने पर (YouTube लिंक के साथ) — सब कुछ Telegram पर अपने आप आ जाएगा। कहीं और चेक करने की जरूरत नहीं।

## ध्यान रखने वाली बातें
- Telegram वाला तरीका "polling" पर आधारित है (हर 5 मिनट चेक होता है) — GitHub का schedule कभी-कभी 1-2 मिनट लेट भी हो सकता है, यह GitHub की तरफ से normal है।
- Gemini की 3 keys में से जो भी काम कर रही होगी, कोड अपने आप उसी पर स्विच हो जाएगा — अलग से कुछ करने की जरूरत नहीं।
- `GEMINI_MODEL_NAME` workflow फाइल में लिखा है (`gemini-3.6-flash`) — मॉडल बदलना हो तो सिर्फ वहीं एक जगह बदलें।
