"""Plan or apply bounded SJFX history retention cleanup."""

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import Config, ensure_runtime_directories
from services.storage import Storage


def main():
    parser = argparse.ArgumentParser(
        description="Clean inactive scans and their database/filesystem artifacts."
    )
    parser.add_argument(
        "--retention-days", type=int, default=Config.HISTORY_RETENTION_DAYS
    )
    parser.add_argument("--max-scans", type=int, default=Config.HISTORY_MAX_SCANS)
    parser.add_argument("--owner-id", default=Config.OWNER_ID)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply deletions. Without this flag the command is a dry run.",
    )
    args = parser.parse_args()
    ensure_runtime_directories()
    storage = Storage(
        Config.DB_PATH, Config.DOCUMENT_CACHE_DIR, Config.SIDECAR_PAYLOAD_BYTES
    )
    result = storage.cleanup_history(
        owner_id=args.owner_id,
        retention_days=args.retention_days,
        max_scans=args.max_scans,
        output_dir=Config.OUTPUT_DIR,
        dry_run=not args.apply,
        limit=args.limit,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
