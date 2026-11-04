"""Thin, explicit Supabase client.

The application talks to Supabase over its HTTP APIs (PostgREST, Storage and
Auth) with `requests`, which keeps the deployment exactly as it is today:
no extra services, no database driver, no connection pooling to manage.

Two keys are used on purpose:
  * SUPABASE_KEY        -> public/anon key, only for end-user auth calls.
  * SUPABASE_SECRET_KEY -> service key, only ever used server side, never
                           rendered into a template or returned by an API.
"""

import logging
import threading
import time
from urllib.parse import quote

import requests

log = logging.getLogger(__name__)

TIMEOUT = 30
STORAGE_TIMEOUT = 60


class SupabaseError(Exception):
    """A failed Supabase call with a safe, human readable message."""

    def __init__(self, message, status=None, code=None, details=None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.details = details

    def __str__(self):
        return self.message


def _describe(response):
    """Turn an error response into a clean message without leaking secrets."""
    try:
        payload = response.json()
    except ValueError:
        payload = None

    message = None
    code = None
    if isinstance(payload, dict):
        code = payload.get("code")
        message = (
            payload.get("message")
            or payload.get("msg")
            or payload.get("error_description")
            or payload.get("error")
        )
        if isinstance(message, dict):
            message = message.get("message")
    if not message:
        message = (response.text or "").strip()[:300] or f"HTTP {response.status_code}"
    return str(message), code


class SupabaseAPI:
    def __init__(self, url, anon_key, service_key, bucket="document"):
        self.url = (url or "").rstrip("/")
        self.anon_key = anon_key or ""
        self.service_key = service_key or self.anon_key
        self.bucket = bucket

    # ------------------------------------------------------------ plumbing
    @property
    def configured(self):
        return bool(self.url and self.anon_key)

    @property
    def storage_ready(self):
        return bool(self.url and self.service_key)

    def _headers(self, service=True, extra=None):
        key = self.service_key if service else self.anon_key
        headers = {"apikey": key, "Authorization": f"Bearer {key}"}
        if extra:
            headers.update(extra)
        return headers

    def _request(self, method, url, *, headers, params=None, json=None, data=None,
                 timeout=TIMEOUT, stream=False, retries=1):
        last_error = None
        for attempt in range(retries + 1):
            try:
                return requests.request(
                    method,
                    url,
                    headers=headers,
                    params=params,
                    json=json,
                    data=data,
                    timeout=timeout,
                    stream=stream,
                )
            except requests.RequestException as error:
                last_error = error
                if attempt < retries:
                    time.sleep(0.4 * (attempt + 1))
        raise SupabaseError(
            "Could not reach the storage service. Please try again."
            if not isinstance(last_error, requests.RequestException)
            else "Network error while contacting Doxari storage. Please retry.",
            status=0,
        )

    # ------------------------------------------------------------ PostgREST
    def rest(self, table, method="GET", *, params=None, json=None, prefer=None,
             timeout=TIMEOUT, retries=1):
        url = f"{self.url}/rest/v1/{table}"
        extra = {"Content-Type": "application/json"}
        if prefer:
            extra["Prefer"] = prefer
        response = self._request(
            method,
            url,
            headers=self._headers(True, extra),
            params=params,
            json=json,
            timeout=timeout,
            retries=retries,
        )
        if response.status_code >= 400:
            message, code = _describe(response)
            raise SupabaseError(message, response.status_code, code)
        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return None

    def rest_page(self, table, *, params=None, count=False):
        """Read one page and report the total when PostgREST returns it."""
        url = f"{self.url}/rest/v1/{table}"
        extra = {"Content-Type": "application/json"}
        if count:
            extra["Prefer"] = "count=exact"
        response = self._request(
            "GET", url, headers=self._headers(True, extra), params=params
        )
        if response.status_code >= 400:
            message, code = _describe(response)
            raise SupabaseError(message, response.status_code, code)

        total = None
        content_range = response.headers.get("Content-Range") or ""
        if "/" in content_range:
            tail = content_range.rsplit("/", 1)[1]
            if tail.isdigit():
                total = int(tail)
        rows = []
        if response.content:
            try:
                rows = response.json() or []
            except ValueError:
                rows = []
        return rows, total

    def ping(self):
        """Cheap health probe used by /healthz."""
        if not self.configured:
            return False, "Supabase is not configured"
        try:
            response = self._request(
                "HEAD",
                f"{self.url}/rest/v1/",
                headers=self._headers(True),
                timeout=10,
            )
            return response.status_code < 500, f"HTTP {response.status_code}"
        except SupabaseError as error:
            return False, error.message

    # ------------------------------------------------------------ Storage
    def _object_url(self, path, signed=False):
        quoted = quote(path, safe="/")
        suffix = "object/sign" if signed else "object"
        return f"{self.url}/storage/v1/{suffix}/{self.bucket}/{quoted}"

    def storage_upload(self, path, data, content_type):
        response = self._request(
            "POST",
            self._object_url(path),
            headers=self._headers(
                True,
                {
                    "Content-Type": content_type or "application/octet-stream",
                    "x-upsert": "false",
                },
            ),
            data=data,
            timeout=STORAGE_TIMEOUT,
            retries=1,
        )
        if response.status_code not in (200, 201):
            message, code = _describe(response)
            raise SupabaseError(f"Storage upload failed: {message}", response.status_code, code)
        return True

    def storage_copy(self, source_path, destination_path):
        """Copy an object inside the bucket (used by 'duplicate document')."""
        response = self._request(
            "POST",
            f"{self.url}/storage/v1/object/copy",
            headers=self._headers(True, {"Content-Type": "application/json"}),
            json={
                "bucketId": self.bucket,
                "sourceKey": source_path,
                "destinationKey": destination_path,
            },
            timeout=STORAGE_TIMEOUT,
        )
        if response.status_code not in (200, 201):
            message, code = _describe(response)
            raise SupabaseError(f"Could not copy the file: {message}", response.status_code, code)
        return True

    def storage_delete(self, path):
        response = self._request(
            "DELETE",
            self._object_url(path),
            headers=self._headers(True, {"Content-Type": "application/json"}),
            json={"prefixes": [path]},
            timeout=STORAGE_TIMEOUT,
        )
        if response.status_code not in (200, 204):
            # A missing object is not an error for us: the record still goes.
            if response.status_code == 404:
                return False
            message, code = _describe(response)
            raise SupabaseError(f"Storage delete failed: {message}", response.status_code, code)
        return True

    def storage_signed_url(self, path, expires_in=300):
        response = self._request(
            "POST",
            self._object_url(path, signed=True),
            headers=self._headers(True, {"Content-Type": "application/json"}),
            json={"expiresIn": int(expires_in)},
            timeout=TIMEOUT,
        )
        if response.status_code != 200:
            message, code = _describe(response)
            raise SupabaseError(f"Could not create a secure link: {message}", response.status_code, code)
        payload = response.json() or {}
        signed = payload.get("signedURL") or payload.get("signedUrl") or ""
        if not signed:
            raise SupabaseError("Could not create a secure link for this file.")
        if signed.startswith("http"):
            return signed
        # Supabase returns either "/object/sign/bucket/path" or the older
        # "/storage/v1/object/sign/bucket/path". Both must resolve to one URL.
        path_part = signed
        if path_part.startswith("/storage/v1"):
            path_part = path_part[len("/storage/v1"):]
        if not path_part.startswith("/"):
            path_part = "/" + path_part
        return f"{self.url}/storage/v1{path_part}"

    def storage_download(self, path, stream=True):
        signed = self.storage_signed_url(path, 120)
        response = self._request(
            "GET", signed, headers={}, timeout=STORAGE_TIMEOUT, stream=stream
        )
        if response.status_code != 200:
            message, _ = _describe(response)
            raise SupabaseError(f"Could not read the file: {message}", response.status_code)
        return response

    def storage_list(self, prefix, limit=100, offset=0):
        response = self._request(
            "POST",
            f"{self.url}/storage/v1/object/list/{self.bucket}",
            headers=self._headers(True, {"Content-Type": "application/json"}),
            json={
                "prefix": prefix,
                "limit": limit,
                "offset": offset,
                "sortBy": {"column": "name", "order": "asc"},
            },
            timeout=STORAGE_TIMEOUT,
        )
        if response.status_code != 200:
            message, code = _describe(response)
            raise SupabaseError(f"Could not list storage: {message}", response.status_code, code)
        return response.json() or []

    # ------------------------------------------------------------ Auth
    def sign_up(self, email, password, metadata=None):
        payload = {"email": email, "password": password}
        if metadata:
            payload["data"] = metadata
        response = self._request(
            "POST",
            f"{self.url}/auth/v1/signup",
            headers=self._headers(False, {"Content-Type": "application/json"}),
            json=payload,
        )
        if response.status_code not in (200, 201):
            raise SupabaseError(*_describe(response), status=response.status_code)
        return response.json()

    def sign_in(self, email, password):
        response = self._request(
            "POST",
            f"{self.url}/auth/v1/token",
            headers=self._headers(False, {"Content-Type": "application/json"}),
            params={"grant_type": "password"},
            json={"email": email, "password": password},
        )
        if response.status_code != 200:
            message, code = _describe(response)
            raise SupabaseError(message, response.status_code, code)
        return response.json()

    def refresh(self, refresh_token):
        response = self._request(
            "POST",
            f"{self.url}/auth/v1/token",
            headers=self._headers(False, {"Content-Type": "application/json"}),
            params={"grant_type": "refresh_token"},
            json={"refresh_token": refresh_token},
        )
        if response.status_code != 200:
            message, code = _describe(response)
            raise SupabaseError(message, response.status_code, code)
        return response.json()

    def user_info(self, access_token):
        response = self._request(
            "GET",
            f"{self.url}/auth/v1/user",
            headers={
                "apikey": self.anon_key,
                "Authorization": f"Bearer {access_token}",
            },
        )
        if response.status_code != 200:
            raise SupabaseError(*_describe(response), status=response.status_code)
        return response.json()

    def recover(self, email, redirect_to=None):
        payload = {"email": email}
        if redirect_to:
            payload["redirect_to"] = redirect_to
        response = self._request(
            "POST",
            f"{self.url}/auth/v1/recover",
            headers=self._headers(False, {"Content-Type": "application/json"}),
            json=payload,
        )
        if response.status_code >= 400:
            raise SupabaseError(*_describe(response), status=response.status_code)
        return True

    def update_password(self, access_token, password):
        response = self._request(
            "PUT",
            f"{self.url}/auth/v1/user",
            headers={
                "apikey": self.anon_key,
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            json={"password": password},
        )
        if response.status_code >= 400:
            raise SupabaseError(*_describe(response), status=response.status_code)
        return True

    def update_profile_metadata(self, access_token, data):
        response = self._request(
            "PUT",
            f"{self.url}/auth/v1/user",
            headers={
                "apikey": self.anon_key,
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            json={"data": data},
        )
        if response.status_code >= 400:
            raise SupabaseError(*_describe(response), status=response.status_code)
        return True

    def delete_account(self, user_id):
        """Admin delete; requires the service key. Used by the delete workflow."""
        if not self.service_key:
            raise SupabaseError("Account deletion is not configured.")
        response = self._request(
            "DELETE",
            f"{self.url}/auth/v1/admin/users/{user_id}",
            headers=self._headers(True),
        )
        if response.status_code >= 400:
            raise SupabaseError(*_describe(response), status=response.status_code)
        return True


# =====================================================================
# Schema capability discovery
# =====================================================================

class Schema:
    """Knows which tables and columns actually exist right now.

    Doxari was built on a single `documents` table. The upgrade adds optional
    tables and columns through one SQL migration. Nothing here assumes the
    migration ran: every feature checks its prerequisites first, so an
    un-migrated deployment keeps working exactly as before while a migrated
    one unlocks folders, trash, sharing and analytics.
    """

    #: table -> minimal column set the app relies on
    REQUIRED = {
        "documents": {"id", "user_id", "filename", "storage_path"},
        "folders": {"id", "user_id", "name"},
        "document_shares": {"id", "document_id", "owner_id"},
        "share_links": {"id", "document_id", "token"},
        "activity_log": {"id", "user_id", "action"},
        "user_preferences": {"id", "user_id"},
    }

    DOCUMENT_COLUMNS = {
        "size": "bigint",
        "mime_type": "text",
        "description": "text",
        "tags": "text[]",
        "deleted_at": "timestamptz",
        "last_accessed_at": "timestamptz",
        "updated_at": "timestamptz",
        "folder_id": "uuid",
        "content_text": "text",
    }

    def __init__(self, api):
        self.api = api
        self._lock = threading.Lock()
        self._tables = {}
        self._loaded_at = 0

    # ---------------------------------------------------------------- load
    def load(self, max_age=300, force=False):
        with self._lock:
            fresh = self._tables and (time.time() - self._loaded_at) < max_age
            if fresh and not force:
                return self._tables
            tables = {}
            if not self.api.configured:
                self._tables, self._loaded_at = {}, time.time()
                return self._tables
            try:
                response = self.api._request(
                    "GET", f"{self.api.url}/rest/v1/", headers=self.api._headers(True)
                )
                if response.status_code == 200:
                    spec = response.json() or {}
                    for name, definition in (spec.get("definitions") or {}).items():
                        props = definition.get("properties") or {}
                        tables[name] = set(props.keys())
            except (SupabaseError, ValueError) as error:  # pragma: no cover
                log.warning("Schema discovery failed: %s", error)

            if not tables:
                tables = self._probe()
            self._tables = tables
            self._loaded_at = time.time()
            return tables

    def _probe(self):
        """Fallback probe used when the OpenAPI document is unavailable."""
        found = {}
        for table, columns in self.REQUIRED.items():
            try:
                response = self.api._request(
                    "GET",
                    f"{self.api.url}/rest/v1/{table}",
                    headers=self.api._headers(
                        True, {"Range": "0-0", "Prefer": "count=exact"}
                    ),
                    params={"select": ",".join(sorted(columns)), "limit": 1},
                )
                if response.status_code < 400:
                    keys = set()
                    payload = response.json() if response.content else []
                    if isinstance(payload, list) and payload:
                        keys = set(payload[0].keys())
                    found[table] = keys | set(columns)
            except SupabaseError:
                continue
        return found

    # ---------------------------------------------------------------- query
    def has_table(self, table):
        return table in self.load()

    def has_columns(self, table, columns):
        existing = self.load().get(table) or set()
        return set(columns).issubset(existing)

    def documents_has(self, column):
        return column in (self.load().get("documents") or set())

    @property
    def folders(self):
        return self.has_table("folders")

    @property
    def sharing(self):
        return self.has_table("document_shares")

    @property
    def share_links(self):
        return self.has_table("share_links")

    @property
    def activity(self):
        return self.has_table("activity_log")

    @property
    def preferences(self):
        return self.has_table("user_preferences")

    @property
    def trash(self):
        return self.documents_has("deleted_at")

    @property
    def tags(self):
        return self.documents_has("tags")

    @property
    def full_text(self):
        return self.documents_has("content_text")

    def missing_setup(self):
        """Human readable list of what the optional migration still adds."""
        missing = []
        if not self.folders:
            missing.append("folders")
        if not self.trash:
            missing.append("recoverable trash")
        if not self.sharing:
            missing.append("document sharing")
        if not self.activity:
            missing.append("activity history")
        if not self.preferences:
            missing.append("saved preferences")
        return missing

    def summary(self):
        tables = self.load()
        return {
            "documents": sorted(tables.get("documents") or []),
            "tables": sorted(tables.keys()),
            "features": {
                "folders": self.folders,
                "trash": self.trash,
                "sharing": self.sharing,
                "share_links": self.share_links,
                "activity": self.activity,
                "preferences": self.preferences,
                "tags": self.tags,
                "full_text": self.full_text,
            },
        }
