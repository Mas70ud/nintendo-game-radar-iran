#!/usr/bin/env python3
"""Persistent, URL-keyed cache for catalog game cover images."""
import hashlib
import mimetypes
import threading
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

CACHE_DIR = Path(__file__).resolve().parent / "data" / "image_cache"
MAX_IMAGE_BYTES = 20 * 1024 * 1024
TIMEOUT = 30
ALLOWED_HOSTS = {
    "psparsi.ir", "s3.techsiro.com", "pspro.ir", "dreamkala.com",
    "nakhlmarket.com", "westgamestore.ir", "igamer.ir", "rosegamers.com",
    "progame.ir",
}
MIME_EXTENSIONS = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
    "image/gif": ".gif", "image/avif": ".avif", "image/bmp": ".bmp",
}
_locks_guard = threading.Lock()
_url_locks = {}


def _key_lock(key):
    with _locks_guard:
        return _url_locks.setdefault(key, threading.Lock())


def _validate_url(url):
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    if parsed.scheme not in {"http", "https"} or host not in ALLOWED_HOSTS:
        raise ValueError("Image URL is not from a supported game store")


def _image_signature(content):
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if content.startswith(b"BM"):
        return "image/bmp"
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    if len(content) >= 12 and content[4:8] == b"ftyp" and content[8:12] in {b"avif", b"avis"}:
        return "image/avif"
    return ""


def get_cached_image(url):
    """Return cached image path and MIME type, downloading only on first use."""
    _validate_url(url)
    key = hashlib.sha256(url.encode("utf-8")).hexdigest()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    lock = _key_lock(key)
    with lock:
        known_extensions = set(MIME_EXTENSIONS.values())
        existing = next((path for path in CACHE_DIR.glob(f"{key}.*")
                         if path.suffix in known_extensions), None)
        if existing:
            mime = next((kind for kind, ext in MIME_EXTENSIONS.items() if ext == existing.suffix), "application/octet-stream")
            return existing, mime

        request = Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36",
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        })
        with urlopen(request, timeout=TIMEOUT) as response:
            _validate_url(response.geturl())
            mime = response.headers.get_content_type().lower()
            content = response.read(MAX_IMAGE_BYTES + 1)
        if len(content) > MAX_IMAGE_BYTES:
            raise ValueError("Image exceeds the 20 MB cache limit")
        if not content:
            raise ValueError("Image response was empty")
        actual_mime = _image_signature(content)
        if not actual_mime:
            raise ValueError(f"Response is not a supported raster image (content type: {mime})")
        if mime not in MIME_EXTENSIONS:
            guessed, _ = mimetypes.guess_type(urlparse(url).path)
            mime = guessed if guessed == actual_mime else actual_mime
        elif mime != actual_mime:
            mime = actual_mime

        destination = CACHE_DIR / f"{key}{MIME_EXTENSIONS[mime]}"
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        temporary.write_bytes(content)
        temporary.replace(destination)
        return destination, mime
