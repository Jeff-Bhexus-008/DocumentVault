"""Document operations that span storage + database.

Kept in one place so an upload, delete, restore or copy can never leave the
bucket and the `documents` table out of step with each other.
"""

import io
import logging
import uuid

from .config import BUCKET_NAME, FILE_TYPES, FALLBACK_CATEGORY
from .models import extension_of, now_iso, type_info
from .supabase_api import SupabaseError

log = logging.getLogger(__name__)


# ---------------------------------------------------------------- text extraction
def extract_text(blob, filename, limit=200000):
    """Best-effort plain-text extraction used for in-document search.

    Only local libraries are used; nothing is sent to a third party here.
    """
    ext = extension_of(filename)
    try:
        if ext in {"txt", "md", "csv", "json"}:
            return blob[:limit].decode("utf-8", errors="ignore")
        if ext == "pdf":
            try:
                from pypdf import PdfReader
            except ImportError:  # optional dependency
                return ""
            reader = PdfReader(io.BytesIO(blob))
            pieces = []
            length = 0
            for page in reader.pages[:40]:
                try:
                    text = page.extract_text() or ""
                except Exception:
                    text = ""
                pieces.append(text)
                length += len(text)
                if length > limit:
                    break
            return "\n".join(pieces)[:limit]
    except Exception as error:  # never fail an upload because of extraction
        log.info("Text extraction skipped for %s: %s", filename, error)
    return ""


# ---------------------------------------------------------------- operations
class DocumentService:
    def __init__(self, api, repo, schema):
        self.api = api
        self.repo = repo
        self.schema = schema

    # ------------------------------------------------------------ helpers
    def _path_for(self, user_id, filename):
        safe = (filename or "file").replace(" ", "_")
        return f"{user_id}/{uuid.uuid4().hex}_{safe}"

    def _folder_label(self, user_id, folder_id, requested_name=None):
        """Resolve the legacy `folder` label from a folder record when used."""
        if folder_id and self.schema.folders:
            folder = self.repo.get_folder(folder_id, user_id)
            if folder:
                return folder.get("name") or FALLBACK_CATEGORY
        return (requested_name or FALLBACK_CATEGORY).strip() or FALLBACK_CATEGORY

    # ------------------------------------------------------------ upload
    def store_upload(self, user_id, file_storage, *, filename, ext, folder=None,
                     folder_id=None, description=None, tags=None):
        """Store one already-validated upload. Returns the database record."""
        blob = file_storage.read()
        if not blob:
            raise SupabaseError("The file was empty.")

        mime = FILE_TYPES.get(ext, ("application/octet-stream",))[0]
        storage_path = self._path_for(user_id, filename)
        self.api.storage_upload(storage_path, blob, mime)

        content_text = ""
        if self.schema.full_text:
            content_text = extract_text(blob, filename)

        try:
            record = self.repo.create_document(
                user_id,
                filename=filename,
                storage_path=storage_path,
                folder=self._folder_label(user_id, folder_id, folder),
                folder_id=folder_id,
                size=len(blob),
                mime_type=mime,
                description=description,
                tags=tags,
                content_text=content_text,
            )
        except Exception as error:
            # The database row failed: remove the object we just wrote so the
            # bucket never holds files the user cannot see or manage.
            try:
                self.api.storage_delete(storage_path)
            except Exception:
                pass
            raise SupabaseError(
                f"The file was uploaded but could not be saved to your library: {getattr(error, 'message', error)}"
            )

        self.repo.storage.invalidate(user_id)
        self.repo.log(user_id, "upload", document=record, meta={"size": len(blob)})
        return record

    # ------------------------------------------------------------ delete
    def delete_document(self, document_id, user_id):
        """Soft delete when trash is available; otherwise report honestly."""
        document = self.repo.get_document(document_id, user_id)
        if not document:
            return None, "Document not found."

        if self.schema.trash:
            self.repo.update_document(document_id, user_id, deleted_at=now_iso())
            self.repo.log(user_id, "delete", document=document)
            return {"mode": "trashed", "document": document}, None

        # No trash column: this is a permanent delete, and the caller must have
        # said so in the interface.
        self._purge_storage(document, user_id)
        self.repo.api.rest(
            "documents", "DELETE",
            params={"id": f"eq.{document_id}", "user_id": f"eq.{user_id}"},
        )
        self.repo.log(user_id, "purge", document=document)
        return {"mode": "deleted", "document": document}, None

    def restore_document(self, document_id, user_id):
        document = self.repo.get_document(document_id, user_id)
        if not document:
            return None, "Document not found."
        if not self.schema.trash:
            return None, "Restoring deleted documents needs the Doxari database upgrade."
        if not document.get("deleted_at"):
            return None, "That document is not in the trash."
        self.repo.update_document(document_id, user_id, deleted_at=None)
        self.repo.log(user_id, "restore", document=document)
        return document, None

    def purge_document(self, document_id, user_id):
        document = self.repo.get_document(document_id, user_id)
        if not document:
            return None, "Document not found."
        self._purge_storage(document, user_id)
        self.repo.api.rest(
            "documents", "DELETE",
            params={"id": f"eq.{document_id}", "user_id": f"eq.{user_id}"},
        )
        self.repo.log(user_id, "purge", document=document)
        return document, None

    def empty_trash(self, user_id):
        documents, _ = self.repo.list_documents(user_id, trashed=True, limit=500, with_total=False)
        removed = 0
        for document in documents:
            self._purge_storage(document, user_id)
            try:
                self.repo.api.rest(
                    "documents", "DELETE",
                    params={"id": f"eq.{document['id']}", "user_id": f"eq.{user_id}"},
                )
                removed += 1
            except Exception:
                continue
        self.repo.log(user_id, "trash_empty", meta={"count": removed})
        return removed

    def _purge_storage(self, document, user_id):
        path = document.get("storage_path") or ""
        if not path or not path.startswith(f"{user_id}/"):
            return
        try:
            self.api.storage_delete(path)
        except SupabaseError as error:
            log.warning("Storage delete failed for %s: %s", path, error)
        self.repo.storage.invalidate(user_id)

    # ------------------------------------------------------------ copy
    def copy_document(self, document_id, user_id):
        document = self.repo.get_document(document_id, user_id)
        if not document:
            return None, "Document not found."
        filename = document["filename"]
        stem, dot, ext = filename.rpartition(".")
        if not dot:
            new_name, ext = f"{filename} (copy)", ""
        else:
            new_name = f"{stem} (copy).{ext}"
        storage_path = self._path_for(user_id, new_name)
        try:
            self.api.storage_copy(document["storage_path"], storage_path)
        except SupabaseError as error:
            return None, error.message
        record = self.repo.create_document(
            user_id,
            filename=new_name,
            storage_path=storage_path,
            folder=document.get("folder") or FALLBACK_CATEGORY,
            folder_id=document.get("folder_id"),
            size=document.get("size"),
            mime_type=document.get("mime_type"),
            description=document.get("description"),
            tags=document.get("tags"),
        )
        self.repo.storage.invalidate(user_id)
        self.repo.log(user_id, "copy", document=record)
        return record, None
