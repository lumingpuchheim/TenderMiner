"""Small shared helpers and the on-disk locations — PARAMETERS.md 9.

The bottom of the cycle: window parsing, the clock, JSON/JSONL readers and
writers, the NaN stamp, and `Paths`. Nothing here knows what a lot or a model
is, which is why every other cycle module may import it and it imports none
of them.

**The clock is a module attribute on purpose.** `rewind_report.freeze_clock`
replaces `util.now_utc` so a replayed Monday renders its own date; that works
only while callers say `util.now_utc()` rather than binding the function into
their own namespace with `from util import now_utc`. Before the split the
same trick patched `loop.now_utc` and reached only loop.py's own callers.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd


def parse_window(spec):
    """'7d' / '2w' / '3m' -> timedelta (months approximated as 30 days)."""
    m = re.fullmatch(r'(\d+)([dwm])', spec.strip().lower())
    if not m:
        raise SystemExit(f"--last '{spec}' is not of the form 7d / 2w / 3m")
    n, unit = int(m.group(1)), m.group(2)
    return timedelta(days=n * {'d': 1, 'w': 7, 'm': 30}[unit])


def now_utc():
    return datetime.now(timezone.utc)


def read_json(path, default):
    p = Path(path)
    if not p.exists():
        return default
    return json.loads(p.read_text(encoding='utf-8'))


def write_json(path, obj):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, indent=2, default=str), encoding='utf-8')


def read_jsonl(path):
    p = Path(path)
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding='utf-8').splitlines() if line.strip()]


def stamp(v):
    """NaN/NaT -> None so ledger rows carry JSON null, never 'nan' strings."""
    try:
        return None if pd.isna(v) else v
    except (TypeError, ValueError):
        return v


def append_jsonl(path, rows):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open('a', encoding='utf-8') as f:
        for r in rows:
            f.write(json.dumps(r, default=str) + '\n')


class Paths:
    """All on-disk locations, rooted at --data-dir / --models-dir (parameters,
    like every window in this program)."""

    def __init__(self, data_dir, models_dir):
        self.data = Path(data_dir)
        self.xml = self.data / 'raw' / 'xml'
        self.store_tenders = self.data / 'store' / 'tenders.parquet'
        self.store_awards = self.data / 'store' / 'awards.parquet'
        # the HOME whose storage holds the cycle's own record — predictions
        # and grades. A directory, not two files (ledger.py owns the format);
        # rewind_report.py points it at its as-of sandbox.
        self.ledger_home = self.data
        # the HOME whose storage holds the delivery record and the gate-config
        # registry — a directory, not a file (ledger.py owns the format).
        # tryout/replay point this at a sandbox.
        self.deliveries_home = self.data
        # the DIRECTORY subscriptions live in, not the file: the storage
        # format belongs to subscriptions.py (tryout/replay point this at a
        # sandbox dir instead)
        self.subs_home = self.data
        self.checkpoint = self.data / 'logs' / 'loop_checkpoint.json'
        self.drift = self.data / 'logs' / 'drift_latest.json'
        self.reports = self.data / 'reports'
        self.models = Path(models_dir)
        self.registry = self.models / 'registry.jsonl'
        self.current = self.models / 'CURRENT'

