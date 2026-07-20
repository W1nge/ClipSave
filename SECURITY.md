# Security Policy

## Local storage boundary

- Managed image and Markdown files are written under `%LOCALAPPDATA%\ClipSave\Library`.
- SQLite records, including captured text and searchable metadata, settings, thumbnails and database backups are written under `%LOCALAPPDATA%\ClipSave\Data`.
- The executable and source directory do not contain user clipboard files.
- Imported images and Markdown are copied into the managed local library. The original remains unchanged.
- Windows file-copy events store only the `CF_HDROP` path strings as text records. ClipSave does not open, resolve, or copy the referenced files automatically.
- ClipSave can send files to the Recycle Bin only when their resolved path is inside the managed library.
- Storage roots that are symbolic links or Windows Junctions are rejected before data directories are created.
- Embedded Markdown links are not opened automatically.

## Network behavior

- Clipboard monitoring, ordinary local search and Markdown reading do not use the network.
- OCR and image descriptions use the configured OpenAI-compatible vision endpoint when the user invokes them or enables automatic processing. The selected image is encoded and sent to that endpoint; it is not sent when both automatic settings are disabled and no AI command is invoked.
- Single-instance coordination uses a local Qt IPC endpoint and only accepts a request to show the existing window.
- Online AI is an independent, explicit action. Automatic OCR and description are opt-in settings and apply only to newly captured or imported images.

## Data protection limits

- Library files and the SQLite database are not encrypted by ClipSave. They inherit the current Windows user's filesystem permissions.
- Clipboard monitoring can capture sensitive content. Pause monitoring before copying secrets that should not be retained.
- New installations and corrupt-settings recovery start with clipboard monitoring paused.
- Anyone with access to the same Windows account may be able to read the local library.

## Reporting a vulnerability

Please use GitHub's private security advisory reporting for this repository. Do not include real clipboard content, API keys or other personal data in a public issue.
