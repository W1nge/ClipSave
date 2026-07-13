# Contributing

## Development setup

1. Use Windows 10 or 11 with Python 3.11+.
2. Create a virtual environment and install the hash-locked Windows dependencies.
3. Run the test suite before submitting changes.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --require-hashes -r requirements-windows.lock
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

## Pull requests

- Keep changes focused and preserve the local data boundary documented in `SECURITY.md`.
- Do not commit clipboard samples, databases, settings, API keys, user paths or generated executables.
- Add or update tests for database, storage or user-visible behavior changes.
- Describe migration and compatibility impact when changing persisted data.
