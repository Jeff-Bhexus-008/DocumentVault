"""Folders: nested folder management and category organisation."""

from flask import Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request, url_for

from ..config import DEFAULT_CATEGORIES
from ..models import FALLBACK_CATEGORY
from ..security import current_user, login_required, wants_json
from ..supabase_api import SupabaseError

bp = Blueprint("folders", __name__)


def _c():
    return current_app.extensions["doxari"]


def _user():
    return current_user()


@bp.route("/folders")
@login_required
def index():
    user = _user()
    repo = _c().repo
    folders = repo.folder_tree(user["id"]) if repo.schema.folders else []
    counts = repo.folder_counts(user["id"]) if repo.schema.folders else {"by_id": {}, "by_name": {}}
    total_docs = sum(counts["by_name"].values())
    return render_template(
        "documents/folders.html",
        folders=folders,
        counts=counts,
        total_docs=total_docs,
        categories=DEFAULT_CATEGORIES,
        supported=repo.schema.folders,
        legacy_counts=counts["by_name"],
        fallback_category=FALLBACK_CATEGORY,
    )


@bp.route("/folders/create", methods=["POST"])
@login_required
def create():
    user = _user()
    repo = _c().repo
    if not repo.schema.folders:
        return _respond("Folders need the Doxari database upgrade.", ok=False, status=400)
    payload = request.get_json(silent=True) or {}
    name = (request.form.get("name") or payload.get("name") or "").strip()
    parent_id = request.form.get("parent_id") or payload.get("parent_id")
    color = request.form.get("color") or None
    icon = request.form.get("icon") or None
    if not name:
        return _respond("Give your folder a name.", ok=False, status=400)
    if len(name) > 60:
        return _respond("Keep folder names under 60 characters.", ok=False, status=400)
    if parent_id:
        parent = repo.get_folder(parent_id, user["id"])
        if not parent:
            return _respond("That parent folder no longer exists.", ok=False, status=400)
    existing = {f["name"].lower() for f in repo.list_folders(user["id"])
                if (f.get("parent_id") or None) == (parent_id or None)}
    if name.lower() in existing:
        return _respond("You already have a folder with that name here.", ok=False, status=400)
    try:
        folder = repo.create_folder(user["id"], name, parent_id=parent_id, color=color, icon=icon)
    except SupabaseError as error:
        return _respond(error.message, ok=False, status=400)
    repo.log(user["id"], "folder_create", folder=folder, meta={"name": name})
    _c().invalidate(user["id"])
    if wants_json():
        return jsonify({"ok": True, "folder": folder, "message": f"Folder “{name}” created."})
    flash(f"Folder “{name}” created.", "success")
    return redirect(url_for("folders.index"))


@bp.route("/folders/<folder_id>/rename", methods=["POST"])
@login_required
def rename(folder_id):
    user = _user()
    repo = _c().repo
    folder = repo.get_folder(folder_id, user["id"])
    if not folder:
        return _respond("Folder not found.", ok=False, status=404)
    name = (request.form.get("name") or "").strip()
    if not name:
        return _respond("Enter a name.", ok=False, status=400)
    fields = {"name": name[:60]}
    color = request.form.get("color")
    icon = request.form.get("icon")
    if color:
        fields["color"] = color
    if icon:
        fields["icon"] = icon
    repo.rename_folder(folder_id, user["id"], **fields)
    _c().invalidate(user["id"])
    if wants_json():
        return jsonify({"ok": True, "message": "Folder updated."})
    flash("Folder updated.", "success")
    return redirect(url_for("folders.index"))


@bp.route("/folders/<folder_id>/delete", methods=["POST"])
@login_required
def delete(folder_id):
    user = _user()
    repo = _c().repo
    folder = repo.get_folder(folder_id, user["id"])
    if not folder:
        return _respond("Folder not found.", ok=False, status=404)
    repo.delete_folder(folder_id, user["id"])
    repo.log(user["id"], "folder_delete", folder=folder, meta={"name": folder["name"]})
    _c().invalidate(user["id"])
    if wants_json():
        return jsonify({"ok": True, "message": "Folder deleted. Its documents were kept."})
    flash("Folder deleted. Its documents were kept.", "success")
    return redirect(url_for("folders.index"))


def _respond(message, ok=True, status=200):
    if wants_json():
        return jsonify({"ok": ok, "error": None if ok else message, "message": message}), status
    flash(message, "success" if ok else "error")
    return redirect(url_for("folders.index"))
