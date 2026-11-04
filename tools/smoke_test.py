"""End-to-end smoke test for Doxari against the local mock Supabase."""
import io
import re
import sys
import time

import requests

BASE = "http://127.0.0.1:5055"
ok, fail = [], []


def check(name, cond, detail=""):
    (ok if cond else fail).append(name)
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  <- {detail}"))


def csrf(session, path):
    html = session.get(BASE + path).text
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    return m.group(1) if m else ""


def register(email, password, name):
    s = requests.Session()
    token = csrf(s, "/register")
    r = s.post(BASE + "/register", data={"csrf_token": token, "email": email, "password": password,
                                        "confirm_password": password, "full_name": name},
               allow_redirects=True)
    return s, r


STAMP = str(int(time.time()))
OWNER = "owner" + STAMP + "@example.com"
FRIEND = "friend" + STAMP + "@example.com"
A = register(OWNER, "Sup3rSecret1", "Amara Owner")[0]

check("dashboard loads after register", A.get(BASE + "/dashboard").status_code == 200)

# ---- upload
files = [
    ("files", ("notes.txt", io.BytesIO(b"Hello Doxari\nsecond line\n"), "text/plain")),
    ("files", ("report.pdf", io.BytesIO(b"%PDF-1.4 fake pdf body"), "application/pdf")),
]
token = csrf(A, "/dashboard")
r = A.post(BASE + "/upload", data={"csrf_token": token}, files=files,
           headers={"X-Requested-With": "XMLHttpRequest"})
payload = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
check("multi-file upload accepted", r.status_code == 200 and payload.get("ok") is True, r.text[:300])
uploaded = payload.get("uploaded") or []

docs = A.get(BASE + "/documents").text
check("documents list shows uploads", "notes.txt" in docs and "report.pdf" in docs)

# ---- folders
token = csrf(A, "/documents")
r = A.post(BASE + "/folders/create", data={"csrf_token": token, "name": "School"},
           headers={"X-Requested-With": "XMLHttpRequest"})
check("folder create", r.status_code == 200 and r.json().get("ok") is True, r.text[:200])
r = A.post(BASE + "/folders/create", data={"csrf_token": token, "name": "Certificates", "parent_id": ""},
           headers={"X-Requested-With": "XMLHttpRequest"})
check("second folder create", r.status_code == 200)

html = A.get(BASE + "/folders").text
check("folders page lists School", "School" in html)

# find folder id from the API listing
import json
resp = A.get(BASE + "/api/search/suggest?q=school")
# move first document into folder via bulk API
doc_id = uploaded[0]["id"] if uploaded else None
if doc_id:
    folder_id = re.search(r'/folders/([0-9a-f-]+)/rename', html)
    folder_id = folder_id.group(1) if folder_id else None
    thinking = A.post(BASE + "/api/bulk", json={"action": "move", "ids": [doc_id], "folder_id": folder_id},
                      headers={"X-CSRF-Token": token, "X-Requested-With": "XMLHttpRequest"})
    check("move document into folder", thinking.status_code == 200 and thinking.json().get("ok") is True, thinking.text[:200])

# ---- preview + text
if doc_id:
    p = A.get(BASE + f"/documents/{doc_id}")
    check("preview page", p.status_code == 200)
    t = A.get(BASE + f"/documents/{doc_id}/text")
    check("text extraction endpoint", t.status_code == 200 and "Hello Doxari" in t.text, t.text[:200])
    d = A.get(BASE + f"/documents/{doc_id}/download")
    check("download original", d.status_code == 200 and b"Hello Doxari" in d.content)
    f = A.post(BASE + f"/documents/{doc_id}/favorite", json={}, headers={"X-CSRF-Token": token, "X-Requested-With": "XMLHttpRequest"})
    check("favorite toggles", f.status_code == 200 and f.json().get("favorite") is True, f.text[:200])
    check("favorites page", "notes.txt" in A.get(BASE + "/favorites").text)

# ---- search
s = A.get(BASE + "/search?q=certificates").text
check("smart search page", "Read as" in s or "search" in s.lower())
s2 = A.get(BASE + "/search?q=report&mode=keyword").text
check("keyword search finds report", "report.pdf" in s2)
s3 = A.get(BASE + "/search?q=zzzznothing").text
check("no-results state", "No results" in s3 or "no results" in s3.lower())

# ---- trash lifecycle
if doc_id:
    A.post(BASE + f"/documents/{doc_id}/delete", headers={"X-CSRF-Token": token, "X-Requested-With": "XMLHttpRequest"})
    trash = A.get(BASE + "/trash").text
    check("deleted doc appears in trash", "notes.txt" in trash)
    A.post(BASE + f"/documents/{doc_id}/restore", headers={"X-CSRF-Token": token, "X-Requested-With": "XMLHttpRequest"})
    check("restored doc leaves trash", "notes.txt" not in A.get(BASE + "/trash").text)
    check("restored doc back in library", "notes.txt" in A.get(BASE + "/documents").text)

# ---- sharing to a second user
B = requests.Session()
B = register(FRIEND, "An0therPass1", "Bola Friend")[0]
if doc_id:
    tokenA = csrf(A, "/documents")
    sh = A.post(BASE + f"/documents/{doc_id}/share",
                data={"csrf_token": tokenA, "email": FRIEND, "permission": "download"},
                headers={"X-Requested-With": "XMLHttpRequest"})
    check("share with registered user", sh.status_code == 200 and sh.json().get("ok") is True, sh.text[:300])
    shared = B.get(BASE + "/shared").text
    check("recipient sees Shared with me", "notes.txt" in shared)
    bp = B.get(BASE + f"/documents/{doc_id}")
    check("recipient can open shared preview", bp.status_code == 200)
    bd = B.get(BASE + f"/documents/{doc_id}/download")
    check("recipient with download permission can download", bd.status_code == 200, str(bd.status_code))

    # private doc must stay private
    other = B.get(BASE + f"/documents/{uploaded[1]['id']}")
    check("private doc blocked for other user", "$" not in "" and other.status_code in (403, 404), str(other.status_code))

    # public share link
    link = A.post(BASE + f"/documents/{doc_id}/link",
                  data={"csrf_token": tokenA, "permission": "view"},
                  headers={"X-Requested-With": "XMLHttpRequest"})
    body = link.json() if link.status_code == 200 else {}
    url = body.get("url") or ""
    if url.startswith("http"):
        url = url.split("5055", 1)[1]
    check("share link created", bool(url), link.text[:200])
    if url:
        anon = requests.Session()
        page = anon.get(BASE + url)
        check("public link page opens anonymously", page.status_code == 200)
        raw = anon.get(BASE + url.replace("/s/", "/s/") + "/file")
        check("public view streams file", raw.status_code == 200 and b"Hello Doxari" in raw.content, str(raw.status_code))
        dl = anon.get(BASE + url + "/download")
        check("view-only link cannot download", dl.status_code in (403, 404), str(dl.status_code))

# ---- analytics / activity / settings
check("analytics page", A.get(BASE + "/analytics").status_code == 200)
check("activity page", A.get(BASE + "/activity").status_code == 200)
sets = A.get(BASE + "/settings")
check("settings page", sets.status_code == 200)
mig = A.get(BASE + "/settings/migration.sql")
check("migration sql downloadable", mig.status_code == 200 and "create table if not exists public.folders" in mig.text)
tok = csrf(A, "/settings")
pref = A.post(BASE + "/settings/preferences", data={"csrf_token": tok, "theme": "dark", "view_mode": "list", "notifications": "on"},
              headers={"X-Requested-With": "XMLHttpRequest"})
check("preferences save", pref.status_code == 200)

# ---- notifications + bulk + empty trash
check("notifications api", A.get(BASE + "/api/notifications").status_code == 200)
if doc_id:
    bulk = A.post(BASE + "/api/bulk", json={"action": "favorite", "ids": [doc_id]}, headers={"X-CSRF-Token": tok, "X-Requested-With": "XMLHttpRequest"})
    check("bulk favorite", bulk.status_code == 200 and bulk.json().get("ok"))

# ---- logout
A.get(BASE + "/logout")
check("logout then dashboard redirects", A.get(BASE + "/dashboard", allow_redirects=False).status_code in (301, 302))

print("\n%d passed, %d failed" % (len(ok), len(fail)))
sys.exit(1 if fail else 0)
