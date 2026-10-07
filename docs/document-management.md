# Document management

Uploaded knowledge is shared with all members of its organisation. Uploading a
payslip does not make it private to the uploader. Owners and admins manage files;
members can inspect authorised extraction previews and source downloads.

The Knowledge detail view shows processing status, a preview of the first 4,000
extracted characters, page and chunk counts, current version, source and indexing
time. Ready means searchable, not a guarantee that any particular question has an
answer. A preview is fetched with current organisation authorisation each time.

Upload up to ten files per batch. Each upload has its own result and ingestion
job. A failed upload can be selected again; failed ingestion has a separate Retry
button. Identical content for the current document reuses its version and job.
For filename collisions, the default asks the user to choose replacement or a
separate document. The detail view's replacement form identifies the target by
its document ID and accepts a different filename. Rename changes the display title
without changing the original stored file or document identity. Re-index reuses
an idle current-version job; it cannot restart an already processing job.

## Retention

Replace immediately changes the current version used by retrieval. Previous
versions remain stored, labelled as historical in the detail view, and can still
be downloaded by authorised organisation members while the document remains in
the library. The detail view lists the latest fifty stored versions.

**Remove from library is not permanent erasure.** It immediately excludes the
document from retrieval, previews, downloads, retry and re-index operations.
Saved answers referencing removed or superseded evidence are redacted when read
and excluded from model history. Original files, historical versions, extracted
text, chunks and stored answer data remain in the database/object store, including
backups. No purge or backup-erasure promise is made. User questions and audit
metadata remain. The UI states this policy before removal. Permanent erasure
requires a separate operator retention process; the app has no purge operation.

## Extraction limits

Files are limited to 20 MiB. Supported formats are PDF, DOCX, UTF-8 text,
Markdown and CSV. CSV requires unique, nonempty headers, at most fifty columns,
two thousand rows and one thousand characters per cell. Each row becomes a
labelled section, preserving its period and numeric formatting. Complete-scope
calculations remain subject to the existing evidence bounds; larger tables must
be scoped or receive a clarification instead of an incomplete total.

PDFs without readable text fail with recovery guidance: export a searchable PDF
or run OCR before upload. OCR is not presently performed by Metis. DOCX tables
retain headings and rows. Embedding requests use batches of at most 64 chunks,
matching the local runtime's request limit. Semantic MiniLM chunks also use the
pinned offline tokenizer and a 254-content-token budget (plus two special
tokens), so dense codes and multilingual text cannot silently truncate.
Existing documents need an explicit re-index to adopt the new chunk boundaries.
