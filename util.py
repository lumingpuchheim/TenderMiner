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
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

# ---------------------------------------------------- the gate override lever
# PARAMETERS.md 10: one candidate gate configuration per PROCESS, declared in
# the environment, so a backplay run measures a knob value without anybody
# editing the constant that holds it.
#
#     TM_GATE_OVERRIDE='{"NOMINATION_BAR": 0.60}' python evidence.py --judge
#
# Keys are the constants' own names, in `evidence.py` or `relevance.py`; each
# module applies the ones it owns and marks them consumed, and a key nobody
# claims RAISES at the first `GateConfig` construction. A silently ignored
# override is the failure mode this project already refuses for subscription
# fields (CLAUDE.md), and it would be worse here: the run would look like a
# measurement of the candidate and be a measurement of the champion.
#
# The override flows into `evidence.rules()` and therefore into the gate
# fingerprint, so a backplay run stamps itself as its own configuration
# without a line of extra bookkeeping.
_OVERRIDE_ENV = 'TM_GATE_OVERRIDE'
_CONSUMED = set()


def gate_override():
    """The parsed override, {} when unset. Parsed on every call — a test may
    change the environment between them, and the cost is a small json.loads."""
    raw = os.environ.get(_OVERRIDE_ENV, '').strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError as e:
        raise SystemExit(f'{_OVERRIDE_ENV} is not valid JSON: {e}')
    if not isinstance(value, dict):
        raise SystemExit(f'{_OVERRIDE_ENV} must be a JSON object, got {type(value).__name__}')
    return value


def consume_override(name):
    _CONSUMED.add(name)


def apply_override(namespace):
    """Apply the override to a module's globals, claiming the names it owns.
    Only existing UPPER_CASE constants may be overridden: a typo is a new
    global otherwise, which is exactly the silent miss this guards."""
    applied = {}
    for key, value in gate_override().items():
        if key.isupper() and key in namespace:
            namespace[key] = value
            consume_override(key)
            applied[key] = value
    return applied


# The modules that claim override keys. The unconsumed check imports them
# first (only when an override is set), so import ORDER cannot make a valid
# key look unclaimed: relevance is often imported before single_bidder.
OVERRIDE_OWNERS = ('evidence', 'relevance', 'single_bidder')


def unconsumed_override():
    keys = set(gate_override())
    if keys:
        import importlib
        for name in OVERRIDE_OWNERS:
            try:
                importlib.import_module(name)
            except ImportError:      # the app image lacks the trainer's deps; fine
                pass
    return sorted(keys - _CONSUMED)


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

