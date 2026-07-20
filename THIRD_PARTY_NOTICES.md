# Third-Party Notices

ClipSave uses and distributes third-party software. This document records the versions currently pinned or bundled by the release process and points to upstream license and source information. It is an informational inventory, not legal advice or a legal conclusion about any particular distribution.

## Runtime components

| Component | Version | License identified by the project/package | Upstream |
| --- | --- | --- | --- |
| CPython runtime | 3.13.5 | Python Software Foundation License Version 2 | [CPython](https://github.com/python/cpython) |
| OpenSSL runtime libraries | 3.x | Apache-2.0 | [OpenSSL](https://www.openssl.org/) |
| SQLite runtime library | 3.x | Public domain | [SQLite](https://www.sqlite.org/) |
| PySide6 Essentials | 6.9.1 | LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only; Qt also offers commercial terms separately | [Qt for Python](https://code.qt.io/cgit/pyside/pyside-setup.git/) |
| shiboken6 | 6.9.1 | LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only; Qt also offers commercial terms separately | [Qt for Python](https://code.qt.io/cgit/pyside/pyside-setup.git/) |
| lucide Python package | 1.1.4 | MIT | [lucide-python](https://github.com/fmacedo/lucide-python) |
| Pillow | 12.3.0 | HPND/Pillow license family; the 12.3.0 wheel's included license text describes it as MIT-CMU | [Pillow](https://github.com/python-pillow/Pillow) |
| Send2Trash | 2.1.0 | BSD-3-Clause | [Send2Trash](https://github.com/arsenetar/send2trash) |
| typing_extensions | 4.16.0 | PSF-2.0 | [typing_extensions](https://github.com/python/typing_extensions) |

Release archives include `THIRD_PARTY_LICENSES/`, containing the license and metadata files supplied by installed wheels. The bundled CPython, OpenSSL and SQLite notices and fixed-version upstream texts needed by PySide6 are included under `THIRD_PARTY_LICENSES/UPSTREAM_LICENSES/`.

## Qt and the application directory

Official ClipSave release archives use PyInstaller `--onedir`. Qt dynamic libraries and plugins remain separate files under `ClipSave/_internal`, so recipients can inspect and replace compatible Qt binaries without modifying `ClipSave.exe`. The repository includes the application source, build script, dependency versions, and installation information needed to rebuild the combined work.

Source retrieval information for the pinned Qt/PySide line:

- Qt 6.9.1 source archive: [Qt 6.9.1 sources](https://download.qt.io/archive/qt/6.9/6.9.1/single/)
- Qt for Python/PySide source archive: [PySide6 release sources](https://download.qt.io/official_releases/QtForPython/pyside6/)
- Qt licensing texts and FAQ: [Qt licensing](https://www.qt.io/licensing/)

Anyone redistributing the `ClipSave` application directory should keep this notice available with the release and verify that the exact Qt binaries in the artifact correspond to the documented source version.

## Build tooling

ClipSave's release script pins PyInstaller 6.21.0. PyInstaller identifies its license as GPL-2.0-or-later with a special exception for distributing bundled applications. Its bootloader is included in the generated executable. Complete terms are provided in PyInstaller's `COPYING.txt` and at the [PyInstaller repository](https://github.com/pyinstaller/pyinstaller).

## Project license

ClipSave's own source code is provided under the [MIT License](LICENSE). Third-party components remain subject to their respective upstream terms.
