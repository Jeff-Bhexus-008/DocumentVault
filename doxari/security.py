"""Security helpers: sessions, CSRF, validation, rate limiting."""

import functools
import os
import re
import secrets
import threading
import time
import unicodedata

from flask import g, jsonify, redirect, request, session, url_for

from .config import (
    ALLOWED_EXTENSIONS,
    FILE_TYPES,
    MAX_UPLOAD_MB,
    SECRET_KEY,
)


# ---------------------------------------------------------------- auth
def current_user():
    user = getattr(g, "user", None)
    if user:
        return user
    if "user_id" not in session:
        return None
    user = {
        "id": session.get("user_id"),
        "email": session.get("user_email") or "",
        "name": session.get("user_name") or "",
        "full_name": session.get("user_name") or "",
    }
    g.user = user
    return user


def display_name(user):
    if not user:
        return "there"
    name = (user.get("full_name") or user.get("name") or "").strip()
    if name:
        return name.split()[0]
    email = user.get("email") or ""
    if "@" in email:
        local = email.split("@")[0]
        local = re.sub(r"[._\-]+", " ", local).strip()
        if local:
            return local.split()[0].capitalize()
    return "there"


def initials(user):
    if not user:
        return "D"
    name = (user.get("full_name") or "").strip()
    if name:
        parts = [p for p in name.split() if p]
        return "".join(p[0].upper() for p in parts[:2])
    email = user.get("email") or ""
    return (email[0].upper() if email else "D")


def wants_json():
    if request.path.startswith("/api/"):
        return True
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return True
    accept = request.headers.get("Accept", "")
    return "application/json" in accept and "text/html" not in accept


def login_required(view):
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        if not current_user():
            if wants_json():
                return jsonify({"ok": False, "error": "Please sign in to continue."}), 401
            return redirect(url_for("auth.login", next=request.full_path.rstrip("?")))
        return view(*args, **kwargs)

    return wrapper


def owner_required(view):
    """Same as login_required but guarantees a usable user id downstream."""
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        user = current_user()
        if not user or not user.get("id"):
            if wants_json():
                return jsonify({"ok": False, "error": "Please sign in to continue."}), 401
            return redirect(url_for("auth.login"))
        return view(*args, **kwargs)

    return wrapper


# ---------------------------------------------------------------- CSRF
CSRF_SESSION_KEY = "_csrf"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}
CSRF_EXEMPT = {"healthz", "public.download_shared_link"}


def csrf_token():
    token = session.get(CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        session[CSRF_SESSION_KEY] = token
    return token


def csrf_valid():
    expected = session.get(CSRF_SESSION_KEY)
    if not expected:
        return False
    supplied = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
    if not supplied and request.is_json:
        supplied = (request.get_json(silent=True) or {}).get("csrf_token")
    if not supplied:
        return False
    return secrets.compare_digest(str(supplied), str(expected))


def protect_from_csrf(app):
    @app.before_request
    def _csrf_guard():
        if request.method in SAFE_METHODS:
            return None
        if request.endpoint in CSRF_EXEMPT:
            return None
        if current_user() is None:
            return None  # auth routes are protected by rate limiting
        if not csrf_valid():
            if wants_json():
                return jsonify({"ok": False, "error": "Your session expired. Refresh the page and try again."}), 400
            return (
                "Your session expired. Please go back, refresh the page and try again.",
                400,
            )
        return None


# ---------------------------------------------------------------- rate limit
class RateLimiter:
    """Small in-process limiter for sensitive endpoints (login, reset)."""

    def __init__(self, attempts=8, window=900):
        self.attempts = attempts
        self.window = window
        self._hits = {}
        self._lock = threading.Lock()

    def _key(self, identity):
        return f"{identity}|{request.remote_addr}"

    def blocked(self, identity):
        with self._lock:
            now = time.time()
            hits = [t for t in self._hits.get(self._key(identity), []) if now - t < self.window]
            self._hits[self._key(identity)] = hits
            return len(hits) >= self.attempts

    def hit(self, identity):
        with self._lock:
            self._hits.setdefault(self._key(identity), []).append(time.time())

    def reset(self, identity):
        with self._lock:
            self._hits.pop(self._key(identity), None)

    def remaining(self, identity):
        with self._lock:
            now = time.time()
            hits = [t for t in self._hits.get(self._key(identity), []) if now - t < self.window]
            return max(0, self.attempts - len(hits))


login_limiter = RateLimiter(attempts=10, window=900)
reset_limiter = RateLimiter(attempts=5, window=900)


# ---------------------------------------------------------------- filenames
UNSAFE_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def sanitize_filename(name):
    """Keep the user's real filename while stripping anything dangerous."""
    name = unicodedata.normalize("NFC", name or "")
    name = name.replace("\x00", "")
    name = os.path.basename(name.replace("\\", "/"))
    name = UNSAFE_CHARS.sub("-", name).strip().strip(".")
    name = re.sub(r"\s+", " ", name)
    if not name:
        return ""
    if len(name) > 150:
        stem, dot, ext = name.rpartition(".")
        if dot and len(ext) <= 8:
            name = stem[: 150 - len(ext) - 1] + "." + ext
        else:
            name = name[:150]
    return name


def extension(filename):
    if "." not in (filename or ""):
        return ""
    return filename.rsplit(".", 1)[1].lower()


MAGIC = {
    "pdf": [b"%PDF"],
    "png": [b"\x89PNG\r\n\x1a\n"],
    "jpg": [b"\xff\xd8\xff"],
    "jpeg": [b"\xff\xd8\xff"],
    "gif": [b"GIF87a", b"GIF89a"],
    "zip": [b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"],
    "docx": [b"PK\x03\x04"],
    "xlsx": [b"PK\x03\x04"],
    "pptx": [b"PK\x03\x04"],
    "doc": [b"\xd0\xcf\x11\xe0"],
    "xls": [b"\xd0\xcf\x11\xe0"],
    "ppt": [b"\xd0\xcf\x11\xe0"],
}


def validate_upload(file_storage, declared_name=None):
    """Validate a single upload on the server before it is stored.

    Returns (ok, error_message, filename, extension).
    """
    filename = sanitize_filename(declared_name or file_storage.filename or "")
    if not filename:
        return False, "That file needs a name.", None, None

    ext = extension(filename)
    if ext not in ALLOWED_EXTENSIONS:
        allowed = ", ".join(sorted(ext.upper() for ext in ALLOWED_EXTENSIONS))
        return False, f"{ext.upper() or 'That'} files are not supported. Allowed: {allowed}.", filename, ext

    head = file_storage.stream.read(512)
    file_storage.stream.seek(0)
    if not head:
        return False, f"“{filename}” is empty.", filename, ext

    signatures = MAGIC.get(ext)
    if signatures and not any(head.startswith(sig) for sig in signatures):
        label = FILE_TYPES.get(ext, ("", ext.upper()))[1]
        return (
            False,
            f"“{filename}” does not look like a valid {label} file. It may be corrupted or renamed.",
            filename,
            ext,
        )

    # Size: werkzeug stores the real length on the stream for form uploads.
    stream = file_storage.stream
    try:
        position = stream.tell()
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        stream.seek(position)
    except Exception:
        size = 0
    if size > MAX_UPLOAD_MB * 1024 * 1024:
        return False, f"“{filename}” is larger than the {MAX_UPLOAD_MB} MB limit.", filename, ext

    return True, None, filename, ext


def safe_next_url(target, fallback):
    if not target:
        return fallback
    target = str(target)
    if not target.startswith("/") or target.startswith("//") or "\\" in target:
        return fallback
    return target
