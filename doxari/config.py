"""Central configuration for Doxari.

Everything the app needs is read from the environment so the existing Render
deployment keeps working with the same variables it already has:
SUPABASE_URL, SUPABASE_KEY, SUPABASE_SECRET_KEY, FLASK_SECRET_KEY.
"""

import hashlib
import os

from dotenv import load_dotenv

load_dotenv()


def _env(name, default=None):
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _int_env(name, default):
    try:
        return int(_env(name, str(default)))
    except (TypeError, ValueError):
        return default


def _bool_env(name, default=False):
    value = _env(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------- Supabase
SUPABASE_URL = (_env("SUPABASE_URL") or "").rstrip("/")
SUPABASE_KEY = _env("SUPABASE_KEY") or ""
SUPABASE_SECRET_KEY = _env("SUPABASE_SECRET_KEY") or ""
BUCKET_NAME = _env("SUPABASE_BUCKET", "document")


def supabase_configured():
    return bool(SUPABASE_URL and SUPABASE_KEY)


def server_configured():
    return bool(SUPABASE_URL and SUPABASE_SECRET_KEY)


# ---------------------------------------------------------------- Flask
def _secret_key():
    key = _env("FLASK_SECRET_KEY")
    if key:
        return key
    # Stable fallback so a missing env var does not silently log every user
    # out on each restart, without ever writing a secret to disk.
    seed = f"{SUPABASE_URL}|{SUPABASE_SECRET_KEY}|doxari-session"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


SECRET_KEY = _secret_key()
COOKIE_SECURE = _bool_env("COOKIE_SECURE", False)
SESSION_DAYS = _int_env("SESSION_DAYS", 14)


# ---------------------------------------------------------------- Uploads
MAX_UPLOAD_MB = _int_env("MAX_UPLOAD_MB", 25)
MAX_CONTENT_LENGTH = MAX_UPLOAD_MB * 1024 * 1024
MAX_BULK_FILES = _int_env("MAX_BULK_FILES", 20)
SIGNED_URL_SECONDS = _int_env("SIGNED_URL_SECONDS", 300)

# extension -> (mime type, friendly label, group)
FILE_TYPES = {
    "pdf": ("application/pdf", "PDF", "pdf"),
    "doc": ("application/msword", "Word", "document"),
    "docx": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "Word",
        "document",
    ),
    "xls": ("application/vnd.ms-excel", "Excel", "spreadsheet"),
    "xlsx": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "Excel",
        "spreadsheet",
    ),
    "csv": ("text/csv", "CSV", "spreadsheet"),
    "ppt": ("application/vnd.ms-powerpoint", "PowerPoint", "presentation"),
    "pptx": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "PowerPoint",
        "presentation",
    ),
    "txt": ("text/plain", "Text", "text"),
    "md": ("text/markdown", "Markdown", "text"),
    "json": ("application/json", "JSON", "text"),
    "jpg": ("image/jpeg", "Image", "image"),
    "jpeg": ("image/jpeg", "Image", "image"),
    "png": ("image/png", "Image", "image"),
    "webp": ("image/webp", "Image", "image"),
    "gif": ("image/gif", "Image", "image"),
    "zip": ("application/zip", "Archive", "archive"),
}

ALLOWED_EXTENSIONS = set(FILE_TYPES)

# Groups used by filters and analytics.
FILE_GROUPS = {
    "pdf": "PDFs",
    "image": "Images",
    "document": "Documents",
    "spreadsheet": "Spreadsheets",
    "presentation": "Presentations",
    "text": "Text files",
    "archive": "Archives",
}

PREVIEWABLE_INLINE = {"pdf", "txt", "md", "csv", "json"} | {
    ext for ext, (_, _, group) in FILE_TYPES.items() if group == "image"
}
# Office formats need a converter/third-party viewer; we show info instead of
# pretending to render them.
OFFICE_EXTENSIONS = {"doc", "docx", "xls", "xlsx", "ppt", "pptx"}

DEFAULT_CATEGORIES = [
    {"name": "School", "icon": "cap", "color": "#166534"},
    {"name": "Work", "icon": "briefcase", "color": "#0F766E"},
    {"name": "Personal", "icon": "user", "color": "#22C55E"},
    {"name": "Certificates", "icon": "award", "color": "#B45309"},
    {"name": "Important", "icon": "star", "color": "#B91C1C"},
]

FALLBACK_CATEGORY = "General"

# ---------------------------------------------------------------- Storage
STORAGE_LIMIT_MB = _int_env("STORAGE_LIMIT_MB", 0)  # 0 = not configured

# ---------------------------------------------------------------- AI
AI_PROVIDER = (_env("AI_PROVIDER") or "").lower()
AI_API_KEY = _env("AI_API_KEY") or ""
AI_MODEL = _env("AI_MODEL") or ""
AI_BASE_URL = (_env("AI_BASE_URL") or "").rstrip("/")
AI_MAX_CHARS = _int_env("AI_MAX_CHARS", 12000)

AI_PRESETS = {
    "openai": ("https://api.openai.com/v1", "gpt-4o-mini", "OpenAI"),
    "groq": ("https://api.groq.com/openai/v1", "llama-3.3-70b-versatile", "Groq"),
    "openrouter": ("https://openrouter.ai/api/v1", "openai/gpt-4o-mini", "OpenRouter"),
    "together": ("https://api.together.xyz/v1", "meta-llama/Llama-3.3-70B-Instruct-Turbo", "Together AI"),
    "deepseek": ("https://api.deepseek.com/v1", "deepseek-chat", "DeepSeek"),
}


def ai_settings():
    """Resolve the AI integration from env, or return None when not configured."""
    if not AI_API_KEY:
        return None
    provider = AI_PROVIDER or "openai"
    base, model, label = AI_PRESETS.get(
        provider, (AI_BASE_URL or "", AI_MODEL or "", provider.title())
    )
    base = AI_BASE_URL or base
    if not base:
        return None
    return {
        "provider": provider,
        "label": label,
        "base_url": base,
        "model": AI_MODEL or model,
        "api_key": AI_API_KEY,
    }


# ---------------------------------------------------------------- Misc
BRAND = {
    "name": "Doxari",
    "tagline": "Your documents, organized and secure.",
}

PUBLIC_BASE_URL = (_env("PUBLIC_BASE_URL") or "").rstrip("/")
