"""A local stand-in for Supabase used to test Doxari end to end.

It implements only the parts of GoTrue, PostgREST and Storage that Doxari
calls, with in-memory data. Two schema profiles are supported so the app can be
tested both before and after the optional database migration.

    MOCK_PROFILE=legacy    python tools/mock_supabase.py
    MOCK_PROFILE=upgraded  python tools/mock_supabase.py
"""

import json
import os
import re
import sys
import time
import uuid
from datetime import datetime, timezone

from flask import Flask, Response, jsonify, request

PROFILE = os.environ.get("MOCK_PROFILE", "upgraded")
SERVICE_KEY = "service-key"
ANON_KEY = "anon-key"

app = Flask(__name__)

DOC_BASE = ["id", "user_id", "filename", "storage_path", "folder", "favorite", "created_at"]
DOC_EXTRA = ["size", "mime_type", "description", "tags", "deleted_at", "last_accessed_at",
             "updated_at", "folder_id", "content_text"]

TABLES = {
    "documents": DOC_BASE + (DOC_EXTRA if PROFILE == "upgraded" else []),
}
if PROFILE == "upgraded":
    TABLES.update(
        {
            "folders": ["id", "user_id", "name", "parent_id", "color", "icon", "created_at", "updated_at"],
            "document_shares": ["id", "document_id", "owner_id", "shared_with_id", "shared_with_email",
                                "permission", "revoked", "created_at"],
            "share_links": ["id", "document_id", "owner_id", "token", "permission", "expires_at",
                            "revoked", "created_at"],
            "activity_log": ["id", "user_id", "action", "document_id", "document_name", "folder_id",
                             "meta", "created_at"],
            "user_preferences": ["id", "user_id", "theme", "notifications", "onboarded", "view_mode",
                                 "ai_consent", "created_at", "updated_at"],
        }
    )

DATA = {name: [] for name in TABLES}
USERS = {}          # email -> {id, email, password, user_metadata}
TOKENS = {}         # access_token -> email
REFRESH = {}        # refresh_token -> email
STORAGE = {}        # bucket -> {path: bytes}


def now():
    return datetime.now(timezone.utc).isoformat()


def new_id():
    return str(uuid.uuid4())


# ---------------------------------------------------------------- filters
def parse_value(value):
    if value.startswith("not."):
        outer = "neq"
        inner = value[4:]
    else:
        outer = None
        inner = value
    if "." in inner:
        op, _, operand = inner.partition(".")
    else:
        op, operand = inner, ""
    return outer, op, operand


def compare(row, column, expression):
    outer, op, operand = parse_value(expression)
    present = column in row
    value = row.get(column)

    if op == "is":
        target = operand.lower()
        if target == "null":
            ok = value is None
        elif target == "true":
            ok = bool(value)
        elif target == "false":
            ok = not bool(value)
        else:
            ok = str(value).lower() == target
    elif op in {"eq", "ilike", "like", "gt", "gte", "lt", "lte", "neq"}:
        if operand.lower() == "null" and op == "eq":
            ok = value is None
        elif op == "eq":
            ok = str(value).lower() == operand.lower() if isinstance(value, bool) else str(value) == operand
            if isinstance(value, bool):
                ok = str(value).lower() == operand.lower()
        elif op == "neq":
            ok = str(value) != operand
        elif op in {"ilike", "like"}:
            pattern = operand.replace("%", ".*")
            pattern = pattern.replace("*", ".*")
            try:
                ok = re.fullmatch(pattern, str(value or ""), re.I | re.S) is not None
            except re.error:
                ok = False
        else:
            try:
                left = float(value)
                right = float(operand)
            except (TypeError, ValueError):
                left, right = str(value or ""), operand
            ok = {"gt": left > right, "gte": left >= right,
                  "lt": left < right, "lte": left <= right}.get(op, False)
    else:
        ok = True

    if outer == "neq":
        return not ok
    return ok


def split_top(text):
    parts, depth, current = [], 0, ""
    for char in text:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += char
    if current:
        parts.append(current)
    return parts


def eval_group(text, row, mode="and"):
    """Evaluate a PostgREST logical expression such as
    `or=(filename.ilike.*a*,folder.ilike.*b*)` or `and=(or(...),or(...))`."""
    text = text.strip()
    results = []
    for part in split_top(text):
        part = part.strip()
        if not part:
            continue
        match = re.match(r"^(or|and)=\((.*)\)$", part, re.S)
        if match:
            results.append(eval_group(match.group(2), row, match.group(1)))
        elif "=" in part:
            column, _, expression = part.partition("=")
            results.append(compare(row, column, expression))
        else:
            # PostgREST also allows bare `column.op.value` inside or=(...)
            column, _, expression = part.partition(".")
            results.append(compare(row, column, expression))
    return all(results) if mode == "and" else any(results)


def apply_filters(table, rows):
    for key, value in request.args.items(multi=True):
        if key in {"select", "order", "limit", "offset", "apikey"}:
            continue
        if key in {"or", "and"}:
            rows = [r for r in rows if eval_group(value.strip()[1:-1] if value.startswith("(") else value, r, key)]
            continue
        if key not in TABLES[table]:
            return None, key
        rows = [r for r in rows if compare(r, key, value)]
    return rows, None


def order_rows(table, rows):
    order = request.args.get("order")
    if not order:
        return rows
    field, _, direction = order.partition(".")
    reverse = direction.lower().startswith("desc")
    return sorted(rows, key=lambda r: (r.get(field) is None, str(r.get(field) or "")), reverse=reverse)


def project(rows):
    select = request.args.get("select")
    if not select or select.strip() == "*":
        return rows
    columns = [c.strip() for c in select.split(",") if c.strip()]
    out = []
    for row in rows:
        item = {}
        for column in columns:
            if column in row:
                item[column] = row[column]
        out.append(item)
    return out


def table_error(message, status=400):
    return jsonify({"message": message, "code": "PGRST100"}), status


@app.get("/rest/v1/")
def openapi():
    definitions = {
        name: {"properties": {column: {"type": "string"} for column in columns}}
        for name, columns in TABLES.items()
    }
    return jsonify({"swagger": "2.0", "definitions": definitions})


@app.route("/rest/v1/<table>", methods=["GET", "POST", "PATCH", "DELETE"])
def rest(table):
    if table not in TABLES:
        return table_error(f"relation \"{table}\" does not exist", 404)
    rows = DATA[table]

    if request.method == "GET":
        filtered, bad = apply_filters(table, list(rows))
        if bad:
            return table_error(f"column {table}.{bad} does not exist", 400)
        filtered = order_rows(table, filtered)
        total = len(filtered)
        try:
            limit = int(request.args.get("limit", 1000))
            offset = int(request.args.get("offset", 0))
        except ValueError:
            limit, offset = 1000, 0
        page = filtered[offset: offset + limit]
        response = jsonify(project(page))
        if request.headers.get("Prefer", "").startswith("count=exact"):
            response.headers["Content-Range"] = f"{offset}-{offset + len(page) - 1}/{total}"
        return response

    if request.method == "POST":
        payload = request.get_json(force=True) or {}
        items = payload if isinstance(payload, list) else [payload]
        created = []
        for item in items:
            row = {column: None for column in TABLES[table]}
            row.update({k: v for k, v in item.items() if k in TABLES[table]})
            row["id"] = item.get("id") or new_id()
            row["created_at"] = item.get("created_at") or now()
            if "updated_at" in row:
                row["updated_at"] = now()
            if table == "documents" and "favorite" in row:
                row["favorite"] = bool(row.get("favorite") or False)
            if table == "user_preferences" and "notifications" in row:
                row["notifications"] = bool(row.get("notifications"))
                row["onboarded"] = bool(row.get("onboarded"))
                row["ai_consent"] = bool(row.get("ai_consent"))
            rows.append(row)
            created.append(row)
        if request.headers.get("Prefer", "").startswith("return=minimal"):
            return ("", 201)
        return jsonify(created), 201

    if request.method == "PATCH":
        filtered, bad = apply_filters(table, list(rows))
        if bad:
            return table_error(f"column {table}.{bad} does not exist", 400)
        payload = request.get_json(force=True) or {}
        for row in filtered:
            for key, value in payload.items():
                if key in TABLES[table]:
                    row[key] = value
            if "updated_at" in row:
                row["updated_at"] = now()
        if request.headers.get("Prefer", "").startswith("return=minimal"):
            return ("", 204)
        return jsonify(filtered)

    # DELETE
    filtered, bad = apply_filters(table, list(rows))
    if bad:
        return table_error(f"column {table}.{bad} does not exist", 400)
    for row in filtered:
        rows.remove(row)
    return ("", 204)


# ---------------------------------------------------------------- auth
@app.post("/auth/v1/signup")
def signup():
    payload = request.get_json(force=True) or {}
    email = (payload.get("email") or "").lower()
    if email in USERS:
        return jsonify({"msg": "User already registered"}), 400
    user = {
        "id": new_id(),
        "email": email,
        "password": payload.get("password"),
        "user_metadata": payload.get("data") or {},
    }
    USERS[email] = user
    token = new_id()
    TOKENS[token] = email
    return jsonify(
        {
            "access_token": token,
            "refresh_token": token + "-refresh",
            "expires_in": 3600,
            "user": {k: user[k] for k in ("id", "email", "user_metadata")},
        }
    ), 200


@app.post("/auth/v1/token")
def token():
    grant = request.args.get("grant_type")
    payload = request.get_json(force=True) or {}
    if grant == "refresh_token":
        email = REFRESH.get(payload.get("refresh_token"))
        if not email:
            return jsonify({"msg": "Invalid refresh token"}), 400
    else:
        email = (payload.get("email") or "").lower()
        user = USERS.get(email)
        if not user or user["password"] != payload.get("password"):
            return jsonify({"msg": "Invalid login credentials"}), 400
    access = new_id()
    refresh = access + "-refresh"
    TOKENS[access] = email
    REFRESH[refresh] = email
    user = USERS[email]
    return jsonify(
        {
            "access_token": access,
            "refresh_token": refresh,
            "expires_in": 3600,
            "user": {k: user[k] for k in ("id", "email", "user_metadata")},
        }
    )


def token_email():
    header = request.headers.get("Authorization", "")
    return TOKENS.get(header.replace("Bearer ", ""))


@app.route("/auth/v1/user", methods=["GET", "PUT"])
def auth_user():
    email = token_email()
    if not email:
        return jsonify({"msg": "Invalid token"}), 401
    user = USERS[email]
    if request.method == "PUT":
        payload = request.get_json(force=True) or {}
        if "data" in payload:
            user["user_metadata"] = payload["data"]
        if "password" in payload:
            user["password"] = payload["password"]
    return jsonify({k: user[k] for k in ("id", "email", "user_metadata")})


@app.post("/auth/v1/recover")
def recover():
    return jsonify({})


@app.get("/auth/v1/admin/users")
def admin_users():
    return jsonify(
        {"users": [{"id": u["id"], "email": u["email"]} for u in USERS.values()], "aud": "authenticated"}
    )


@app.delete("/auth/v1/admin/users/<user_id>")
def admin_delete_user(user_id):
    for email, user in list(USERS.items()):
        if user["id"] == user_id:
            del USERS[email]
            return jsonify({})
    return jsonify({"msg": "User not found"}), 404


# ---------------------------------------------------------------- storage
@app.post("/storage/v1/object/<bucket>/<path:path>")
def storage_upload(bucket, path):
    blob = request.get_data()
    STORAGE.setdefault(bucket, {})[path] = blob
    return jsonify({"Key": f"{bucket}/{path}"})


@app.delete("/storage/v1/object/<bucket>/<path:path>")
def storage_delete(bucket, path):
    STORAGE.setdefault(bucket, {}).pop(path, None)
    return ("", 204)


@app.post("/storage/v1/object/copy")
def storage_copy():
    payload = request.get_json(force=True) or {}
    bucket = payload.get("bucketId")
    source = payload.get("sourceKey")
    destination = payload.get("destinationKey")
    objects = STORAGE.setdefault(bucket, {})
    if source not in objects:
        return jsonify({"message": "not found"}), 404
    objects[destination] = objects[source]
    return jsonify({"Key": destination})


@app.post("/storage/v1/object/sign/<bucket>/<path:path>")
def storage_sign(bucket, path):
    return jsonify({"signedURL": f"/storage/v1/object/sign/{bucket}/{path}?token=abc"})


@app.get("/storage/v1/object/sign/<bucket>/<path:path>")
def storage_signed(bucket, path):
    blob = STORAGE.setdefault(bucket, {}).get(path)
    if blob is None:
        return ("", 404)
    return Response(blob, mimetype="application/octet-stream")


@app.post("/storage/v1/object/list/<bucket>")
def storage_list(bucket):
    payload = request.get_json(force=True) or {}
    prefix = payload.get("prefix") or ""
    objects = STORAGE.setdefault(bucket, {})
    items = []
    for path, blob in objects.items():
        if not path.startswith(prefix + "/"):
            continue
        items.append(
            {
                "name": path.split("/", 1)[1],
                "id": new_id(),
                "updated_at": now(),
                "created_at": now(),
                "metadata": {"size": len(blob), "mimetype": "application/octet-stream"},
            }
        )
    offset = int(payload.get("offset") or 0)
    limit = int(payload.get("limit") or 100)
    return jsonify(items[offset: offset + limit])


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 54321))
    print(f"mock supabase ({PROFILE}) on {port}", flush=True)
    app.run(host="127.0.0.1", port=port, threaded=True)
