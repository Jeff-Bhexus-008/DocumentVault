"""Doxari application factory."""

import logging
import threading
import time

from flask import Flask, g, jsonify, render_template, request, session

from . import ai
from .config import (
    BRAND,
    BUCKET_NAME,
    DEFAULT_CATEGORIES,
    FILE_GROUPS,
    MAX_UPLOAD_MB,
    SECRET_KEY,
    SESSION_DAYS,
    STORAGE_LIMIT_MB,
    COOKIE_SECURE,
    SUPABASE_KEY,
    SUPABASE_SECRET_KEY,
    SUPABASE_URL,
    supabase_configured,
)
from .models import FALLBACK_CATEGORY, Repository, human_size, display_date, display_datetime, relative_time
from .security import (
    csrf_token,
    current_user,
    display_name,
    initials,
    protect_from_csrf,
)
from .services import DocumentService
from .supabase_api import Schema, SupabaseAPI

log = logging.getLogger(__name__)


class CountCache:
    """Short-lived cache for sidebar counters (keeps page loads to one call)."""

    def __init__(self, ttl=25):
        self.ttl = ttl
        self._data = {}
        self._lock = threading.Lock()

    def get(self, user_id, factory):
        with self._lock:
            entry = self._data.get(user_id)
            if entry and (time.time() - entry[0]) < self.ttl:
                return entry[1]
        value = factory()
        with self._lock:
            self._data[user_id] = (time.time(), value)
        return value

    def invalidate(self, user_id):
        with self._lock:
            self._data.pop(user_id, None)


class Doxari:
    """Container holding the shared services for one app instance."""

    def __init__(self):
        self.api = SupabaseAPI(SUPABASE_URL, SUPABASE_KEY, SUPABASE_SECRET_KEY, BUCKET_NAME)
        self.schema = Schema(self.api)
        self.repo = Repository(self.api, self.schema)
        self.service = DocumentService(self.api, self.repo, self.schema)
        self.counts = CountCache()
        self.folders = CountCache(ttl=40)
        self.ai = ai

    # Convenience accessors ------------------------------------------------
    def user_counts(self, user_id, force=False):
        if force:
            self.counts.invalidate(user_id)
        return self.counts.get(user_id, lambda: self.repo.counts(user_id))

    def user_folders(self, user_id, force=False):
        if not self.schema.folders:
            return []
        if force:
            self.folders.invalidate(user_id)
        return self.folders.get(user_id, lambda: self.repo.list_folders(user_id))

    def prefs(self, user_id):
        cached = getattr(g, "_doxari_prefs", None)
        if cached is not None:
            return cached
        prefs = self.repo.get_preferences(user_id)
        if prefs.get("theme") in (None, "system"):
            cookie = request.cookies.get("doxari_theme")
            if cookie in {"light", "dark"}:
                prefs["theme"] = cookie
        g._doxari_prefs = prefs
        return prefs

    def save_prefs(self, user_id, **fields):
        saved = self.repo.save_preferences(user_id, **fields)
        g._doxari_prefs = None
        return saved

    def invalidate(self, user_id):
        self.counts.invalidate(user_id)
        self.folders.invalidate(user_id)
        self.repo.storage.invalidate(user_id)


def create_app():
    app = Flask(__name__, static_folder="static", template_folder="templates")
    app.secret_key = SECRET_KEY
    app.config.update(
        MAX_CONTENT_LENGTH=MAX_UPLOAD_MB * 1024 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=COOKIE_SECURE or False,
        PERMANENT_SESSION_LIFETIME=SESSION_DAYS * 24 * 3600,
        TEMPLATES_AUTO_RELOAD=False,
        JSON_SORT_KEYS=False,
    )

    container = Doxari()
    app.extensions["doxari"] = container

    if not supabase_configured():
        log.warning("SUPABASE_URL / SUPABASE_KEY are missing: auth and storage will not work.")
    if not SUPABASE_SECRET_KEY:
        log.warning("SUPABASE_SECRET_KEY is missing: document operations will fail.")

    protect_from_csrf(app)

    # ------------------------------------------------------------ blueprints
    from .views.main import bp as main_bp
    from .views.auth import bp as auth_bp
    from .views.documents import bp as documents_bp
    from .views.folders import bp as folders_bp
    from .views.sharing import bp as sharing_bp
    from .views.insights import bp as insights_bp
    from .views.settings import bp as settings_bp
    from .views.ai_routes import bp as ai_bp

    for blueprint in (
        main_bp, auth_bp, documents_bp, folders_bp, sharing_bp,
        insights_bp, settings_bp, ai_bp,
    ):
        app.register_blueprint(blueprint)

    # ------------------------------------------------------------ templates
    @app.template_filter("filesize")
    def _filesize(value):
        return human_size(value)

    @app.template_filter("nicedate")
    def _nicedate(value):
        return display_date(value)

    @app.template_filter("nicedatetime")
    def _nicedatetime(value):
        return display_datetime(value)

    @app.template_filter("relative")
    def _relative(value):
        return relative_time(value)

    @app.context_processor
    def inject_helpers():
        def query_url(**updates):
            """Build a URL that keeps the current filters and applies updates."""
            from urllib.parse import urlencode

            args = request.args.to_dict(flat=True)
            for key, value in updates.items():
                if value in (None, "", False):
                    args.pop(key, None)
                else:
                    args[key] = value
            query = urlencode(args)
            return f"{request.path}?{query}" if query else request.path

        def doc_kind(document):
            group = (document or {}).get("type_group") or "other"
            return {
                "pdf": "pdf",
                "image": "image",
                "spreadsheet": "spreadsheet",
                "presentation": "presentation",
                "document": "document",
                "text": "text",
                "archive": "archive",
            }.get(group, "file")

        def doc_icon(document):
            kind = doc_kind(document)
            return {
                "pdf": "file-text",
                "image": "image",
                "spreadsheet": "chart",
                "presentation": "list",
                "document": "file-text",
                "text": "file-text",
                "archive": "folder",
            }.get(kind, "file")

        return {"query_url": query_url, "doc_kind": doc_kind, "doc_icon": doc_icon}

    # Macros call other macros without context, so these helpers are also
    # registered as globals and stay reachable from inside nested macros.
    @app.template_global()
    def query_url(**updates):
        from urllib.parse import urlencode

        args = request.args.to_dict(flat=True)
        for key, value in updates.items():
            if value in (None, "", False):
                args.pop(key, None)
            else:
                args[key] = value
        query = urlencode(args)
        return f"{request.path}?{query}" if query else request.path

    @app.template_global()
    def doc_kind(document):
        group = (document or {}).get("type_group") or "other"
        return {
            "pdf": "pdf",
            "image": "image",
            "spreadsheet": "spreadsheet",
            "presentation": "presentation",
            "document": "document",
            "text": "text",
            "archive": "archive",
        }.get(group, "file")

    @app.template_global()
    def doc_icon(document):
        return {
            "pdf": "file-text",
            "image": "image",
            "spreadsheet": "chart",
            "presentation": "list",
            "document": "file-text",
            "text": "file-text",
            "archive": "folder",
        }.get(doc_kind(document), "file")

    @app.context_processor
    def inject_globals():
        user = current_user()
        container = app.extensions["doxari"]
        prefs = {"theme": "system", "view_mode": "grid", "notifications": True}
        counts = {"documents": 0, "folders": 0, "trash": 0, "favorites": 0,
                  "shared_with_me": 0, "shared_by_me": 0}
        if user:
            prefs = container.prefs(user["id"])
            try:
                counts = container.user_counts(user["id"])
            except Exception:
                pass
        sidebar_usage = None
        if user:
            try:
                totals = container.repo.storage_totals(user["id"])
                limit_bytes = STORAGE_LIMIT_MB * 1024 * 1024 if STORAGE_LIMIT_MB else 0
                sidebar_usage = {
                    "used_display": totals["used_display"],
                    "files": totals["files"],
                    "percent": (
                        min(100, round(totals["used"] / limit_bytes * 100, 1)) if limit_bytes else None
                    ),
                }
            except Exception:
                sidebar_usage = None

        theme = prefs.get("theme") or request.cookies.get("doxari_theme") or "light"
        if theme == "system":
            theme = "system"
        return {
            "current_user": user,
            "user_display_name": display_name(user),
            "user_initials": initials(user),
            "csrf_token": csrf_token,
            "caps": container.schema.summary()["features"],
            "prefs": prefs,
            "nav_counts": counts,
            "folders_for_modals": container.user_folders(user["id"]) if user else [],
            "sidebar_usage": sidebar_usage,
            "theme": theme,
            "brand": BRAND,
            "categories": DEFAULT_CATEGORIES,
            "fallback_category": FALLBACK_CATEGORY,
            "file_groups": FILE_GROUPS,
            "max_upload_mb": MAX_UPLOAD_MB,
            "storage_limit_mb": STORAGE_LIMIT_MB,
            "ai_status": container.ai.status(),
            "active_endpoint": request.endpoint,
            "today": time.strftime("%Y-%m-%d"),
        }

    # ------------------------------------------------------------ errors
    @app.errorhandler(404)
    def not_found(_error):
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": "Not found."}), 404
        return render_template("errors/404.html"), 404

    @app.errorhandler(413)
    def too_large(_error):
        message = f"That file is larger than the {MAX_UPLOAD_MB} MB limit."
        if request.path.startswith("/api/") or request.headers.get("X-Requested-With"):
            return jsonify({"ok": False, "error": message}), 413
        return render_template("errors/413.html", message=message), 413

    @app.errorhandler(500)
    def server_error(error):
        log.exception("Unhandled error: %s", error)
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": "Something went wrong on our side. Please retry."}), 500
        return render_template("errors/500.html"), 500

    @app.after_request
    def security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
        )
        if request.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    return app
