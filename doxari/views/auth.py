"""Authentication: sign in, sign up, sign out, password reset."""

import time

from flask import (
    Blueprint,
    current_app,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from ..security import (
    current_user,
    login_limiter,
    reset_limiter,
    safe_next_url,
)
from ..supabase_api import SupabaseError

bp = Blueprint("auth", __name__)

EMAIL_HINTS = {
    "invalid login credentials": "That email and password don't match. Please try again.",
    "email not confirmed": "Please confirm your email address first. Check your inbox for the link.",
    "user already registered": "This email is already registered. Try signing in instead.",
    "already registered": "This email is already registered. Try signing in instead.",
    "password should be at least": "Your password is too short. Use at least 8 characters.",
    "invalid email": "That email address doesn't look right.",
    "email rate limit exceeded": "Too many emails sent. Please wait a minute and try again.",
}


def friendly(message):
    text = (message or "").strip()
    low = text.lower()
    for needle, friendly_text in EMAIL_HINTS.items():
        if needle in low:
            return friendly_text
    if len(text) > 180:
        return "Something went wrong. Please try again."
    return text or "Something went wrong. Please try again."


def _start_session(payload):
    user = payload.get("user") or {}
    metadata = user.get("user_metadata") or {}
    session.permanent = True
    session["user_id"] = user.get("id")
    session["user_email"] = user.get("email")
    session["user_name"] = metadata.get("full_name") or metadata.get("name") or ""
    session["access_token"] = payload.get("access_token")
    session["refresh_token"] = payload.get("refresh_token")
    session["token_at"] = int(time.time())
    session["expires_in"] = int(payload.get("expires_in") or 3600)


def access_token(force_refresh=False):
    """Return a usable access token, refreshing it when it is near expiry."""
    token = session.get("access_token")
    issued = session.get("token_at") or 0
    expires_in = session.get("expires_in") or 3600
    if token and not force_refresh and (time.time() - issued) < max(60, expires_in - 120):
        return token
    refresh_token = session.get("refresh_token")
    if not refresh_token:
        return token
    api = current_app.extensions["doxari"].api
    try:
        payload = api.refresh(refresh_token)
    except SupabaseError:
        return token
    session["access_token"] = payload.get("access_token")
    session["refresh_token"] = payload.get("refresh_token", refresh_token)
    session["token_at"] = int(time.time())
    session["expires_in"] = int(payload.get("expires_in") or 3600)
    return session["access_token"]


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user():
        return redirect(url_for("documents.dashboard"))

    next_url = request.values.get("next") or ""
    error = None
    email = ""

    if request.method == "POST":
        email = (request.form.get("email") or "").strip()
        password = request.form.get("password") or ""

        if not email or not password:
            error = "Enter your email and password to continue."
        elif login_limiter.blocked(email):
            error = "Too many attempts. Please wait a few minutes and try again."
        else:
            api = current_app.extensions["doxari"].api
            try:
                payload = api.sign_in(email, password)
                _start_session(payload)
                login_limiter.reset(email)
                app = current_app.extensions["doxari"]
                app.counts.invalidate(session["user_id"])
                return redirect(safe_next_url(next_url, url_for("documents.dashboard")))
            except SupabaseError as err:
                login_limiter.hit(email)
                error = friendly(err.message)

    return render_template("auth/login.html", error=error, email=email, next_url=next_url)


@bp.route("/register", methods=["GET", "POST"])
def register():
    if current_user():
        return redirect(url_for("documents.dashboard"))

    error = None
    notice = None
    form = {"full_name": "", "email": ""}

    if request.method == "POST":
        form["full_name"] = (request.form.get("full_name") or "").strip()
        form["email"] = (request.form.get("email") or "").strip()
        password = request.form.get("password") or ""
        confirm = request.form.get("confirm_password") or ""

        api = current_app.extensions["doxari"].api

        if not form["email"] or not password:
            error = "Enter your email and a password to create your account."
        elif len(password) < 8:
            error = "Use at least 8 characters for your password."
        elif password != confirm:
            error = "Those passwords don't match."
        elif not api.configured:
            error = "Sign-up is unavailable: this Doxari server is not connected to Supabase yet."
        else:
            try:
                result = api.sign_up(
                    form["email"], password, {"full_name": form["full_name"]} if form["full_name"] else None
                )
                if result.get("access_token"):
                    _start_session(result)
                    return redirect(url_for("documents.dashboard"))
                notice = "Account created. Check your inbox to confirm your email, then sign in."
                return render_template(
                    "auth/register.html", error=None, notice=notice, form=form
                )
            except SupabaseError as err:
                error = friendly(err.message)

    return render_template("auth/register.html", error=error, notice=notice, form=form)


@bp.route("/logout", methods=["GET", "POST"])
def logout():
    session.clear()
    response = redirect(url_for("main.home"))
    response.delete_cookie("doxari_theme")
    return response


@bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    message = None
    error = None
    if request.method == "POST":
        email = (request.form.get("email") or "").strip()
        if not email:
            error = "Enter the email address for your account."
        elif reset_limiter.blocked(email):
            error = "Too many reset requests. Please wait a few minutes."
        else:
            api = current_app.extensions["doxari"].api
            reset_limiter.hit(email)
            try:
                api.recover(email, url_for("auth.reset_password", _external=True))
                message = "If that email has a Doxari account, a reset link is on its way."
            except SupabaseError:
                # Never reveal whether an account exists.
                message = "If that email has a Doxari account, a reset link is on its way."
    return render_template("auth/forgot.html", message=message, error=error)


@bp.route("/reset-password")
def reset_password():
    return render_template("auth/reset.html")


@bp.route("/reset-password/complete", methods=["POST"])
def reset_password_complete():
    payload = request.get_json(silent=True) or {}
    token = (payload.get("access_token") or "").strip()
    password = payload.get("password") or ""
    if not token:
        return jsonify({"ok": False, "error": "This reset link is invalid or has expired."}), 400
    if len(password) < 8:
        return jsonify({"ok": False, "error": "Use at least 8 characters."}), 400
    api = current_app.extensions["doxari"].api
    try:
        api.update_password(token, password)
    except SupabaseError as err:
        return jsonify({"ok": False, "error": friendly(err.message)}), 400
    return jsonify({"ok": True, "redirect": url_for("auth.login")})
