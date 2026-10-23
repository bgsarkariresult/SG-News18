"""
buffer_publisher.py  --  SG-News18 reel ko Buffer (Instagram / Facebook) par bhejne ke liye.

Flow:
  1. Video ko GitHub Release me upload karo  -> public direct URL milta hai
  2. Wahi URL Buffer API (GraphQL) ko do     -> channels ki queue me post

Use (reels_maker.py me YouTube upload ke baad):
    from buffer_publisher import publish_reel_to_buffer
    publish_reel_to_buffer(video_file, caption, article_url=post_url)

Env / Secrets:
    BUFFER_ACCESS_TOKEN   (GitHub secret, zaroori)
    GITHUB_TOKEN          (Actions me workflow se pass hota hai)
    GITHUB_REPOSITORY     (Actions me apne aap milta hai, e.g. bgsarkariresult/SG-News18)
    BUFFER_SERVICES       (optional, default "instagram,facebook")
    KEEP_RELEASES         (optional, default 20 -> isse purani reel releases delete)
"""
import os
import time
import logging
import requests

BUFFER_API = "https://api.buffer.com"


def _notify(msg):
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat, "text": msg, "parse_mode": "HTML"},
            timeout=20,
        )
    except Exception as e:
        logging.warning(f"Telegram notify failed: {e}")


# ---------------------------------------------------------------
# 1. GitHub Release -> public video URL
# ---------------------------------------------------------------
def upload_video_to_github_release(video_path):
    gh_token = os.getenv("GITHUB_TOKEN", "").strip()
    gh_repo = os.getenv("GITHUB_REPOSITORY", "").strip()
    if not (gh_token and gh_repo):
        logging.error("GITHUB_TOKEN / GITHUB_REPOSITORY nahi mile")
        return None
    if not os.path.exists(video_path) or os.path.getsize(video_path) < 500_000:
        logging.error("Video file missing ya bahut chhoti hai")
        return None

    headers = {
        "Authorization": f"Bearer {gh_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    tag = f"reel-{int(time.time())}"
    try:
        r = requests.post(
            f"https://api.github.com/repos/{gh_repo}/releases",
            json={"tag_name": tag, "name": f"Reel {tag}", "body": "Auto reel",
                  "draft": False, "prerelease": True},
            headers=headers, timeout=60,
        )
        if r.status_code not in (200, 201):
            logging.error(f"Release create fail: {r.status_code} {r.text[:200]}")
            return None
        upload_url = r.json()["upload_url"].split("{")[0]
        filename = f"{tag}.mp4"
        with open(video_path, "rb") as f:
            up = requests.post(
                f"{upload_url}?name={filename}",
                headers={**headers, "Content-Type": "video/mp4"},
                data=f, timeout=300,
            )
        if up.status_code in (200, 201):
            url = up.json().get("browser_download_url")
            logging.info(f"GitHub Release URL: {url}")
            return url
        logging.error(f"Asset upload fail: {up.status_code} {up.text[:200]}")
    except Exception as e:
        logging.error(f"GitHub Release error: {e}")
    return None


def cleanup_old_releases(keep=None):
    """Sirf 'reel-' wale releases rakhta hai (latest `keep`), baaki + unke tags delete."""
    gh_token = os.getenv("GITHUB_TOKEN", "").strip()
    gh_repo = os.getenv("GITHUB_REPOSITORY", "").strip()
    keep = int(keep or os.getenv("KEEP_RELEASES", "20"))
    if not (gh_token and gh_repo):
        return
    headers = {"Authorization": f"Bearer {gh_token}", "Accept": "application/vnd.github+json"}
    try:
        res = requests.get(f"https://api.github.com/repos/{gh_repo}/releases?per_page=100",
                           headers=headers, timeout=30)
        rels = [r for r in res.json() if str(r.get("tag_name", "")).startswith("reel-")]
        rels.sort(key=lambda r: r.get("created_at", ""), reverse=True)
        for r in rels[keep:]:
            requests.delete(f"https://api.github.com/repos/{gh_repo}/releases/{r['id']}",
                            headers=headers, timeout=30)
            requests.delete(f"https://api.github.com/repos/{gh_repo}/git/refs/tags/{r['tag_name']}",
                            headers=headers, timeout=30)
    except Exception as e:
        logging.warning(f"Release cleanup skip: {e}")


# ---------------------------------------------------------------
# 2. Buffer
# ---------------------------------------------------------------
def _buffer_headers():
    token = os.getenv("BUFFER_ACCESS_TOKEN", "").strip()
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def get_buffer_channels():
    allowed = {s.strip().lower() for s in
               os.getenv("BUFFER_SERVICES", "instagram,facebook").split(",") if s.strip()}
    h = _buffer_headers()
    try:
        r = requests.post(BUFFER_API,
                          json={"query": "query { account { organizations { id name } } }"},
                          headers=h, timeout=20)
        orgs = r.json().get("data", {}).get("account", {}).get("organizations", [])
        if not orgs:
            logging.error(f"Buffer org nahi mila: {r.text[:200]}")
            return []
        q = """query GetChannels($input: ChannelsInput!) {
                 channels(input: $input) { id service name } }"""
        r2 = requests.post(BUFFER_API,
                           json={"query": q, "variables": {"input": {"organizationId": orgs[0]["id"]}}},
                           headers=h, timeout=20)
        out = []
        for ch in r2.json().get("data", {}).get("channels", []):
            service = (ch.get("service") or "").lower()
            if service in allowed:
                out.append({"id": ch["id"], "service": service, "name": ch.get("name", "")})
        return out
    except Exception as e:
        logging.error(f"Buffer channels error: {e}")
        return []


def send_to_buffer(video_url, caption):
    channels = get_buffer_channels()
    if not channels:
        _notify("❌ Buffer: koi channel nahi mila (token / BUFFER_SERVICES check karo)")
        return False

    mutation = """mutation CreatePost($input: CreatePostInput!) {
      createPost(input: $input) {
        ... on PostActionSuccess { post { id } }
        ... on MutationError { message }
      } }"""

    ok_any = False
    for ch in channels:
        metadata = {}
        if ch["service"] == "instagram":
            metadata = {"instagram": {"type": "reel", "shouldShareToFeed": True}}
        elif ch["service"] == "facebook":
            metadata = {"facebook": {"type": "reel"}}

        inp = {
            "channelId": ch["id"],
            "text": caption,
            "mode": "addToQueue",
            "schedulingType": "automatic",
            "assets": [{"video": {"url": video_url}}],
        }
        if metadata:
            inp["metadata"] = metadata

        try:
            res = requests.post(BUFFER_API, json={"query": mutation, "variables": {"input": inp}},
                                headers=_buffer_headers(), timeout=30)
            body = res.json()
            # kuch accounts par 'automatic' schedulingType allowed nahi hota
            if "errors" in body and "SchedulingType" in str(body):
                inp["schedulingType"] = "notification"
                res = requests.post(BUFFER_API, json={"query": mutation, "variables": {"input": inp}},
                                    headers=_buffer_headers(), timeout=30)
                body = res.json()

            result = (body.get("data") or {}).get("createPost") or {}
            if result.get("post"):
                logging.info(f"Buffer OK: {ch['name']} ({ch['service']})")
                ok_any = True
            else:
                msg = result.get("message") or str(body.get("errors") or body)[:300]
                logging.error(f"Buffer fail {ch['name']}: {msg}")
                _notify(f"❌ Buffer fail: {ch['name']} ({ch['service']})\n<code>{msg}</code>")
        except Exception as e:
            logging.error(f"Buffer post error {ch['name']}: {e}")
            _notify(f"❌ Buffer error: {ch['name']}\n<code>{e}</code>")

    return ok_any


# ---------------------------------------------------------------
# 3. Ek call me sab kuch
# ---------------------------------------------------------------
def publish_reel_to_buffer(video_path, caption, article_url=""):
    """Reel (9:16 mp4) ko GitHub Release par chadhao aur Buffer queue me daalo."""
    if not os.getenv("BUFFER_ACCESS_TOKEN", "").strip():
        logging.info("BUFFER_ACCESS_TOKEN nahi hai, Buffer skip")
        return False

    full_caption = caption.strip()
    if article_url:
        full_caption += f"\n\nPoori khabar: {article_url}"

    video_url = upload_video_to_github_release(video_path)
    if not video_url:
        _notify("❌ Buffer ke liye video URL nahi ban paya (GitHub Release fail)")
        return False

    ok = send_to_buffer(video_url, full_caption)
    if ok:
        _notify(f"📲 Reel Buffer queue me bhej di gayi\n🔗 {video_url}")
    cleanup_old_releases()
    return ok
