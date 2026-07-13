from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from PySide6.QtCore import QCoreApplication

from clipsave_app.app import SingleInstance
from clipsave_app.database import LibraryDatabase
from clipsave_app.maintenance import (
    CONFIRMATION_PHRASE,
    PERMANENT_CONFIRMATION_PHRASE,
    clean_indexed_duplicates,
    scan_orphans,
)
from clipsave_app.storage import ensure_storage_directories, migrate_legacy_layout


def main() -> int:
    parser = argparse.ArgumentParser(description="ClipSave local library maintenance")
    parser.add_argument("--apply", type=Path, help="Recycle indexed duplicate files from this manifest")
    parser.add_argument("--confirm", default="", help=f"Required phrase: {CONFIRMATION_PHRASE}")
    parser.add_argument("--permanent", action="store_true", help="Permanently delete instead of using the Recycle Bin")
    args = parser.parse_args()

    qt_app = QCoreApplication.instance() or QCoreApplication(sys.argv)
    single = SingleInstance()
    if single._endpoint_is_active() or not single.listen(lambda: None):
        print(json.dumps({"error": "Close ClipSave before running maintenance."}, ensure_ascii=False))
        return 3

    ensure_storage_directories()
    migrate_legacy_layout()
    ensure_storage_directories()
    database = LibraryDatabase()
    try:
        if args.apply:
            if args.permanent and args.confirm != PERMANENT_CONFIRMATION_PHRASE:
                parser.error(f"--permanent requires --confirm {PERMANENT_CONFIRMATION_PHRASE}")
            result = clean_indexed_duplicates(database, args.apply, args.confirm, permanent=args.permanent)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if not result["errors"] else 2
        manifest, report = scan_orphans(database)
        print(json.dumps({"manifest": str(manifest), **report["summary"]}, ensure_ascii=False, indent=2))
        return 0
    finally:
        database.close()


if __name__ == "__main__":
    raise SystemExit(main())
