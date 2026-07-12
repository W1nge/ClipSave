# Code Audit

Audit date: 2026-07-13

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
- Startup reconciliation recovers unique image and Markdown files left between file
  creation and the database insert by an unexpected process termination. It also revives
  restored files and reindexes same-path replacements by content identity.
- Duplicate cleanup revalidates the current indexed path immediately before deletion,
  and the maintenance CLI refuses to run concurrently with the main application.
- Direct dependencies and PyInstaller are pinned, build/install scripts fail closed,
  CI covers Python 3.11-3.13, and third-party notices are documented.
- Clipboard capture uses Windows change notifications with a polling fallback, and native
  payload sizes are rejected before Qt materializes oversized text or DIB data.
- Text and Markdown use separate deduplication domains; tag search, summary-only queries,
  stable pagination, and bounded AI/OCR execution prevent cross-kind loss and memory spikes.
- Missing and zero-length primary databases restore the newest valid backup. New backups
  use monotonic sequence numbers, reject redirected backup directories, and no longer hold
  the main database lock during validation and rotation.
- Local storage is resolved from the Windows Local AppData known folder and rejects UNC,
  remote-drive, ancestor Junction, final symlink, and hard-link write targets.
- Keyboard grid selection, favorite clicks, stale image-copy completion, compact toolbar
  layout, short-screen scrolling, and semantic-search capture refresh behavior are covered.
- Release bundles include wheel and upstream license texts, dirty-build provenance, PE version
  metadata, executable/archive checksums, x64 enforcement, and frozen OCR import validation.
- Transient SQLite migration failures such as busy, disk-full, and I/O errors no longer trigger
  corruption recovery. Missing-primary recovery preserves stale WAL/SHM files, corrupt-set
  preservation rolls back partial renames, and backup candidates are copied once before schema
  and integrity validation.
- Clean shutdown now refuses to exit when the final backup fails, and the shared AI/OCR worker
  pool is shut down deterministically after outstanding work completes.
- Focused notes are flushed before the final backup, failed shutdown resumes clipboard capture
  without re-baselining, and all live background registries are rechecked during shutdown.
- Recycle-bin deletion now copies the verified file into a random sibling directory, sends that
  same-name copy to the Recycle Bin, and deletes the originally opened identity by handle. A
  path replacement is preserved rather than accidentally recycled.
- Managed imports use tracked temporary files and publish only after complete hash verification;
  failed SQLite commits roll back, and identical interrupted legacy migrations resume safely.
- Legacy/current database collisions compare the complete logical SQLite state including WAL
  commits, unsupported newer-schema backups are excluded from rotation, and clipboard queue-idle
  publication is serialized with task acceptance and completion.
- Failed note saves retain per-item drafts, invalid imports are reported as failures, and settings
  updates validate all values before an atomic save.
- Invalid same-path image replacements are hidden when reindexing fails, and corrupt backups are
  capped separately while backups from newer schema versions remain preserved.
- Reconciliation compares against the post-import disk identity so rapid valid replacements remain
  visible, and clipboard shutdown fully retires its persistence worker after queuing the sentinel.
- Shutdown retries retained note drafts for every item, refused exits restore AI/OCR controls,
  filtered-out items clear details, and rebuilt dynamic navigation preserves its active state.
- Tag color indicators survive active-state refreshes, and local packaging reapplies both the
  hash-locked runtime and build dependency sets before creating an artifact.
- New thumbnail generations receive bounded replacement worker capacity, transient restore errors
  abort instead of rebuilding empty, and interrupted cross-volume migrations reuse identical copies.
- Capture/scanner races preserve the indexed PNG, clipboard sequence wrap is handled, and the
  single-instance endpoint is derived from the authenticated Windows process-token SID.
- Scanner ownership is revalidated against the capture hash, identical migration cleanup holds
  verified source/destination handles, and a global Windows mutex enforces actual single-instance ownership.
- Thumbnail shutdown pauses new requests, rapid viewport changes are coalesced, and cache identity
  includes the database content hash so metadata-preserving replacements cannot reuse stale previews.
- Capture, import, and reconciliation commits hold identity-locked file handles; startup activation
  retries while the mutex owner opens IPC, malformed maintenance manifests fail before deletion,
  and release provenance omits builder-local executable paths.
- Reconciliation resolves restored duplicate hashes without aborting, all failed managed imports
  close identity guards before cleanup, startup contenders can take over after an owner exits,
  and Windows UI tests explicitly close their databases before temporary-directory cleanup.
- Official builds require a clean tree and official CPython 3.13.5; unsupported theme/hotkey
  pseudo-settings were removed, hotkey registration failure is surfaced, collection failures reload
  persisted state, and maintenance manifests use collision-resistant names.
- Database startup rejects unsafe WAL/SHM leaves, image imports fully decode payloads, monitoring
  baselines by sequence without materializing clipboard data, and session shutdown requests run the
  clean persistence path. Smoke-only storage overrides require a ready file.
- Release 0.3.0 includes OpenSSL/SQLite notices and a binary-specific README; saved sorting is shown
  with compact labels that remain valid at the minimum window width.
- Real SQLite rows render in the grid, transient dialogs release their retained documents, active
  searches refresh after notes/OCR changes, and deleted items reject late AI/OCR completions.
- Sidebar animations are reused without width jumps, refreshed tags inherit collapsed state, list
  selection clears its keyboard current index, and large Markdown avoids synchronous rich parsing.
- Markdown viewers reject local, network, and embedded `data:` resources so compressed inline images
  cannot expand into unbounded Qt pixmaps.
- Windows frameless resizing restores the native sizing frame, removes its visual non-client area,
  preserves the taskbar work area when maximized, and uses real per-window DPI for all eight hit zones.
- Sidebar animation no longer restacks a divider child on every frame, settings render without a
  hidden overflow scrollbar, and the brand/header boundary remains aligned in both themes.
- Windows effects, clipboard listeners, global hotkeys, known-folder lookup, and storage drive/path
  checks now use explicit pointer-sized ctypes signatures. Per-Monitor-V2 DPI awareness is preferred.
- Windows 11 system backdrops are gated to 22H2, failed DWM calls fall back to blur-behind, and the
  application reports total backdrop failure instead of silently claiming success.
- Legacy VBS source and release launchers were removed. Release archives now contain only the actual
  application directory, documentation, checksums, and required license material.
- Windows session-end shutdown now waits for clipboard persistence, creates the final validated
  backup, and closes SQLite before allowing logoff to continue.
- AI image requests encode the exact identity-locked snapshot approved by the user, reject
  cross-origin redirects before credentials can be forwarded, and use bounded single-read chunks
  so cancellation and deadlines are rechecked while receiving a response.
- Frozen startup readiness now fails on reconciliation errors and uncaught Qt callback exceptions;
  final test validation also scans stderr for tracebacks instead of trusting the process exit code alone.

## Verification

- 350 unit and regression tests pass with no background callback tracebacks.
- Python bytecode compilation, `pip check`, and `git diff --check` pass.
- A live SQLite snapshot of the real database migrated from schema v2 to v3 with all
  81 item rows and relationship counts unchanged, `PRAGMA quick_check` returning `ok`,
  and no foreign-key violations.
- The release EXE reports product version 0.3.0, executable/archive checksums recompute correctly,
  the manifest exactly matches the ZIP file set, and OCR import, isolated-profile
  startup/clean-shutdown, and frozen dual-instance exclusion smoke tests exit successfully.
- Local verification archives are explicitly labeled `UNOFFICIAL`, and their generated SHA-256
  sidecars are recomputed during release validation rather than recorded as permanent evidence.
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
- Managed creation, reading, hashing, permanent deletion, and the original-file side of recycle
  operations use verified Windows handles and reject reparse points and hard links. Recycle Bin
  restoration returns into a random sibling directory inside the library; startup reconciliation
  indexes restored content from that directory.
- Python's standard SQLite binding opens the primary database by path, and publishing a completed
  backup still uses a final path rename. Storage roots and leaf files are validated, but fully
  eliminating same-user path replacement races would require a custom SQLite VFS and handle-based
  rename publication.
- Release builds are not code-signed and do not yet publish an SBOM, malware scan result, or
  artifact attestation from a clean official-CPython build host.
- Runtime and build wheels are version-pinned and hash-locked for Windows x64 across Python
  3.11-3.13. The local verified build currently uses a conda-derived Python runtime; official
  CI packaging is restricted to a fixed official CPython 3.13.5 host.
- Cross-user IPC, mixed-DPI multi-monitor transitions, OCR language packs, and recycle-bin behavior
  still need clean-machine end-to-end testing in addition to unit tests and the real-HWND probe.
