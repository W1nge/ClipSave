# Security Policy

## Local storage boundary

- Automatically captured text, images and Markdown are written only under `%LOCALAPPDATA%\ClipSave\Library`.
- SQLite data, settings and thumbnails are written under `%LOCALAPPDATA%\ClipSave\Data`.
- The executable and source directory do not contain user clipboard files.
- Imported images and Markdown are copied into the managed local library. The original remains unchanged.
- ClipSave can send files to the Recycle Bin only when their resolved path is inside the managed library.
- Embedded Markdown links are not opened automatically.

## Network behavior

- Clipboard monitoring, local search, Markdown reading and Windows OCR do not use the network.
- Single-instance coordination uses a local Qt IPC endpoint and only accepts a request to show the existing window.
- Online AI is an independent, explicit action. It runs only after the user configures a provider and invokes an AI command.

## Data protection limits

- Library files and the SQLite database are not encrypted by ClipSave. They inherit the current Windows user's filesystem permissions.
- Clipboard monitoring can capture sensitive content. Pause monitoring before copying secrets that should not be retained.
- Anyone with access to the same Windows account may be able to read the local library.

## Reporting a vulnerability

Please use GitHub's private security advisory reporting for this repository. Do not include real clipboard content, API keys or other personal data in a public issue.
