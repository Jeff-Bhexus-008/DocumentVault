"""Public pages: landing, health, sitemap."""

from flask import Blueprint, current_app, jsonify, redirect, render_template, request, url_for

from ..config import PUBLIC_BASE_URL, supabase_configured
from ..security import current_user

bp = Blueprint("main", __name__)


@bp.route("/")
def home():
    if current_user():
        return redirect(url_for("documents.dashboard"))
    return render_template("landing.html")


@bp.route("/welcome")
def welcome():
    return render_template("landing.html")


@bp.route("/healthz")
def healthz():
    container = current_app.extensions["doxari"]
    ok, detail = container.api.ping()
    schema = container.schema.summary()
    return (
        jsonify(
            {
                "status": "ok" if ok else "degraded",
                "supabase": {"reachable": ok, "detail": detail, "configured": supabase_configured()},
                "storage": {"bucket": container.api.bucket},
                "features": schema["features"],
            }
        ),
        200 if ok else 503,
    )


def _base_url():
    return PUBLIC_BASE_URL or request.url_root.rstrip("/")


@bp.route("/sitemap.xml")
def sitemap():
    base = _base_url()
    pages = ["/", "/login", "/register"]
    body = "".join(
        f"<url><loc>{base}{path}</loc></url>" for path in pages
    )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{body}</urlset>"
    )
    return current_app.response_class(xml, mimetype="application/xml")


@bp.route("/robots.txt")
def robots():
    base = _base_url()
    body = (
        "User-agent: *\n"
        "Disallow: /dashboard\n"
        "Disallow: /documents\n"
        "Disallow: /folders\n"
        "Disallow: /trash\n"
        "Disallow: /settings\n"
        "Disallow: /analytics\n"
        "Disallow: /shared\n"
        "Disallow: /api/\n"
        f"Sitemap: {base}/sitemap.xml\n"
    )
    return current_app.response_class(body, mimetype="text/plain")
