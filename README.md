# ClipSave

[English](README.md) | [简体中文](README.zh-CN.md)

ClipSave is a local-first Windows clipboard library for automatically saving, browsing, and organizing text, images, and Markdown.

## Features

- Automatically capture clipboard text and images, and turn Windows file-copy events into local path records
- Browse captured content by day with grid and list views
- Search, sort, favorite, and organize items with collections, tags, and notes
- Read-only Markdown rendering with a link back to the local source file
- OpenAI-compatible vision OCR with searchable recognized text
- Collapsible navigation, an optional detail panel, and Windows acrylic surfaces
- System-tray resident mode, single-instance protection, and the global `Ctrl+Alt+V` wake-up shortcut
- Optional OpenAI-compatible image descriptions and on-demand AI-expanded local search

## Download

The latest official Windows release is available on the [GitHub Releases page](https://github.com/W1nge/ClipSave/releases/latest).

- `ClipSave-<version>-windows-x64-installer.exe` is the recommended per-user installer. It does not require administrator permission and installs under `%LOCALAPPDATA%\Programs\ClipSave`.
- `ClipSave-<version>-windows-x64.zip` is the portable package. Keep `ClipSave.exe` and its adjacent `_internal` directory together.
- Matching `.sha256` files are provided for both downloads.

Uninstalling the application does not remove the separate `%LOCALAPPDATA%\ClipSave` user-data directory.

## Local data boundary

Clipboard capture, ordinary search, and Markdown reading run locally. OCR and image descriptions use the configured vision provider only when the user invokes them or enables the corresponding automatic setting:

```text
%LOCALAPPDATA%\ClipSave\Library   Managed clipboard files
%LOCALAPPDATA%\ClipSave\Data      Database, settings, backups, and caches
```

The program directory does not store user clipboard files. Manually imported images and Markdown files are copied into the managed local library; the originals are left unchanged.

Copying files in Windows Explorer is outside the file-content capture scope. ClipSave records the copied path as text and does not open, resolve, or copy the referenced file automatically.

Online AI is a separate, explicit feature. It runs only after the user configures a provider and invokes an AI command or enables automatic OCR/description. Automatic processing applies to new captured or imported images and does not retroactively process the existing library. See [SECURITY.md](SECURITY.md) for the complete data and network boundary.

### Configure Image AI

In **Settings**, enter the provider Base URL and vision model name; enter an API key only when the provider requires authentication. The **automatic OCR** and **automatic image description** switches are independent and disabled by default. When enabled, each newly captured or imported image is processed in the background. OCR sends the fixed prompt `ocr this`, while image descriptions use ClipSave's built-in retrieval-oriented prompt.

When ordinary local search is too narrow, **Expand Search** sends only the current search phrase to the configured model. The returned synonyms and related expressions are combined with OR and matched locally against titles, content, tags, notes, OCR text, and AI descriptions. ClipSave does not send library records to the model for expanded search and does not require an embedding model or vector index.

New installations start with automatic capture paused. Existing valid settings preserve the previous capture state; if the settings file is damaged, ClipSave resumes in the paused state.

## Requirements

- Windows 10 or Windows 11
- Python 3.11, 3.12, or 3.13 for running from source

## Run from source

```powershell
git clone https://github.com/W1nge/ClipSave.git
cd ClipSave
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --require-hashes -r requirements-windows.lock
.\.venv\Scripts\python.exe clipsave.py
```

You can also run `install.bat` to install and verify the locked dependencies, then start the source version with `.venv\Scripts\pythonw.exe clipsave.py`.

## Build the executable

```powershell
.\build.bat
```

`build.bat` pins PyInstaller 6.21.0 and produces the `build\release\ClipSave\` application directory and a versioned ZIP after dependency checks pass. An official archive named `ClipSave-<version>-windows-x64.zip` is produced only when `CLIPSAVE_OFFICIAL_BUILD=1`, the specified official CPython runtime, a clean Git worktree, and the locked distributions are all present. Other local builds are labeled `UNOFFICIAL` and use a distinct filename.

Qt DLLs and plugins live under `_internal`. A failed build returns a non-zero exit code and removes incomplete release output.

## Keyboard shortcuts

| Shortcut | Action |
| --- | --- |
| `Ctrl+K` / `Ctrl+F` | Focus the search box |
| `Ctrl+B` | Expand or collapse the left navigation |
| `Ctrl+I` | Expand or collapse the detail panel |
| `Ctrl+Alt+V` | Wake ClipSave from anywhere |

## Tests

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

See [SECURITY.md](SECURITY.md) for security boundaries and known limitations, and [CHANGELOG.md](CHANGELOG.md) for release history.

## Local library maintenance

The default maintenance command only scans the library and writes a manifest under `%LOCALAPPDATA%\ClipSave\Data\maintenance`; it does not delete files:

```powershell
.\.venv\Scripts\python.exe clipsave_maintenance.py
```

Cleanup only processes copies whose hashes exactly match valid database records and revalidates each file before acting. Recycle Bin and permanent deletion require explicit confirmation phrases. Unindexed files are never deleted automatically.

## License

[MIT License](LICENSE)

See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for bundled components, upstream licenses, Qt libraries, and source-acquisition information. That document is provided for release information and is not legal advice.
