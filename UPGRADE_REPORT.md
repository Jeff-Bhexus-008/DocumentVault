# Doxari Upgrade Report

Upgrade of the existing Doxari deployment (Flask + Supabase auth/Postgres/storage,
deployed on Render). The application was upgraded **in place**: the same entry
point (`app:app`), the same environment variables, the same authentication, the
same `documents` table and the same private `document` storage bucket.

---

## 1. Features implemented

**Design system and shell**

- Forest-green brand system: `#166534` primary, `#22C55E` secondary, `#86EFAC`
  accent, `#F8FAF8` background, `#1F2937` text, sage/mint/gray supporting tones.
- New Doxari logo (geometric document + leaf) as inline SVG and image asset, plus
  a consistent icon set for documents, folders, sharing, security, navigation.
- Responsive sidebar + mobile bottom navigation, sticky app bar, command-style
  global search, rounded cards, green-tinted shadows, tasteful hover/transition
  effects, optional dark mode (persisted per user and in `localStorage`).
- Accessible focus rings, skip link, labelled controls, `aria-live` regions,
  skeleton loaders, empty states, toast notifications and friendly error pages.

**Dashboard (`/dashboard`)**

- Personalised greeting from the signed-in account, "Your documents, organized and
  secure" tagline, Upload Document and New Folder actions, global search bar.
- Real summary cards: total documents, folders, storage used, available storage
  (when `STORAGE_LIMIT_MB` is configured), recent uploads, favorites, documents
  shared with the user. All figures come from Postgres counts and stored sizes.
- Recent activity feed, recently accessed/uploaded document grid, quick-access
  shortcuts to All Documents, Folders, Favorites, Recent, Shared, Trash, Settings.

**Document management (`/documents`, `/folders`, `/favorites`, `/recent`)**

- Single and multi-file upload with per-file progress, duplicate-submit guards and
  clear per-file error reporting; nothing is reported as uploaded unless storage
  accepted the object and the row was written.
- Folders with nesting, rename, colour/icon, delete (documents return to the
  library, never silently destroyed), breadcrumb navigation, folder-scoped search.
- Rename, move, duplicate, download, preview, favorite/unfavorite, delete.
- Sort by name, date uploaded, date modified, size, type; grid and list views;
  multi-select with bulk favorite/unfavorite/move/delete/restore/purge.
- Metadata panel: size, type, upload date, last modified, folder, tags, description.
- Server-side validation of extension, MIME family and size (`MAX_UPLOAD_MB`,
  default 25 MB) in addition to the browser checks.

**Search (`/search`)**

- Filename, folder, description and (when the migration has run) extracted
  `content_text` search through a single PostgREST `or` filter.
- Natural-language planning without any AI provider: "favorites", "recent",
  "largest", "pdfs", "school certificates", "last 3 months", type and folder
  filters, all translated into the same query primitives.
- Filters for type group, folder and upload date, plus sorting; suggestions,
  `did you mean` folder matches, explicit no-results state and a `mode=keyword`
  switch for literal matching.

**Preview (`/documents/<id>`)**

- PDF, images, text/markdown/CSV/JSON rendered in-page from short-lived signed
  URLs (2 minutes) that are minted server-side after an ownership or share check.
- Office formats (doc/docx/xls/xlsx/ppt/pptx) and archives show a professional
  file-information screen with a working download action instead of a broken
  viewer.
- Navigation to previous/next document, favorite, share, rename, move, download.

**Organization**

- Default categories School, Work, Personal, Certificates, Important plus General,
  implemented as folders with icon and colour, so they are renameable and real.
- Custom folders/subfolders, tags, descriptions/notes, favorites, recently
  accessed tracking (`last_accessed_at`), drag-and-drop move onto folder targets.

**Sharing (`/shared`)**

- Share a document with another registered Doxari account at view-only or
  download permission; permission can be changed or revoked at any time.
- Public share links with a configurable permission and expiry; view-only links
  cannot download because the server refuses the download route for that token.
- "Shared with me" and "Shared by me" views showing the permission per document.
- Every share read is re-validated on the server against `document_shares` /
  `share_links`; the front end never decides access.

**Trash (`/trash`)**

- Deleting sets `deleted_at` (soft delete) and keeps the folder, so restore puts
  the document back where it was; deleted items show when they were deleted.
- Restore, permanent delete (with strong confirmation), and Clear Trash. Storage
  objects are only removed on permanent deletion, so restore always works.

**Insights (`/analytics`, `/activity`, notifications)**

- Document totals, storage used versus limit, file-type distribution, upload
  activity for the last 12 weeks, largest documents, most active folders, recent
  uploads. Charts are drawn from live aggregates; no invented history.
- Activity history written by the server for upload, rename, move, favorite,
  delete, restore, purge, share, revoke, download and sign-in events.
- Notification centre fed by real activity and storage-threshold events, with an
  unread badge and a mark-all-read action.

**Settings (`/settings`)**

- Profile name update (Supabase user metadata), email display, avatar initial,
  theme/appearance, grid-or-list default, notification opt-in, onboarding replay,
  password change through Supabase auth, storage information, sign-out.
- Account deletion workflow with typed confirmation; it removes the user's
  documents, storage objects and account, and explains the consequence first.
- One-click download of the SQL migration (`/settings/migration.sql`).

**AI (optional, `/api/ai/*`)**

- Provider-agnostic integration (OpenAI, Groq, OpenRouter, Together, DeepSeek) via
  `AI_PROVIDER`, `AI_API_KEY`, optional `AI_MODEL` / `AI_BASE_URL`.
- Summarise, key points, short description, keyword/tag suggestions and
  document Q&A, all executed server-side; keys never reach the browser.
- The UI shows the AI panel only when `/api/ai/status` reports a configured
  provider, and states plainly that it is not configured otherwise. Natural
  language search works without any provider.

---

## 2. Existing features preserved

- Same Supabase project: existing accounts, sessions and passwords continue to
  work; login, registration and password reset still go through Supabase Auth.
- Same `documents` table and rows; no column was dropped or renamed and no data
  was migrated destructively. New columns are additive and nullable.
- Same private `document` storage bucket and the existing object paths
  (`<user_id>/<file>`) are read as before, so old files still open and download.
- Same `app:app` entry point, same environment variable names, same Render
  service configuration; no new infrastructure or paid dependency is required.
- Old routes from the previous version (`/download/<id>`, `/view/<id>`,
  `/delete/<id>`, `/favorite/<id>`) still resolve, so bookmarks keep working.
- Every feature degrades safely on an un-migrated database: the app starts, lists
  and downloads documents, and simply hides folders/sharing/trash affordances it
  cannot back with a table (verified against a legacy-schema run).

---

## 3. Database and storage changes

All changes are in `migrations/2026_11_04_doxari_upgrade.sql` (also downloadable
from Settings). Every statement is idempotent (`if not exists`), additive, and
safe to re-run.

- `public.documents`: added `size`, `mime_type`, `description`, `tags text[]`,
  `deleted_at`, `last_accessed_at`, `updated_at`, `folder_id`, `content_text`.
  Indexes on `(user_id, created_at desc)`, `(user_id, folder_id)`,
  `(user_id, deleted_at)`, `(user_id, lower(filename))`.
- `public.folders` (new): id, user_id, name, parent_id, color, icon, timestamps.
- `public.document_shares` (new): document_id, owner_id, shared_with_id,
  shared_with_email, permission, revoked, timestamps.
- `public.share_links` (new): document_id, owner_id, token, permission, expires_at,
  revoked, timestamps.
- `public.activity_log` (new): user_id, action, document_id, meta jsonb, created_at.
- `public.user_preferences` (new): user_id, theme, view_mode, notifications,
  onboarding flags.
- RLS enabled on every new table plus `documents`, with owner-scoped policies and
  additive read policies for recipients of a share. `storage.objects` policy keeps
  the `document` bucket private and limits objects to the owning user's prefix.
- Storage layout unchanged (`document/<user_id>/<random>_<filename>`); storage
  objects are deleted only on permanent deletion or account deletion.

---

## 4. Security improvements

- Every document operation resolves the document through an ownership query
  (`id` **and** `user_id`), so changing an ID in a URL returns 404 instead of
  another user's file. Verified for preview, raw, text, download, favorite,
  rename, move, delete, restore, purge and share.
- Private files are never exposed by permanent public URL: previews and downloads
  use signed URLs minted server-side after the permission check (2 minute TTL).
- Public share links are opaque 56-character tokens, checked for revocation and
  expiry on each request, and a view-only token cannot reach the download route.
- CSRF protection on all mutating routes (session token, constant-time compare),
  with JSON and header support for fetch calls.
- Upload validation on the server: extension allow-list, MIME family check,
  sanitised filename, per-file size cap and a hard `MAX_CONTENT_LENGTH`.
- Supabase service-role key stays server-side only; the browser never receives it.
  The Supabase anon key and URL are the only values rendered to the client, and
  only through the configured environment.
- Session hardening: `HttpOnly`, `SameSite=Lax`, `Secure` when served over HTTPS,
  rotating secret from `FLASK_SECRET_KEY`, explicit sign-out that clears session.
- Error handling returns friendly messages without leaking Postgres text, tokens
  or stack traces; unexpected errors log server-side only.
- Confirmation dialogs for permanent delete, clear trash, revoke share and
  account deletion.

---

## 5. Needs additional configuration or paid services

- **Database migration must be run once** in the Supabase SQL editor (or via
  `psql`) using `migrations/2026_11_04_doxari_upgrade.sql`. Until then, folders,
  trash, sharing, activity and analytics stay hidden and the classic library keeps
  working. This is a deliberate safety property, not a bug.
- **Optional:** `STORAGE_LIMIT_MB` to show a real "available storage" figure and
  storage-threshold notifications. Supabase's project quota is not exposed to the
  app, so without it Doxari shows used storage only rather than guessing a limit.
- **Optional AI:** set `AI_PROVIDER` and `AI_API_KEY` (and optionally `AI_MODEL`,
  `AI_BASE_URL`) on the server. No key is bundled. Until configured, the AI panel
  reports "not configured" and natural-language search still works.
- **Office document preview** (doc/docx/xls/xlsx/ppt/pptx) needs an external
  converter or viewer service (for example LibreOffice in the container or a
  commercial viewer). Doxari currently shows file information plus download for
  those formats rather than a broken viewer. Everything else previews natively.
- **Full-text search inside PDFs** is stored in `content_text` and searchable when
  the migration is applied and extraction succeeds; PDFs that are scanned images
  need OCR, which is not included.

---

## 6. Remaining issues / notes

- Office previews and OCR are the two knowingly unimplemented items above; both
  degrade to a clear information screen instead of a fake feature.
- Analytics starts recording from the upgrade date; there is no fabricated
  twelve-month history. Charts fill in as activity accumulates.
- Uploads are validated and streamed per file; very large archives are limited by
  `MAX_UPLOAD_MB` (default 25 MB), which can be raised via the environment.
- The previous single-file templates in `/templates` and `/static` at the repo
  root are left in place untouched for reference; the running application serves
  `doxari/templates` and `doxari/static`. They can be removed in a follow-up once
  the new UI has been live for a while.

---

## 7. Verification performed

Two automated suites run against a local PostgREST-compatible mock of the Supabase
API, plus a legacy-schema run:

- `tools/smoke_test.py` - **35/35 passing**: registration, dashboard, multi-file
  upload, listing, folder create, move, preview, text extraction, download,
  favorite, favorites page, smart and keyword search, no-results state, trash
  delete/restore, share to a second account, recipient access, private-document
  isolation, share link creation, anonymous link view, view-only download refusal,
  analytics, activity, settings, migration download, preference save, notifications,
  bulk action, sign-out.
- `tools/security_test.py` - **27/27 passing**: anonymous block on every page and
  file route, cross-user block on preview/text/download/signed-URL/delete/favorite/
  share/bulk, view-only share enforcement, blocked file type, oversized upload,
  missing CSRF rejection.
- Legacy profile run (original `documents` schema, no migration): registration,
  upload, listing, all pages, preview and download all return 200.
