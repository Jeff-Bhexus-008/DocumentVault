import os
import uuid
from urllib.parse import quote

import requests

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    session,
    flash
)

from dotenv import load_dotenv
from werkzeug.utils import secure_filename


load_dotenv()


# =========================
# SUPABASE CONFIGURATION
# =========================

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
SUPABASE_SECRET_KEY = os.getenv("SUPABASE_SECRET_KEY")


# =========================
# FLASK CONFIGURATION
# =========================

app = Flask(__name__)

app.secret_key = os.getenv("FLASK_SECRET_KEY")

app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024


# =========================
# FILE CONFIGURATION
# =========================

ALLOWED_EXTENSIONS = {
    "pdf",
    "doc",
    "docx",
    "txt",
    "png",
    "jpg",
    "jpeg"
}

BUCKET_NAME = "document"


def allowed_file(filename):
    return (
        "." in filename
        and filename.rsplit(".", 1)[1].lower()
        in ALLOWED_EXTENSIONS
    )


# =========================
# SUPABASE HEADERS
# =========================

def server_headers():
    return {
        "apikey": SUPABASE_SECRET_KEY,
        "Authorization": f"Bearer {SUPABASE_SECRET_KEY}"
    }


# =========================
# HOME
# =========================

@app.route("/")
def home():
    return render_template("index.html")


# =========================
# REGISTER
# =========================

@app.route("/register", methods=["GET", "POST"])
def register():

    if request.method == "POST":

        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")

        if not email or not password:
            return "Email and password are required."

        try:

            response = requests.post(
                f"{SUPABASE_URL}/auth/v1/signup",
                headers={
                    "apikey": SUPABASE_KEY,
                    "Content-Type": "application/json"
                },
                json={
                    "email": email,
                    "password": password
                },
                timeout=30
            )

            if response.status_code not in (200, 201):
                return f"Registration error: {response.text}"

            return redirect(url_for("login"))

        except Exception as error:
            return f"Registration error: {error}"

    return render_template("register.html")


# =========================
# LOGIN
# =========================

@app.route("/login", methods=["GET", "POST"])
def login():

    if request.method == "POST":

        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")

        try:

            response = requests.post(
                f"{SUPABASE_URL}/auth/v1/token?grant_type=password",
                headers={
                    "apikey": SUPABASE_KEY,
                    "Content-Type": "application/json"
                },
                json={
                    "email": email,
                    "password": password
                },
                timeout=30
            )

            if response.status_code != 200:
                return f"Login error: {response.text}"

            data = response.json()

            session["user_id"] = data["user"]["id"]
            session["user_email"] = data["user"]["email"]
            session["access_token"] = data["access_token"]
            session["refresh_token"] = data["refresh_token"]

            return redirect(url_for("dashboard"))

        except Exception as error:
            return f"Login error: {error}"

    return render_template("login.html")


# =========================
# DASHBOARD
# =========================

@app.route("/dashboard")
def dashboard():

    if "user_id" not in session:
        return redirect(url_for("login"))

    user_id = session["user_id"]

    try:

        response = requests.get(
            f"{SUPABASE_URL}/rest/v1/documents",
            headers={
                **server_headers(),
                "Content-Type": "application/json"
            },
            params={
                "user_id": f"eq.{user_id}",
                "select": "id,filename,storage_path,created_at",
                "order": "created_at.desc"
            },
            timeout=30
        )

        if response.status_code != 200:
            documents = []
        else:
            documents = response.json()

    except Exception:
        documents = []

    return render_template(
        "dashboard.html",
        user_email=session.get("user_email"),
        documents=documents
    )


# =========================
# UPLOAD DOCUMENT
# =========================

@app.route("/upload", methods=["POST"])
def upload():

    if "user_id" not in session:
        return redirect(url_for("login"))

    if "file" not in request.files:

        flash("Please select a file.")

        return redirect(url_for("dashboard"))

    file = request.files["file"]

    if file.filename == "":

        flash("Please select a file.")

        return redirect(url_for("dashboard"))

    if not allowed_file(file.filename):

        flash(
            "File type not allowed. "
            "Use PDF, DOC, DOCX, TXT, PNG, JPG or JPEG."
        )

        return redirect(url_for("dashboard"))

    original_filename = secure_filename(file.filename)

    user_id = session["user_id"]

    unique_filename = (
        f"{uuid.uuid4().hex}_{original_filename}"
    )

    storage_path = f"{user_id}/{unique_filename}"

    try:

        file_data = file.read()

        upload_url = (
            f"{SUPABASE_URL}/storage/v1/object/"
            f"{BUCKET_NAME}/{quote(storage_path, safe='/')}"
        )

        upload_response = requests.post(
            upload_url,
            headers={
                **server_headers(),
                "Content-Type": (
                    file.content_type
                    or "application/octet-stream"
                ),
                "x-upsert": "false"
            },
            data=file_data,
            timeout=60
        )

        if upload_response.status_code not in (200, 201):

            return (
                "Upload failed: "
                f"{upload_response.text}"
            )

        database_response = requests.post(
            f"{SUPABASE_URL}/rest/v1/documents",
            headers={
                **server_headers(),
                "Content-Type": "application/json",
                "Prefer": "return=minimal"
            },
            json={
                "user_id": user_id,
                "filename": original_filename,
                "storage_path": storage_path
            },
            timeout=30
        )

        if database_response.status_code not in (200, 201):

            # Remove uploaded file if database insertion fails.

            requests.delete(
                upload_url,
                headers=server_headers(),
                timeout=30
            )

            return (
                "Database error: "
                f"{database_response.text}"
            )

        flash("Document uploaded successfully.")

    except Exception as error:
        return f"Upload error: {error}"

    return redirect(url_for("dashboard"))


# =========================
# DOWNLOAD DOCUMENT
# =========================

@app.route("/download/<document_id>")
def download(document_id):

    if "user_id" not in session:
        return redirect(url_for("login"))

    user_id = session["user_id"]

    try:

        # Find the document belonging to this user
        response = requests.get(
            f"{SUPABASE_URL}/rest/v1/documents",
            headers=server_headers(),
            params={
                "id": f"eq.{document_id}",
                "user_id": f"eq.{user_id}",
                "select": "filename,storage_path"
            },
            timeout=30
        )

        if response.status_code != 200:
            return "Unable to find document."

        documents = response.json()

        if not documents:
            return "Document not found."

        document = documents[0]

        filename = document["filename"]
        storage_path = document["storage_path"]

        # Create a signed URL
        sign_url = (
            f"{SUPABASE_URL}/storage/v1/object/sign/"
            f"{BUCKET_NAME}/{quote(storage_path, safe='/')}"
        )

        sign_response = requests.post(
            sign_url,
            headers={
                **server_headers(),
                "Content-Type": "application/json"
            },
            json={
                "expiresIn": 300
            },
            timeout=30
        )

        if sign_response.status_code != 200:
            return f"Download error: {sign_response.text}"

        signed_url = sign_response.json()["signedURL"]

        if signed_url.startswith("/"):
            signed_url = (
                SUPABASE_URL
                + "/storage/v1"
                + signed_url
            )

        # Download the actual file from Supabase
        file_response = requests.get(
            signed_url,
            timeout=60
        )

        if file_response.status_code != 200:
            return "Unable to download file."

        # Send file to browser as an attachment
        from flask import Response

        return Response(
            file_response.content,
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{filename}"'
                ),
                "Content-Type": (
                    file_response.headers.get(
                        "Content-Type",
                        "application/octet-stream"
                    )
                )
            }
        )

    except Exception as error:
        return f"Download error: {error}"
    if "user_id" not in session:
        return redirect(url_for("login"))

    user_id = session["user_id"]

    try:

        response = requests.get(
            f"{SUPABASE_URL}/rest/v1/documents",
            headers=server_headers(),
            params={
                "id": f"eq.{document_id}",
                "user_id": f"eq.{user_id}",
                "select": "storage_path"
            },
            timeout=30
        )

        if response.status_code != 200:
            return "Unable to find document."

        documents = response.json()

        if not documents:
            return "Document not found."

        storage_path = documents[0]["storage_path"]

        sign_url = (
            f"{SUPABASE_URL}/storage/v1/object/sign/"
            f"{BUCKET_NAME}/{quote(storage_path, safe='/')}"
        )

        sign_response = requests.post(
            sign_url,
            headers={
                **server_headers(),
                "Content-Type": "application/json"
            },
            json={
                "expiresIn": 300
            },
            timeout=30
        )

        if sign_response.status_code != 200:
            return f"Download error: {sign_response.text}"

        signed_url = sign_response.json()["signedURL"]

        if signed_url.startswith("/"):
            signed_url = (
                SUPABASE_URL
                + "/storage/v1"
                + signed_url
            )

        return redirect(signed_url)

    except Exception as error:
        return f"Download error: {error}"


# =========================
# DELETE DOCUMENT
# =========================

@app.route("/delete/<document_id>", methods=["POST"])
def delete(document_id):

    if "user_id" not in session:
        return redirect(url_for("login"))

    user_id = session["user_id"]

    try:

        response = requests.get(
            f"{SUPABASE_URL}/rest/v1/documents",
            headers=server_headers(),
            params={
                "id": f"eq.{document_id}",
                "user_id": f"eq.{user_id}",
                "select": "storage_path"
            },
            timeout=30
        )

        documents = response.json()

        if not documents:
            return "Document not found."

        storage_path = documents[0]["storage_path"]

        storage_url = (
            f"{SUPABASE_URL}/storage/v1/object/"
            f"{BUCKET_NAME}/{quote(storage_path, safe='/')}"
        )

        storage_response = requests.delete(
            storage_url,
            headers=server_headers(),
            timeout=30
        )

        if storage_response.status_code not in (200, 204):

            return (
                "Storage delete error: "
                f"{storage_response.text}"
            )

        database_response = requests.delete(
            f"{SUPABASE_URL}/rest/v1/documents",
            headers=server_headers(),
            params={
                "id": f"eq.{document_id}",
                "user_id": f"eq.{user_id}"
            },
            timeout=30
        )

        if database_response.status_code not in (200, 204):

            return (
                "Database delete error: "
                f"{database_response.text}"
            )

        flash("Document deleted.")

    except Exception as error:
        return f"Delete error: {error}"

    return redirect(url_for("dashboard"))


# =========================
# LOGOUT
# =========================

@app.route("/logout")
def logout():

    session.clear()

    return redirect(url_for("login"))


# =========================
# RUN
# =========================

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5000)),
        debug=True
    )

