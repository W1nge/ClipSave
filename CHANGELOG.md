# Changelog

## 0.3.2 - 2026-07-13

- Added an optional per-user Windows startup setting with rollback when the startup entry cannot be changed.
- Added a no-admin Windows installer that preserves the separate local ClipSave data directory during uninstall.
- Published an installer checksum alongside the portable ZIP and its checksum.

## 0.3.1 - 2026-07-13

- Fixed AI requests failing before slower local or remote model backends could return their first response byte.
- Preserved cross-monitor and partially off-screen window placement after moving, resizing, and opening details.
- Restored precision-touchpad scrolling by accumulating fractional wheel deltas.
- Added clipboard contention retries, safer persistence shutdown, and worker-side image deduplication state.
- Escaped SQLite search wildcards and made image metadata indexing use one stable file handle.
- Added acknowledged single-instance wake-up messages and moved the global hotkey ID into the valid application range.
- Kept settings memory aligned with atomically published files after durability-sync failures.
- Added list-view favorite controls, selected-detail text copying, transient error tooltips, and high-DPI rendering fixes.
- Hardened maintenance manifests and Windows managed-file creation against temporary files, cross-library cleanup, and empty-file residue.
- Expanded the regression suite to 368 tests and validated the frozen Windows build with real HWND and startup smoke probes.

## 0.3.0 - 2026-07-12

- Fixed clipboard retry, duplicate-image cleanup, text fidelity, and resource-limit bugs.
- Added atomic settings recovery, validated local storage roots, and hardened single-instance IPC.
- Added SQLite schema migrations, locking, stable queries, path repair, and safer import deduplication.
- Fixed stale UI selections, asynchronous result races, hidden preview work, and file-operation errors.
- Expanded regression coverage from 8 to 69 tests and documented the full audit in `AUDIT.md`.
- Added a dry-run orphan manifest and explicitly confirmed duplicate-file cleanup tool.
- Moved clipboard image encoding, hashing and database persistence to a bounded background queue.
- Replaced per-item grid and table widgets with Qt model/delegate views for large-library performance.
- Moved visible thumbnail decoding to a bounded worker pool with UI-thread pixmap caching, stale-result guards and file-change invalidation.
- Added SQLite startup integrity checks, three rotating validated backups, and preservation/recovery of corrupt database sidecar files.
- Prevented identical snapshots from consuming backup rotation slots and added a final validated backup on clean shutdown.
- Acquired the per-user single-instance endpoint before opening storage or starting clipboard monitoring.
- Refused shutdown while clipboard, reconciliation or import writes remain active, preventing daemon-worker data loss.
- Recovered structurally invalid schemas from preserved backups and reconciled crash-orphaned images at startup.
- Moved startup library checks and batch imports off the UI thread with cancellation-aware task tracking.
- Revalidated maintenance targets against the current indexed path and blocked cleanup while ClipSave is running.
- Staged release builds before replacing the previous EXE and added Windows `py.exe` launcher discovery to installation.
- Added tracked cancellation and bounded shutdown waiting for AI, OCR and semantic-search tasks.
- Pinned all verified direct runtime dependencies and removed the unused `pyperclip` dependency.
- Pinned PyInstaller 6.21.0 and made install/build failures return non-zero exit codes without unconditional success messages.
- Separated the source and release launchers so an old `ClipSave.exe` cannot silently replace a source run.
- Added Python 3.11-3.13 CI coverage, dependency consistency checks, read-only workflow permissions and credential-free checkout.
- Added third-party license notices, Qt single-file distribution details and additional sensitive-file ignore rules.
- Switched clipboard monitoring to Windows change notifications with native payload-size preflight and bounded persistence.
- Added schema v3 with separate text/file deduplication, tag search, summary pagination, and stricter schema validation.
- Restored missing or truncated databases from monotonic validated backups and rejected redirected backup directories.
- Resolved Local AppData through the Windows known-folder API and rejected network, Junction, symlink, and hard-link storage targets.
- Fixed stale image-copy completion, keyboard grid selection, favorite-selection mismatch, compact toolbar overlap, and short-screen detail/settings layout.
- Added retryable AI/OCR executor shutdown, nonblocking bounded IPC clients, and deterministic socket cleanup.
- Added a versioned x64 release ZIP with PE metadata, verified checksums, frozen OCR import testing, dirty-build provenance, and bundled third-party license texts.
- Prevented transient SQLite migration errors from triggering destructive recovery, preserved stale sidecars, and made corrupt-file preservation rollback-safe.
- Reconciled crash-orphaned Markdown files, restored matching missing files, and reindexed same-path replacements by content.
- Refused clean shutdown when the final database backup fails and added deterministic shared AI/OCR executor shutdown.
- Added CPython runtime licensing and provenance, Unicode-safe standard checksum manifests, and retryable graceful packaged-app smoke readiness.
- Hash-locked Windows x64 runtime and build wheels and pinned official packaging to CPython 3.13.5.
- Added full frozen OCR runtime smoke coverage and fail-closed PyInstaller missing-module checks.
- Fixed focused-note loss, clipboard resume gaps, reentrant shutdown tasks, thumbnail retry/shutdown behavior, and startup IPC show requests.
- Made recycle-bin deletion identity-safe, managed imports temporary-and-verified, commit failures rollback-safe, and interrupted identical migrations resumable.
- Preserved WAL-only migration history, protected newer-schema backups from rotation, and made clipboard idle publication atomic.
- Retained failed note drafts, reported invalid imports correctly, and rejected oversized settings without erasing previous values.
- Hid invalid same-path file replacements and capped corrupt backup accumulation without deleting newer-schema backups.
- Kept rapidly changing valid replacements indexed and closed the clipboard shutdown/resume worker race.
- Made Windows CI extract Unicode archive paths with Python and read checksum manifests explicitly as UTF-8.
- Retried all retained note drafts on exit and restored filtered details, task controls, and dynamic navigation state.
- Preserved tag color indicators across navigation refreshes and enforced the runtime hash lock during packaging.
- Prevented stale thumbnail jobs from starving the current viewport and made transient restore failures fail closed.
- Resumed identical cross-volume migration copies without duplication and preserved scanner-owned captures.
- Bound single-instance IPC to the Windows user SID and handled 32-bit clipboard sequence wraparound.
- Added a Windows global mutex for real single-instance enforcement and revalidated scanner-owned captures.
- Added frozen dual-process single-instance coverage to the Windows release workflow.
- Made identical migration cleanup handle-atomic and paused/coalesced thumbnail scheduling during shutdown and scrolling.
- Keyed thumbnails by indexed content hash to invalidate metadata-preserving same-path replacements.
- Held identity-locked handles across capture/import/reconciliation commits and retried startup activation IPC.
- Rejected malformed maintenance manifests before deletion and removed builder-local executable paths from releases.
- Made reconciliation duplicate-safe, cleaned every failed managed import, and added startup-owner failover.
- Closed test databases explicitly so isolated Windows UI tests cannot leak locked temporary files.
- Enforced clean official CPython 3.13.5 builds and removed unsupported theme/hotkey settings.
- Surfaced hotkey registration failures, rolled back failed collection UI, and made maintenance reports unique.
- Protected SQLite sidecars, fully decoded imported images, bounded clipboard startup, and handled session shutdown.
- Restricted smoke storage overrides, added native OpenSSL/SQLite licensing, and shipped a release-specific README.
- Released version 0.3.0 and restored saved sort labels without compact-toolbar overlap.
- Expanded the audit regression suite to 238 tests, including a real schema v2-to-v3 migration snapshot.
- Fixed real `sqlite3.Row` grid painting, transient-dialog lifetime leaks, stale search results after notes/OCR updates, and late AI/OCR results after deletion.
- Reused sidebar animations, preserved collapsed tag state across metadata refreshes, and cleared stale list current indexes with selection.
- Rendered large Markdown as plain text and blocked embedded `data:` images that could expand into unbounded Qt pixmaps.
- Added bounded, noninteractive Windows session shutdown and native preflight for registered PNG clipboard payloads.
- Distinguished local unverified packages from official releases and tightened version, launcher, runner, and dependency-lock release contracts.
- Hardened fixed native clipboard snapshots, resumable bounded shutdown, SQLite leaf/sidecar validation, backup publication, and session-end request recovery.
- Expanded the final audit regression suite to 319 tests and completed a fresh zero-actionable-findings review of the latest UI state.

## 0.2.0 - 2026-07-11

- Rebuilt ClipSave as a PySide6 Windows desktop application.
- Added local SQLite indexing, image and Markdown browsing, collections, tags and favorites.
- Added collapsible navigation, daily browsing, optional details panel and acrylic styling.
- Added local Windows OCR, tray operation, single-instance handling and global wake shortcut.
- Separated program files from the `%LOCALAPPDATA%\ClipSave` user-data store.
- Added optional OpenAI-compatible image descriptions and semantic search.
