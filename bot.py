import os
import re
import html
import json
import time
import base64
import asyncio
import hashlib
import subprocess
import requests
from http.cookiejar import MozillaCookieJar

try:
    import yt_dlp
except ImportError:  # jalur cadangan Facebook; bot tetap jalan tanpa ini
    yt_dlp = None
from aiohttp import web
from telethon import TelegramClient, events
from telethon.tl.types import DocumentAttributeVideo
from pymediainfo import MediaInfo

# ==========================================
# KONFIGURASI BOT (Environment Variables / Default)
# ==========================================
API_ID = int(os.getenv("API_ID", "1916950"))
API_HASH = os.getenv("API_HASH", "9e268fee501ad809e4f5f598adcb970c")
BOT_TOKEN = os.getenv("BOT_TOKEN", "8988516745:AAEQnDxcgUY5c_ip44PlxltGf7kdYHg9rwY")
PORT = int(os.getenv("PORT", "8000"))

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMP_DIR = os.path.join(BASE_DIR, "downloads", "bot_temp")
os.makedirs(TEMP_DIR, exist_ok=True)

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

# Facebook ngasih halaman versi ringan (og:image) ke UA bot, sering lolos login wall
FACEBOOK_BOT_UA = "TelegramBot (like TwitterBot)"
# Opsional: proxy buat request ke facebook.com, misal "http://user:pass@host:port"
FB_PROXY = os.getenv("FB_PROXY", "").strip()
# Cookie Netscape export dari browser (biar Facebook nggak anggap kita bot / login wall).
# Bisa dari file (FB_COOKIE_FILE / fb_cookies.txt) atau base64 di env (FB_COOKIES_B64).
FB_COOKIE_FILE = os.getenv("FB_COOKIE_FILE", "").strip()
FB_COOKIES_B64 = os.getenv("FB_COOKIES_B64", "").strip()


def get_facebook_cookie_file():
    """Path file cookie Netscape buat facebook.com, atau None kalau nggak ada."""
    if FB_COOKIES_B64:
        target = os.path.join(TEMP_DIR, "fb_cookies.txt")
        if not os.path.exists(target):
            try:
                with open(target, "wb") as f:
                    f.write(base64.b64decode(FB_COOKIES_B64))
                os.chmod(target, 0o600)
            except Exception as e:
                print(f"FB_COOKIES_B64 gagal dipakai: {e}")
                return None
        return target

    candidates = [FB_COOKIE_FILE] if FB_COOKIE_FILE else []
    candidates += [
        os.path.join(BASE_DIR, "fb_cookies.txt"),
        os.path.join(BASE_DIR, "www.facebook.com_cookies.txt"),
    ]
    for path in candidates:
        if path and os.path.exists(path):
            return path
    return None

client = TelegramClient(os.path.join(BASE_DIR, "bot_session"), API_ID, API_HASH)


# ==========================================
# DUMMY HTTP SERVER (UNTUK HEALTH CHECK KOYEB)
# ==========================================
async def start_healthcheck_server():
    """Server HTTP ringan agar platform Cloud seperti Koyeb/Render mendeteksi bot aktif."""
    async def handle_ping(request):
        return web.json_response({
            "status": "healthy",
            "bot": "NgunduhMantuh_bot",
            "message": "Bot TikTok, Facebook & X Downloader is running"
        })

    app = web.Application()
    app.router.add_get("/", handle_ping)
    app.router.add_get("/health", handle_ping)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"🌐 Health check HTTP server aktif di port {PORT}")


# ==========================================
# HELPER SCRAPER TIKTOK
# ==========================================
def resolve_tiktok_url(url):
    """Mengikuti redirect URL pendek (vt.tiktok.com atau vm.tiktok.com)."""
    url = url.strip()
    if "vt.tiktok.com" in url or "vm.tiktok.com" in url:
        try:
            r = requests.get(url, headers=DEFAULT_HEADERS, allow_redirects=True, timeout=10)
            return r.url
        except Exception:
            return url
    return url


def extract_media_id(url):
    """Mengekstrak ID postingan dari URL."""
    m = re.search(r'/(?:video|photo)/(\d+)', url)
    if m:
        return m.group(1)
    m = re.search(r'/(\d{15,25})', url)
    if m:
        return m.group(1)
    cleaned = re.sub(r'\W+', '', url)
    return cleaned[-12:] if cleaned else "media"


def get_data_ssstik(url, media_id):
    """Scraper ssstik.io untuk video atau foto."""
    try:
        s = requests.Session()
        s.headers.update(DEFAULT_HEADERS)
        s.get("https://ssstik.io/en", timeout=10)
        res = s.post(
            "https://ssstik.io/abc?url=dl",
            data={"id": url, "locale": "en", "tt": "0"},
            timeout=15
        )
        html = res.text

        # Cek apakah postingan foto/carousel
        img_links = re.findall(r'href=[\x22\x27](https://tikcdn\.io/ssstik/aHR0[^\x22\x27]+)[\x22\x27]', html)
        if img_links:
            return {"id": media_id, "images": img_links, "type": "photo"}

        # Cek apakah video
        video_links = [
            l for l in re.findall(r'href=[\x22\x27](https://tikcdn\.io/ssstik/[^\x22\x27]+)[\x22\x27]', html)
            if "/m/" not in l
        ]
        if video_links:
            return {"id": media_id, "play": video_links[0], "type": "video"}
    except Exception:
        pass
    return None


def get_data_musicaldown(url, media_id):
    """Fallback scraper musicaldown.com."""
    try:
        s = requests.Session()
        s.headers.update(DEFAULT_HEADERS)
        r = s.get("https://musicaldown.com/en", timeout=10)
        inputs = dict(re.findall(r'<input[^>]*name=[\x22\x27]([^\x22\x27]+)[\x22\x27][^>]*value=[\x22\x27]([^\x22\x27]*)[\x22\x27]', r.text))
        post_data = inputs.copy()
        for name in re.findall(r'<input[^>]*name=[\x22\x27]([^\x22\x27]+)[\x22\x27]', r.text):
            if name not in post_data:
                post_data[name] = url
        r_post = s.post(
            "https://musicaldown.com/download",
            data=post_data,
            headers={**DEFAULT_HEADERS, "Referer": "https://musicaldown.com/en"},
            timeout=15
        )
        links = [l for l in re.findall(r'href=[\x22\x27](https?://[^\s\x22\x27]+)[\x22\x27]', r_post.text) if "muscdn.app" in l]
        if links:
            return {"id": media_id, "play": links[0], "type": "video"}
    except Exception:
        pass
    return None


def fetch_tiktok_media_info(url):
    """Mengambil informasi media menggunakan multi-backend scraper."""
    resolved_url = resolve_tiktok_url(url)
    media_id = extract_media_id(resolved_url)

    # Coba SSSTik
    data = get_data_ssstik(url, media_id)
    if not data and resolved_url != url:
        data = get_data_ssstik(resolved_url, media_id)
    if data:
        return data

    # Fallback MusicalDown
    data = get_data_musicaldown(url, media_id)
    if not data and resolved_url != url:
        data = get_data_musicaldown(resolved_url, media_id)
    if data:
        return data

    return None


def download_binary(url, dest_path, referer="https://www.tiktok.com/"):
    """Mendownload file binary ke path lokal (retry kalau koneksi direset)."""
    headers = {**DEFAULT_HEADERS, "Referer": referer}
    for attempt in range(3):
        try:
            r = requests.get(url, headers=headers, stream=True, timeout=60)
            if r.status_code != 200:
                print(f"download status {r.status_code} (percobaan {attempt + 1})")
                return False
            with open(dest_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=128 * 1024):
                    if chunk:
                        f.write(chunk)
            return True
        except requests.RequestException as e:
            print(f"download error (percobaan {attempt + 1}): {e}")
            time.sleep(2)
    return False


def get_video_metadata(file_path):
    """Mendapatkan metadata durasi, lebar, dan tinggi video."""
    try:
        media_info = MediaInfo.parse(file_path)
        for track in media_info.tracks:
            if track.track_type == "Video":
                duration = int(track.duration / 1000) if track.duration else 0
                width = track.width if track.width else 0
                height = track.height if track.height else 0
                return duration, width, height
    except Exception:
        pass
    return 0, 0, 0


def generate_thumbnail(video_path):
    """Membuat thumbnail video dengan ffmpeg."""
    thumb_path = video_path + "_thumb.jpg"
    command = [
        "ffmpeg",
        "-y",
        "-ss", "00:00:02",
        "-i", video_path,
        "-vframes", "1",
        "-q:v", "2",
        thumb_path
    ]
    try:
        subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0:
            return thumb_path
    except Exception:
        pass
    return None


# ==========================================
# HELPER FACEBOOK / REELS (getmyfb.com)
# ==========================================
FACEBOOK_URL_PATTERN = r'https?://(?:[a-zA-Z0-9_-]+\.)*(?:facebook\.com|fb\.watch|fb\.me)/\S+'
# Cuma url yang kelihatan video/reel — biar link profil/foto nggak bikin bot nyahut
FACEBOOK_MEDIA_HINTS = (
    '/reel/', '/reels/', '/share/', '/videos/', '/video.php', '/watch', '/v/',
    'fb.watch/', 'fb.me/',
)
GETMYFB_URL = "https://getmyfb.com/process"
GETMYFB_HEADERS = {
    **DEFAULT_HEADERS,
    "Accept": "*/*",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "HX-Request": "true",
    "HX-Target": "target",
    "HX-Current-URL": "https://getmyfb.com/",
    "Origin": "https://getmyfb.com",
    "Referer": "https://getmyfb.com/",
}


def is_facebook_media_url(url):
    """True kalau URL Facebook kelihatan video/reel (bukan link profil/foto)."""
    lowered = url.lower()
    return any(hint in lowered for hint in FACEBOOK_MEDIA_HINTS)


def extract_facebook_media_id(url):
    """Ambil ID postingan Facebook dari URL (reel, video, atau share link)."""
    for pattern in (r'/(?:reel|videos?)/(\d{6,})', r'[?&]v=(\d{6,})', r'/share/r/([A-Za-z0-9_-]+)'):
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return hashlib.md5(url.split('?')[0].encode()).hexdigest()[:12]


def parse_getmyfb_html(page):
    """Ambil link download dari HTML hasil getmyfb.com: video (HD/SD) atau foto."""
    links = {}
    has_photo = False
    for block in re.findall(r'<li class="results-list-item">(.*?)</li>', page, re.S):
        label = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', block)).strip().lower()
        href = re.search(r'href=["\']([^"\']+)["\']', block)
        if not href:
            continue
        if label.startswith('720') or '(hd)' in label:
            links.setdefault('hd', href.group(1))
        elif label.startswith('360') or '(sd)' in label:
            links.setdefault('sd', href.group(1))
        elif 'photo' in label or 'foto' in label:
            has_photo = True  # link foto getmyfb cuma placeholder kecil, kita scrape sendiri
    thumb = re.search(r'<img class="results-item-image"\s+src=["\']([^"\']+)["\']', page)
    return links, (thumb.group(1) if thumb else None), has_photo


def fetch_facebook_media_info(url):
    """Scrape getmyfb.com untuk dapat link video HD/SD. Return None kalau gagal."""
    media_id = extract_facebook_media_id(url)
    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)
    for attempt in range(3):
        try:
            session.get("https://getmyfb.com/", timeout=15)
            res = session.post(
                GETMYFB_URL,
                data={"id": url, "locale": "en"},
                headers=GETMYFB_HEADERS,
                timeout=30
            )
            if '"rate_limit"' in res.text:
                print(f"getmyfb rate limit (percobaan {attempt + 1}), tunggu dulu")
                time.sleep(4 * (attempt + 1))
                continue
            links, thumb_url, has_photo = parse_getmyfb_html(res.text)
            if links.get('hd') or links.get('sd'):
                return {
                    "id": media_id,
                    "type": "video",
                    "play": links.get('hd') or links.get('sd'),
                    "quality": "HD" if links.get('hd') else "SD",
                    "thumb_url": thumb_url,
                }
            if has_photo:
                return {"id": media_id, "kind": "photo"}
            # Server kasih sinyal lewat header hx-trigger kalau link gagal/privat
            trigger = (res.headers.get("hx-trigger") or "").lower()
            snippet = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', res.text)).strip()[:160]
            print(f"getmyfb gagal (percobaan {attempt + 1}): status={res.status_code} "
                  f"trigger={trigger or '-'} body='{snippet}'")
            if "private" in trigger:
                return {"error": "private"}
            if "fail" in trigger:
                return {"error": "fail"}
        except requests.RequestException as e:
            print(f"getmyfb error (percobaan {attempt + 1}): {e}")
    return None


def download_facebook_via_ytdlp(url, dest_dir, base_name):
    """Jalur cadangan: download langsung dari facebook.com pakai yt-dlp."""
    if yt_dlp is None:
        print("yt-dlp nggak terpasang, jalur cadangan dilewati")
        return None

    opts = {
        "format": "hd/bv*+ba/b",
        "format_sort": ["res:1080"],
        "outtmpl": os.path.join(dest_dir, base_name + ".%(ext)s"),
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "nocheckcertificate": True,
        "retries": 2,
        "socket_timeout": 30,
    }
    cookie_file = get_facebook_cookie_file()
    if cookie_file:
        opts["cookiefile"] = cookie_file
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            path = ydl.prepare_filename(info)
            if not os.path.exists(path):
                # setelah merge, ekstensinya bisa berubah
                base = os.path.splitext(path)[0]
                for ext in ('.mp4', '.mkv', '.webm'):
                    if os.path.exists(base + ext):
                        path = base + ext
                        break
            if os.path.exists(path):
                print(f"yt-dlp ok: {os.path.basename(path)} ({info.get('height')}p)")
                return path, info.get("height")
            print(f"yt-dlp selesai tapi file nggak ketemu: {path}")
    except Exception as e:
        print(f"yt-dlp gagal: {e}")
    return None


FACEBOOK_PAGE_HEADERS = {
    **DEFAULT_HEADERS,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
}


def _fb_get_page(url, headers, tries=2):
    """GET halaman Facebook pakai header tertentu. Return HTML atau None."""
    session = requests.Session()
    session.headers.update(headers)
    if FB_PROXY:
        session.proxies = {"http": FB_PROXY, "https": FB_PROXY}
    cookie_file = get_facebook_cookie_file()
    if cookie_file:
        jar = MozillaCookieJar(cookie_file)
        try:
            jar.load(ignore_discard=True, ignore_expires=True)
            session.cookies.update(jar)
        except Exception as e:
            print(f"cookie facebook gagal dimuat: {e}")
    try:
        session.get("https://www.facebook.com/", timeout=20)  # ambil cookie dulu
    except requests.RequestException:
        pass  # cookie cuma pelengkap, lanjut aja

    for attempt in range(tries):
        try:
            res = session.get(url, timeout=30, allow_redirects=True)
            body = res.text
            if res.status_code == 200 and len(body) >= 5000:
                return body
            lowered = body[:200000].lower()
            if "isn't available" in lowered or "is not available" in lowered:
                print("facebook: postingan nggak tersedia (privat / sudah dihapus)")
                return None
            hint = ""
            if cookie_file and "log" in lowered[:2000]:
                hint = " — cookie mungkin expired, export ulang"
            print(f"facebook page ditolak: status={res.status_code} bytes={len(body)} "
                  f"(kemungkinan login wall / blokir IP){hint}")
            if res.status_code in (404, 410):
                return None
        except requests.RequestException as e:
            print(f"facebook page error (percobaan {attempt + 1}): {e}")
        time.sleep(2)
    return None


def find_facebook_post_id(page, url):
    """Cari ID numerik postingan dari halaman / URL."""
    ids = re.findall(r'story_fbid=(\d{8,})', page)
    og = re.search(r'property="og:url"[^>]*content="([^"]*)"', page)
    if og:
        ids += re.findall(r'\d{8,}', html.unescape(og.group(1)))
    ids += re.findall(r'\d{8,}', url or '')
    unique = []
    for pid in ids:
        if pid not in unique:
            unique.append(pid)
    return unique


def _find_story_object(node, post_id):
    """Cari object JSON yang punya post_id + attachments (data postingan)."""
    if isinstance(node, dict):
        value = node.get('post_id') or node.get('story_fbid')
        if str(value) == post_id and isinstance(node.get('attachments'), list):
            return node
        for child in node.values():
            if isinstance(child, (dict, list)):
                found = _find_story_object(child, post_id)
                if found is not None:
                    return found
    elif isinstance(node, list):
        for child in node:
            found = _find_story_object(child, post_id)
            if found is not None:
                return found
    return None


def _photos_from_attachments(attachments):
    """Ambil URL foto dari struktur attachments Facebook."""
    uris = []
    for att in attachments:
        if not isinstance(att, dict):
            continue
        style = ((att.get('styles') or {}).get('attachment') or {})
        nodes = ((style.get('all_subattachments') or {}).get('nodes')) or []
        if nodes:
            for node in nodes:
                media = (node or {}).get('media') or {}
                uri = (media.get('image') or {}).get('uri')
                if isinstance(uri, str) and 'scontent' in uri:
                    uris.append(uri)
        else:
            media = att.get('media') or style.get('media') or {}
            uri = (media.get('image') or {}).get('uri')
            if isinstance(uri, str) and 'scontent' in uri:
                uris.append(uri)
    return uris


def signed_out_page(page):
    """True kalau yang balik ternyata halaman login Facebook (cookie expired)."""
    lowered = page[:300000].lower()
    return 'log into facebook' in lowered and 'name="email"' in lowered


def _parse_facebook_photos(page, url=None, limit=10):
    """Ambil URL foto milik postingan (bukan gambar lain yang nempel di halaman)."""
    ids = find_facebook_post_id(page, url)
    blobs = re.findall(r'<script type="application/json"[^>]*>(.*?)</script>', page, re.S)
    for post_id in ids:
        for blob in blobs:
            if post_id not in blob:
                continue
            try:
                data = json.loads(blob)
            except ValueError:
                continue
            story = _find_story_object(data, post_id)
            if story is None:
                continue
            uris = _photos_from_attachments(story['attachments'])
            if not uris:
                continue
            unique, seen = [], set()
            for uri in uris:
                key = re.search(r'/(\d+_\d+_\d+)_n\.jpg', uri)
                marker = key.group(1) if key else uri
                if marker in seen:
                    continue
                seen.add(marker)
                # buang parameter ctp -> dapat ukuran asli, bukan versi kecil
                unique.append(re.sub(r'[&?]ctp=[^&]*', '', uri))
            return unique[:limit]

    # postingan nggak ada di halaman (login wall / sudah dihapus) -> jangan asal ambil og:image
    if ids and not any(pid in page for pid in ids):
        print("facebook: id postingan nggak ketemu di halaman (login wall / post nggak tersedia)")
        return []

    # cadangan: foto pertama dari og:image (aman, nggak ikut gambar lain)
    if signed_out_page(page):
        return []
    og = re.search(r'property="og:image"[^>]*content="([^"]*)"', page)
    if og:
        return [re.sub(r'[&?]ctp=[^&]*', '', html.unescape(og.group(1)))]
    return []


def fetch_facebook_photos(url, limit=10):
    """Scrape halaman post Facebook buat ambil foto asli (termasuk carousel)."""
    # Jalur 1: header browser lengkap (pakai cookie kalau ada) -> carousel lengkap
    page = _fb_get_page(url, FACEBOOK_PAGE_HEADERS)
    photos = _parse_facebook_photos(page, url, limit) if page else []
    print(f"facebook foto ketemu: {len(photos)}")

    # Jalur 2: UA bot. Facebook kasih versi ringan yang lolos dari login wall
    # (biasanya cuma foto pertama, tapi lumayan daripada gagal total di IP datacenter)
    if not photos:
        bot_headers = {**DEFAULT_HEADERS, "User-Agent": FACEBOOK_BOT_UA}
        page = _fb_get_page(url, bot_headers)
        photos = _parse_facebook_photos(page, url, limit) if page else []
        print(f"facebook foto via UA bot: {len(photos)}")

    return photos


# ==========================================
# HELPER X / TWITTER (api.fxtwitter.com)
# ==========================================
TWITTER_URL_PATTERN = r'https?://(?:www\.|mobile\.|m\.)?(?:twitter\.com|x\.com)/\S+'
FXTWITTER_API = "https://api.fxtwitter.com/status/{tweet_id}"


def extract_tweet_id(url):
    """Ambil ID status dari URL Twitter/X. None kalau bukan link status."""
    match = re.search(r'/(?:status|statuses)/(\d{5,})', url or '')
    return match.group(1) if match else None


def pick_best_twitter_variant(variants):
    """Pilih MP4 bitrate tertinggi dari daftar variant."""
    mp4s = [
        v for v in (variants or [])
        if isinstance(v, dict) and 'mp4' in str(v.get('content_type', ''))
        and isinstance(v.get('url'), str)
    ]
    if not mp4s:
        return None
    return max(mp4s, key=lambda v: int(v.get('bitrate') or 0))


def fetch_twitter_media_info(tweet_id):
    """Ambil media tweet via fxtwitter. Return dict {'videos': [...], 'photos': [...]} atau error."""
    payload = None
    for attempt in range(2):
        try:
            res = requests.get(
                FXTWITTER_API.format(tweet_id=tweet_id),
                headers=DEFAULT_HEADERS,
                timeout=30
            )
        except requests.RequestException as e:
            print(f"fxtwitter error (percobaan {attempt + 1}): {e}")
            time.sleep(2)
            continue

        if res.status_code == 404:
            return {"error": "notfound"}
        if res.status_code != 200:
            print(f"fxtwitter status {res.status_code}: {res.text[:160]}")
            time.sleep(2)
            continue

        try:
            payload = res.json()
        except ValueError:
            print(f"fxtwitter balas bukan JSON: {res.text[:160]}")
            time.sleep(2)
            continue
        break

    if payload is None:
        return None

    tweet = payload.get("tweet") or {}
    if not tweet:
        return {"error": "notfound"}

    media = tweet.get("media") or {}

    def _norm_photo_url(photo_url):
        if not photo_url:
            return None
        if re.search(r'[?&]name=', photo_url):
            return re.sub(r'([?&])name=[A-Za-z0-9]+', r'\1name=orig', photo_url)
        return photo_url + ('&' if '?' in photo_url else '?') + 'name=orig'

    def _video_item(video):
        if not isinstance(video, dict):
            return None
        if video.get("type") == "gif":
            gif_url = video.get("url")
            if not gif_url:
                return None
            return {"kind": "video", "url": gif_url, "is_gif": True, "duration": 0, "width": 0, "height": 0}
        best = pick_best_twitter_variant(video.get("variants"))
        if not best:
            return None
        return {
            "kind": "video",
            "url": best["url"],
            "width": video.get("width") or 0,
            "height": video.get("height") or 0,
            "duration": int(video.get("duration") or 0),
            "is_gif": False,
        }

    # Pakai media.all kalau ada supaya urutan campuran (video+foto) ikut urutan tweet.
    raw_items = media.get("all")
    if not isinstance(raw_items, list) or not raw_items:
        raw_items = []
        for v in (media.get("videos") or []):
            raw_items.append(v)
        for p in (media.get("photos") or []):
            raw_items.append({"type": "photo", **p} if isinstance(p, dict) else p)

    items = []
    seen_urls = set()
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        mtype = raw.get("type")
        item = None
        if mtype in ("video", "gif"):
            item = _video_item(raw)
        elif mtype == "photo":
            purl = _norm_photo_url(raw.get("url"))
            if purl:
                item = {"kind": "photo", "url": purl}
        if not item:
            continue
        url = item.get("url")
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        items.append(item)

    videos = [i for i in items if i["kind"] == "video"]
    photos = [i["url"] for i in items if i["kind"] == "photo"]

    return {
        "id": tweet_id,
        "author": (tweet.get("author") or {}).get("screen_name") or "",
        "text": (tweet.get("text") or "").strip(),
        "created_at": tweet.get("created_at") or "",
        "videos": videos,
        "photos": photos,
        "items": items,
    }



# ==========================================
# TELEGRAM BOT HANDLERS
# ==========================================

@client.on(events.NewMessage(pattern=r'^/(start|help)'))
async def start_handler(event):
    welcome_text = (
        "👋 **Selamat datang di TikTok, Facebook & X Downloader Bot!**\n\n"
        "Kirimkan link TikTok, Facebook/Reels, atau X/Twitter ke sini, "
        "dan saya akan mengunduh serta mengirimkannya tanpa watermark.\n\n"
        "📌 **Format yang didukung:**\n"
        "• `https://vt.tiktok.com/...`\n"
        "• `https://www.tiktok.com/@user/video/...`\n"
        "• `https://www.tiktok.com/@user/photo/...`\n"
        "• `https://www.facebook.com/reel/...`\n"
        "• `https://www.facebook.com/share/r/...`\n"
        "• `https://fb.watch/...`\n"
        "• `https://x.com/user/status/...`\n"
        "• `https://twitter.com/user/status/...`\n\n"
        "_Silakan tempel (paste) link-nya sekarang!_"
    )
    await event.reply(welcome_text)


async def send_facebook_photos(event, status_msg, fb_url, created_files):
    """Ambil + kirim foto postingan Facebook (single/carousel). True kalau terkirim."""
    await status_msg.edit("🖼 **Sedang memproses foto postingan...**")
    photos = await asyncio.to_thread(fetch_facebook_photos, fb_url)
    if not photos:
        return False

    await status_msg.edit(f"⬇ **Sedang mendownload {len(photos)} foto...**")
    img_paths = []
    for idx, photo_url in enumerate(photos, start=1):
        img_path = os.path.join(TEMP_DIR, f"fb_photo_{event.id}_{idx}.jpg")
        created_files.append(img_path)
        ok = await asyncio.to_thread(download_binary, photo_url, img_path, "https://www.facebook.com/")
        if ok and os.path.exists(img_path) and os.path.getsize(img_path) > 0:
            img_paths.append(img_path)

    if not img_paths:
        return False

    await status_msg.edit(f"⬆ **Mengunggah {len(img_paths)} foto ke Telegram...**")
    caption = (
        f"🖼 **Facebook Photo** ({len(img_paths)} foto)\n"
        f"🔗 [Link Facebook]({fb_url})"
    )
    await client.send_file(
        event.chat_id,
        file=img_paths,
        caption=caption,
        reply_to=event.message.id
    )
    await status_msg.delete()
    return True


async def handle_facebook_message(event, status_msg, fb_url):
    """Download video Facebook/Reels: getmyfb.com dulu, yt-dlp kalau diblok."""
    created_files = []
    quality = "HD"
    try:
        media_id = extract_facebook_media_id(fb_url)
        video_path = None
        photos_tried = False

        data = await asyncio.to_thread(fetch_facebook_media_info, fb_url)

        if data and data.get("error") == "private":
            await status_msg.edit(
                "🔒 **Video ini privat / tidak bisa diakses publik.**\n"
                "Coba link dari postingan yang publik ya."
            )
            return

        # Post foto: getmyfb nandain "Download Photo" -> kita scrape sendiri halamannya
        if data and data.get("kind") == "photo":
            photos_tried = True
            if await send_facebook_photos(event, status_msg, fb_url, created_files):
                return

        # Jalur 1: video dari link langsung hasil scrape getmyfb.com
        if data and data.get("play"):
            quality = data.get("quality", "HD")
            await status_msg.edit(f"⬇ **Sedang mendownload video ({quality})...**")
            video_path = os.path.join(TEMP_DIR, f"fb_{media_id}_{event.id}.mp4")
            created_files.append(video_path)
            ok = await asyncio.to_thread(download_binary, data["play"], video_path, "https://getmyfb.com/")
            if not ok or not os.path.exists(video_path) or os.path.getsize(video_path) == 0:
                print("Download dari link getmyfb gagal, pindah ke yt-dlp")
                video_path = None

        # Jalur 2: yt-dlp, download langsung dari facebook.com
        if not video_path:
            await status_msg.edit("🔁 **Sumber pertama nggak bisa dipakai, coba jalur cadangan...**")
            result = await asyncio.to_thread(
                download_facebook_via_ytdlp, fb_url, TEMP_DIR, f"fb_{media_id}_{event.id}"
            )
            if result:
                video_path, height = result
                created_files.append(video_path)
                quality = f"{height}p" if height else "HD"

        # Post foto yang nggak ketebak getmyfb (misal link /share/p/...)
        if not video_path and not photos_tried and re.search(r'/(?:share/p|photos?)/', fb_url):
            if await send_facebook_photos(event, status_msg, fb_url, created_files):
                return

        if not video_path:
            await status_msg.edit(
                "❌ **Gagal mengambil media dari link Facebook tersebut.**\n"
                "Pastikan postingan publik dan link masih aktif. Kalau postingan cuma "
                "dibagikan ke grup/teman tertentu, Facebook nggak ngebolehin diunduh."
            )
            return

        await status_msg.edit("⬆ **Sedang mengunggah video ke Telegram...**")

        duration, width, height = await asyncio.to_thread(get_video_metadata, video_path)
        if width and height:
            # video vertikal (reels) -> kualitas pakai sisi pendek, misal 720p
            quality = f"{min(width, height)}p"
        thumb_path = await asyncio.to_thread(generate_thumbnail, video_path)
        if thumb_path:
            created_files.append(thumb_path)

        attributes = []
        if duration or width or height:
            attributes.append(DocumentAttributeVideo(
                duration=duration,
                w=width,
                h=height,
                supports_streaming=True
            ))

        caption = (
            f"🎬 **Facebook Video** ({quality})\n"
            f"🔗 [Link Facebook]({fb_url})"
        )

        await client.send_file(
            event.chat_id,
            file=video_path,
            caption=caption,
            thumb=thumb_path,
            attributes=attributes,
            reply_to=event.message.id
        )
        await status_msg.delete()

    except Exception as e:
        print(f"Error handling Facebook message: {e}")
        try:
            await status_msg.edit(f"❌ **Terjadi kesalahan:** {e}")
        except Exception:
            pass

    finally:
        for fpath in created_files:
            if os.path.exists(fpath):
                try:
                    os.remove(fpath)
                except Exception:
                    pass


async def handle_twitter_message(event, status_msg, tweet_url, tweet_id):
    """Download media tweet (video/GIF/foto) lalu kirim ke Telegram."""
    created_files = []
    try:
        data = await asyncio.to_thread(fetch_twitter_media_info, tweet_id)

        if data and data.get("error") == "notfound":
            await status_msg.edit(
                "❌ **Tweet-nya nggak ketemu.**\n"
                "Bisa jadi sudah dihapus, akun dikunci, atau tweet-nya privat."
            )
            return

        if not data:
            await status_msg.edit(
                "❌ **Gagal mengambil media dari link X/Twitter tersebut.**\n"
                "Coba lagi beberapa saat atau pakai link tweet lain."
            )
            return

        items = data.get("items") or []
        caption_head = f"@{data['author']}" if data.get("author") else "X"

        if not items:
            teks = (data.get("text") or "").strip()
            await status_msg.edit(
                f"ℹ️ **Tweet ini nggak ada video/fotonya.**\n\n"
                f"{teks[:600] if teks else '(tanpa teks)'}"
            )
            return

        videos = [i for i in items if i.get("kind") == "video"]
        photos = [i for i in items if i.get("kind") == "photo"]
        total = len(items)
        sent_any = False
        video_idx = 0
        photo_paths = []
        photo_count = len(photos[:10])

        for idx, item in enumerate(items, start=1):
            kind = item.get("kind")

            if kind == "video":
                video_idx += 1
                await status_msg.edit(
                    f"⬇ **Mendownload video {video_idx}/{len(videos)}...**"
                )
                video_path = os.path.join(TEMP_DIR, f"x_{tweet_id}_{event.id}_{idx}.mp4")
                created_files.append(video_path)

                ok = await asyncio.to_thread(
                    download_binary, item["url"], video_path, "https://x.com/"
                )
                if not ok or not os.path.exists(video_path) or os.path.getsize(video_path) == 0:
                    await status_msg.edit("❌ **Gagal mendownload file video dari server X.**")
                    return

                await status_msg.edit("⬆ **Sedang mengunggah video ke Telegram...**")

                duration, width, height = await asyncio.to_thread(get_video_metadata, video_path)
                if not duration and item.get("duration"):
                    duration = int(item["duration"])
                if not width and item.get("width"):
                    width = int(item["width"])
                if not height and item.get("height"):
                    height = int(item["height"])

                thumb_path = await asyncio.to_thread(generate_thumbnail, video_path)
                if thumb_path:
                    created_files.append(thumb_path)

                attributes = []
                if duration or width or height:
                    attributes.append(DocumentAttributeVideo(
                        duration=duration,
                        w=width,
                        h=height,
                        supports_streaming=True
                    ))

                label = "GIF" if item.get("is_gif") else "Video"
                caption = f"🎬 **{label} dari {caption_head}**\n🔗 [Link tweet]({tweet_url})"
                if total > 1:
                    caption += f"\n({idx}/{total})"

                await client.send_file(
                    event.chat_id,
                    file=video_path,
                    caption=caption,
                    thumb=thumb_path,
                    attributes=attributes,
                    reply_to=event.message.id
                )
                sent_any = True

            elif kind == "photo":
                if len(photo_paths) >= 10:
                    continue
                pidx = len(photo_paths) + 1
                await status_msg.edit(f"⬇ **Mendownload foto {pidx}/{photo_count}...**")
                img_path = os.path.join(TEMP_DIR, f"x_{tweet_id}_{event.id}_{idx}.jpg")
                created_files.append(img_path)
                ok = await asyncio.to_thread(download_binary, item["url"], img_path, "https://x.com/")
                if ok and os.path.exists(img_path) and os.path.getsize(img_path) > 0:
                    photo_paths.append(img_path)
                else:
                    print(f"gagal download foto X: {item.get('url')}")

        if photo_paths:
            await status_msg.edit(f"⬆ **Mengunggah {len(photo_paths)} foto ke Telegram...**")
            await client.send_file(
                event.chat_id,
                file=photo_paths,
                caption=f"🖼 **Foto dari {caption_head}**\n🔗 [Link tweet]({tweet_url})",
                reply_to=event.message.id
            )
            sent_any = True

        if not sent_any:
            await status_msg.edit("❌ **Gagal mendownload media dari server X.**")
            return

        await status_msg.delete()

    except Exception as e:
        print(f"Error handling X/Twitter message: {e}")
        try:
            await status_msg.edit(f"❌ **Terjadi kesalahan:** {e}")
        except Exception:
            pass

    finally:
        for fpath in created_files:
            if os.path.exists(fpath):
                try:
                    os.remove(fpath)
                except Exception:
                    pass


@client.on(events.NewMessage)
async def message_handler(event):
    text = event.raw_text.strip()
    if text.startswith("/"):
        return

    # Link X / Twitter
    tw_match = re.search(TWITTER_URL_PATTERN, text)
    if tw_match:
        tweet_url = tw_match.group(0).rstrip('.,)')
        tweet_id = extract_tweet_id(tweet_url)
        if tweet_id:
            status_msg = await event.reply("⏳ **Sedang memproses link X/Twitter...**")
            await handle_twitter_message(event, status_msg, tweet_url, tweet_id)
            return

    # Link Facebook / Reels
    fb_match = re.search(FACEBOOK_URL_PATTERN, text)
    if fb_match:
        fb_url = fb_match.group(0).rstrip('.,)')
        if is_facebook_media_url(fb_url):
            status_msg = await event.reply("⏳ **Sedang memproses link Facebook...**")
            await handle_facebook_message(event, status_msg, fb_url)
            return

    # Cari URL TikTok di dalam pesan
    pattern = r'https?://(?:(?:[a-zA-Z0-9_-]+\.)?tiktok\.com)/\S+'
    match = re.search(pattern, text)
    if not match:
        return

    tiktok_url = match.group(0)
    status_msg = await event.reply("⏳ **Sedang memproses link TikTok...**")

    created_files = []

    try:
        data = await asyncio.to_thread(fetch_tiktok_media_info, tiktok_url)
        if not data:
            await status_msg.edit(
                "❌ **Gagal mengambil media dari link TikTok tersebut.**\n"
                "Pastikan postingan bersifat publik dan link masih aktif."
            )
            return

        media_id = data.get("id", "media")
        media_type = data.get("type")

        # ----------------------------------------------------
        # KASUS 1: VIDEO TIKTOK
        # ----------------------------------------------------
        if media_type == "video" and data.get("play"):
            await status_msg.edit("⬇ **Sedang mendownload video...**")
            video_path = os.path.join(TEMP_DIR, f"{media_id}_{event.id}.mp4")
            created_files.append(video_path)

            ok = await asyncio.to_thread(download_binary, data["play"], video_path)
            if not ok or not os.path.exists(video_path) or os.path.getsize(video_path) == 0:
                await status_msg.edit("❌ **Gagal mendownload file video dari server.**")
                return

            await status_msg.edit("⬆ **Sedang mengunggah video ke Telegram...**")

            duration, width, height = await asyncio.to_thread(get_video_metadata, video_path)
            thumb_path = await asyncio.to_thread(generate_thumbnail, video_path)
            if thumb_path:
                created_files.append(thumb_path)

            attributes = []
            if duration or width or height:
                attributes.append(DocumentAttributeVideo(
                    duration=duration,
                    w=width,
                    h=height,
                    supports_streaming=True
                ))

            caption = (
                f"🎬 **TikTok Video** (ID: `{media_id}`)\n"
                f"🔗 [Link TikTok]({tiktok_url})"
            )

            await client.send_file(
                event.chat_id,
                file=video_path,
                caption=caption,
                thumb=thumb_path,
                attributes=attributes,
                reply_to=event.message.id
            )
            await status_msg.delete()

        # ----------------------------------------------------
        # KASUS 2: POSTINGAN FOTO / CAROUSEL
        # ----------------------------------------------------
        elif media_type == "photo" and data.get("images"):
            images = data["images"]
            total_img = len(images)
            await status_msg.edit(f"⬇ **Sedang mendownload {total_img} foto...**")

            img_paths = []
            for idx, img_url in enumerate(images, start=1):
                img_path = os.path.join(TEMP_DIR, f"{media_id}_{event.id}_{idx}.jpg")
                created_files.append(img_path)
                ok = await asyncio.to_thread(download_binary, img_url, img_path)
                if ok and os.path.exists(img_path) and os.path.getsize(img_path) > 0:
                    img_paths.append(img_path)

            if not img_paths:
                await status_msg.edit("❌ **Gagal mendownload gambar-gambar postingan.**")
                return

            await status_msg.edit(f"⬆ **Mengunggah {len(img_paths)} foto ke Telegram...**")

            caption = (
                f"📸 **TikTok Photo Album** ({len(img_paths)} Foto)\n"
                f"🔗 [Link TikTok]({tiktok_url})"
            )

            await client.send_file(
                event.chat_id,
                file=img_paths,
                caption=caption,
                reply_to=event.message.id
            )
            await status_msg.delete()

        else:
            await status_msg.edit("❌ **Format media TikTok tidak dikenali.**")

    except Exception as e:
        print(f"Error handling message: {e}")
        try:
            await status_msg.edit(f"❌ **Terjadi kesalahan:** {e}")
        except Exception:
            pass

    finally:
        for fpath in created_files:
            if os.path.exists(fpath):
                try:
                    os.remove(fpath)
                except Exception:
                    pass


# ==========================================
# MAIN ENTRYPOINT
# ==========================================
async def start_bot():
    # Jalankan HTTP Health Check Server untuk Koyeb / Cloud
    await start_healthcheck_server()

    # Jalankan Telegram Bot Client
    await client.start(bot_token=BOT_TOKEN)

    me = await client.get_me()
    print("=" * 45)
    print("🤖 TIKTOK, FACEBOOK & X DOWNLOADER TELEGRAM BOT")
    print(f"✅ Bot Berhasil Berjalan!")
    print(f"   Nama    : {me.first_name}")
    print(f"   Username: @{me.username}")
    print(f"   Bot ID  : {me.id}")
    cookie_file = get_facebook_cookie_file()
    print(f"   Cookie FB: {'ada (' + os.path.basename(cookie_file) + ')' if cookie_file else 'nggak ada'}")
    print("=" * 45)
    print("Menunggu pesan link TikTok / Facebook / X... (Ctrl+C untuk stop)")

    await client.run_until_disconnected()


def main():
    print("Menghubungkan ke Telegram...")
    client.loop.run_until_complete(start_bot())


if __name__ == '__main__':
    main()
