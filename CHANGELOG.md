# Changelog

## 1.1.5 - 2026-09-29

- On Windows 10, Acrylic and the Qt foreground now share one native window and composition clip. Resizing from the left/top keeps their edges aligned while the cached Qt backing store drives live card reflow.
- Connected keyboard and Chinese IME input to that host, including focus recovery after resize, candidate positioning, and normal minimize/tray/close behavior.
- Reused card and text preview caches during resize and sidebar transitions, preserving text size and improving reflow performance for large grids.
- Kept text wrapping and preview clipping aligned with each moving card during resize, including cache growth and fractional-DPI transitions back to settled cards.
- Made paper layers opaque and restored the underlying card's content during peeling, with a flat, unprinted folded back and no gradient, highlight, or shadow coating.
- Softened paper removal with a detached drift, slight rotation and a 360 ms fade, while keeping the original peel speed, opaque paper during the peel, and its moving outline.
- Kept dragged paper moving and fading along the release direction, with full detachment in every direction instead of a fixed bottom-left destination.
- Improved the resting fold's visibility on yellow paper with a darker warm outline and crease.
- Kept long tags and collection headings within narrow panels, added explicit tag removal buttons, and preserved text preview position and selection during metadata updates.
- Redesigned item details with pinned actions, content-sized text previews, distinct collection/tag/note sections, collapsible metadata, and image-only recognition tools. Long Markdown opens at the beginning, and missing images show an explicit state.
- Fixed native-host wheel routing through child widgets into enclosing scroll areas, including detail labels and handoff from short previews or the end of long text.
- Kept the detail collection picker inside the main composition surface, anchored to its field; wheel scrolling no longer changes an item's collection.
- Restored task execution after a refused shutdown, closed late AI connections after cancellation, and prevented the original card outline from showing across lifted paper while preserving the lifted sheet's own moving outline.
- Fixed missed external clipboard captures by detecting Windows formats directly, deferring notification reads, and retrying busy or not-yet-rendered clipboard data without committing the change as processed.
- Upgraded PySide6/Qt to 6.9.3 to fix repeated `qt_imageToWinHBITMAP, failed to create dibsection` errors and UI stalls when dragging the window over the taskbar.
- Deleting an item now keeps the detail panel open instead of collapsing it while the grid updates.
- Tuned dark-mode Acrylic to blur 8, saturation 0.77 and #2D2D2D tint at 98/255 alpha; removed the top and sidebar black overlays and set the search overlay to 100/255.
- Moved card timestamps left by 1 px, kept text previews at a fixed font size during card movement, restored dragging on frameless windows with an imperceptible Acrylic hit-test layer, and prevented raw HTML in captured Markdown from hiding later entries.
- Hardened clipboard capture, background-job cancellation, usage-journal recovery and schema downgrade handling; improved Markdown previews and error diagnostics.

## 1.1.4 - 2026-09-25

- Re-capturing existing text or images now reuses the stored item and moves it to the top of the newest ordering instead of being silently ignored. Usage is recorded as plain-text lines in a human-readable journal under `Data\Usage`, so the database schema stays at v5 and remains openable by every earlier release.
- Copying items from within ClipSave also updates their last-used time, and cards, list rows and the detail panel show recency alongside the original capture date.
- Daily text exports (`clipboard_YYYY-MM-DD.md`) are no longer re-imported as full-day markdown items during startup scans.
- Removed the never-used `Data\thumbnails` directory and the runtime-dead embedding code paths left over from the removed vector search; the columns themselves are kept for older-version compatibility.
- Added a "where your data lives" map to the README describing every stored file, what is authoritative and what happens when it is deleted.
- Rebuilt card Markdown previews with a dedicated escaped-HTML renderer featuring compact heading hierarchy, shaded code, muted quotes, task checkboxes and colored links, with no resource loading.
- Darkened dark-mode card paper to rgb(41,41,41) against the rgb(32,32,32) home surface and restored translucent acrylic search-field styling with focus borders.
- Removed the one-frame acrylic flash when reopening the window from the tray by synchronizing the backdrop window during show instead of deferring it a loop iteration.
- Added `clipsave_maintenance.py --downgrade-schema` so users can explicitly move the database back to schema v5 (with a validated automatic backup) and keep using earlier releases, keeping data ownership with the user.

## 1.1.3 - 2026-09-22

- Rebuilt grid cards as layered paper with a draggable, click-to-tear favorite corner, non-blocking persistence, contextual time/date labels and simplified content presentation.
- Redesigned list view with image thumbnails, compact metadata, accurate total counts and incremental loading for large libraries.
- Refined the dark visual hierarchy with acrylic top and side bars, solid high-density content surfaces, a two-color ClipSave wordmark and rounded library scrollbars.
- Removed user-facing sorting, added browser-style middle-click auto-scroll, stabilized detail-panel card transitions and moved capture-state notifications to the monitoring toggle.

## 1.1.2 - 2026-09-19

- Removed confirmed dead, test-only and compatibility-only code across database, clipboard, Windows backdrop, frame and installer paths without changing intended product behavior.
- Simplified tests to exercise the real production paths directly instead of preserving helper APIs solely for test access.
- Kept the Windows 10 Acrylic implementation and its safety/geometry guarantees intact while reducing the maintenance surface.
- Moved user-triggered library search, navigation, pagination and mutation refresh queries off the GUI thread, with cancellation and stale-result protection.
- Reduced repeated full-file hashing and image decoding in reconcile, import and clipboard image-save paths while preserving identity-locked safety checks.
- Tightened smoke/interactive Windows release gates, AI request total deadlines, compare-and-set result persistence and shutdown task tracking.
- Consolidated duplicated AI/OCR request orchestration and shared shutdown cancellation/finalization paths.

## 1.1.1 - 2026-09-19

- Rebuilt Windows 10 Acrylic around an always-on Windows.UI.Composition effect path so blur remains visually identical while moving and resizing the window.
- Added a dedicated pure-Win32 backdrop HWND behind the Qt UI, with pixel-perfect live geometry synchronization and no material switching during interactive move/resize.
- Fixed the Composition visual tree root sizing bug that allowed the effect graph to attach successfully while rendering no visible blur.
- Added compositor-side Gaussian blur and saturation through Win2D, including the required VCRT forwarders and release license notices.
- Expanded Windows Acrylic release validation with controlled blur probes, geometry-lock checks, packaged visual smoke tests and regression coverage.

## 1.1.0 - 2026-07-30

- Rebuilt sidebar and detail-panel transitions around measured start/end geometry, linear high-refresh progress, stable layering and reversible animation transactions.
- Added cached, monotonic card reflow for every visible row while keeping card headers, text size, image aspect ratio and unrelated controls stable.
- Unified static and animated plain-text layout, preserving explicit line breaks while allowing URLs, paths, email addresses and other machine-readable spans to wrap one character at a time.
- Kept detail content fixed during its horizontal reveal and removed splitter minimum-size feedback that caused one-frame jumps.
- Strengthened OCR transcription instructions, filtered model reasoning from visible results and added high-reasoning search expansion with automatic compatibility fallback.

## 1.0.0 - 2026-07-23

- Replaced embedding-based semantic search with on-demand AI query expansion. Expanded terms are OR-matched locally without uploading library records or requiring a vector model.
- Removed runtime embedding generation and the embedding-model setting while retaining legacy database columns for backward compatibility.
- Added resumable bulk OCR and image-description processing with persistent stage-level progress, so interrupted jobs continue without repeating completed OCR requests.
- Added Markdown rendering inside homepage cards, a plain-text reader opened by triple-click, single-line path presentation, and streamlined cards with larger preview areas.
- Added independent automatic OCR and image-description controls backed by the configured OpenAI-compatible vision service.
- Corrected native maximize, restore, Aero Snap and cross-monitor work-area synchronization for the frameless Windows window.
- Improved clipboard contention recovery, precision-touchpad scrolling, single-instance wake-up delivery, settings durability, database recovery and asynchronous shutdown safety.
- Refined light and dark Fluent surfaces, custom scrollbars, detail resizing and edge-aligned detail scrolling.

## 0.3.5 - 2026-07-21

- Use native Windows maximize state for the custom title-bar button so maximize and restore remain synchronized with the shell.

## 0.3.4 - 2026-07-21

- Replaced the old Windows OCR path with OpenAI-compatible vision OCR using the `ocr this` prompt.
- Added opt-in automatic OCR and image description for newly captured or imported images.
- Added a structured image-description prompt for visible text, scene details, layout, colors, and search keywords.
- Added right-click detail toggling for every item type and image triple-click opening while preserving double-click copy.
- Themed text context menus and Markdown dialogs for light and dark mode, and removed the translucent detail splitter gap.
- Fixed Aero Snap maximize/restore state synchronization and prevented restored windows from retaining maximized client bounds.

## 0.3.3 - 2026-07-14

- Added asynchronous, recoverable deletion for large managed files and indexed content.
- Added bounded homepage pagination, independent sidebar scrolling, expandable tag entries,
  responsive dialogs, and disabled detail controls when no item is selected.
- Added embedding provider/model/dimension/revision metadata and filtered semantic search so
  vectors from incompatible configurations are never compared.
- Migrated database timestamps to UTC storage while keeping date navigation in local time.

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
- Expanded regression coverage from 8 to 69 tests and documented the full audit conclusions.
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
