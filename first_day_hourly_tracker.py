#!/usr/bin/env python3
"""
Record hourly snapshots for projects in their first 24 crowdfunding hours.

This script is designed to live beside daily_growth_tracker.py on the server.
Cron can run it once per hour. It optionally asks the existing daily tracker to
refresh data, then copies only projects whose start time is within 24 hours into
data/first_day_hourly/YYYY-MM-DD.jsonl.
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Optional


BASE_DIR = Path(__file__).resolve().parent
RECORD_DIRS = [
    BASE_DIR / "daily_records",
    BASE_DIR / "records",
    BASE_DIR / "snapshots",
]
OUT_DIR = BASE_DIR / "first_day_hourly"
LOG_FILE = BASE_DIR / "first_day_hourly.log"


def log(message: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {message}"
    print(line)
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def parse_time(value: str) -> Optional[datetime]:
    value = (value or "").strip()
    if not value:
        return None
    for fmt in (
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(value[: len(fmt)], fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def pick(row: Dict[str, str], names: Iterable[str]) -> str:
    for name in names:
        if row.get(name):
            return row[name]
    return ""


def row_start_time(row: Dict[str, str]) -> Optional[datetime]:
    return parse_time(
        pick(
            row,
            (
                "start_time",
                "start_at",
                "launch_time",
                "launch_at",
                "sd",
                "start_date",
                "started_at",
            ),
        )
    )


def row_identity(row: Dict[str, str]) -> str:
    return (
        pick(row, ("project_id", "id", "url", "link", "l"))
        or pick(row, ("name", "project_name", "title", "n"))
        or json.dumps(row, sort_keys=True, ensure_ascii=False)
    )


def latest_csv_files() -> List[Path]:
    files: List[Path] = []
    for directory in RECORD_DIRS:
        if directory.exists():
            files.extend(directory.glob("*.csv"))
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)[:12]


def read_rows() -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    for path in latest_csv_files():
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    row["_source_file"] = str(path)
                    rows.append(row)
        except OSError as exc:
            log(f"skip unreadable csv {path}: {exc}")
    return rows


def first_day_rows(rows: Iterable[Dict[str, str]], now: datetime) -> List[Dict[str, str]]:
    result: List[Dict[str, str]] = []
    seen = set()
    for row in rows:
        start = row_start_time(row)
        if not start or start > now or now - start > timedelta(hours=24):
            continue
        identity = row_identity(row)
        if identity in seen:
            continue
        seen.add(identity)
        result.append(row)
    return result


def snapshot_record(row: Dict[str, str], now: datetime) -> Dict[str, str]:
    return {
        "captured_at": now.isoformat(timespec="seconds"),
        "captured_hour": now.strftime("%Y-%m-%d %H:00"),
        "project_id": pick(row, ("project_id", "id")),
        "name": pick(row, ("name", "project_name", "title", "n")),
        "url": pick(row, ("url", "link", "l")),
        "platform": pick(row, ("platform", "source", "pl")),
        "start_time": pick(row, ("start_time", "start_at", "launch_time", "launch_at", "sd", "start_date", "started_at")),
        "amount": pick(row, ("amount", "pledged", "raised", "funding_amount", "money")),
        "backers": pick(row, ("backers", "supporters", "people", "backer_count")),
        "followers": pick(row, ("followers", "watchers", "reservations", "appointments")),
        "progress": pick(row, ("progress", "percent", "pg")),
        "source_file": row.get("_source_file", ""),
    }


def existing_keys(path: Path) -> set:
    keys = set()
    if not path.exists():
        return keys
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                item = json.loads(line)
                keys.add((item.get("captured_hour"), item.get("project_id") or item.get("url") or item.get("name")))
    except (OSError, json.JSONDecodeError):
        return keys
    return keys


def write_snapshots(records: List[Dict[str, str]], now: datetime) -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{now:%Y-%m-%d}.jsonl"
    keys = existing_keys(path)
    written = 0
    with path.open("a", encoding="utf-8") as f:
        for record in records:
            key = (record["captured_hour"], record["project_id"] or record["url"] or record["name"])
            if key in keys:
                continue
            f.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            written += 1
    return written


def refresh_daily_tracker() -> None:
    tracker = BASE_DIR / "daily_growth_tracker.py"
    if not tracker.exists():
        log("daily_growth_tracker.py not found; record from existing csv only")
        return
    commands = [
        [sys.executable, str(tracker), "--mode", "all", "--auto-gf"],
        [sys.executable, str(tracker), "--mode", "all"],
        [sys.executable, str(tracker)],
    ]
    for cmd in commands:
        try:
            subprocess.run(cmd, cwd=str(BASE_DIR), check=True, timeout=1800)
            log("daily tracker refreshed")
            return
        except Exception as exc:
            log(f"daily tracker command failed: {' '.join(cmd)} ({exc})")
    log("daily tracker refresh failed; record from existing csv only")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fetch", action="store_true", help="run daily_growth_tracker.py before recording")
    parser.add_argument("--dry-run", action="store_true", help="print summary without writing jsonl")
    args = parser.parse_args()

    now = datetime.now().replace(minute=0, second=0, microsecond=0)
    if args.fetch:
        refresh_daily_tracker()
    rows = first_day_rows(read_rows(), now)
    records = [snapshot_record(row, now) for row in rows]
    if args.dry_run:
        log(f"dry run: {len(records)} first-day project rows")
        return 0
    written = write_snapshots(records, now)
    log(f"first-day hourly snapshot complete: {written} new rows, {len(records)} matched")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
