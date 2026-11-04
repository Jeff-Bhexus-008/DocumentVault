"""Security checks: unauthenticated access, ownership, share permissions."""
import io, re, sys, time, requests

BASE = "http://127.0.0.1:5055"
ok, fail = [], []


def check(name, cond, detail=""):
    (ok if cond else fail).append(name)
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  <- {detail}"))


def csrf(s, path):
    html = s.get(BASE + path).text
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    return m.group(1) if m else ""


def register(email, name):
    s = requests.Session()
    t = csrf(s, "/register")
    s.post(BASE + "/register", data={"csrf_token": t, "email": email, "password": "Sup3rSecret1",
                                     "confirm_password": "Sup3rSecret1", "full_name": name})
    return s


stamp = int(time.time())
A = register(f"sec{stamp}@example.com", "Owner")
B = register(f"int{stamp}@example.com", "Intruder")
anon = requests.Session()

t = csrf(A, "/dashboard")
up = A.post(BASE + "/upload", data={"csrf_token": t},
            files=[("files", ("private.txt", io.BytesIO(b"top secret"), "text/plain"))],
            headers={"X-Requested-With": "XMLHttpRequest"}).json()
doc = up["uploaded"][0]["id"]

# anonymous access
for path in ["/dashboard", "/documents", "/trash", "/settings", "/analytics", f"/documents/{doc}"]:
    r = anon.get(BASE + path, allow_redirects=False)
    check(f"anonymous blocked {path}", r.status_code in (301, 302, 303, 401), str(r.status_code))
for path in [f"/documents/{doc}/download", f"/documents/{doc}/raw", f"/documents/{doc}/text"]:
    r = anon.get(BASE + path, allow_redirects=False)
    check(f"anonymous blocked {path}", r.status_code in (301, 302, 303, 401, 403), str(r.status_code))
r = anon.post(BASE + f"/documents/{doc}/delete", allow_redirects=False)
check("anonymous cannot delete", r.status_code in (301, 302, 303, 400, 401, 403), str(r.status_code))
check("document still exists", "private.txt" in A.get(BASE + "/documents").text)

# other-user access
tb = csrf(B, "/documents")
for path in [f"/documents/{doc}", f"/documents/{doc}/text", f"/documents/{doc}/download",
             f"/api/documents/{doc}/url"]:
    r = B.get(BASE + path)
    check(f"stranger blocked {path}", r.status_code in (403, 404), str(r.status_code))
r = B.post(BASE + f"/documents/{doc}/delete", headers={"X-CSRF-Token": tb, "X-Requested-With": "XMLHttpRequest"})
check("stranger cannot delete", r.status_code in (403, 404), str(r.status_code))
r = B.post(BASE + f"/documents/{doc}/favorite", headers={"X-CSRF-Token": tb, "X-Requested-With": "XMLHttpRequest"})
check("stranger cannot favorite", r.status_code in (403, 404), str(r.status_code))
r = B.post(BASE + f"/documents/{doc}/share", data={"csrf_token": tb, "email": f"sec{stamp}@example.com"},
           headers={"X-Requested-With": "XMLHttpRequest"})
check("stranger cannot share", r.status_code in (403, 404), str(r.status_code))
r = B.post(BASE + "/api/bulk", json={"action": "delete", "ids": [doc]},
           headers={"X-CSRF-Token": tb, "X-Requested-With": "XMLHttpRequest"})
check("stranger bulk delete blocked", r.status_code in (200, 403) and
      "private.txt" in A.get(BASE + "/documents").text, r.text[:200])

# view-only share
ta = csrf(A, "/documents")
r = A.post(BASE + f"/documents/{doc}/share",
           data={"csrf_token": ta, "email": f"int{stamp}@example.com", "permission": "view"},
           headers={"X-Requested-With": "XMLHttpRequest"})
check("view-only share", r.status_code == 200 and r.json().get("ok") is True, r.text[:200])
check("viewer can preview", B.get(BASE + f"/documents/{doc}").status_code == 200)
r = B.get(BASE + f"/documents/{doc}/download")
check("view-only cannot download", r.status_code in (403, 404), str(r.status_code))
r = B.post(BASE + f"/documents/{doc}/delete", headers={"X-CSRF-Token": tb, "X-Requested-With": "XMLHttpRequest"})
check("viewer cannot delete owner's doc", r.status_code in (403, 404), str(r.status_code))

# file validation
bad = A.post(BASE + "/upload", data={"csrf_token": ta},
             files=[("files", ("virus.exe", io.BytesIO(b"MZ"), "application/x-msdownload"))],
             headers={"X-Requested-With": "XMLHttpRequest"})
body = bad.json() if bad.status_code == 200 else {}
check("blocked file type rejected", bad.status_code == 400 or (body.get("failed") or body.get("error")),
      bad.text[:150])
check("rejected upload not listed", "virus.exe" not in A.get(BASE + "/documents").text)
big = A.post(BASE + "/upload", data={"csrf_token": ta},
             files=[("files", ("big.txt", io.BytesIO(b"0" * (26 * 1024 * 1024)), "text/plain"))],
             headers={"X-Requested-With": "XMLHttpRequest"})
check("oversized upload rejected", big.status_code in (400, 413) or b"failed" in big.content,
      str(big.status_code))

# csrf
r = A.post(BASE + f"/documents/{doc}/delete", headers={"X-Requested-With": "XMLHttpRequest"})
check("csrf required for mutations", r.status_code == 400, str(r.status_code))

print("\n%d passed, %d failed" % (len(ok), len(fail)))
sys.exit(1 if fail else 0)
