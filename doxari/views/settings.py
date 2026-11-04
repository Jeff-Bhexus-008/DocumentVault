"""Profile, appearance, notifications, security and account settings."""

import os

from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from ..config import DEFAULT_CATEGORIES, MAX_UPLOAD_MB, STORAGE_LIMIT_MB
from ..models import human_size
from ..security import current_user, login_required, wants_json
from ..supabase_api import SupabaseError

bp = Blueprint("settings", __name__)


def _c():
    return current_app.extensions["doxari"]


def _respond(message, ok=True, redirect_to=None):
    if wants_json():
        return jsonify({"ok": ok, "message": message if ok else None, "error": None if ok else message})
    flash(message, "success" if ok else "error")
    return redirect(redirect_to or url_for("settings.index"))


@bp.route("/settings")
@login_required
def index():
    user = current_user()
    container = _c()
    repo = container.repo
    storage = repo.storage_totals(user["id"])
    counts = container.user_counts(user["id"])
    limit_bytes = STORAGE_LIMIT_MB * 1024 * 1024 if STORAGE_LIMIT_MB else 0
    schema = container.schema.summary()
    return render_template(
        "settings/index.html",
        counts=counts,
        storage=storage,
        usage={
            "limit": limit_bytes,
            "limit_display": human_size(limit_bytes) if limit_bytes else None,
            "percent": min(100, round(storage["used"] / limit_bytes * 100, 1)) if limit_bytes else None,
            "remaining_display": human_size(max(0, limit_bytes - storage["used"])) if limit_bytes else None,
        },
        schema=schema,
        missing=container.schema.missing_setup(),
        ai=container.ai.status(),
        prefs=container.prefs(user["id"]),
        max_upload_mb=MAX_UPLOAD_MB,
        categories=DEFAULT_CATEGORIES,
        bucket=container.api.bucket,
    )


@bp.route("/settings/migration.sql")
@login_required
def migration():
    """Serve the optional database upgrade as a downloadable SQL file."""
    path = os.path.join(current_app.root_path, "..", "migrations", "2026_11_04_doxari_upgrade.sql")
    if not os.path.exists(path):
        abort(404)
    with open(path, "r", encoding="utf-8") as handle:
        sql = handle.read()
    return Response(
        sql,
        mimetype="text/plain",
        headers={"Content-Disposition": 'attachment; filename="doxari-upgrade.sql"'},
    )


@bp.route("/settings/profile", methods=["POST"])
@login_required
def profile():
    user = current_user()
    full_name = (request.form.get("full_name") or "").strip()
    if len(full_name) > 80:
        return _respond("Keep your name under 80 characters.", ok=False)
    from .auth import access_token

    token = access_token()
    if not token:
        return _respond("Please sign in again to update your profile.", ok=False)
    api = _c().api
    try:
        api.update_profile_metadata(token, {"full_name": full_name})
    except SupabaseError as error:
        return _respond(f"Could not save your profile: {error.message}", ok=False)
    session["user_name"] = full_name
    _c().repo.log(user["id"], "settings", meta={"field": "profile"})
    return _respond("Profile updated.")


@bp.route("/settings/preferences", methods=["POST"])
@login_required
def preferences():
    user = current_user()
    container = _c()
    theme = (request.form.get("theme") or "light").lower()
    if theme not in {"light", "dark", "system"}:
        theme = "light"
    view_mode = (request.form.get("view_mode") or "grid").lower()
    if view_mode not in {"grid", "list"}:
        view_mode = "grid"

    fields = {
        "theme": theme,
        "view_mode": view_mode,
        "notifications": request.form.get("notifications") == "on",
        "ai_consent": request.form.get("ai_consent") == "on",
    }
    saved = container.save_prefs(user["id"], **fields)
    response = _respond(
        "Preferences saved." if saved else "Theme saved for this browser. Run the database upgrade to store all preferences on your account."
    )
    if hasattr(response, "set_cookie"):
        response.set_cookie("doxari_theme", theme if theme != "system" else "", max_age=365 * 24 * 3600,
                            samesite="Lax", httponly=False)
    return response


@bp.route("/api/preferences", methods=["POST"])
@login_required
def api_preferences():
    """Partial preference update used by the theme and view-mode toggles."""
    user = current_user()
    container = _c()
    payload = request.get_json(silent=True) or {}
    fields = {}
    if payload.get("theme") in {"light", "dark", "system"}:
        fields["theme"] = payload["theme"]
    if payload.get("view_mode") in {"grid", "list"}:
        fields["view_mode"] = payload["view_mode"]
    if isinstance(payload.get("notifications"), bool):
        fields["notifications"] = payload["notifications"]
    if isinstance(payload.get("ai_consent"), bool):
        fields["ai_consent"] = payload["ai_consent"]
    if not fields:
        return jsonify({"ok": False, "error": "Nothing to update."}), 400
    saved = container.save_prefs(user["id"], **fields)
    response = jsonify({"ok": True, "saved": saved, "prefs": fields})
    if "theme" in fields:
        response.set_cookie(
            "doxari_theme",
            fields["theme"] if fields["theme"] != "system" else "",
            max_age=365 * 24 * 3600,
            samesite="Lax",
        )
    return response


@bp.route("/settings/onboarding", methods=["POST"])
@login_required
def onboarding():
    user = current_user()
    container = _c()
    container.save_prefs(user["id"], onboarded=True)
    if wants_json():
        return jsonify({"ok": True})
    return redirect(request.form.get("next") or url_for("documents.dashboard"))


@bp.route("/settings/password", methods=["POST"])
@login_required
def password():
    user = current_user()
    current = request.form.get("current_password") or ""
    new = request.form.get("new_password") or ""
    confirm = request.form.get("confirm_password") or ""
    if len(new) < 8:
        return _respond("Use at least 8 characters for your new password.", ok=False)
    if new != confirm:
        return _respond("Those new passwords don't match.", ok=False)
    api = _c().api
    try:
        tokens = api.sign_in(user["email"], current)
    except SupabaseError:
        return _respond("Your current password is incorrect.", ok=False)
    try:
        api.update_password(tokens["access_token"], new)
    except SupabaseError as error:
        return _respond(f"Could not update your password: {error.message}", ok=False)
    session["access_token"] = tokens.get("access_token")
    session["refresh_token"] = tokens.get("refresh_token")
    _c().repo.log(user["id"], "settings", meta={"field": "password"})
    return _respond("Password updated.")


@bp.route("/settings/delete-account", methods=["POST"])
@login_required
def delete_account():
    user = current_user()
    container = _c()
    confirmation = (request.form.get("confirm_email") or "").strip().lower()
    password = request.form.get("password") or ""
    if confirmation != (user["email"] or "").lower():
        return _respond("Type your account email exactly to confirm.", ok=False)
    api = container.api
    try:
        api.sign_in(user["email"], password)
    except SupabaseError:
        return _respond("That password is incorrect. Nothing was deleted.", ok=False)

    # Delete the user's files first so nothing is orphaned in storage.
    documents, _ = container.repo.list_documents(user["id"], limit=500, with_total=False)
    documents += container.repo.list_documents(user["id"], trashed=True, limit=500, with_total=False)
    for document in documents:
        container.service._purge_storage(document, user["id"])
    for table in ("document_shares", "share_links", "activity_log", "user_preferences", "folders"):
        if container.schema.has_table(table):
            try:
                column = "owner_id" if table in {"document_shares", "share_links"} else "user_id"
                if table == "document_shares":
                    container.api.rest(table, "DELETE", params={"owner_id": f"eq.{user['id']}"})
                    container.api.rest(table, "DELETE", params={"shared_with_id": f"eq.{user['id']}"})
                else:
                    container.api.rest(table, "DELETE", params={column: f"eq.{user['id']}"})
            except Exception:
                pass
    try:
        container.api.rest("documents", "DELETE", params={"user_id": f"eq.{user['id']}"})
    except Exception:
        pass
    try:
        api.delete_account(user["id"])
    except SupabaseError as error:
        return _respond(
            f"Your documents were removed but the account could not be closed automatically: {error.message} "
            "Contact support to finish closing it.",
            ok=False,
        )
    session.clear()
    flash("Your Doxari account and documents have been deleted.", "success")
    return redirect(url_for("main.home"))
