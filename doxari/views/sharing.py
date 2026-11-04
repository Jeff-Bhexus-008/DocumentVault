"""Secure document sharing: with another account, or through a revocable link."""

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
    stream_with_context,
    url_for,
)

from ..config import PUBLIC_BASE_URL
from ..security import current_user, login_required, wants_json
from ..supabase_api import SupabaseError

bp = Blueprint("sharing", __name__)


def _c():
    return current_app.extensions["doxari"]


def _base_url():
    return PUBLIC_BASE_URL or request.url_root.rstrip("/")


@bp.route("/shared")
@login_required
def shared():
    user = current_user()
    repo = _c().repo
    if not repo.schema.sharing:
        return render_template("sharing/index.html", supported=False, with_me=[], by_me=[], links=[])
    with_me = repo.list_shared_with_me(user["id"])
    try:
        by_me_rows = repo.api.rest(
            "document_shares",
            params={"select": "*", "owner_id": f"eq.{user['id']}",
                    "order": "created_at.desc"},
        )
        by_me_rows = [row for row in by_me_rows if not row.get("revoked")]
    except Exception:
        by_me_rows = []
    by_me = []
    for share in by_me_rows:
        document = repo.get_document(share["document_id"], user["id"])
        share["document"] = document
        share["when"] = share.get("created_at")
        by_me.append(share)
    return render_template(
        "sharing/index.html",
        supported=True,
        with_me=with_me,
        by_me=[s for s in by_me if s["document"]],
        links=[],
    )


@bp.route("/documents/<document_id>/share", methods=["POST"])
@login_required
def share_document(document_id):
    user = current_user()
    repo = _c().repo
    document = repo.get_document(document_id, user["id"])
    if not document:
        return _json_or_flash("Document not found.", ok=False, status=404)
    if not repo.schema.sharing:
        return _json_or_flash(
            "Sharing needs the Doxari database upgrade. Nothing was shared.", ok=False, status=400
        )

    email = (request.form.get("email") or "").strip().lower()
    permission = (request.form.get("permission") or "view").strip().lower()
    if permission not in {"view", "download"}:
        permission = "view"
    if not email or "@" not in email:
        return _json_or_flash("Enter the email address of a Doxari account.", ok=False, status=400)
    if email == (user.get("email") or "").lower():
        return _json_or_flash("That is your own account.", ok=False, status=400)

    recipient = repo.find_user_by_email(email)
    if not recipient:
        return _json_or_flash(
            "No Doxari account uses that email yet. Ask them to sign up first, then share again.",
            ok=False,
            status=404,
        )

    existing = repo.api.rest(
        "document_shares",
        params={"select": "id", "document_id": f"eq.{document_id}",
                "shared_with_id": f"eq.{recipient['id']}"},
    )
    existing = [row for row in existing if not row.get("revoked")]
    if existing:
        repo.api.rest(
            "document_shares", "PATCH",
            params={"id": f"eq.{existing[0]['id']}", "owner_id": f"eq.{user['id']}"},
            json={"permission": permission}, prefer="return=minimal",
        )
    else:
        repo.share_document(user["id"], document, email, permission, recipient["id"])
    repo.log(user["id"], "share", document=document, meta={"email": email, "permission": permission})
    return _json_or_flash(
        f"Shared “{document['filename']}” with {email} ({permission} access)."
    )


@bp.route("/shares/<share_id>/revoke", methods=["POST"])
@login_required
def revoke(share_id):
    user = current_user()
    repo = _c().repo
    if not repo.schema.sharing:
        return _json_or_flash("Sharing is not enabled on this deployment.", ok=False, status=400)
    rows = repo.api.rest(
        "document_shares",
        params={"select": "*", "id": f"eq.{share_id}", "owner_id": f"eq.{user['id']}", "limit": 1},
    ) or []
    if not rows:
        return _json_or_flash("Share not found.", ok=False, status=404)
    repo.revoke_share(share_id, user["id"])
    repo.log(user["id"], "unshare", document={"id": rows[0]["document_id"]},
             meta={"email": rows[0].get("shared_with_email")})
    return _json_or_flash("Access revoked.")


@bp.route("/documents/<document_id>/link", methods=["POST"])
@login_required
def create_link(document_id):
    user = current_user()
    repo = _c().repo
    document = repo.get_document(document_id, user["id"])
    if not document:
        return _json_or_flash("Document not found.", ok=False, status=404)
    if not repo.schema.share_links:
        return _json_or_flash("Share links need the Doxari database upgrade.", ok=False, status=400)

    permission = (request.form.get("permission") or "view").strip().lower()
    if permission not in {"view", "download"}:
        permission = "view"
    try:
        days = int(request.form.get("expires_days") or 7)
    except ValueError:
        days = 7
    days = max(0, min(days, 365))

    link = repo.create_share_link(user["id"], document, permission=permission, expires_days=days or None)
    repo.log(user["id"], "share", document=document, meta={"link": True, "permission": permission})
    url = f"{_base_url()}{url_for('sharing.public_link', token=link['token'])}"
    return _json_or_flash("Share link created.", payload={"url": url, "token": link["token"]})


@bp.route("/links/<link_id>/revoke", methods=["POST"])
@login_required
def revoke_link(link_id):
    user = current_user()
    repo = _c().repo
    if not repo.schema.share_links:
        return _json_or_flash("Share links are not enabled.", ok=False, status=400)
    repo.revoke_share_link(link_id, user["id"])
    repo.log(user["id"], "unshare", meta={"link_id": link_id})
    return _json_or_flash("Link revoked. It no longer opens the document.")


# =====================================================================
# Public link (no account needed)
# =====================================================================

@bp.route("/s/<token>")
def public_link(token):
    repo = _c().repo
    link = repo.resolve_share_token(token)
    if not link:
        return render_template("sharing/expired.html"), 404
    document = link["document"]
    return render_template(
        "sharing/public.html",
        token=token,
        link=link,
        document=document,
        can_download=(link.get("permission") == "download"),
        previewable=document.get("can_preview"),
    )


@bp.route("/s/<token>/file")
def public_file(token):
    repo = _c().repo
    link = repo.resolve_share_token(token)
    if not link:
        abort(404)
    document = link["document"]
    try:
        upstream = _c().api.storage_download(document["storage_path"])
    except SupabaseError:
        abort(502)

    def generate():
        try:
            for chunk in upstream.iter_content(65536):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    response = Response(stream_with_context(generate()), mimetype=document.get("mime") or "application/octet-stream")
    response.headers["Content-Disposition"] = "inline"
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Content-Security-Policy"] = "default-src 'none'; img-src 'self' data:"
    return response


@bp.route("/s/<token>/download")
def download_shared_link(token):
    repo = _c().repo
    link = repo.resolve_share_token(token)
    if not link or link.get("permission") != "download":
        abort(404)
    document = link["document"]
    try:
        upstream = _c().api.storage_download(document["storage_path"])
    except SupabaseError:
        abort(502)

    def generate():
        try:
            for chunk in upstream.iter_content(65536):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    response = Response(stream_with_context(generate()), mimetype=document.get("mime") or "application/octet-stream")
    safe_name = (document.get("filename") or "document").replace('"', "")
    response.headers["Content-Disposition"] = f'attachment; filename="{safe_name}"'
    response.headers["Cache-Control"] = "private, no-store"
    return response


def _json_or_flash(message, ok=True, status=200, payload=None):
    if wants_json():
        body = {"ok": ok, "message": message if ok else None, "error": None if ok else message}
        if payload:
            body.update(payload)
        return jsonify(body), status
    flash(message, "success" if ok else "error")
    target = request.form.get("next") or url_for("documents.preview", document_id=request.view_args.get("document_id")) \
        if request.view_args and request.view_args.get("document_id") else url_for("sharing.shared")
    return redirect(target)
