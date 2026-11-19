# Botik — TikTok & Facebook Downloader Bot

Bot Telegram buat ngunduh video/foto TikTok sama video Facebook/Reels, terus dikirim balik ke chat tanpa watermark. Jalan di Koyeb/Railway/Render (ada HTTP health check bawaan).

## Fitur

| Sumber | Yang didukung | Cara kerja |
|---|---|---|
| TikTok | Video tanpa watermark, foto/carousel | scrape endpoint web TikTok (`fetch_tiktok_media_info`) |
| Facebook | Reels, video, link share (`/share/r/...`, `/share/v/...`), `fb.watch` | scrape [getmyfb.com](https://getmyfb.com) → link `ssscdn.io` (HD 720p, fallback SD 360p); kalau getmyfb diblok (sering di IP datacenter), otomatis fallback ke **yt-dlp** |
| Facebook foto | Postingan foto tunggal & carousel (`/share/p/...`) | getmyfb cuma kasih placeholder, jadi foto diambil langsung dari HTML postingan (og:image + semua `scontent.../t39.30808-6/`, param `ctp` dibuang biar resolusi asli) |

- Deteksi link otomatis dari teks pesan (boleh ada teks lain di sekitarnya).
- Link profil Facebook diabaikan biar bot nggak nyahut di grup.
- Video dikirim pakai metadata durasi/resolusi + thumbnail hasil `ffmpeg`.
- File temp selalu dihapus setelah upload (sukses maupun gagal).

## Struktur

```
bot.py            # semua logika: parser TikTok, parser Facebook, handler Telegram, health check
Dockerfile        # python:3.12-slim + ffmpeg + mediainfo
requirements.txt  # requests, tqdm, pymediainfo, telethon, aiohttp
```

## Environment variable

| Nama | Wajib | Default |
|---|---|---|
| `BOT_TOKEN` | ya | — (token dari @BotFather) |
| `API_ID` | ya | — |
| `API_HASH` | ya | — |
| `PORT` | tidak | `8000` |

Jangan commit nilai aslinya. Kalau jalan di Koyeb, isi lewat tab **Environment**.

## Jalanin lokal

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt

# ffmpeg + mediainfo harus ada di PATH buat thumbnail & metadata
set BOT_TOKEN=... & set API_ID=... & set API_HASH=...
python bot.py
```

Health check: `http://localhost:8000/health`.

## Deploy (Docker / Koyeb)

```bash
docker build -t botik .
docker run --rm -p 8000:8000 -e BOT_TOKEN=... -e API_ID=... -e API_HASH=... botik
```

Di Koyeb: deploy dari repo, set port `8000` sebagai HTTP port, isi env var di atas. `.session` bot dibuat otomatis di dalam container (`bot_session.session`).

## Deploy ke Railway

`railway.toml` sudah disiapkan (builder Dockerfile + healthcheck `/health`), jadi tinggal:

```bash
npm i -g @railway/cli
railway login
railway init
railway up
```

Di dashboard Railway: **Variables** → `BOT_TOKEN`, `API_ID`, `API_HASH`; **Settings → Networking → Generate Domain** dengan target port `8000`. Railway nyuntik env `PORT` sendiri dan bot sudah baca variabel itu.

Soal biaya: plan **Free** cuma kasih $1 kredit/bulan (RAM maks 0.5 GB per service). Bot yang nyala 24 jam bakal habis kredit dalam beberapa hari, jadi praktis butuh plan **Hobby $5/bulan**. Untuk bot always-on, Koyeb free lebih hemat.

## Catatan perawatan

- **getmyfb.com** itu situs pihak ketiga: kalau markup hasil `/process` berubah, sesuaikan `parse_getmyfb_html()`. Endpoint-nya `POST https://getmyfb.com/process` dengan form `id=<url>&locale=en` + header `HX-Request: true`; hasil sukses ditandai header `hx-trigger: resultsuccess`, gagal `resultfail`/`resultisprivate`.
- Kalau getmyfb balas challenge Cloudflare (sering dari IP datacenter: Railway, VPS luar), bot otomatis lanjut ke `download_facebook_via_ytdlp()`. Kegagalan getmyfb dicatat di log: `getmyfb gagal: status=... trigger=... body='...'`.
- Link download `ssscdn.io` cuma valid sementara (token base64) — download harus langsung setelah scrape.
- TikTok scraping gampang kena rate limit kalau dipakai rame-rame.
