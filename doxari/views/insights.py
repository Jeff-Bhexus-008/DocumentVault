"""Search, analytics and activity history."""

from flask import Blueprint, current_app, jsonify, render_template, request

from .. import ai as ai_module
from ..config import FILE_GROUPS, STORAGE_LIMIT_MB
from ..models import human_size
from ..security import current_user, login_required
from ..views.documents import SORTS

bp = Blueprint("insights", __name__)


def _c():
    return current_app.extensions["doxari"]


@bp.route("/search")
@login_required
def search():
    user = current_user()
    repo = _c().repo
    query = (request.args.get("q") or "").strip()
    group = (request.args.get("type") or "").strip() or None
    folder_id = request.args.get("folder_id") or None
    sort = request.args.get("sort") or "newest"
    date_from = request.args.get("from") or None
    keyword_mode = (request.args.get("mode") or "smart") == "keyword"

    folders = repo.list_folders(user["id"]) if repo.schema.folders else []
    folder_names = [f["name"] for f in folders]
    plan = ai_module.plan_search(query, folder_names) if query else None

    documents, total = [], 0
    matched_folders = []
    active_filters = {}

    if query or group or folder_id:
        search_term = query
        search_group = group
        effective_folder_id = folder_id
        if plan and not keyword_mode and plan["triggered"]:
            search_term = plan["search"] or ""
            search_group = search_group or plan["group"]
            plan_folder = plan.get("folder")
            if plan_folder and not effective_folder_id:
                for folder in folders:
                    if folder["name"].lower() == plan_folder.lower():
                        effective_folder_id = folder["id"]
                        break
            sort = plan["sort"] if plan["sort"] != "newest" else sort
            date_from = date_from or plan.get("date_from")
        active_filters = {
            "search": search_term,
            "group": search_group,
            "folder_id": effective_folder_id,
            "sort": sort,
            "date_from": date_from,
        }
        smart = bool(plan and plan["triggered"] and not keyword_mode)
        documents, total = repo.list_documents(
            user["id"],
            search=search_term or None,
            group=search_group if search_group in FILE_GROUPS else None,
            folder_id=effective_folder_id,
            favorites=plan["favorites"] if smart else False,
            date_from=date_from,
            sort=sort,
            limit=30,
        )
        if query:
            needle = query.lower()
            matched_folders = [f for f in folders if needle and needle in f["name"].lower()]

    suggestions = [
        {"label": "PDFs uploaded this month", "url": "/search?q=pdf&from=" + _month_start()},
        {"label": "Favorites", "url": "/favorites"},
        {"label": "Images", "url": "/search?q=&type=image"},
        {"label": "Spreadsheets", "url": "/search?q=&type=spreadsheet"},
    ]

    return render_template(
        "insights/search.html",
        query=query,
        plan=plan,
        documents=documents,
        total=total,
        folders=folders,
        matched_folders=matched_folders,
        group=group,
        folder_id=folder_id,
        sort=sort,
        date_from=date_from,
        keyword_mode=keyword_mode,
        active_filters=active_filters,
        sorts=SORTS,
        suggestions=suggestions,
        file_groups=FILE_GROUPS,
        full_text=repo.schema.full_text,
    )


def _month_start():
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).date().isoformat()


@bp.route("/api/search/suggest")
@login_required
def suggest():
    """Filename suggestions for the global search box."""
    user = current_user()
    repo = _c().repo
    term = (request.args.get("q") or "").strip()
    if len(term) < 2:
        return jsonify({"ok": True, "results": []})
    documents, _ = repo.list_documents(user["id"], search=term, limit=6, with_total=False)
    folders = repo.list_folders(user["id"]) if repo.schema.folders else []
    folder_hits = [f for f in folders if term.lower() in f["name"].lower()][:3]
    return jsonify(
        {
            "ok": True,
            "results": [
                {
                    "type": "document",
                    "label": doc["filename"],
                    "meta": f"{doc['type_label']} · {doc['size_display']}",
                    "url": f"/documents/{doc['id']}",
                }
                for doc in documents
            ]
            + [
                {
                    "type": "folder",
                    "label": folder["name"],
                    "meta": "Folder",
                    "url": f"/documents?folder_id={folder['id']}",
                }
                for folder in folder_hits
            ],
        }
    )


@bp.route("/analytics")
@login_required
def analytics():
    user = current_user()
    repo = _c().repo
    data = repo.analytics(user["id"])
    counts = _c().user_counts(user["id"])
    storage = repo.storage_totals(user["id"])
    limit_bytes = STORAGE_LIMIT_MB * 1024 * 1024 if STORAGE_LIMIT_MB else 0
    return render_template(
        "insights/analytics.html",
        data=data,
        counts=counts,
        storage=storage,
        usage={
            "limit": limit_bytes,
            "limit_display": human_size(limit_bytes) if limit_bytes else None,
            "percent": min(100, round(storage["used"] / limit_bytes * 100, 1)) if limit_bytes else None,
            "remaining_display": human_size(max(0, limit_bytes - storage["used"])) if limit_bytes else None,
        },
    )


@bp.route("/activity")
@login_required
def activity():
    user = current_user()
    repo = _c().repo
    items = repo.list_activity(user["id"], limit=100)
    return render_template(
        "insights/activity.html",
        items=items,
        supported=repo.schema.activity,
        groups=_group_by_day(items),
    )


def _group_by_day(items):
    from ..models import parse_iso

    groups = []
    current_key = None
    for item in items:
        parsed = parse_iso(item.get("created_at"))
        key = parsed.strftime("%A, %d %B %Y") if parsed else "Unknown"
        if key != current_key:
            groups.append({"label": key, "items": []})
            current_key = key
        groups[-1]["items"].append(item)
    return groups


@bp.route("/api/notifications")
@login_required
def notifications():
    user = current_user()
    repo = _c().repo
    counts = _c().user_counts(user["id"])
    items = repo.list_activity(user["id"], limit=10)
    storage = repo.storage_totals(user["id"])
    alerts = []
    if STORAGE_LIMIT_MB:
        limit_bytes = STORAGE_LIMIT_MB * 1024 * 1024
        percent = storage["used"] / limit_bytes * 100 if limit_bytes else 0
        if percent >= 85:
            alerts.append(
                {
                    "icon": "alert",
                    "title": "Storage almost full",
                    "detail": f"You have used {percent:.0f}% of {human_size(limit_bytes)}.",
                    "url": "/settings#storage",
                }
            )
    if not repo.schema.activity:
        alerts.append(
            {
                "icon": "info",
                "title": "Activity history is off",
                "detail": "Run the Doxari database upgrade to record document events.",
                "url": "/settings#database",
            }
        )
    return jsonify(
        {
            "ok": True,
            "count": len(items),
            "items": [
                {
                    "icon": item.get("icon"),
                    "verb": item.get("verb"),
                    "name": item.get("document_name"),
                    "when": item.get("when"),
                    "document_id": item.get("document_id"),
                }
                for item in items
            ],
            "alerts": alerts,
            "trash": counts.get("trash", 0),
        }
    )
