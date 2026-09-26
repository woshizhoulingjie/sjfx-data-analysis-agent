"""Optional, separate-process version observer for registered server collections.

This is a snapshot verifier, NOT a kernel access auditor. No web/model processes
are loaded. Invalidated collections require re-registration and are not auto-approved.
"""
from __future__ import annotations

import argparse
import logging
import time
from pathlib import Path

from .store import Store
from .worker import verify_inventory


def observe_once(store, *, interval=60, limit=20):
    interval = max(30, interval)
    with store.db() as db:
        rows = db.execute(
            "SELECT c.id FROM collections c LEFT JOIN settings s ON s.key='verified:'||c.id "
            "WHERE c.state='ready' AND COALESCE(CAST(s.value AS REAL),0)<? "
            "ORDER BY COALESCE(CAST(s.value AS REAL),0),c.created LIMIT ?",
            (time.time()-interval, min(max(limit, 1), 100))).fetchall()
    for row in rows:
        verify_inventory(store, row['id'])
    return len(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', required=True)
    parser.add_argument('--interval', type=int, default=60)
    parser.add_argument('--limit', type=int, default=20)
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    if not (Path(args.state) / 'protection.db').is_file():
        parser.error('必须指定现有文件保护数据库目录；不会创建生产数据库')
    store = Store(args.state)
    interval = max(30, args.interval)
    while True:
        count = observe_once(store, interval=interval, limit=args.limit)
        logging.getLogger(__name__).info('Verified %s registered collections', count)
        if args.once:
            return
        time.sleep(interval)


if __name__ == '__main__':
    main()
