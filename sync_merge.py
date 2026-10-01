#!/usr/bin/env python3
"""Merge a fresh pipeline.db snapshot from the Mac into the server copy,
preserving the server-authoritative publishing decisions.

Usage: python3 sync_merge.py /path/to/incoming_snapshot.db
Replaces data/pipeline.db with the snapshot, after copying published_status
values (published/discarded) from the current server DB into it by id.
"""
import shutil
import sqlite3
import sys
import os

BASE = os.path.dirname(os.path.abspath(__file__))
LIVE = os.path.join(BASE, "data", "pipeline.db")


def main(incoming):
    if not os.path.exists(incoming):
        sys.exit(f"incoming snapshot not found: {incoming}")
    if os.path.exists(LIVE):
        old = sqlite3.connect(LIVE)
        keep = old.execute(
            "SELECT id, published_status FROM images "
            "WHERE published_status IN ('published','discarded')").fetchall()
        old.close()
        new = sqlite3.connect(incoming)
        for rid, st in keep:
            new.execute("UPDATE images SET published_status=? WHERE id=?", (st, rid))
        new.commit()
        new.close()
        print(f"preserved {len(keep)} server publishing decision(s)")
    os.makedirs(os.path.dirname(LIVE), exist_ok=True)
    shutil.move(incoming, LIVE)
    print(f"live DB replaced: {LIVE}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else sys.exit("usage: sync_merge.py <snapshot.db>"))
