# Code Audit

Audit date: 2026-07-12

This audit covered local storage boundaries, settings recovery, single-instance IPC,
SQLite integrity, clipboard capture, OCR and AI service handling, Qt UI state and
performance, packaging, CI, dependencies, and release documentation.

## Fixed in the audited revision

- Clipboard snapshots are committed only after successful persistence, and transient
  clipboard or disk failures are retried instead of silently losing content.
- Repeated images no longer leave newly created unindexed PNG files.
- Clipboard text preserves whitespace and both text and images have resource limits.
- Settings use validated, atomic writes with a previous valid backup. Corrupt settings
  recover with clipboard monitoring disabled.
- Storage directories are validated before creation and reject symlink or Junction
  roots and migration targets.
- Single-instance IPC is scoped to the current user, validates its protocol, and does
  not unconditionally remove an active endpoint.
- SQLite uses schema versioning, a busy timeout, transaction-level locking, stable
  ordering, direct item lookup, path normalization, and concurrency-safe deduplication.
- Same-path Markdown changes update an existing record instead of creating a new row.
- Navigation clears hidden selections, preventing Delete from acting on an item that
  is no longer visible.
- AI, OCR, and semantic-search results are tied to their originating requests and no
  longer overwrite a newer detail or search state.
- AI, OCR, and semantic-search work is tracked and cancellation-aware. Shutdown
  invalidates pending results, requests cancellation, and waits for workers for a
  bounded period before closing the database and UI.
- Hidden grids pause preview work; list refresh avoids full row auto-resizing; file,
  import, recycle-bin, and settings errors are handled in the UI.
- Visible image thumbnails are decoded on a bounded worker pool. `QPixmap` creation
  and cache updates stay on the UI thread, while generation and file metadata guards
  reject stale results and invalidate changed files.
- OCR closes WinRT resources on success and failure. AI responses and embeddings are
  bounded and structurally validated.
- SQLite now runs a startup integrity check, creates three rotating validated backups,
  avoids rotating identical snapshots, writes a final snapshot on clean shutdown, and
  preserves corrupt database, WAL, and SHM files before restoring the newest compatible
  backup or rebuilding the index for a library rescan.
- Single-instance ownership is acquired before storage migration, database opening,
  window creation, or clipboard monitoring, preventing simultaneous cold starts from
  briefly running two writers.
- Shutdown now refuses to terminate while clipboard persistence, startup reconciliation,
  or an import is still inside a local write. Failed quit attempts leave the application
  available instead of terminating daemon workers with pending data.
- Structurally invalid application schemas now use the same preserve-and-restore path as
  SQLite corruption, while unsupported newer schema versions remain untouched.
- Startup path checks and library reconciliation, plus user-selected batch imports, run
  on tracked background workers instead of blocking the GUI thread.
- Startup reconciliation recovers unique image files left between PNG creation and the
  database insert by an unexpected process termination.
- Duplicate cleanup revalidates the current indexed path immediately before deletion,
  and the maintenance CLI refuses to run concurrently with the main application.
- Direct dependencies and PyInstaller are pinned, build/install scripts fail closed,
  CI covers Python 3.11-3.13, and third-party notices are documented.

## Verification

- 96 unit and regression tests pass.
- Python bytecode compilation, `pip check`, and `git diff --check` pass.
- A copy of the real database migrated from schema v1 to v2 with the row count
  unchanged and `PRAGMA quick_check` returning `ok`.
- The packaged application starts in about one second on the audit machine, remains
  responsive, settles to zero measured CPU over a five-second idle sample, and rejects
  a second visible instance.
- A 5,000-item model/view stress case initialized in about 0.18 seconds. Switching to
  the 5,000-row list took about 0.055 seconds, with a constant number of child widgets.
- A 12-megapixel clipboard image returned from the UI poll path in about 0.0004 seconds;
  PNG encoding, hashing and database persistence completed on the worker in about 0.325 seconds.

## Remaining risks

- Python's standard `urllib` cannot forcibly interrupt a request while the operating
  system is blocked establishing a connection or inside one response read. Requests
  have a 90-second network timeout, response reads are bounded and cancellation is
  checked between chunks; application shutdown itself waits only for a bounded period.
- The persistence queue is bounded by task count and an approximately 260 MiB memory
  budget. Clipboard changes that arrive faster than sustained disk throughput can fill
  the queue; the current clipboard snapshot is retried, but intermediate clipboard states
  may be lost once Windows no longer exposes them.
- Historical duplicate-path database rows are reported but not automatically merged,
  because favorites, notes, collections, and tags may differ between versions.
- Historical unindexed files are never deleted automatically. Cleanup must present an
  exact manifest and require explicit user confirmation.
- Release builds are not code-signed and do not yet publish checksums, an SBOM, malware
  scan results, or artifact attestations from a clean official-CPython build host.
- GitHub Actions and transitive Python dependencies are version-pinned only partially;
  action commit SHAs and a hash-locked wheel set remain release-hardening work.
- Windows Junction, cross-user IPC, DPI, multi-monitor, OCR language-pack, and recycle-
  bin behavior still need clean-machine end-to-end testing in addition to unit tests.
