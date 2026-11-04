"""AI assistant endpoints. Inert until a provider is configured and consented."""

from flask import Blueprint, current_app, jsonify, request

from .. import ai as ai_module
from ..security import current_user, login_required

bp = Blueprint("ai_routes", __name__)


def _guard():
    """Return (document, error_response) after checking consent and setup."""
    status = ai_module.status()
    if not status["configured"]:
        return None, (
            jsonify(
                {
                    "ok": False,
                    "code": "not_configured",
                    "error": (
                        "AI features are not switched on for this Doxari server yet. "
                        "An administrator can enable them with AI_PROVIDER and AI_API_KEY."
                    ),
                }
            ),
            503,
        )
    user = current_user()
    prefs = current_app.extensions["doxari"].prefs(user["id"])
    if not prefs.get("ai_consent"):
        return None, (
            jsonify(
                {
                    "ok": False,
                    "code": "consent_required",
                    "error": (
                        "Turn on the AI data-processing option in Settings first. "
                        "It explains that document text is sent to the AI provider."
                    ),
                }
            ),
            403,
        )
    payload = request.get_json(silent=True) or {}
    document_id = payload.get("document_id")
    repo = current_app.extensions["doxari"].repo
    document = repo.get_document(document_id, user["id"])
    if not document:
        return None, (jsonify({"ok": False, "error": "Document not found."}), 404)
    return document, None


@bp.route("/api/ai/status")
@login_required
def status():
    user = current_user()
    prefs = current_app.extensions["doxari"].prefs(user["id"])
    info = ai_module.status()
    info["consent"] = bool(prefs.get("ai_consent"))
    return jsonify({"ok": True, **info})


@bp.route("/api/ai/summarize", methods=["POST"])
@login_required
def summarize():
    document, error = _guard()
    if error:
        return error
    payload = request.get_json(silent=True) or {}
    style = payload.get("style") or "short"
    repo = current_app.extensions["doxari"].repo
    text = ai_module.text_for_document(current_app.extensions["doxari"].api, repo, document)
    if len(text.strip()) < 40:
        return jsonify(
            {
                "ok": False,
                "error": (
                    "There isn't enough readable text in this file to summarise. "
                    "Scanned PDFs and images need text recognition, which isn't enabled."
                ),
            }
        ), 422
    try:
        result = ai_module.summarise(text, document["filename"], style)
    except ai_module.AIUnavailable as failure:
        return jsonify({"ok": False, "error": str(failure)}), 502
    repo.log(current_user()["id"], "ai", document=document, meta={"task": "summarise"})
    return jsonify({"ok": True, "result": result, "provider": ai_module.status()["label"]})


@bp.route("/api/ai/describe", methods=["POST"])
@login_required
def describe():
    document, error = _guard()
    if error:
        return error
    container = current_app.extensions["doxari"]
    text = ai_module.text_for_document(container.api, container.repo, document)
    if len(text.strip()) < 40:
        return jsonify({"ok": False, "error": "Not enough readable text in this file."}), 422
    try:
        description = ai_module.describe(text, document["filename"])
    except ai_module.AIUnavailable as failure:
        return jsonify({"ok": False, "error": str(failure)}), 502
    if container.repo.schema.documents_has("description"):
        container.repo.update_document(document["id"], current_user()["id"], description=description)
    container.repo.log(current_user()["id"], "ai", document=document, meta={"task": "describe"})
    return jsonify(
        {
            "ok": True,
            "result": description,
            "saved": container.repo.schema.documents_has("description"),
        }
    )


@bp.route("/api/ai/keywords", methods=["POST"])
@login_required
def keywords():
    document, error = _guard()
    if error:
        return error
    container = current_app.extensions["doxari"]
    text = ai_module.text_for_document(container.api, container.repo, document)
    if len(text.strip()) < 40:
        return jsonify({"ok": False, "error": "Not enough readable text in this file."}), 422
    try:
        tags = ai_module.keywords(text, document["filename"])
    except ai_module.AIUnavailable as failure:
        return jsonify({"ok": False, "error": str(failure)}), 502
    container.repo.log(current_user()["id"], "ai", document=document, meta={"task": "keywords"})
    return jsonify({"ok": True, "tags": tags, "saved": False})


@bp.route("/api/ai/ask", methods=["POST"])
@login_required
def ask():
    document, error = _guard()
    if error:
        return error
    payload = request.get_json(silent=True) or {}
    question = (payload.get("question") or "").strip()
    if len(question) < 3:
        return jsonify({"ok": False, "error": "Ask a question about this document."}), 400
    container = current_app.extensions["doxari"]
    text = ai_module.text_for_document(container.api, container.repo, document)
    if len(text.strip()) < 40:
        return jsonify({"ok": False, "error": "Not enough readable text in this file to answer from."}), 422
    try:
        answer = ai_module.answer(question, text, document["filename"])
    except ai_module.AIUnavailable as failure:
        return jsonify({"ok": False, "error": str(failure)}), 502
    container.repo.log(current_user()["id"], "ai", document=document, meta={"task": "ask"})
    return jsonify({"ok": True, "result": answer})
