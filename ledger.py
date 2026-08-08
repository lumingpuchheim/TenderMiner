"""TenderMining ledgers — where the append-only records live.

doc/STORAGE.md phase 2, step 3. Same shape as `subscriptions.py`, one layer
over: callers name a **home directory** and ask for a ledger by name, and this
module decides whether that home's records are in the SQLite database or still
in `<home>/ledger/<name>.jsonl`. A caller never opens either.

    rows = ledger.read(paths.deliveries_home, 'deliveries')
    ledger.append(paths.deliveries_home, 'deliveries', new_rows)

Why a module rather than a few functions in `db.py`: the database is one
storage option, and the fallback-to-file rule, the staleness guard and the
sandbox semantics are decisions about *records*, not about SQL. Keeping them
here means step 4 (predictions, grades) is a call-site change with no new
storage logic, and it means a sandbox — `tryout.py`, `replay.py` — gets the
same rules the real ledgers get instead of its own private ones.

## The staleness guard

A home can hold both a database and the original files, because
`db.py --migrate` deliberately does not delete anything. The database answers
in that case, so a file holding rows the database lacks would be silently
ignored — a customer served from a record that is missing the last cycle. That
is the failure this guard exists to make loud: `read` raises, and the message
names the fix (`python db.py --migrate`).

The comparison is by ROW COUNT, not by content. These are append-only logs, so
"the file has more lines than the table has rows" is exactly the question, and
it costs one `COUNT(*)` plus a line count rather than a full diff of 90,000
rows on every read.
"""

import json
import sys
from pathlib import Path

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')


class LedgerError(RuntimeError):
    """Ledger storage that cannot be trusted to be complete."""


def _spec(name):
    import db
    if name not in db.LEDGERS:
        raise LedgerError(f'unknown ledger {name!r}; '
                          f'known: {", ".join(sorted(db.LEDGERS))}')
    return db.LEDGERS[name]


def file_path(home, name):
    """Where this ledger's text file is (or would be) under `home`."""
    return Path(home) / _spec(name)[0]


def storage(home, name):
    """-> ('db', db_path) | ('jsonl', file_path). Never None: an absent file is
    an empty ledger, not an error, because a fresh deployment has no records
    yet and the first append must be allowed to create it."""
    import db
    p = Path(home)
    if p.suffix in ('.jsonl', '.db', '.sqlite'):
        raise LedgerError(
            f'pass the ledger HOME DIRECTORY, not a storage file ({p.name}). '
            f'Storage belongs to ledger.py; call ledger.read(home, name).')
    if db.path_for(p).exists():
        return ('db', db.path_for(p))
    return ('jsonl', file_path(p, name))


def _read_file(path):
    if not Path(path).exists():
        return []
    return [json.loads(line) for line in
            Path(path).read_text(encoding='utf-8').splitlines() if line.strip()]


def read(home, name):
    """Every row of this ledger, in append order."""
    import db
    kind, path = storage(home, name)
    if kind == 'jsonl':
        return _read_file(path)
    table = _spec(name)[1]
    con = db.connect(home, create=False)
    rows = [json.loads(db.unpack(r['raw'])) for r in
            con.execute(f'SELECT raw FROM {table} ORDER BY seq')]
    _assert_not_stale(con, home, name, table, len(rows))
    return rows


def _assert_not_stale(con, home, name, table, n_db):
    legacy = file_path(home, name)
    if not legacy.exists():
        return
    n_file = sum(1 for line in legacy.read_text(encoding='utf-8').splitlines()
                 if line.strip())
    if n_file > n_db:
        raise LedgerError(
            f'{legacy} has {n_file} rows but the database has {n_db}. The '
            f'database is what gets served, so those {n_file - n_db} row(s) '
            f'would be invisible. Run `python db.py --migrate` to take them '
            f'in (it is idempotent), or remove the stale file.')


def append(home, name, rows):
    """Append rows. Returns the number actually written — a row that collides
    with the table's natural key is skipped, which is how re-running a cycle
    stays idempotent instead of duplicating a customer's record."""
    import db
    rows = list(rows)
    if not rows:
        return 0
    kind, path = storage(home, name)
    if kind == 'jsonl':
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with Path(path).open('a', encoding='utf-8') as f:
            for r in rows:
                f.write(json.dumps(r, default=str) + '\n')
        return len(rows)
    table = _spec(name)[1]
    con = db.connect(home)
    n = sum(db.insert(con, table, r) for r in rows)
    con.commit()
    return n


def start(home):
    """Create empty storage for a sandbox home, matching whatever the real
    ledgers use. tryout.py and replay.py call this so their scratch world is
    the shipped storage rather than a second format only sandboxes know."""
    import db
    db.init(home)
    return Path(home)
