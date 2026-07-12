# Changelog

## Unreleased

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

## 0.2.0 - 2026-07-11

- Rebuilt ClipSave as a PySide6 Windows desktop application.
- Added local SQLite indexing, image and Markdown browsing, collections, tags and favorites.
- Added collapsible navigation, daily browsing, optional details panel and acrylic styling.
- Added local Windows OCR, tray operation, single-instance handling and global wake shortcut.
- Separated program files from the `%LOCALAPPDATA%\ClipSave` user-data store.
- Added optional OpenAI-compatible image descriptions and semantic search.
