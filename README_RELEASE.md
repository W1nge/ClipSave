# ClipSave

ClipSave is a local-first Windows clipboard library for text, images, and Markdown.

## Run

Keep the complete extracted directory together, then run `ClipSave\ClipSave.exe`.
The adjacent `ClipSave\_internal` directory is required and must not be moved separately.

For a normal per-user installation, run the `windows-x64-installer.exe` asset from the
GitHub release. It installs under `%LOCALAPPDATA%\Programs\ClipSave`, creates a Start
Menu shortcut, and does not require administrator permission. Uninstalling the program
does not remove the separate `%LOCALAPPDATA%\ClipSave` data directory.

## Local data

ClipSave stores captured files only under:

```text
%LOCALAPPDATA%\ClipSave\Library
```

The SQLite database, settings, backups, and caches are stored under:

```text
%LOCALAPPDATA%\ClipSave\Data
```

Online AI is optional and runs only after a provider is configured and the user invokes an AI command or enables automatic OCR/description. Automatic processing applies to new images only. In Settings, provide the provider Base URL and vision model, then enable either switch as needed; OCR uses the fixed prompt `ocr this`, while image descriptions use ClipSave's built-in retrieval-oriented prompt. Expand Search sends only the current query to that model and applies the returned OR terms to the local library; it does not upload library records or require an embedding model.

## Integrity

`SHA256SUMS.txt` covers every distributed file except the checksum manifest itself. The adjacent
`.zip.sha256` file verifies the release archive.

These hashes provide integrity checks only. They do not authenticate who produced or published the archive; verify downloads against the project's official release channel.

## Project

- Source and documentation: https://github.com/W1nge/ClipSave
- Security policy: https://github.com/W1nge/ClipSave/blob/main/SECURITY.md
- License: `LICENSE`
- Third-party notices: `THIRD_PARTY_NOTICES.md` and `THIRD_PARTY_LICENSES\`
