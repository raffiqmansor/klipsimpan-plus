"""KlipSimpan+ — muat turun video awam daripada pelbagai platform.

Pautan mesti HTTPS dan hosnya disenaraikan oleh pentadbir. Ini bukan proksi URL
umum. Tidak menerima cookies/log masuk, playlist, video ber-DRM atau video peribadi.
"""
from __future__ import annotations

import os
import re
import json
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import yt_dlp
from flask import Flask, abort, jsonify, render_template, request, send_file

app = Flask(__name__, static_folder="static", template_folder="templates")
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024

ROOT_DIR = Path(tempfile.gettempdir()) / "klipsimpan_plus_jobs"
ROOT_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
MAX_BYTES = int(os.environ.get("MAX_VIDEO_MB", "200")) * 1024 * 1024
MAX_DURATION = int(os.environ.get("MAX_VIDEO_MINUTES", "15")) * 60
TTL_SECONDS = 20 * 60
MAX_RUNNING = 2
JOBS: dict[str, dict] = {}
LOCK = threading.RLock()
RUNNING = threading.BoundedSemaphore(MAX_RUNNING)

# Padanan sempadan subdomain: foo.youtube.com diterima; youtube.com.evil.test ditolak.
# Tambahan hos sendiri (EXTRA_VIDEO_DOMAINS) ditetapkan di *pelayan*, bukan oleh pengguna.
PLATFORMS: dict[str, tuple[str, ...]] = {
    "YouTube": ("youtube.com", "youtu.be", "youtube-nocookie.com"),
    "TikTok": ("tiktok.com", "tiktokv.com"),
    "Instagram": ("instagram.com", "instagr.am"),
    "Facebook": ("facebook.com", "fb.watch"),
    "X / Twitter": ("x.com", "twitter.com"),
    "Reddit": ("reddit.com", "redd.it", "v.redd.it"),
    "Vimeo": ("vimeo.com",),
    "Dailymotion": ("dailymotion.com", "dai.ly"),
    "Pinterest": ("pinterest.com", "pin.it"),
    "Twitch": ("twitch.tv", "clips.twitch.tv"),
    "Threads": ("threads.net", "threads.com"),
    "Bilibili": ("bilibili.com", "b23.tv"),
    "Streamable": ("streamable.com",),
    "Snapchat": ("snapchat.com",),
    "Bluesky": ("bsky.app",),
    "Tumblr": ("tumblr.com",),
    "LinkedIn": ("linkedin.com",),
    "Internet Archive": ("archive.org",),
}


def _extra_domains() -> tuple[str, ...]:
    domains: list[str] = []
    for candidate in os.environ.get("EXTRA_VIDEO_DOMAINS", "").split(","):
        domain = candidate.strip().lower().rstrip(".")
        if re.fullmatch(r"(?:[a-z0-9-]+\.)+[a-z]{2,63}", domain):
            domains.append(domain)
    return tuple(domains)


def platform_for(host: str) -> str | None:
    for name, domains in PLATFORMS.items():
        if any(host == domain or host.endswith("." + domain) for domain in domains):
            return name
    if any(host == domain or host.endswith("." + domain) for domain in _extra_domains()):
        return "Laman tambahan"
    return None


def validate_url(raw: object) -> str:
    """Tolak alamat dalaman, format URL kabur dan domain luar senarai."""
    if not isinstance(raw, str) or not raw.strip() or len(raw) > 2000:
        raise ValueError("Masukkan pautan video yang sah (maksimum 2,000 aksara).")
    raw = raw.strip()
    if any(ord(c) < 32 or ord(c) == 127 for c in raw) or "\\" in raw:
        raise ValueError("Pautan mengandungi aksara yang tidak dibenarkan.")
    try:
        parts = urlsplit(raw)
        port = parts.port
        host = (parts.hostname or "").lower().rstrip(".")
    except ValueError as exc:
        raise ValueError("Format pautan tidak sah.") from exc
    if parts.scheme != "https" or parts.username or parts.password or port is not None:
        raise ValueError("Gunakan pautan HTTPS biasa tanpa nama pengguna atau port khas.")
    if not re.fullmatch(r"[a-z0-9.-]+", host) or ".." in host:
        raise ValueError("Nama domain tidak sah.")
    if platform_for(host) is None:
        raise ValueError("Laman ini belum tersenarai. Platform yang disokong dipaparkan di bawah ruang pautan.")
    if not parts.path and not parts.query:
        raise ValueError("Masukkan pautan video khusus, bukan halaman utama platform.")
    # Parameter sauh (#...) tidak diperlukan untuk pengekstrakan.
    return raw


def friendly_error(exc: BaseException) -> str:
    msg = str(exc).lower()
    if any(x in msg for x in ("drm", "protected content", "widevine")):
        return "Video dilindungi DRM. Aplikasi tidak boleh memintas perlindungan tersebut."
    if any(x in msg for x in ("login", "log in", "private", "sign in", "permission", "cookies", "members only")):
        return "Video mungkin peribadi, terhad atau memerlukan log masuk. Hanya video yang dapat diakses tanpa log masuk disokong."
    if any(x in msg for x in ("unsupported url", "no video formats", "no formats found", "cannot parse data")):
        return "Pautan ini belum dapat diekstrak. Cuba pautan terus ke video atau platform lain yang disokong."
    if "requested format is not available" in msg:
        return "Tiada format video yang boleh diperoleh untuk pautan ini."
    if any(x in msg for x in ("timed out", "timeout", "network is unreachable")):
        return "Pelayan sumber tidak memberikan respons. Sila cuba lagi kemudian."
    return "Video gagal diproses. Periksa pautan, akses awam dan cuba lagi."


def ydl_options(**overrides: object) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "ignoreerrors": False,
        "socket_timeout": 20,
        "retries": 2,
        "fragment_retries": 2,
        "extractor_retries": 1,
        "concurrent_fragment_downloads": 1,
        "format": "bv[ext=mp4]+ba[ext=m4a]/bv+ba/b[ext=mp4]/b",
        "format_sort": ["res:720", "+codec:avc:m4a", "ext:mp4:m4a"],
        "merge_output_format": "mp4",
        "max_filesize": MAX_BYTES,
        "cachedir": False,
        "extract_flat": False,
    }
    opts.update(overrides)
    return opts


def enforce_meta(info: dict) -> None:
    if not isinstance(info, dict) or info.get("_type") in {"playlist", "multi_video"} or info.get("entries"):
        raise ValueError("Hanya satu video boleh dimuat turun. Pautan playlist tidak disokong.")
    if info.get("is_live"):
        raise ValueError("Strim langsung tidak disokong. Gunakan pautan rakaman selepas siaran tamat.")
    duration = info.get("duration")
    try:
        if duration is not None and float(duration) > MAX_DURATION:
            raise ValueError(f"Had panjang video ialah {MAX_DURATION // 60} minit.")
    except (TypeError, OverflowError):
        pass
    for key in ("filesize", "filesize_approx"):
        try:
            if info.get(key) is not None and int(info[key]) > MAX_BYTES:
                raise ValueError(f"Saiz video melebihi had {MAX_BYTES // 1024**2} MB.")
        except (TypeError, OverflowError):
            pass


def cleanup_expired() -> None:
    now = time.monotonic()
    with LOCK:
        expired = [jid for jid, j in JOBS.items() if j["status"] in ("ready", "error") and now - j["updated"] > TTL_SECONDS]
        dirs = [Path(JOBS[jid]["directory"]) for jid in expired]
        for jid in expired:
            del JOBS[jid]
    for directory in dirs:
        shutil.rmtree(directory, ignore_errors=True)


def update_job(jobid: str, **values: object) -> None:
    with LOCK:
        if jobid in JOBS:
            JOBS[jobid].update(values, updated=time.monotonic())


def safe_filename(title: object, jobid: str) -> str:
    name = re.sub(r"[^\w\s-]", "", str(title or ""), flags=re.UNICODE)
    name = re.sub(r"\s+", " ", name).strip(" ._-")[:75]
    return (name or f"KlipSimpan_{jobid[:8]}") + ".mp4"


def probe_streams(path: Path) -> tuple[dict, dict]:
    """Pastikan MP4 mempunyai aliran gambar dan suara yang sebenar.

    Sambungan .mp4 tidak membuktikan fail mengandungi video. ffprobe
    menyemak codec, dimensi dan kewujudan aliran audio yang boleh dimainkan.
    """
    try:
        process = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "stream=codec_type,codec_name,pix_fmt,width,height", "-of", "json", str(path)],
            capture_output=True, text=True, check=True, timeout=25,
        )
        streams = json.loads(process.stdout).get("streams", [])
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError, ValueError) as exc:
        raise ValueError("Fail yang dimuat turun tidak dapat disahkan sebagai video yang sah.") from exc
    video = next((stream for stream in streams
                  if stream.get("codec_type") == "video"
                  and int(stream.get("width") or 0) > 0
                  and int(stream.get("height") or 0) > 0), None)
    audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
    if video is None:
        raise ValueError("Sumber hanya mempunyai audio tanpa gambar video. Tiada MP4 dimuat turun.")
    if audio is None:
        raise ValueError("Sumber video tidak mempunyai audio. Muat turun dibatalkan untuk mengelakkan fail tidak lengkap.")
    return video, audio


def ensure_mp4(job_dir: Path) -> Path:
    """Terima hanya fail lengkap; kod semula kepada format serasi iPad jika perlu.

    Utamakan output akhir `video.<ext>` yang dihasilkan selepas merge.
    Jangan memilih fail terbesar daripada fail perantaraan video/audio.
    """
    completed = [job_dir / f"video.{ext}" for ext in ("mp4", "mkv", "webm", "mov", "ts")]
    original = next((p for p in completed if p.is_file()), None)
    if original is None:
        raise ValueError("Video dan audio belum berjaya digabungkan. Cuba pautan lain.")
    video, audio = probe_streams(original)
    is_h264_compatible = video.get("codec_name") == "h264" and video.get("pix_fmt") == "yuv420p"
    is_aac = audio.get("codec_name") == "aac"
    if original.suffix.lower() == ".mp4" and is_h264_compatible and is_aac:
        return original

    target = job_dir / "klip_terhasil.mp4"
    video_codec = ["-c:v", "copy"] if is_h264_compatible else [
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "25",
        "-pix_fmt", "yuv420p", "-profile:v", "main",
    ]
    audio_codec = ["-c:a", "copy"] if is_aac else [
        "-c:a", "aac", "-b:a", "128k", "-ac", "2",
    ]
    try:
        subprocess.run([
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(original), "-map", "0:v:0", "-map", "0:a:0",
            *video_codec, *audio_codec,
            "-movflags", "+faststart", "-fs", str(MAX_BYTES + 1), str(target),
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=420)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        raise ValueError("Penukaran video ke MP4 serasi iPad gagal. Cuba video yang lebih pendek.") from exc
    if not target.is_file():
        raise ValueError("Fail MP4 tidak dapat disediakan.")
    output_video, output_audio = probe_streams(target)
    if output_video.get("codec_name") != "h264" or output_audio.get("codec_name") != "aac":
        raise ValueError("Format akhir tidak serasi dengan iPad. Fail tidak diedarkan.")
    original.unlink(missing_ok=True)
    return target


def download_worker(jobid: str, url: str) -> None:
    with LOCK:
        job_dir = Path(JOBS[jobid]["directory"])
    try:
        update_job(jobid, status="processing", message="Mendapatkan maklumat video…", progress=0)

        def progress_hook(progress: dict) -> None:
            if progress.get("status") == "downloading":
                done = int(progress.get("downloaded_bytes") or 0)
                if done > MAX_BYTES:
                    raise ValueError(f"Had muat turun ialah {MAX_BYTES // 1024**2} MB.")
                total = progress.get("total_bytes") or progress.get("total_bytes_estimate") or 0
                percent = min(96, round(done / total * 96)) if total else 0
                update_job(jobid, status="downloading", message="Video sedang dimuat turun…", progress=percent)
            elif progress.get("status") == "finished":
                update_job(jobid, status="processing", message="Menggabungkan video dan audio…", progress=97)

        opts = ydl_options(outtmpl=str(job_dir / "video.%(ext)s"), progress_hooks=[progress_hook], paths={"home": str(job_dir)})
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            enforce_meta(info)
            update_job(jobid, title=info.get("title") or "Video", platform=platform_for((urlsplit(url).hostname or "").lower()))
            ydl.download([url])
        update_job(jobid, status="processing", message="Mengesahkan gambar, suara dan keserasian iPad…", progress=98)
        output = ensure_mp4(job_dir)
        if output.stat().st_size > MAX_BYTES:
            raise ValueError(f"Fail melebihi had {MAX_BYTES // 1024**2} MB.")
        if output.stat().st_size == 0:
            raise ValueError("Fail video kosong.")
        with LOCK:
            title = JOBS[jobid].get("title")
        update_job(jobid, status="ready", message="MP4 disahkan mengandungi gambar dan suara.", progress=100,
                   filename=safe_filename(title, jobid), file=str(output), size=output.stat().st_size)
    except Exception as exc:
        error = str(exc) if isinstance(exc, ValueError) else friendly_error(exc)
        update_job(jobid, status="error", message=error, progress=0)
        shutil.rmtree(job_dir, ignore_errors=True)
    finally:
        RUNNING.release()


@app.before_request
def housekeeping():
    cleanup_expired()


def embed_allowed_origins() -> str:
    """Allow Google Sites to embed only the application page.

    Optional additional *exact HTTPS origins* can be set through the Render
    environment variable EMBED_ALLOWED_ORIGINS, separated by spaces or commas.
    Paths, ports, wildcards and malformed origins are intentionally rejected.
    """
    origins = [
        "'self'",
        "https://sites.google.com",
        "https://*.google.com",
        "https://*.googleusercontent.com",
    ]
    for origin in re.split(r"[,\s]+", os.environ.get("EMBED_ALLOWED_ORIGINS", "")):
        origin = origin.strip().lower()
        if re.fullmatch(r"https://(?:[a-z0-9-]+\.)+[a-z]{2,63}", origin) and origin not in origins:
            origins.append(origin)
    return " ".join(origins)


@app.after_request
def headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    # An X-Frame-Options: DENY header blocked embedding inside Google Sites.
    # Keep anti-clickjacking protection for other routes, but allow trusted
    # Google Sites ancestors through CSP for the HTML application page only.
    home_page = request.path == "/"
    if home_page:
        response.headers.pop("X-Frame-Options", None)
    else:
        response.headers["X-Frame-Options"] = "DENY"
    policy = (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' https: data:; "
        "connect-src 'self'; base-uri 'none'; form-action 'self'"
    )
    response.headers["Content-Security-Policy"] = (
        policy + "; frame-ancestors " +
        (embed_allowed_origins() if home_page else "'none'")
    )
    response.headers["Cache-Control"] = (
        "no-store" if home_page or request.path.startswith("/api/")
        else "public, max-age=300"
    )
    return response


@app.get("/")
def home():
    return render_template("index.html", max_mb=MAX_BYTES // 1024**2, max_min=MAX_DURATION // 60)


@app.get("/health")
def health():
    return jsonify(status="ok")


@app.get("/manifest.webmanifest")
def manifest():
    return send_file("static/manifest.webmanifest", mimetype="application/manifest+json")


@app.get("/service-worker.js")
def service_worker():
    response = send_file("static/service-worker.js", mimetype="text/javascript")
    response.headers["Service-Worker-Allowed"] = "/"
    return response


@app.get("/api/platforms")
def platforms():
    return jsonify(ok=True, platforms=list(PLATFORMS))


@app.post("/api/info")
def info():
    data = request.get_json(silent=True) or {}
    try:
        url = validate_url(data.get("url"))
        with yt_dlp.YoutubeDL(ydl_options(skip_download=True)) as ydl:
            details = ydl.extract_info(url, download=False)
        enforce_meta(details)
        platform = platform_for((urlsplit(url).hostname or "").lower())
        return jsonify(ok=True, data={
            "title": str(details.get("title") or "Video")[:180],
            "thumbnail": str(details.get("thumbnail") or "")[:2000],
            "duration": details.get("duration"),
            "uploader": str(details.get("uploader") or "")[:90],
            "platform": platform,
        })
    except ValueError as exc:
        return jsonify(ok=False, error=str(exc)), 400
    except Exception as exc:
        return jsonify(ok=False, error=friendly_error(exc)), 422


@app.post("/api/download")
def start_download():
    data = request.get_json(silent=True) or {}
    try:
        url = validate_url(data.get("url"))
    except ValueError as exc:
        return jsonify(ok=False, error=str(exc)), 400
    if not RUNNING.acquire(blocking=False):
        return jsonify(ok=False, error="Pelayan sedang memproses dua video. Cuba lagi sebentar."), 429
    jobid = uuid.uuid4().hex
    folder = ROOT_DIR / jobid
    try:
        folder.mkdir(mode=0o700)
        with LOCK:
            JOBS[jobid] = {
                "status": "processing", "message": "Permintaan diterima…", "progress": 0,
                "updated": time.monotonic(), "directory": str(folder),
            }
        threading.Thread(target=download_worker, args=(jobid, url), daemon=True).start()
    except Exception:
        RUNNING.release()
        shutil.rmtree(folder, ignore_errors=True)
        return jsonify(ok=False, error="Gagal memulakan muat turun."), 500
    return jsonify(ok=True, job_id=jobid), 202


@app.get("/api/status/<jobid>")
def status(jobid: str):
    if not re.fullmatch(r"[a-f0-9]{32}", jobid):
        abort(404)
    with LOCK:
        job = JOBS.get(jobid)
        if job is None:
            abort(404)
        view = {k: job.get(k) for k in ("status", "message", "progress", "title", "size", "platform")}
    view["download_url"] = f"/api/file/{jobid}" if view["status"] == "ready" else None
    return jsonify(ok=True, **view)


@app.get("/api/file/<jobid>")
def download_file(jobid: str):
    if not re.fullmatch(r"[a-f0-9]{32}", jobid):
        abort(404)
    with LOCK:
        job = JOBS.get(jobid)
        if not job or job.get("status") != "ready":
            abort(404)
        filename = job["filename"]
        path = job["file"]
    if not Path(path).is_file():
        abort(404)
    return send_file(path, as_attachment=True, download_name=filename,
                     mimetype="video/mp4", conditional=True)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")), debug=False)
