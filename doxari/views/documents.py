"""Dashboard, document browsing, upload, preview and every document action."""

import json
import os
import mimetypes

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
    send_file,
    stream_with_context,
    url_for,
)

from ..config import FILE_TYPES, MAX_BULK_FILES, MAX_UPLOAD_MB, SIGNED_URL_SECONDS, STORAGE_LIMIT_MB
from ..models import FALLBACK_CATEGORY, extension_of, human_size, type_info
from ..security import login_required, validate_upload, wants_json
from ..services import extract_text
from ..supabase_api import SupabaseError

bp = Blueprint("documents", __name__)

SORTS = {
    "newest": "Newest first",
    "oldest": "Oldest first",
    "name": "Name A to Z",
    "name_desc": "Name Z to A",
    "modified": "Recently modified",
    "largest": "Largest first",
    "smallest": "Smallest first",
}


def _container():
    return current_app.extensions["doxari"]


def _user():
    from ..security import current_user

    return current_user()


def parse_filters():
    args = request.args
    page = max(1, int(args.get("page", 1) or 1))
    per_page = 24
    filters = {
        "search": (args.get("q") or "").strip(),
        "extensions": [],
        "group": (args.get("type") or "").strip().lower() or None,
        "folder": args.get("folder") or None,
        "folder_id": args.get("folder_id") or None,
        "favorites": (args.get("favorites") or "").lower() in {"1", "true", "yes"},
        "sort": args.get("sort") or "newest",
        "date_from": args.get("from") or None,
        "date_to": args.get("to") or None,
        "page": page,
        "per_page": per_page,
    }
    if filters["group"] not in (None, "", "all"):
        if filters["group"] not in {"pdf", "image", "document", "spreadsheet", "presentation", "text", "archive"}:
            filters["extensions"] = [filters["group"]]
            filters["group"] = None
    else:
        filters["group"] = None
    if filters["sort"] not in SORTS:
        filters["sort"] = "newest"
    return filters


def _filter_summary(filters, folder_record=None):
    bits = []
    if filters["search"]:
        bits.append(f"“{filters['search']}”")
    if filters["group"]:
        bits.append(filters["group"].title())
    if filters["extensions"]:
        bits.append(filters["extensions"][0].upper())
    if folder_record:
        bits.append(folder_record["name"])
    elif filters["folder"]:
        bits.append(filters["folder"])
    if filters["favorites"]:
        bits.append("Favorites")
    if filters["date_from"] or filters["date_to"]:
        bits.append("Date range")
    return " · ".join(bits)


# =====================================================================
# Dashboard
# =====================================================================

@bp.route("/dashboard")
@login_required
def dashboard():
    user = _user()
    repo = _container().repo
    counts = _container().user_counts(user["id"])
    storage = repo.storage_totals(user["id"])
    recent = repo.recent_uploads(user["id"], limit=6)
    accessed = repo.recently_accessed(user["id"], limit=4)
    activity = repo.list_activity(user["id"], limit=8)
    folders = repo.list_folders(user["id"]) if repo.schema.folders else []

    limit_bytes = STORAGE_LIMIT_MB * 1024 * 1024 if STORAGE_LIMIT_MB else 0
    usage = {
        "used": storage["used"],
        "used_display": human_size(storage["used"]),
        "limit": limit_bytes,
        "limit_display": human_size(limit_bytes) if limit_bytes else None,
        "percent": min(100, round(storage["used"] / limit_bytes * 100, 1)) if limit_bytes else None,
        "remaining_display": human_size(max(0, limit_bytes - storage["used"])) if limit_bytes else None,
        "files": storage["files"],
    }

    return render_template(
        "documents/dashboard.html",
        counts=counts,
        usage=usage,
        recent=recent,
        accessed=accessed,
        activity=activity,
        folders=folders,
        is_empty=counts["documents"] == 0 and counts["trash"] == 0,
        missing_setup=_container().schema.missing_setup(),
    )


# =====================================================================
# Browse
# =====================================================================

@bp.route("/documents")
@login_required
def list_documents():
    user = _user()
    repo = _container().repo
    filters = parse_filters()
    folder_record = None
    if filters["folder_id"] and repo.schema.folders:
        folder_record = repo.get_folder(filters["folder_id"], user["id"])
        if not folder_record:
            abort(404)

    documents, total = repo.list_documents(
        user["id"],
        search=filters["search"],
        extensions=filters["extensions"],
        group=filters["group"],
        folder=None if folder_record else filters["folder"],
        folder_id=filters["folder_id"],
        favorites=filters["favorites"],
        date_from=filters["date_from"],
        date_to=filters["date_to"],
        sort=filters["sort"],
        limit=filters["per_page"],
        offset=(filters["page"] - 1) * filters["per_page"],
    )
    pages = max(1, -(-total // filters["per_page"]))
    return render_template(
        "documents/list.html",
        documents=documents,
        total=total,
        filters=filters,
        folder_record=folder_record,
        page=filters["page"],
        pages=pages,
        sorts=SORTS,
        heading=folder_record["name"] if folder_record else (filters["folder"] or "All documents"),
        summary=_filter_summary(filters, folder_record),
        folders=repo.list_folders(user["id"]) if repo.schema.folders else [],
        breadcrumbs=_breadcrumbs(folder_record, repo, user["id"]),
    )


def _breadcrumbs(folder_record, repo, user_id):
    trail = []
    current = folder_record
    guard = 0
    while current and guard < 10:
        trail.append(current)
        parent_id = current.get("parent_id")
        current = repo.get_folder(parent_id, user_id) if parent_id else None
        guard += 1
    return list(reversed(trail))


@bp.route("/favorites")
@login_required
def favorites():
    user = _user()
    repo = _container().repo
    filters = parse_filters()
    filters["favorites"] = True
    documents, total = repo.list_documents(
        user["id"], favorites=True, search=filters["search"], sort=filters["sort"],
        limit=filters["per_page"], offset=(filters["page"] - 1) * filters["per_page"],
    )
    return render_template(
        "documents/list.html",
        documents=documents,
        total=total,
        filters=filters,
        folder_record=None,
        page=filters["page"],
        pages=max(1, -(-total // filters["per_page"])),
        sorts=SORTS,
        heading="Favorites",
        summary="Documents you starred",
        folders=repo.list_folders(user["id"]) if repo.schema.folders else [],
        breadcrumbs=[],
    )


@bp.route("/recent")
@login_required
def recent():
    user = _user()
    repo = _container().repo
    filters = parse_filters()
    documents = repo.recently_accessed(user["id"], limit=24)
    return render_template(
        "documents/list.html",
        documents=documents,
        total=len(documents),
        filters=filters,
        folder_record=None,
        page=1,
        pages=1,
        sorts=SORTS,
        heading="Recent documents",
        summary="What you opened or uploaded most recently",
        folders=repo.list_folders(user["id"]) if repo.schema.folders else [],
        breadcrumbs=[],
    )


@bp.route("/trash")
@login_required
def trash():
    user = _user()
    repo = _container().repo
    filters = parse_filters()
    if not repo.schema.trash:
        return render_template(
            "documents/trash.html",
            documents=[],
            total=0,
            filters=filters,
            sorts=SORTS,
            unavailable=True,
        )
    documents, total = repo.list_documents(
        user["id"], trashed=True, search=filters["search"], sort="newest",
        limit=filters["per_page"], offset=(filters["page"] - 1) * filters["per_page"],
    )
    return render_template(
        "documents/trash.html",
        documents=documents,
        total=total,
        filters=filters,
        page=filters["page"],
        pages=max(1, -(-total // filters["per_page"])),
        sorts=SORTS,
        unavailable=False,
    )


# =====================================================================
# Preview
# =====================================================================

@bp.route("/documents/<document_id>")
@login_required
def preview(document_id):
    user = _user()
    repo = _container().repo
    document = repo.get_document(document_id, user["id"], include_trashed=True)
    shared_permission = None
    if not document:
        document = repo.get_shared_document(document_id, user["id"])
        if document:
            shared_permission = document.get("share_permission")
    if not document:
        abort(404)

    if not shared_permission:
        repo.touch(document_id, user["id"])

    siblings = []
    if not shared_permission and not document.get("deleted_at"):
        siblings, _ = repo.list_documents(
            user["id"],
            folder_id=document.get("folder_id"),
            folder=None if document.get("folder_id") else document.get("folder"),
            sort="name",
            limit=500,
        )
    index = next((i for i, item in enumerate(siblings) if item["id"] == document["id"]), None)
    prev_doc = siblings[index - 1] if index not in (None, 0) else None
    next_doc = siblings[index + 1] if index is not None and index + 1 < len(siblings) else None

    shares = repo.list_shares_for_document(document_id, user["id"]) if not shared_permission else []
    links = repo.list_share_links(document_id, user["id"]) if not shared_permission else []

    return render_template(
        "documents/preview.html",
        document=document,
        shared_permission=shared_permission,
        prev_doc=prev_doc,
        next_doc=next_doc,
        shares=shares,
        share_links=links,
        folders=repo.list_folders(user["id"]) if repo.schema.folders else [],
        info=type_info(document.get("filename")),
        can_ai=_container().ai.can_analyse(document.get("filename") or ""),
        ai_status=_container().ai.status(),
        prefs=_container().prefs(user["id"]),
    )


@bp.route("/documents/<document_id>/text")
@login_required
def document_text(document_id):
    """Readable text for text-based previews (no download needed)."""
    user = _user()
    repo = _container().repo
    document = repo.get_document(document_id, user["id"], include_trashed=False)
    if not document:
        return jsonify({"ok": False, "error": "Document not found."}), 404
    try:
        response = _container().api.storage_download(document["storage_path"])
        blob = response.content[:400000]
    except SupabaseError as error:
        return jsonify({"ok": False, "error": error.message}), 502
    text = extract_text(blob, document["filename"], limit=200000)
    if not text:
        text = blob.decode("utf-8", errors="replace")[:200000]
    return jsonify({"ok": True, "text": text})


@bp.route("/api/documents/<document_id>/url")
@login_required
def signed_url(document_id):
    """Short-lived, permission-checked URL for the in-browser viewer."""
    user = _user()
    repo = _container().repo
    document = repo.get_document(document_id, user["id"])
    if not document:
        document = repo.get_shared_document(document_id, user["id"])
    if not document:
        return jsonify({"ok": False, "error": "Document not found."}), 404
    try:
        url = _container().api.storage_signed_url(document["storage_path"], SIGNED_URL_SECONDS)
    except SupabaseError as error:
        return jsonify({"ok": False, "error": error.message}), 502
    return jsonify({"ok": True, "url": url, "expires_in": SIGNED_URL_SECONDS})


# =====================================================================
# Upload
# =====================================================================

@bp.route("/upload", methods=["POST"])
@login_required
def upload():
    user = _user()
    container = _container()
    files = request.files.getlist("files") or request.files.getlist("file")
    files = [f for f in files if f and f.filename]

    folder_id = (request.form.get("folder_id") or "").strip() or None
    folder_name = (request.form.get("folder") or "").strip() or None
    description = (request.form.get("description") or "").strip() or None
    tags = [t.strip().lower() for t in (request.form.get("tags") or "").split(",") if t.strip()]

    def respond(payload, status=200):
        if wants_json():
            return jsonify(payload), status
        for item in payload.get("uploaded", []):
            flash(f"“{item['name']}” uploaded.", "success")
        for item in payload.get("failed", []):
            flash(f"{item['name']}: {item['error']}", "error")
        return redirect(url_for("documents.list_documents"))

    if not files:
        return respond({"ok": False, "error": "Choose at least one file to upload.", "uploaded": [], "failed": []}, 400)
    if len(files) > MAX_BULK_FILES:
        return respond(
            {"ok": False, "error": f"Upload up to {MAX_BULK_FILES} files at a time.",
             "uploaded": [], "failed": []},
            400,
        )

    uploaded, failed = [], []
    for file_storage in files:
        ok, error, filename, ext = validate_upload(file_storage)
        if not ok:
            failed.append({"name": file_storage.filename or "file", "error": error})
            try:
                container.repo.log(user["id"], "upload_failed", meta={"name": file_storage.filename, "error": error})
            except Exception:
                pass
            continue
        try:
            record = container.service.store_upload(
                user["id"], file_storage, filename=filename, ext=ext,
                folder=folder_name, folder_id=folder_id,
                description=description, tags=tags or None,
            )
            uploaded.append({"id": record.get("id"), "name": filename})
        except SupabaseError as err:
            failed.append({"name": filename, "error": err.message})
        except Exception as error:  # pragma: no cover - defensive
            current_app.logger.exception("Upload failed")
            failed.append({"name": filename, "error": "Upload failed. Please try again."})

    container.invalidate(user["id"])
    status = 200 if uploaded else 400
    return respond(
        {
            "ok": bool(uploaded),
            "uploaded": uploaded,
            "failed": failed,
            "message": (
                f"{len(uploaded)} file{'s' if len(uploaded) != 1 else ''} uploaded"
                if uploaded
                else "No files were uploaded."
            ),
        },
        status,
    )


# =====================================================================
# Actions
# =====================================================================

def _owned_or_404(document_id, *, include_trashed=True):
    user = _user()
    document = _container().repo.get_document(document_id, user["id"], include_trashed=include_trashed)
    if not document:
        abort(404)
    return user, document


def _done(message, category="success", redirect_to=None):
    if wants_json():
        return jsonify({"ok": True, "message": message})
    flash(message, category)
    return redirect(redirect_to or request.form.get("next") or url_for("documents.list_documents"))


@bp.route("/documents/<document_id>/rename", methods=["POST"])
@login_required
def rename(document_id):
    user, document = _owned_or_404(document_id)
    new_name = (request.form.get("filename") or "").strip()
    if not new_name:
        return _done("Enter a new name.", "error")
    if "/" in new_name or "\\" in new_name:
        return _done("A name cannot contain slashes.", "error")
    clean_ext = extension_of(new_name)
    if clean_ext not in FILE_TYPES:
        return _done("Keep a supported file extension, for example .pdf.", "error")
    if len(new_name) > 150:
        return _done("That name is too long (150 characters max).", "error")

    old_name = document["filename"]
    _container().repo.update_document(document_id, user["id"], filename=new_name)
    _container().repo.log(user["id"], "rename", document={**document, "filename": new_name},
                         meta={"from": old_name})
    _container().invalidate(user["id"])
    return _done(f"Renamed to “{new_name}”.", redirect_to=url_for("documents.preview", document_id=document_id))


@bp.route("/documents/<document_id>/move", methods=["POST"])
@login_required
def move(document_id):
    user, document = _owned_or_404(document_id)
    repo = _container().repo
    folder_id = (request.form.get("folder_id") or "").strip() or None
    folder_name = (request.form.get("folder") or "").strip() or FALLBACK_CATEGORY

    if folder_id and repo.schema.folders:
        folder = repo.get_folder(folder_id, user["id"])
        if not folder:
            return _done("That folder no longer exists.", "error")
        folder_name = folder["name"]
    elif not repo.schema.folders:
        folder_id = None

    repo.update_document(document_id, user["id"], folder=folder_name, folder_id=folder_id)
    repo.log(user["id"], "move", document=document, meta={"to": folder_name})
    _container().invalidate(user["id"])
    return _done(f"Moved to “{folder_name}”.", redirect_to=request.form.get("next"))


@bp.route("/documents/<document_id>/favorite", methods=["POST"])
@login_required
def favorite(document_id):
    user, document = _owned_or_404(document_id)
    new_state = not bool(document.get("favorite"))
    _container().repo.update_document(document_id, user["id"], favorite=new_state)
    _container().repo.log(user["id"], "favorite" if new_state else "unfavorite", document=document)
    if wants_json():
        return jsonify({"ok": True, "favorite": new_state,
                        "message": "Added to favorites." if new_state else "Removed from favorites."})
    flash("Added to favorites." if new_state else "Removed from favorites.", "success")
    return redirect(request.form.get("next") or request.referrer or url_for("documents.list_documents"))


@bp.route("/documents/<document_id>/delete", methods=["POST"])
@login_required
def delete(document_id):
    user, document = _owned_or_404(document_id)
    result, error = _container().service.delete_document(document_id, user["id"])
    _container().invalidate(user["id"])
    if error:
        return _done(error, "error")
    if result["mode"] == "trashed":
        return _done("Moved to trash. You can restore it any time.")
    return _done("Document deleted permanently.", "success", redirect_to=url_for("documents.list_documents"))


@bp.route("/documents/<document_id>/restore", methods=["POST"])
@login_required
def restore(document_id):
    user, document = _owned_or_404(document_id)
    _, error = _container().service.restore_document(document_id, user["id"])
    _container().invalidate(user["id"])
    if error:
        return _done(error, "error")
    return _done("Document restored.", "success", redirect_to=url_for("documents.trash"))


@bp.route("/documents/<document_id>/purge", methods=["POST"])
@login_required
def purge(document_id):
    user, document = _owned_or_404(document_id)
    _, error = _container().service.purge_document(document_id, user["id"])
    _container().invalidate(user["id"])
    if error:
        return _done(error, "error")
    return _done("Document deleted permanently.", "success", redirect_to=url_for("documents.trash"))


@bp.route("/documents/<document_id>/copy", methods=["POST"])
@login_required
def copy(document_id):
    user, _document = _owned_or_404(document_id)
    record, error = _container().service.copy_document(document_id, user["id"])
    _container().invalidate(user["id"])
    if error:
        return _done(error, "error")
    return _done(f"Created “{record['filename']}”.", redirect_to=url_for("documents.preview", document_id=record["id"]))


@bp.route("/documents/<document_id>/details", methods=["POST"])
@login_required
def details(document_id):
    user, document = _owned_or_404(document_id)
    repo = _container().repo
    fields = {}
    if repo.schema.documents_has("description"):
        fields["description"] = (request.form.get("description") or "").strip()[:1000] or None
    if repo.schema.tags:
        raw = request.form.get("tags") or ""
        tags = [t.strip().lower()[:24] for t in raw.split(",") if t.strip()][:10]
        fields["tags"] = tags
    if not fields:
        return _done("Notes and tags need the Doxari database upgrade.", "error")
    repo.update_document(document_id, user["id"], **fields)
    return _done("Details saved.", redirect_to=url_for("documents.preview", document_id=document_id))


@bp.route("/trash/empty", methods=["POST"])
@login_required
def empty_trash():
    user = _user()
    if request.form.get("confirm") != "EMPTY":
        return _done("Type EMPTY to confirm.", "error", redirect_to=url_for("documents.trash"))
    removed = _container().service.empty_trash(user["id"])
    _container().invalidate(user["id"])
    return _done(f"Trash emptied. {removed} document{'s' if removed != 1 else ''} deleted.", "success",
                 redirect_to=url_for("documents.trash"))


# =====================================================================
# Bulk operations
# =====================================================================

@bp.route("/api/bulk", methods=["POST"])
@login_required
def bulk():
    user = _user()
    payload = request.get_json(silent=True) or {}
    action = (payload.get("action") or "").strip()
    ids = [str(i) for i in (payload.get("ids") or []) if i]
    if not ids:
        return jsonify({"ok": False, "error": "Select at least one document."}), 400
    if len(ids) > 200:
        return jsonify({"ok": False, "error": "Up to 200 documents at a time."}), 400

    container = _container()
    repo = container.repo
    done, failed = 0, []

    for document_id in ids:
        document = repo.get_document(document_id, user["id"])
        if not document:
            failed.append({"id": document_id, "error": "Not found"})
            continue
        try:
            if action == "delete":
                container.service.delete_document(document_id, user["id"])
            elif action == "restore":
                _, error = container.service.restore_document(document_id, user["id"])
                if error:
                    raise SupabaseError(error)
            elif action == "purge":
                container.service.purge_document(document_id, user["id"])
            elif action in {"favorite", "unfavorite"}:
                repo.update_document(document_id, user["id"], favorite=action == "favorite")
                repo.log(user["id"], action, document=document)
            elif action == "move":
                folder_id = payload.get("folder_id") or None
                folder_name = (payload.get("folder") or FALLBACK_CATEGORY)
                if folder_id and repo.schema.folders:
                    folder = repo.get_folder(folder_id, user["id"])
                    if not folder:
                        raise SupabaseError("Folder not found")
                    folder_name = folder["name"]
                repo.update_document(document_id, user["id"], folder=folder_name, folder_id=folder_id)
                repo.log(user["id"], "move", document=document, meta={"to": folder_name})
            else:
                return jsonify({"ok": False, "error": "Unknown action."}), 400
            done += 1
        except SupabaseError as error:
            failed.append({"id": document_id, "error": error.message})
        except Exception:
            failed.append({"id": document_id, "error": "Failed"})

    container.invalidate(user["id"])
    labels = {
        "delete": "moved to trash",
        "restore": "restored",
        "purge": "deleted permanently",
        "favorite": "added to favorites",
        "unfavorite": "removed from favorites",
        "move": "moved",
    }
    message = f"{done} document{'s' if done != 1 else ''} {labels.get(action, 'updated')}"
    if failed:
        message += f", {len(failed)} could not be processed"
    return jsonify({"ok": done > 0, "message": message, "count": done, "failed": failed})


# =====================================================================
# File transfer
# =====================================================================

@bp.route("/documents/<document_id>/download")
@login_required
def download(document_id):
    user = _user()
    repo = _container().repo
    document = repo.get_document(document_id, user["id"])
    permission = "owner" if document else None
    if not document:
        document = repo.get_shared_document(document_id, user["id"])
        if document:
            permission = document.get("share_permission") or "view"
    if not document:
        abort(404)
    if permission not in {"owner", "download"}:
        abort(403)

    try:
        upstream = _container().api.storage_download(document["storage_path"])
    except SupabaseError:
        if wants_json():
            return jsonify({"ok": False, "error": "The file could not be read from storage."}), 502
        flash("That file could not be read from storage. Please try again.", "error")
        return redirect(url_for("documents.preview", document_id=document_id))

    filename = document.get("filename") or "document"
    mime = document.get("mime") or "application/octet-stream"
    repo.log(user["id"], "download", document=document)

    def generate():
        try:
            for chunk in upstream.iter_content(65536):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    response = Response(stream_with_context(generate()), mimetype=mime)
    safe_name = filename.replace('"', "")
    response.headers["Content-Disposition"] = f'attachment; filename="{safe_name}"'
    response.headers["Content-Length"] = str(document.get("size") or "")
    response.headers["Cache-Control"] = "private, no-store"
    return response


@bp.route("/documents/<document_id>/raw")
@login_required
def raw(document_id):
    """Inline bytes, used as a same-origin fallback for the viewer."""
    user = _user()
    repo = _container().repo
    document = repo.get_document(document_id, user["id"]) or repo.get_shared_document(document_id, user["id"])
    if not document:
        abort(404)
    try:
        upstream = _container().api.storage_download(document["storage_path"])
    except SupabaseError:
        abort(502)
    mime = document.get("mime") or "application/octet-stream"
    if extension_of(document["filename"]) == "pdf":
        mime = "application/pdf"

    def generate():
        try:
            for chunk in upstream.iter_content(65536):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    response = Response(stream_with_context(generate()), mimetype=mime)
    response.headers["Content-Disposition"] = "inline"
    if extension_of(document["filename"]) != "pdf":
        # Keep non-PDF inline responses inert; the PDF viewer needs its own scope.
        response.headers["Content-Security-Policy"] = "default-src 'none'; img-src 'self' data:"
    return response


@bp.route("/documents/<document_id>/thumb")
@login_required
def thumbnail(document_id):
    """Small inline image used by grid cards for image documents."""
    user = _user()
    repo = _container().repo
    document = repo.get_document(document_id, user["id"]) or repo.get_shared_document(document_id, user["id"])
    if not document or document.get("type_group") != "image":
        abort(404)
    try:
        upstream = _container().api.storage_download(document["storage_path"])
    except SupabaseError:
        abort(502)
    blob = upstream.content[:3_000_000]

    def generate():
        yield blob

    response = Response(stream_with_context(generate()), mimetype=document.get("mime") or "image/jpeg")
    response.headers["Cache-Control"] = "private, max-age=600"
    response.headers["Content-Disposition"] = "inline"
    return response


# Legacy routes kept so old links and bookmarks keep working.
@bp.route("/download/<document_id>")
@login_required
def legacy_download(document_id):
    return download(document_id)


@bp.route("/view/<document_id>")
@login_required
def legacy_view(document_id):
    return redirect(url_for("documents.preview", document_id=document_id))


@bp.route("/delete/<document_id>", methods=["POST"])
@login_required
def legacy_delete(document_id):
    return delete(document_id)


@bp.route("/favorite/<document_id>", methods=["POST"])
@login_required
def legacy_favorite(document_id):
    return favorite(document_id)
