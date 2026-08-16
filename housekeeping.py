"""Housekeeping the weekly cycle does on its way past — PARAMETERS.md 9.

Two sweeps of derived files nothing reads between runs: the TED discovery
caches (unresumable the day after their date window passes) and the as-of
scratch worlds the rewind programs rebuild from the store every time. Age is
the safety catch, not a policy — a rewind in progress has fresh files, so a
sweep cannot pull the floor out from under a half-hour run. Never fails a
cycle.
"""
from __future__ import annotations

import shutil
import time


def _prune_scratch_world(paths, max_age_days):
    """Delete stale as-of scratch worlds once nothing in them has been
    touched for `max_age_days`. -> (files, bytes).

    The rewind programs rebuild these directories from the real store on
    every run (`asof.py`) — a filtered copy of the parquet store plus a full
    copy of the embeddings, entirely reconstructible, and nothing reads them
    between runs. At 203.8 MB apiece they were the second largest thing
    under `data/` after the notice archive. Swept per subdirectory: each
    world under `data/asof/` ages on its own clock, so a fresh rewind never
    protects a stale one. The three pre-phase-5 homes are swept by the same
    rule until they stop existing on operator machines.

    Age is the safety catch, not a policy: a rewind in progress has fresh
    files, so a sweep cannot pull the floor out from under a half-hour run.
    """
    asof_root = paths.data / 'asof'
    worlds = ([d for d in asof_root.iterdir() if d.is_dir()]
              if asof_root.exists() else [])
    worlds += [paths.data / n for n in ('backtest_world', 'playback_asof',
                                        'replay_asof')]
    n_total, freed_total = 0, 0
    for world in worlds:
        if not world.exists():
            continue
        files = [f for f in world.rglob('*') if f.is_file()]
        if not files:
            continue
        newest = max(f.stat().st_mtime for f in files)
        if newest >= time.time() - max_age_days * 86400:
            continue
        freed_total += sum(f.stat().st_size for f in files)
        n_total += len(files)
        shutil.rmtree(world, ignore_errors=True)
    return n_total, freed_total


def prune_caches(paths, max_age_days=30):
    """Delete discovery caches older than `max_age_days`.

    The TED search resume cache is keyed by a hash of the query, and a query
    names a date window — so a cache is unresumable the day after its window
    passes. Nothing removed them and the directory reached 1.13 GB across 1,132
    dead scopes, all written within a fortnight. It is not the weekly cycle that
    creates them (bulk.py borrows only helpers from download.py, never
    search_all), but the cycle is the only thing that runs regularly, so it is
    where the sweeping belongs.

    Safe by construction: these are derived files. The notices are in the raw
    archive and the parquet store, and the worst case is re-querying a scope
    that happens to be repeated. Never fails a cycle.
    """
    try:
        import download
        n, freed = download.prune_discovery(max_age_days)
        if n:
            print(f'[prune] {n} stale discovery cache file(s), '
                  f'freed {freed / 1e6:.1f} MB')
        wn, wfreed = _prune_scratch_world(paths, max_age_days)
        if wn:
            print(f'[prune] as-of scratch worlds untouched for '
                  f'{max_age_days}d, freed {wfreed / 1e6:.1f} MB ({wn} files)')
        return n + wn
    except Exception as e:
        print(f'[prune] skipped ({e})')
        return 0

