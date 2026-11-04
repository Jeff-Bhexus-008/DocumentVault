"""Data access for Doxari.

Every function that touches a user's data takes the caller's `user_id` and
applies it as a database filter. Ownership is therefore enforced in one place
and cannot be forgotten in a view. Requests that reference a document by id
always resolve it through `get_document`, which returns None when the record
does not belong to the caller, so changing an id in a URL can never reach
someone else's file.
"""

import calendar
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

from .config import (
    DEFAULT_CATEGORIES,
    FILE_TYPES,
    FALLBACK_CATEGORY,
    FILE_GROUPS,
)

# ---------------------------------------------------------------- helpers


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def parse_iso(value):
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S"):
            try:
                parsed = datetime.strptime(text, fmt)
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    return None


def extension_of(filename):
    if "." not in (filename or ""):
        return ""
    return filename.rsplit(".", 1)[1].lower()


def type_info(filename):
    ext = extension_of(filename)
    mime, label, group = FILE_TYPES.get(ext, ("application/octet-stream", ext.upper() or "File", "other"))
    return {"ext": ext, "mime": mime, "label": label, "group": group}


def human_size(size):
    size = float(size or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.1f} {unit}"
        size /= 1024


def display_date(value):
    parsed = parse_iso(value)
    if not parsed:
        return "—"
    return parsed.strftime("%d %b %Y")


def display_datetime(value):
    parsed = parse_iso(value)
    if not parsed:
        return "—"
    return parsed.strftime("%d %b %Y, %H:%M")


def relative_time(value):
    parsed = parse_iso(value)
    if not parsed:
        return "—"
    delta = datetime.now(timezone.utc) - parsed
    seconds = delta.total_seconds()
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)} h ago"
    if seconds < 604800:
        return f"{int(seconds // 86400)} d ago"
    return parsed.strftime("%d %b %Y")


def escape_filter(value):
    """Escape a value used inside a PostgREST filter expression."""
    return str(value).replace("\\", "\\\\").replace(",", " ").replace(")", " ").replace("(", " ").replace("*", "")


# ---------------------------------------------------------------- storage index

class StorageIndex:
    """Maps `user_id/path` -> real object size and timestamp.

    Supabase Storage already knows every file's size and mime type, so Doxari
    can show real sizes on day one even before the optional `size` column is
    added by the migration. Results are cached briefly per user to keep the
    dashboard to a single storage call.
    """

    def __init__(self, api, ttl=120):
        self.api = api
        self.ttl = ttl
        self._cache = {}
        self._lock = threading.Lock()

    def get(self, user_id, force=False):
        with self._lock:
            entry = self._cache.get(user_id)
            if entry and not force and (time.time() - entry["at"]) < self.ttl:
                return entry["data"]
        data = {}
        offset = 0
        for _ in range(20):
            try:
                objects = self.api.storage_list(user_id, limit=100, offset=offset)
            except Exception:
                break
            if not objects:
                break
            for item in objects:
                name = item.get("name") or ""
                if not name:
                    continue
                meta = item.get("metadata") or {}
                path = f"{user_id}/{name}"
                data[path] = {
                    "size": int(meta.get("size") or 0),
                    "mime": meta.get("mimetype") or "",
                    "updated": item.get("updated_at") or item.get("created_at"),
                }
            if len(objects) < 100:
                break
            offset += 100
        with self._lock:
            self._cache[user_id] = {"at": time.time(), "data": data}
        return data

    def invalidate(self, user_id):
        with self._lock:
            self._cache.pop(user_id, None)


# ---------------------------------------------------------------- repository

def active_shares(rows):
    """Share rows that are still live.

    Rows written before the database default existed can carry a NULL
    `revoked`, so a plain `revoked=eq.false` filter would hide them.
    Filtering here works on both migrated and un-migrated data.
    """
    return [row for row in (rows or []) if not row.get("revoked")]


class Repository:
    def __init__(self, api, schema):
        self.api = api
        self.schema = schema
        self.storage = StorageIndex(api)

    # -------------------------------------------------------------- utility
    def doc_select(self):
        """Columns to read from `documents`, limited to what really exists."""
        base = ["id", "user_id", "filename", "storage_path", "folder", "favorite", "created_at"]
        optional = [
            "size",
            "mime_type",
            "description",
            "tags",
            "deleted_at",
            "last_accessed_at",
            "updated_at",
            "folder_id",
        ]
        columns = list(base)
        for column in optional:
            if self.schema.documents_has(column):
                columns.append(column)
        return ",".join(columns)

    def enrich(self, documents, user_id, index=None):
        """Attach display metadata derived from the storage layer."""
        if not documents:
            return []
        index = index if index is not None else self.storage.get(user_id)
        out = []
        for doc in documents:
            info = type_info(doc.get("filename"))
            stored = index.get(doc.get("storage_path") or "") or {}
            size = doc.get("size") or stored.get("size") or 0
            mime = doc.get("mime_type") or stored.get("mime") or info["mime"]
            created = doc.get("created_at")
            updated = doc.get("updated_at") or stored.get("updated") or created
            item = dict(doc)
            item.update(
                {
                    "ext": info["ext"],
                    "type_label": info["label"],
                    "type_group": info["group"],
                    "size": int(size or 0),
                    "size_display": human_size(size),
                    "mime": mime,
                    "created_display": display_date(created),
                    "created_relative": relative_time(created),
                    "updated_display": display_datetime(updated),
                    "updated_relative": relative_time(updated),
                    "updated_raw": updated or created,
                    "folder_name": doc.get("folder") or FALLBACK_CATEGORY,
                    "is_trashed": bool(doc.get("deleted_at")),
                    "can_preview": info["ext"] in {"pdf", "txt", "md", "csv", "json"} or info["group"] == "image",
                    "tags": doc.get("tags") or [],
                }
            )
            out.append(item)
        return out

    # -------------------------------------------------------------- documents
    def _document_filters(
        self,
        *,
        search=None,
        extensions=None,
        group=None,
        folder=None,
        folder_id=None,
        favorites=False,
        trashed=False,
        date_from=None,
        date_to=None,
    ):
        filters = []
        or_groups = []
        schema = self.schema

        # Trash state ---------------------------------------------------
        if schema.trash:
            filters.append(("deleted_at", "not.is.null" if trashed else "is.null"))
        elif trashed:
            # No trash column: a "trash" listing is genuinely empty.
            filters.append(("id", "eq.00000000-0000-0000-0000-000000000000"))

        if favorites:
            filters.append(("favorite", "eq.true"))

        if folder_id and schema.folders:
            filters.append(("folder_id", f"eq.{folder_id}"))
        elif folder:
            filters.append(("folder", f"eq.{escape_filter(folder)}"))

        if date_from:
            filters.append(("created_at", f"gte.{date_from}"))
        if date_to:
            filters.append(("created_at", f"lte.{date_to}"))

        # Search --------------------------------------------------------
        if search:
            term = escape_filter(search.strip())
            if term:
                clauses = [
                    f"filename.ilike.*{term}*",
                    f"folder.ilike.*{term}*",
                ]
                if schema.documents_has("description"):
                    clauses.append(f"description.ilike.*{term}*")
                if schema.full_text:
                    clauses.append(f"content_text.ilike.*{term}*")
                or_groups.append(clauses)

        # File type -----------------------------------------------------
        resolved = list(extensions or [])
        if group and group in FILE_GROUPS:
            resolved += [ext for ext, (_, _, g) in FILE_TYPES.items() if g == group]
        if resolved:
            or_groups.append([f"filename.ilike.*.{ext}" for ext in dict.fromkeys(resolved)])

        return filters, or_groups

    def _params(self, filters, or_groups, select, order=None, limit=None, offset=None):
        params = {"select": select}
        for column, expression in filters:
            params[column] = expression
        if len(or_groups) == 1:
            params["or"] = "(" + ",".join(or_groups[0]) + ")"
        elif len(or_groups) > 1:
            inner = ",".join("or=(" + ",".join(group) + ")" for group in or_groups)
            params["and"] = f"({inner})"
        if order:
            params["order"] = order
        if limit is not None:
            params["limit"] = int(limit)
        if offset:
            params["offset"] = int(offset)
        return params

    SORT_MAP = {
        "name": "filename.asc",
        "name_desc": "filename.desc",
        "newest": "created_at.desc",
        "oldest": "created_at.asc",
        "modified": None,  # resolved below
        "largest": None,
        "smallest": None,
        "type": None,
    }

    def list_documents(
        self,
        user_id,
        *,
        search=None,
        extensions=None,
        group=None,
        folder=None,
        folder_id=None,
        favorites=False,
        trashed=False,
        date_from=None,
        date_to=None,
        sort="newest",
        limit=24,
        offset=0,
        with_total=True,
    ):
        filters, or_groups = self._document_filters(
            search=search,
            extensions=extensions,
            group=group,
            folder=folder,
            folder_id=folder_id,
            favorites=favorites,
            trashed=trashed,
            date_from=date_from,
            date_to=date_to,
        )
        filters = [(f"user_id", f"eq.{user_id}")] + filters
        select = self.doc_select()

        order, python_sort = self._resolve_sort(sort)

        params = self._params(filters, or_groups, select, order)
        total = None
        try:
            if python_sort or not with_total:
                rows = self.api.rest(
                    "documents", params={**params, "limit": 1000 if python_sort else max(limit, 1)}
                ) or []
                if python_sort:
                    rows = self._python_sort(rows, python_sort, user_id)
                    total = len(rows)
                    rows = rows[offset: offset + limit]
                total = total if total is not None else len(rows)
            else:
                params.update({"limit": limit, "offset": offset})
                rows, total = self.api.rest_page("documents", params=params, count=True)
        except Exception:
            rows, total = [], 0

        documents = self.enrich(rows, user_id)
        return documents, (total if total is not None else len(documents))

    def _resolve_sort(self, sort):
        sort = sort or "newest"
        if sort == "modified":
            if self.schema.documents_has("updated_at"):
                return "updated_at.desc", None
            return "created_at.desc", None
        if sort in {"largest", "smallest"}:
            if self.schema.documents_has("size"):
                return f"size.{'desc' if sort == 'largest' else 'asc'}", None
            return None, sort
        if sort == "type":
            return "filename.asc", None
        return self.SORT_MAP.get(sort, "created_at.desc"), None

    def _python_sort(self, rows, mode, user_id):
        documents = self.enrich(rows, user_id)
        reverse = mode == "largest"
        return sorted(documents, key=lambda d: d.get("size") or 0, reverse=reverse)

    def get_document(self, document_id, user_id, *, include_trashed=True):
        """Return the document only if the caller owns it."""
        if not document_id:
            return None
        params = {
            "select": self.doc_select(),
            "id": f"eq.{document_id}",
            "user_id": f"eq.{user_id}",
            "limit": 1,
        }
        try:
            rows = self.api.rest("documents", params=params) or []
        except Exception:
            return None
        if not rows:
            return None
        doc = rows[0]
        if not include_trashed and doc.get("deleted_at"):
            return None
        enriched = self.enrich([doc], user_id)
        return enriched[0] if enriched else doc

    def create_document(self, user_id, *, filename, storage_path, folder=None, folder_id=None,
                        size=None, mime_type=None, description=None, tags=None, content_text=None):
        payload = {"user_id": user_id, "filename": filename, "storage_path": storage_path}
        if folder:
            payload["folder"] = folder
        if folder_id and self.schema.folders:
            payload["folder_id"] = folder_id
        if self.schema.documents_has("size") and size is not None:
            payload["size"] = int(size)
        if self.schema.documents_has("mime_type") and mime_type:
            payload["mime_type"] = mime_type
        if self.schema.documents_has("description") and description:
            payload["description"] = description
        if self.schema.tags and tags:
            payload["tags"] = tags
        if self.schema.full_text and content_text:
            payload["content_text"] = content_text[:200000]
        if self.schema.documents_has("updated_at"):
            payload["updated_at"] = now_iso()
        rows = self.api.rest(
            "documents", "POST", json=payload, prefer="return=representation"
        )
        return (rows or [payload])[0]

    def update_document(self, document_id, user_id, **fields):
        payload = {}
        for key, value in fields.items():
            if key in {"size", "mime_type", "description", "tags", "last_accessed_at",
                       "updated_at", "folder_id", "folder", "filename", "favorite", "deleted_at"}:
                payload[key] = value
        if not payload:
            return True
        if self.schema.documents_has("updated_at") and "updated_at" not in payload:
            payload["updated_at"] = now_iso()
        self.api.rest(
            "documents",
            "PATCH",
            params={"id": f"eq.{document_id}", "user_id": f"eq.{user_id}"},
            json=payload,
            prefer="return=minimal",
        )
        return True

    def touch(self, document_id, user_id):
        if self.schema.documents_has("last_accessed_at"):
            try:
                self.api.rest(
                    "documents",
                    "PATCH",
                    params={"id": f"eq.{document_id}", "user_id": f"eq.{user_id}"},
                    json={"last_accessed_at": now_iso()},
                    prefer="return=minimal",
                )
            except Exception:
                pass

    def counts(self, user_id):
        """Cheap aggregate counts straight from the database."""
        result = {
            "documents": 0,
            "folders": 0,
            "trash": 0,
            "favorites": 0,
            "shared_with_me": 0,
            "shared_by_me": 0,
        }
        base = {"user_id": f"eq.{user_id}"}

        def total(extra=None):
            params = dict(base)
            if self.schema.trash:
                params["deleted_at"] = "is.null"
            if extra:
                params.update(extra)
            try:
                _, count = self.api.rest_page(
                    "documents", params={**params, "select": "id", "limit": 1}, count=True
                )
                return count or 0
            except Exception:
                return 0

        result["documents"] = total()
        result["favorites"] = total({"favorite": "eq.true"})
        if self.schema.trash:
            try:
                _, count = self.api.rest_page(
                    "documents",
                    params={"user_id": f"eq.{user_id}", "deleted_at": "not.is.null",
                            "select": "id", "limit": 1},
                    count=True,
                )
                result["trash"] = count or 0
            except Exception:
                result["trash"] = 0
        if self.schema.folders:
            try:
                _, count = self.api.rest_page(
                    "folders", params={"user_id": f"eq.{user_id}", "select": "id", "limit": 1},
                    count=True,
                )
                result["folders"] = count or 0
            except Exception:
                result["folders"] = 0
        if self.schema.sharing:
            try:
                rows = self.api.rest(
                    "document_shares",
                    params={"shared_with_id": f"eq.{user_id}", "select": "id,revoked"},
                )
                result["shared_with_me"] = len(active_shares(rows))
            except Exception:
                result["shared_with_me"] = 0
            try:
                rows = self.api.rest(
                    "document_shares",
                    params={"owner_id": f"eq.{user_id}", "select": "id,revoked"},
                )
                result["shared_by_me"] = len(active_shares(rows))
            except Exception:
                result["shared_by_me"] = 0
        return result

    def storage_totals(self, user_id):
        index = self.storage.get(user_id)
        total = sum(item["size"] for item in index.values())
        return {"used": total, "used_display": human_size(total), "files": len(index)}

    def recent_uploads(self, user_id, limit=5):
        documents, _ = self.list_documents(user_id, sort="newest", limit=limit, with_total=False)
        return documents

    def recently_accessed(self, user_id, limit=5):
        if self.schema.documents_has("last_accessed_at"):
            filters = [("user_id", f"eq.{user_id}")]
            if self.schema.trash:
                filters.append(("deleted_at", "is.null"))
            params = {
                "select": self.doc_select(),
                "user_id": f"eq.{user_id}",
                "last_accessed_at": "not.is.null",
                "order": "last_accessed_at.desc",
                "limit": limit,
            }
            if self.schema.trash:
                params["deleted_at"] = "is.null"
            try:
                rows = self.api.rest("documents", params=params) or []
                return self.enrich(rows, user_id)
            except Exception:
                pass
        return self.recent_uploads(user_id, limit)

    # -------------------------------------------------------------- folders
    def folder_select(self):
        columns = ["id", "user_id", "name", "created_at"]
        for optional in ("parent_id", "color", "icon", "description", "updated_at"):
            if self.schema.has_columns("folders", [optional]):
                columns.append(optional)
        return ",".join(columns)

    def list_folders(self, user_id):
        if not self.schema.folders:
            return []
        try:
            rows = self.api.rest(
                "folders",
                params={"select": self.folder_select(), "user_id": f"eq.{user_id}",
                        "order": "name.asc"},
            ) or []
        except Exception:
            return []
        by_id = {row["id"]: row for row in rows}
        for row in rows:
            trail, cursor, guard = [], row, 0
            while cursor and guard < 10:
                trail.append(cursor.get("name") or "")
                parent_id = cursor.get("parent_id")
                cursor = by_id.get(parent_id) if parent_id else None
                guard += 1
            row["path"] = " / ".join(reversed(trail))
        return rows

    def folder_tree(self, user_id):
        folders = self.list_folders(user_id)
        by_parent = {}
        for folder in folders:
            by_parent.setdefault(folder.get("parent_id") or "root", []).append(folder)
        for folder in folders:
            folder["children"] = by_parent.get(folder["id"], [])
        return by_parent.get("root", [])

    def get_folder(self, folder_id, user_id):
        if not folder_id or not self.schema.folders:
            return None
        try:
            rows = self.api.rest(
                "folders",
                params={"select": self.folder_select(), "id": f"eq.{folder_id}",
                        "user_id": f"eq.{user_id}", "limit": 1},
            ) or []
        except Exception:
            return None
        return rows[0] if rows else None

    def create_folder(self, user_id, name, parent_id=None, color=None, icon=None):
        payload = {"user_id": user_id, "name": name}
        if self.schema.has_columns("folders", ["parent_id"]) and parent_id:
            payload["parent_id"] = parent_id
        if color and self.schema.has_columns("folders", ["color"]):
            payload["color"] = color
        if icon and self.schema.has_columns("folders", ["icon"]):
            payload["icon"] = icon
        rows = self.api.rest("folders", "POST", json=payload, prefer="return=representation")
        return (rows or [payload])[0]

    def rename_folder(self, folder_id, user_id, **fields):
        payload = {k: v for k, v in fields.items() if k in {"name", "color", "icon", "parent_id"}}
        if not payload:
            return True
        if self.schema.has_columns("folders", ["updated_at"]):
            payload["updated_at"] = now_iso()
        self.api.rest(
            "folders", "PATCH",
            params={"id": f"eq.{folder_id}", "user_id": f"eq.{user_id}"},
            json=payload, prefer="return=minimal",
        )
        return True

    def delete_folder(self, folder_id, user_id):
        """Delete a folder, moving its documents to the parent folder."""
        folder = self.get_folder(folder_id, user_id)
        if not folder:
            return False
        parent_id = folder.get("parent_id")
        # Detach documents from the folder but keep them safe.
        try:
            self.api.rest(
                "documents", "PATCH",
                params={"folder_id": f"eq.{folder_id}", "user_id": f"eq.{user_id}"},
                json={"folder_id": parent_id},
                prefer="return=minimal",
            )
        except Exception:
            pass
        try:
            self.api.rest(
                "folders", "PATCH",
                params={"parent_id": f"eq.{folder_id}", "user_id": f"eq.{user_id}"},
                json={"parent_id": parent_id},
                prefer="return=minimal",
            )
        except Exception:
            pass
        self.api.rest(
            "folders", "DELETE",
            params={"id": f"eq.{folder_id}", "user_id": f"eq.{user_id}"},
        )
        return True

    def folder_counts(self, user_id):
        """documents per folder id, plus per legacy category name."""
        counts = {"by_id": {}, "by_name": {}}
        if not self.schema.folders:
            return counts
        try:
            rows = self.api.rest(
                "documents",
                params={"select": "folder_id,folder", "user_id": f"eq.{user_id}", "limit": 1000},
            ) or []
        except Exception:
            return counts
        for row in rows:
            if row.get("folder_id"):
                counts["by_id"][row["folder_id"]] = counts["by_id"].get(row["folder_id"], 0) + 1
            name = row.get("folder") or FALLBACK_CATEGORY
            counts["by_name"][name] = counts["by_name"].get(name, 0) + 1
        return counts

    # -------------------------------------------------------------- activity
    def log(self, user_id, action, *, document=None, folder=None, meta=None):
        if not self.schema.activity:
            return
        payload = {"user_id": user_id, "action": action}
        if document:
            payload["document_id"] = document.get("id")
            payload["document_name"] = document.get("filename")
        if folder:
            payload["folder_id"] = folder.get("id") if isinstance(folder, dict) else str(folder)
        if meta:
            payload["meta"] = meta
        for key in list(payload.keys()):
            if key in {"action"}:
                continue
        try:
            self.api.rest("activity_log", "POST", json=payload, prefer="return=minimal")
        except Exception:
            # Activity history must never break the operation it records.
            pass

    def list_activity(self, user_id, limit=20):
        if not self.schema.activity:
            return []
        try:
            rows = self.api.rest(
                "activity_log",
                params={"select": "*", "user_id": f"eq.{user_id}",
                        "order": "created_at.desc", "limit": limit},
            ) or []
        except Exception:
            return []
        labels = {
            "upload": ("upload", "Uploaded"),
            "download": ("download", "Downloaded"),
            "rename": ("edit", "Renamed"),
            "move": ("folder", "Moved"),
            "delete": ("trash", "Moved to trash"),
            "restore": ("restore", "Restored"),
            "purge": ("trash", "Deleted permanently"),
            "favorite": ("star", "Favorited"),
            "unfavorite": ("star", "Removed from favorites"),
            "share": ("share", "Shared"),
            "unshare": ("share", "Revoked access"),
            "upload_failed": ("alert", "Upload failed"),
            "folder_create": ("folder", "Created folder"),
            "folder_delete": ("folder", "Deleted folder"),
            "copy": ("copy", "Duplicated"),
            "trash_empty": ("trash", "Emptied trash"),
            "settings": ("settings", "Updated settings"),
            "ai": ("sparkle", "Used AI assistant"),
        }
        for row in rows:
            icon, verb = labels.get(row.get("action"), ("file", row.get("action", "Updated")))
            row["icon"] = icon
            row["verb"] = verb
            row["when"] = relative_time(row.get("created_at"))
            row["document_name"] = row.get("document_name") or ""
        return rows

    # -------------------------------------------------------------- shares
    def share_document(self, owner_id, document, email, permission="view", user_id=None):
        payload = {
            "document_id": document["id"],
            "owner_id": owner_id,
            "permission": permission,
            "shared_with_email": email,
            "shared_with_id": user_id,
        }
        rows = self.api.rest(
            "document_shares", "POST", json=payload, prefer="return=representation"
        )
        return (rows or [payload])[0]

    def list_shares_for_document(self, document_id, owner_id):
        if not self.schema.sharing:
            return []
        try:
            rows = self.api.rest(
                "document_shares",
                params={"select": "*", "document_id": f"eq.{document_id}",
                        "owner_id": f"eq.{owner_id}",
                        "order": "created_at.desc"},
            )
            rows = active_shares(rows)
        except Exception:
            return []
        for row in rows:
            row["when"] = relative_time(row.get("created_at"))
        return rows

    def list_shared_with_me(self, user_id):
        if not self.schema.sharing:
            return []
        try:
            shares = self.api.rest(
                "document_shares",
                params={"select": "*", "shared_with_id": f"eq.{user_id}",
                        "order": "created_at.desc"},
            )
            shares = active_shares(shares)
        except Exception:
            return []
        documents = []
        for share in shares:
            doc = self.get_shared_document(share["document_id"], user_id)
            if doc:
                doc["share_permission"] = share.get("permission") or "view"
                doc["share_id"] = share.get("id")
                doc["shared_by"] = share.get("owner_id")
                doc["shared_when"] = relative_time(share.get("created_at"))
                documents.append(doc)
        return documents

    def get_shared_document(self, document_id, viewer_id):
        """A document the viewer may read because it was shared with them."""
        if not self.schema.sharing:
            return None
        try:
            rows = self.api.rest(
                "documents",
                params={"select": self.doc_select(), "id": f"eq.{document_id}", "limit": 1},
            ) or []
        except Exception:
            return None
        if not rows:
            return None
        doc = rows[0]
        if doc.get("deleted_at"):
            return None
        if doc.get("user_id") == viewer_id:
            enriched = self.enrich([doc], viewer_id)
            if enriched:
                enriched[0]["share_permission"] = "owner"
            return enriched[0] if enriched else doc
        share = self.share_permission(document_id, viewer_id)
        if not share:
            return None
        enriched = self.enrich([doc], doc.get("user_id"))
        if not enriched:
            return doc
        enriched[0]["share_permission"] = share.get("permission") or "view"
        return enriched[0]

    def share_permission(self, document_id, viewer_id):
        if not self.schema.sharing:
            return None
        try:
            rows = self.api.rest(
                "document_shares",
                params={"select": "*", "document_id": f"eq.{document_id}",
                        "shared_with_id": f"eq.{viewer_id}"},
            )
            rows = active_shares(rows)
        except Exception:
            return None
        return rows[0] if rows else None

    def revoke_share(self, share_id, owner_id):
        self.api.rest(
            "document_shares", "PATCH",
            params={"id": f"eq.{share_id}", "owner_id": f"eq.{owner_id}"},
            json={"revoked": True}, prefer="return=minimal",
        )
        return True

    def find_user_by_email(self, email):
        """Look up a registered Doxari user through the Supabase admin API."""
        if not email:
            return None
        try:
            response = self.api._request(
                "GET",
                f"{self.api.url}/auth/v1/admin/users",
                headers=self.api._headers(True),
                params={"page": 1, "per_page": 200},
            )
            if response.status_code != 200:
                return None
            payload = response.json() or {}
            users = payload.get("users") if isinstance(payload, dict) else payload
            target = email.strip().lower()
            for user in users or []:
                if (user.get("email") or "").lower() == target:
                    return {"id": user.get("id"), "email": user.get("email")}
        except Exception:
            return None
        return None

    # -------------------------------------------------------------- share links
    def create_share_link(self, owner_id, document, permission="view", expires_days=7):
        token = uuid.uuid4().hex + uuid.uuid4().hex[:8]
        expires_at = None
        if expires_days:
            expires_at = (datetime.now(timezone.utc) + timedelta(days=int(expires_days))).isoformat()
        payload = {
            "document_id": document["id"],
            "owner_id": owner_id,
            "token": token,
            "permission": permission,
            "expires_at": expires_at,
        }
        rows = self.api.rest("share_links", "POST", json=payload, prefer="return=representation")
        created = (rows or [payload])[0]
        created["url_hint"] = token
        return created

    def list_share_links(self, document_id, owner_id):
        if not self.schema.share_links:
            return []
        try:
            rows = self.api.rest(
                "share_links",
                params={"select": "*", "document_id": f"eq.{document_id}",
                        "owner_id": f"eq.{owner_id}",
                        "order": "created_at.desc"},
            )
            rows = active_shares(rows)
        except Exception:
            return []
        now = datetime.now(timezone.utc)
        for row in rows:
            expires = parse_iso(row.get("expires_at"))
            row["expired"] = bool(expires and expires < now)
            row["expires_display"] = display_date(row.get("expires_at")) if row.get("expires_at") else "No expiry"
        return rows

    def resolve_share_token(self, token):
        if not self.schema.share_links or not token:
            return None
        try:
            rows = self.api.rest(
                "share_links",
                params={"select": "*", "token": f"eq.{token}", "limit": 1},
            ) or []
        except Exception:
            return None
        if not rows:
            return None
        link = rows[0]
        if link.get("revoked"):
            return None
        expires = parse_iso(link.get("expires_at"))
        if expires and expires < datetime.now(timezone.utc):
            return None
        try:
            docs = self.api.rest(
                "documents",
                params={"select": self.doc_select(), "id": f"eq.{link['document_id']}", "limit": 1},
            ) or []
        except Exception:
            return None
        if not docs or docs[0].get("deleted_at"):
            return None
        enriched = self.enrich([docs[0]], docs[0].get("user_id"))
        document = enriched[0] if enriched else docs[0]
        document["share_permission"] = link.get("permission") or "view"
        link["document"] = document
        return link

    def revoke_share_link(self, link_id, owner_id):
        self.api.rest(
            "share_links", "PATCH",
            params={"id": f"eq.{link_id}", "owner_id": f"eq.{owner_id}"},
            json={"revoked": True}, prefer="return=minimal",
        )
        return True

    # -------------------------------------------------------------- preferences
    def get_preferences(self, user_id):
        defaults = {
            "theme": "system",
            "notifications": True,
            "onboarded": False,
            "view_mode": "grid",
            "ai_consent": False,
        }
        if not self.schema.preferences:
            return defaults
        try:
            rows = self.api.rest(
                "user_preferences",
                params={"select": "*", "user_id": f"eq.{user_id}", "limit": 1},
            ) or []
        except Exception:
            return defaults
        if not rows:
            return defaults
        prefs = defaults.copy()
        prefs.update({k: v for k, v in rows[0].items() if v is not None})
        return prefs

    def save_preferences(self, user_id, **fields):
        if not self.schema.preferences:
            return False
        allowed = {"theme", "notifications", "onboarded", "view_mode", "ai_consent"}
        payload = {k: v for k, v in fields.items() if k in allowed}
        if not payload:
            return False
        existing = self.api.rest(
            "user_preferences",
            params={"select": "id", "user_id": f"eq.{user_id}", "limit": 1},
        ) or []
        if existing:
            self.api.rest(
                "user_preferences", "PATCH",
                params={"user_id": f"eq.{user_id}"}, json=payload,
                prefer="return=minimal",
            )
        else:
            self.api.rest(
                "user_preferences", "POST",
                json={"user_id": user_id, **payload}, prefer="return=minimal",
            )
        return True

    # -------------------------------------------------------------- analytics
    def analytics(self, user_id):
        index = self.storage.get(user_id)
        try:
            rows = self.api.rest(
                "documents",
                params={"select": "id,filename,folder,created_at%s" % (
                    ",size" if self.schema.documents_has("size") else ""),
                    "user_id": f"eq.{user_id}", "limit": 1000},
            ) or []
        except Exception:
            rows = []

        total_docs = len(rows)
        by_group = {}
        by_ext = {}
        total_bytes = 0
        monthly = {}
        per_folder = {}

        for row in rows:
            info = type_info(row.get("filename"))
            group = info["group"]
            by_group[group] = by_group.get(group, 0) + 1
            by_ext[info["ext"] or "other"] = by_ext.get(info["ext"] or "other", 0) + 1
            stored = index.get(row.get("storage_path") or "") or {}
            size = int(row.get("size") or stored.get("size") or 0)
            total_bytes += size
            created = parse_iso(row.get("created_at"))
            if created:
                key = created.strftime("%Y-%m")
                monthly[key] = monthly.get(key, 0) + 1
            folder_name = row.get("folder") or FALLBACK_CATEGORY
            entry = per_folder.setdefault(folder_name, {"name": folder_name, "count": 0, "size": 0})
            entry["count"] += 1
            entry["size"] += size

        # Six months of upload activity, zero-filled so the chart is honest.
        months = []
        today = datetime.now(timezone.utc)
        for offset in range(5, -1, -1):
            month = today.month - offset
            year = today.year
            while month <= 0:
                month += 12
                year -= 1
            key = f"{year}-{month:02d}"
            months.append({
                "key": key,
                "label": calendar.month_abbr[month],
                "count": monthly.get(key, 0),
            })

        largest = sorted(
            self.enrich(rows, user_id, index), key=lambda d: d.get("size") or 0, reverse=True
        )[:5]

        group_labels = [
            {
                "key": group,
                "label": FILE_GROUPS.get(group, group.title()),
                "count": count,
            }
            for group, count in sorted(by_group.items(), key=lambda item: item[1], reverse=True)
        ]

        return {
            "total_documents": total_docs,
            "total_bytes": total_bytes,
            "total_size": human_size(total_bytes),
            "file_groups": group_labels,
            "extensions": sorted(by_ext.items(), key=lambda item: item[1], reverse=True)[:8],
            "months": months,
            "max_month": max([m["count"] for m in months] + [1]),
            "folders": sorted(per_folder.values(), key=lambda f: f["count"], reverse=True)[:6],
            "largest": largest,
            "has_history": any(m["count"] for m in months),
        }
