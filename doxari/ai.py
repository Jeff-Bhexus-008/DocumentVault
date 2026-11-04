"""Optional AI document assistant.

Nothing here runs unless an AI provider is configured through environment
variables, and nothing is ever sent to a provider until the signed-in user has
explicitly turned on AI features and confirmed the data-processing note.
API keys stay on the server.
"""

import json
import logging
import re

import requests

from .config import AI_MAX_CHARS, ai_settings
from .models import extension_of, parse_iso

log = logging.getLogger(__name__)


class AIUnavailable(Exception):
    pass


class AIConsentRequired(Exception):
    pass


def status():
    settings = ai_settings()
    if not settings:
        return {
            "configured": False,
            "label": None,
            "model": None,
            "note": (
                "AI features are ready but not switched on. Add AI_PROVIDER and "
                "AI_API_KEY on the server to enable summaries, key points and "
                "question answering for your own documents."
            ),
        }
    return {
        "configured": True,
        "label": settings["label"],
        "model": settings["model"],
        "note": (
            f"AI features use {settings['label']} ({settings['model']}). Document text "
            "you choose to analyse is sent to that provider from the Doxari server."
        ),
    }


def _chat(system, prompt, *, max_tokens=700):
    settings = ai_settings()
    if not settings:
        raise AIUnavailable("AI features are not configured on this server yet.")
    payload = {
        "model": settings["model"],
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt[:AI_MAX_CHARS]},
        ],
        "temperature": 0.2,
        "max_tokens": max_tokens,
    }
    try:
        response = requests.post(
            f"{settings['base_url']}/chat/completions",
            headers={
                "Authorization": f"Bearer {settings['api_key']}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=60,
        )
    except requests.RequestException:
        raise AIUnavailable("The AI provider could not be reached. Please try again.")

    if response.status_code >= 400:
        log.warning("AI provider error %s: %s", response.status_code, response.text[:300])
        raise AIUnavailable("The AI provider returned an error. Please try again shortly.")
    try:
        data = response.json()
        return (data["choices"][0]["message"]["content"] or "").strip()
    except (ValueError, KeyError, IndexError):
        raise AIUnavailable("The AI provider sent an unexpected response.")


SYSTEM = (
    "You are Doxari's document assistant. You help a person understand their own "
    "stored documents. Be concise, factual and never invent details that are not "
    "in the document. Reply in plain text without markdown headings."
)


def summarise(text, filename, detail="short"):
    style = {
        "short": "a 3-4 sentence summary",
        "bullets": "5 to 7 short bullet-style lines using '• ' as the bullet",
        "keypoints": "the key points, decisions, dates, amounts and action items as '• ' bullets",
    }.get(detail, "a 3-4 sentence summary")
    prompt = f"Document name: {filename}\n\nDocument text:\n{text}\n\nWrite {style}."
    return _chat(SYSTEM, prompt)


def describe(text, filename):
    prompt = (
        f"Document name: {filename}\n\nDocument text:\n{text}\n\n"
        "Write one friendly sentence (max 25 words) describing what this document is, "
        "suitable as a description in a document manager."
    )
    return _chat(SYSTEM, prompt, max_tokens=120)


def keywords(text, filename):
    prompt = (
        f"Document name: {filename}\n\nDocument text:\n{text}\n\n"
        "Return 5 to 8 short tags (1-2 words each) that describe this document, "
        "as a JSON array of strings and nothing else."
    )
    raw = _chat(SYSTEM, prompt, max_tokens=120)
    match = re.search(r"\[.*\]", raw, re.S)
    if not match:
        return []
    try:
        tags = json.loads(match.group(0))
    except ValueError:
        return []
    cleaned = []
    for tag in tags:
        if isinstance(tag, str):
            tag = tag.strip().strip("#").lower()
            if tag and len(tag) <= 24 and tag not in cleaned:
                cleaned.append(tag)
    return cleaned[:8]


def answer(question, text, filename):
    prompt = (
        f"Document name: {filename}\n\nDocument text:\n{text}\n\n"
        f"Question: {question}\n\n"
        "Answer only from the document. If the answer is not in the document, say so plainly."
    )
    return _chat(SYSTEM, prompt, max_tokens=500)


# ---------------------------------------------------------------- natural language search
TYPE_WORDS = {
    "pdf": ("pdf",),
    "pdfs": ("pdf",),
    "image": ("image",),
    "images": ("image",),
    "photo": ("image",),
    "photos": ("image",),
    "picture": ("image",),
    "pictures": ("image",),
    "spreadsheet": ("spreadsheet",),
    "spreadsheets": ("spreadsheet",),
    "excel": ("spreadsheet",),
    "word": ("document",),
    "documents": ("document",),
    "doc": ("document",),
    "docs": ("document",),
    "presentation": ("presentation",),
    "presentations": ("presentation",),
    "slides": ("presentation",),
    "powerpoint": ("presentation",),
    "text": ("text",),
    "zip": ("archive",),
    "archives": ("archive",),
}

STOP_WORDS = {
    "find", "show", "me", "my", "the", "a", "an", "all", "of", "in", "from", "for",
    "please", "get", "documents", "document", "files", "file", "that", "with", "and",
    "recent", "recently", "latest", "newest", "most", "uploaded", "added", "modified",
    "oldest", "older", "favorite", "favorites", "starred", "this", "last", "week",
    "month", "year", "today", "yesterday",
}


def plan_search(query, folder_names=None):
    """Turn a natural-language query into the filters Doxari already supports.

    This is deterministic and needs no external provider, so natural-language
    search works even when AI is switched off.
    """
    text = (query or "").strip().lower()
    plan = {
        "search": "",
        "group": None,
        "extensions": [],
        "favorites": False,
        "sort": "newest",
        "date_from": None,
        "triggered": False,
        "explanation": [],
    }
    if not text:
        return plan

    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)

    if re.search(r"\b(favorite|favorites|starred)\b", text):
        plan["favorites"] = True
        plan["triggered"] = True
        plan["explanation"].append("only favorites")
    if re.search(r"\b(recent|recently|latest|newest)\b", text):
        plan["sort"] = "newest"
        plan["triggered"] = True
        plan["explanation"].append("newest first")
    if re.search(r"\b(oldest|earliest)\b", text):
        plan["sort"] = "oldest"
        plan["triggered"] = True
        plan["explanation"].append("oldest first")
    if re.search(r"\b(largest|biggest)\b", text):
        plan["sort"] = "largest"
        plan["triggered"] = True
        plan["explanation"].append("largest first")

    if re.search(r"\btoday\b", text):
        plan["date_from"] = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        plan["triggered"] = True
        plan["explanation"].append("uploaded today")
    elif re.search(r"\byesterday\b", text):
        start = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        plan["date_from"] = start.isoformat()
        plan["triggered"] = True
        plan["explanation"].append("uploaded since yesterday")
    else:
        match = re.search(r"\b(?:last|past|this)\s+(\d+)?\s*(day|days|week|weeks|month|months|year|years)\b", text)
        if match:
            amount = int(match.group(1) or 1)
            unit = match.group(2)
            days = amount * {"day": 1, "days": 1, "week": 7, "weeks": 7,
                             "month": 30, "months": 30, "year": 365, "years": 365}[unit]
            plan["date_from"] = (now - timedelta(days=days)).isoformat()
            plan["triggered"] = True
            plan["explanation"].append(f"uploaded in the last {days} day{'s' if days > 1 else ''}")

    words = re.findall(r"[a-z0-9][a-z0-9'&.-]*", text)
    for word in words:
        if word in TYPE_WORDS:
            group = TYPE_WORDS[word][0]
            if group not in {"image", "document", "spreadsheet", "presentation", "text", "archive"}:
                plan["extensions"].append(group)
            else:
                plan["group"] = group
            plan["triggered"] = True
            if group not in [e.get("group") for e in plan["explanation"] if isinstance(e, dict)]:
                plan["explanation"].append({"group": group})

    # Folder / category match, e.g. "school certificates"
    matched_folder = None
    for folder in folder_names or []:
        if folder and folder.lower() in text:
            matched_folder = folder
            break
    if matched_folder:
        plan["folder"] = matched_folder
        plan["triggered"] = True
        plan["explanation"].append(f"inside “{matched_folder}”")

    keywords = [w for w in words if w not in STOP_WORDS and w not in TYPE_WORDS]
    # Drop words already used by a folder match so the search stays useful.
    if matched_folder:
        folder_words = set(re.findall(r"[a-z0-9]+", matched_folder.lower()))
        keywords = [w for w in keywords if w not in folder_words]
    plan["search"] = " ".join(keywords[:6])

    plan["explanation"] = [
        item if isinstance(item, str) else dict(item) for item in plan["explanation"]
    ]
    return plan


# ---------------------------------------------------------------- document text
def text_for_document(api, repo, document):
    """Fetch the text used for AI analysis, with a size guard."""
    if repo.schema.full_text:
        stored = document.get("content_text")
        if stored:
            return stored
    try:
        response = api.storage_download(document["storage_path"])
        blob = response.content
    except Exception:
        return ""
    from .services import extract_text

    return extract_text(blob, document.get("filename") or "", limit=AI_MAX_CHARS)


def can_analyse(filename):
    ext = extension_of(filename)
    return ext in {"pdf", "txt", "md", "csv", "json"}
