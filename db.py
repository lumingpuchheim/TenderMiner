"""TenderMining storage — the SQLite database behind the ledgers.

doc/STORAGE.md phase 2, step 1. This module is a pure addition: it defines the
schema, migrates the JSONL ledgers into it, exports them back out, and proves
the round trip. **Nothing reads it yet** — the switch-over is steps 2-4, one
ledger family at a time, so a cycle can never be broken by this file alone.

## Why a database at all

The ledgers are what the system *decided and promised*: a prediction, a
delivery to a named customer, a grade, a subscription version. They are read
per customer, joined to each other, and must never be silently edited. Three
things follow that a file cannot give:

  * **append-only stops depending on discipline** — a trigger rejects UPDATE
    and DELETE on every ledger table, so "the record is frozen" is enforced by
    the storage rather than remembered by people;
  * **PII becomes separable** — `customer` holds identity (mutable, deletable
    on request), `subscription_version` holds the market filter (append-only).
    In the JSONL those were one object, which made an erasure request a
    contradiction;
  * **the 51 MB double parse disappears** — `predictions.jsonl` is read whole
    twice per cycle today (the dedup set in `predict_open`, the receipt
    fallback in `deliver`). Both become indexed lookups, and the dedup key
    becomes a real UNIQUE constraint instead of a set built in memory.

## The `raw` column, and why every table has one

Each row keeps the exact JSON text it had as a ledger line, alongside the
typed columns used for querying. That is deliberate redundancy and it buys the
one property this migration must have: **export is byte-faithful**, so
`--export-jsonl` reproduces the frozen originals and `--verify` can prove it
by comparison rather than by assertion. It also absorbs the ledgers'
heterogeneity — rows written months apart have different keys (early
prediction rows have no `cpv_main`, early delivery rows no `gate_config`) and
a typed-only schema would silently invent values for the missing ones.

Reading is always from the typed columns. `raw` is for export and audit.

Usage:
    python db.py --init                 # create an empty database
    python db.py --migrate              # JSONL -> database (keeps originals)
    python db.py --verify               # export and diff against the originals
    python db.py --export-jsonl DIR     # regenerate the text ledgers
    python db.py --status               # what is in there
"""

import argparse
import json
import sqlite3
import sys
import zlib
from pathlib import Path

import config

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

DB_NAME = 'tendermining.db'
SCHEMA_VERSION = 3   # 2: explicit `seq`; 3: customer_id is a soft reference

# ---------------------------------------------------------------- the schema

# Ledger tables are append-only, enforced below. `raw` is the verbatim JSON
# line (see the module docstring). Column types are advisory in SQLite; they
# document intent and drive the typed reads.
#
# Every ledger carries `seq`: the append position, explicit rather than implied.
# SQLite would give it for free as `rowid`, and the first version of this file
# used `ORDER BY rowid` to reproduce a ledger in the order it was written — but
# `rowid` does not exist in Postgres, so that one shortcut would have had to be
# unpicked from every read at the moment the engine changed. It is a column now
# (doc/STORAGE.md 6.2), assigned as MAX(seq)+1 within the table, which is SQL
# both engines accept. Correct under one writer, which is what this system is;
# a Postgres deployment would hand the job to a real sequence.
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Identity, contact, billing, consent. MUTABLE and DELETABLE on request:
-- this is the only table holding personal data, so an erasure request is one
-- row rather than an edit to an append-only record.
CREATE TABLE IF NOT EXISTS customer (
    customer_id  TEXT PRIMARY KEY,
    name         TEXT,
    award_names  TEXT,             -- JSON array: winner_names spellings
    contact_email TEXT,
    contact_note TEXT,
    billing_note TEXT,
    consent_at   TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT
);

-- The market filter, versioned and never edited (doc/SUBSCRIPTIONS.md).
CREATE TABLE IF NOT EXISTS subscription_version (
    sub_id            TEXT NOT NULL,
    version           INTEGER NOT NULL,
    -- A SOFT reference, deliberately not a FOREIGN KEY (schema 3). With the
    -- constraint on, `DELETE FROM customer` failed outright, which made the
    -- erasure this table's whole design was for impossible; and ON DELETE SET
    -- NULL cannot help, because setting it would be an UPDATE on an
    -- append-only table. So the column is a plain pointer: erasing a customer
    -- leaves their market history intact with a customer_id that resolves to
    -- nothing, which is the correct outcome -- the person is gone, the
    -- business record remains. A test asserts the DELETE succeeds.
    customer_id       TEXT,
    effective_from    TEXT,
    active            INTEGER NOT NULL DEFAULT 1,
    cpv_prefixes      TEXT,        -- JSON array
    nuts_prefixes     TEXT,        -- JSON array
    min_deadline_days INTEGER,
    max_picks         INTEGER,
    profile_refs      TEXT,        -- JSON array
    profile_texts     TEXT,        -- JSON array
    min_relevance     REAL,
    min_code_hard     REAL,
    min_code_soft     REAL,
    seq               INTEGER NOT NULL,
    raw               BLOB NOT NULL,
    PRIMARY KEY (sub_id, version)
);
CREATE INDEX IF NOT EXISTS ix_subver_seq ON subscription_version (seq);
CREATE INDEX IF NOT EXISTS ix_subver_asof
    ON subscription_version (sub_id, effective_from, version);

-- One row per (lot revision, model). The UNIQUE key IS the dedup rule
-- predict_open builds in memory today, so re-scoring becomes INSERT OR IGNORE.
CREATE TABLE IF NOT EXISTS prediction (
    ts                 TEXT NOT NULL,
    model              TEXT NOT NULL,
    procedure_id       TEXT NOT NULL,
    lot_id             TEXT NOT NULL,
    notice_id          TEXT,
    publication_date   TEXT,
    deadline_date      TEXT,
    score              REAL,
    threshold          REAL,
    flag               INTEGER,
    tier               TEXT,
    cpv3               TEXT,
    cpv_main           TEXT,
    place_nuts3        TEXT,
    publication_number TEXT,
    buyer_name         TEXT,
    est_value_lot      REAL,
    title              TEXT,
    seq                INTEGER NOT NULL,
    raw                BLOB NOT NULL,
    UNIQUE (procedure_id, lot_id, notice_id, model)
);
CREATE INDEX IF NOT EXISTS ix_pred_seq  ON prediction (seq);
CREATE INDEX IF NOT EXISTS ix_pred_lot   ON prediction (procedure_id, lot_id);
CREATE INDEX IF NOT EXISTS ix_pred_model ON prediction (model);

CREATE TABLE IF NOT EXISTS grade (
    graded_at                 TEXT NOT NULL,
    procedure_id              TEXT NOT NULL,
    lot_id                    TEXT NOT NULL,
    label                     INTEGER,
    n_tenders                 INTEGER,
    award_pub                 TEXT,
    award_publication_number  TEXT,
    cpv3                      TEXT,
    place_nuts3               TEXT,
    score                     REAL,
    tier                      TEXT,
    flag                      INTEGER,
    correct                   INTEGER,
    model                     TEXT,
    seq                       INTEGER NOT NULL,
    raw                       BLOB NOT NULL,
    PRIMARY KEY (procedure_id, lot_id)
);
CREATE INDEX IF NOT EXISTS ix_grade_seq ON grade (seq);
CREATE INDEX IF NOT EXISTS ix_grade_pub ON grade (award_pub);

-- What each customer was actually shown, on which day, under which rules.
CREATE TABLE IF NOT EXISTS delivery (
    ts                 TEXT NOT NULL,
    sub_id             TEXT NOT NULL,
    sub_version        INTEGER,
    procedure_id       TEXT NOT NULL,
    lot_id             TEXT NOT NULL,
    notice_id          TEXT,
    model              TEXT,
    score              REAL,
    slice_rank         INTEGER,
    slice_size         INTEGER,
    slice_tier         TEXT,
    publication_number TEXT,
    buyer_name         TEXT,
    title              TEXT,
    kind               TEXT NOT NULL DEFAULT 'pick',
    relevance_score    REAL,
    code_relevance     REAL,
    profile_version    INTEGER,
    embed_model_tag    TEXT,
    gate_config        TEXT,
    gate_mode          TEXT,
    seq                INTEGER NOT NULL,
    raw                BLOB NOT NULL,
    UNIQUE (sub_id, procedure_id, lot_id, ts, kind)
);
CREATE INDEX IF NOT EXISTS ix_deliv_seq ON delivery (seq);
CREATE INDEX IF NOT EXISTS ix_deliv_sub ON delivery (sub_id, ts);
CREATE INDEX IF NOT EXISTS ix_deliv_lot ON delivery (procedure_id, lot_id);

-- A customer's own wins, learned as profile references (doc/RELEVANCE.md 9).
CREATE TABLE IF NOT EXISTS learned_ref (
    sub_id                   TEXT NOT NULL,
    pub                      TEXT NOT NULL,
    learned_at               TEXT NOT NULL,
    source                   TEXT,
    gate_verdict             TEXT,
    procedure_id             TEXT,
    lot_id                   TEXT,
    award_publication_date   TEXT,
    note                     TEXT,
    seq                      INTEGER NOT NULL,
    raw                      BLOB NOT NULL,
    PRIMARY KEY (sub_id, pub)
);
CREATE INDEX IF NOT EXISTS ix_learned_seq ON learned_ref (seq);
CREATE INDEX IF NOT EXISTS ix_learned_asof ON learned_ref (sub_id, learned_at);

-- Resolves the gate_config fingerprint stamped on delivery rows.
-- Market-scale simulated picks: what the system WOULD have recommended to
-- every winner company in the store, joined against outcomes by
-- `simulation.py check`. A record of decisions like any other, and the last
-- ledger to get a table (doc/STORAGE.md 5.4) -- left outside for a while
-- because nothing ever queried it by key, which is also why it could not be
-- joined to anything else.
CREATE TABLE IF NOT EXISTS simulation (
    ts                 TEXT NOT NULL,
    company            TEXT NOT NULL,
    procedure_id       TEXT NOT NULL,
    lot_id             TEXT NOT NULL,
    notice_id          TEXT,
    model              TEXT,
    score              REAL,
    cpv3               TEXT,
    place_nuts3        TEXT,
    publication_number TEXT,
    deadline_date      TEXT,
    seq                INTEGER NOT NULL,
    raw                BLOB NOT NULL,
    UNIQUE (company, procedure_id, lot_id)
);
CREATE INDEX IF NOT EXISTS ix_sim_seq     ON simulation (seq);
CREATE INDEX IF NOT EXISTS ix_sim_company ON simulation (company);
CREATE INDEX IF NOT EXISTS ix_sim_lot     ON simulation (procedure_id, lot_id);

-- SIMULATION.md "the gate rides along" (2026-08-10): one relevance-gate
-- verdict per simulated pick, same natural key as `simulation`. A separate
-- table, not new columns: `simulation` rows are append-only history and the
-- verdict arrives on its own schedule (the backlog pass judges picks written
-- before the gate rode along).
CREATE TABLE IF NOT EXISTS simulation_gate (
    ts           TEXT NOT NULL,
    company      TEXT NOT NULL,
    procedure_id TEXT NOT NULL,
    lot_id       TEXT NOT NULL,
    verdict      TEXT NOT NULL,
    gate_pass    INTEGER,
    text         REAL,
    code         REAL,
    why          TEXT,
    seq          INTEGER NOT NULL,
    raw          BLOB NOT NULL,
    UNIQUE (company, procedure_id, lot_id)
);
CREATE INDEX IF NOT EXISTS ix_simg_seq     ON simulation_gate (seq);
CREATE INDEX IF NOT EXISTS ix_simg_company ON simulation_gate (company);

-- SIMULATION.md "the gate rides along" (2026-08-10): the verdict x outcome
-- join, snapshotted once per cycle. The join itself is a computation over
-- `simulation`, `simulation_gate` and `grade` — this table is its TIME SERIES,
-- so "did the gate's admissions pull ahead of its rejections, and when" is a
-- query rather than a diff of two rendered dashboards. One row per
-- (cycle ts, verdict); a cycle that finds nothing gradeable still writes its
-- zero row, because "nothing was gradeable on that day" is the record.
CREATE TABLE IF NOT EXISTS gate_outcome (
    ts             TEXT NOT NULL,
    verdict        TEXT NOT NULL,
    graded         INTEGER,
    lonely_rate    REAL,
    own_wins       INTEGER,
    own_rate       REAL,
    picks_total    INTEGER,
    verdicts_total INTEGER,
    seq            INTEGER NOT NULL,
    raw            BLOB NOT NULL,
    UNIQUE (ts, verdict)
);
CREATE INDEX IF NOT EXISTS ix_gout_seq     ON gate_outcome (seq);
CREATE INDEX IF NOT EXISTS ix_gout_verdict ON gate_outcome (verdict, ts);

CREATE TABLE IF NOT EXISTS gate_config (
    fingerprint TEXT PRIMARY KEY,
    first_seen  TEXT NOT NULL,
    mode        TEXT,
    seq         INTEGER NOT NULL,
    raw         BLOB NOT NULL
);

-- Capability tokens (doc/APP.md 3). Owned by tokens.py; no other module reads
-- or writes it. Deliberately NOT in LEDGER_TABLES: a token is mutable state,
-- not a frozen record — revocation and first use are stamped in place, and
-- revocation that could not take effect immediately would not be revocation.
CREATE TABLE IF NOT EXISTS token (
    token        TEXT PRIMARY KEY,
    purpose      TEXT NOT NULL,      -- t signup | f feedback | s stop | c recall
    sub_id       TEXT NOT NULL,      -- the subject; for `t`, the target-list firm
    procedure_id TEXT,               -- `f` only: the lot the verdict is about
    lot_id       TEXT,               -- `f` only
    verdict      TEXT,               -- `f` only: never in the URL, always here
    created_at   TEXT NOT NULL,
    revoked_at   TEXT,
    used_at      TEXT
);
CREATE INDEX IF NOT EXISTS ix_token_subject ON token (sub_id, purpose);
"""

# Append-only enforcement. Ledgers are frozen records; `customer` is
# deliberately excluded because personal data must stay correctable and
# erasable.
LEDGER_TABLES = ('subscription_version', 'prediction', 'grade', 'delivery',
                 'learned_ref', 'gate_config', 'simulation')

# The one deliberate SQLite-specific construct left (doc/STORAGE.md 6.2). The
# RULE — a ledger row is never updated or deleted — is portable; only this
# spelling is not. Postgres expresses it with a trigger function raising an
# exception, which is a rewrite of these six lines and nothing else, because no
# other code depends on how the refusal is produced.
#
# NB SQLite has no implicit string concatenation: a RAISE message must be a
# single literal.
TRIGGERS = ''.join(f"""
CREATE TRIGGER IF NOT EXISTS {t}_no_update BEFORE UPDATE ON {t}
BEGIN SELECT RAISE(ABORT, '{t} is append-only: write a new row, never edit one'); END;
CREATE TRIGGER IF NOT EXISTS {t}_no_delete BEFORE DELETE ON {t}
BEGIN SELECT RAISE(ABORT, '{t} is append-only: rows are a frozen record'); END;
""" for t in LEDGER_TABLES)


def pack(line):
    """The verbatim ledger line, zlib-compressed.

    The `raw` column duplicates the typed columns, which is what makes export
    byte-faithful — but paid in full it doubled the database (107 MB against
    51 MB of source text, measured). German notice prose compresses hard, so
    storing it packed keeps the audit fidelity and makes the database SMALLER
    than the text it replaces. Decompression is paid only by --export and
    --verify, never by a query.
    """
    return zlib.compress(line.encode('utf-8'), 6)


def unpack(blob):
    if isinstance(blob, str):       # tolerate a pre-compression database
        return blob
    return zlib.decompress(blob).decode('utf-8')


def path_for(data_dir):
    return Path(data_dir) / DB_NAME


def connect(data_dir, create=True):
    """A connection with WAL, foreign keys and Row access.

    WAL so a reader (a dashboard render, an operator query) never blocks the
    cycle's writes — the single-writer assumption the JSONL ledgers made is
    exactly what breaks first when a signup form or a reply handler arrives.
    """
    p = path_for(data_dir)
    if not create and not p.exists():
        return None
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(p)
    con.row_factory = sqlite3.Row
    # SQLite-only tuning, deliberately confined to this function: WAL so a
    # reader never blocks the cycle's writes. A different engine configures
    # itself elsewhere and needs none of this.
    con.execute('PRAGMA journal_mode=WAL')
    con.execute('PRAGMA foreign_keys=ON')
    con.execute('PRAGMA synchronous=NORMAL')
    _assert_schema_current(con, p)
    # Additive self-heal (2026-08-10, the simulation_gate table): SCHEMA and
    # TRIGGERS are IF-NOT-EXISTS throughout, so a table added by newer code
    # materialises on first touch without a migration. An older-version
    # database was already refused above, so this can only ADD objects —
    # it can never reinterpret existing rows.
    con.executescript(SCHEMA)
    con.executescript(TRIGGERS)
    return con


def _assert_schema_current(con, path):
    """Refuse a database written by an older schema.

    Silently reading one would be the worst outcome: schema 1 had no `seq`
    column, so every export would come back in an undefined order and look
    plausible. The remedy is cheap while the JSONL originals exist — delete and
    re-migrate — so the message says exactly that.
    """
    try:
        row = con.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()
    except sqlite3.OperationalError:
        return                      # brand new file; init() is about to fill it
    if row is None:
        return
    found = int(row['value'])
    if found < SCHEMA_VERSION:
        raise RuntimeError(
            f'{path} was written by schema {found}, this code expects '
            f'{SCHEMA_VERSION}. Delete it and run `python db.py --migrate` '
            f'again — the JSONL originals are still there, migration is '
            f'idempotent, and --verify will prove the result.')


def init(data_dir):
    con = connect(data_dir)
    con.executescript(SCHEMA)
    con.executescript(TRIGGERS)
    con.execute(f'{IGNORE_PREFIX} meta(key, value) VALUES (?, ?)',
                ('schema_version', str(SCHEMA_VERSION)))
    con.commit()
    return con


# ------------------------------------------------------------- ledger mapping

# ledger name -> (source path relative to data dir, table, key columns)
# The column list per table is read from the schema at runtime, so adding a
# column never means updating a second list here.
LEDGERS = {
    'subscriptions': ('subscriptions.jsonl', 'subscription_version'),
    'predictions': ('ledger/predictions.jsonl', 'prediction'),
    'grades': ('ledger/grades.jsonl', 'grade'),
    'deliveries': ('ledger/deliveries.jsonl', 'delivery'),
    'learned_refs': ('ledger/learned_refs.jsonl', 'learned_ref'),
    'gate_configs': ('ledger/gate_configs.jsonl', 'gate_config'),
    'simulations': ('ledger/simulations.jsonl', 'simulation'),
    'simulations_gate': ('ledger/simulations_gate.jsonl', 'simulation_gate'),
    'gate_outcomes': ('ledger/gate_outcomes.jsonl', 'gate_outcome'),
}

# Fields that are JSON arrays in the ledger and stay JSON text in a column.
JSON_COLUMNS = {'cpv_prefixes', 'nuts_prefixes', 'profile_refs',
                'profile_texts', 'award_names'}


def columns_of(con, table):
    """The table's columns, via the DB-API cursor description rather than
    `PRAGMA table_info` — the pragma is SQLite-only, while an empty SELECT and
    `cursor.description` work on every DB-API driver."""
    cur = con.execute(f'SELECT * FROM {table} LIMIT 0')
    return [d[0] for d in cur.description]


def _cell(value):
    """A ledger value as a SQLite-storable scalar."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return value


def row_values(con, table, row, raw_line=None, extra=None):
    """Map a ledger dict onto the table's columns. Unknown ledger keys are not
    dropped — they survive verbatim in `raw`, which is what export reads.

    `extra` supplies columns the ledger row does not carry (the derived
    `customer_id`). It must be set at INSERT time: the append-only trigger
    rejects a later UPDATE, which is the trigger working as intended — it
    caught this migration's first draft trying to backfill.
    """
    out = {}
    for c in columns_of(con, table):
        if c == 'raw':
            out[c] = pack(raw_line if raw_line is not None
                          else json.dumps(row, ensure_ascii=False, default=str))
        elif extra and c in extra:
            out[c] = _cell(extra[c])
        elif c in row:
            out[c] = _cell(row[c])
        # A column the row does not carry is OMITTED, not set to NULL, so the
        # schema's DEFAULT applies. The ledgers are heterogeneous across time —
        # delivery rows written before `kind` existed have no `kind` — and
        # inserting an explicit NULL into `kind TEXT NOT NULL DEFAULT 'pick'`
        # made OR IGNORE drop 12 real rows silently. --verify caught it; this
        # is why the round-trip check exists.
    return out


# `INSERT OR IGNORE` is SQLite/MySQL; Postgres spells it `INSERT ... ON CONFLICT
# DO NOTHING`. Both mean "a row that collides with the natural key is not an
# error", which is what makes re-running a cycle idempotent. Kept in one place so
# the difference is a constant rather than a search.
IGNORE_PREFIX = 'INSERT OR IGNORE INTO'
INSERT_PREFIX = 'INSERT INTO'


def insert(con, table, row, raw_line=None, ignore=True, extra=None):
    """Append one row, assigning its `seq`. Returns 1, or 0 when the natural key
    already holds it."""
    vals = row_values(con, table, row, raw_line, extra)
    vals.pop('seq', None)
    cols = list(vals)
    head = IGNORE_PREFIX if ignore else INSERT_PREFIX
    # seq is computed inside the statement so it cannot race with itself between
    # a SELECT and an INSERT
    sql = (f'{head} {table} ({", ".join(cols)}, seq) '
           f'VALUES ({", ".join("?" * len(cols))}, '
           f'(SELECT COALESCE(MAX(seq), 0) + 1 FROM {table}))')
    return con.execute(sql, [vals[c] for c in cols]).rowcount


def read_jsonl_lines(path):
    """(row, verbatim line) pairs. The line is kept so export can reproduce
    the original byte-for-byte."""
    p = Path(path)
    if not p.exists():
        return []
    out = []
    for i, line in enumerate(p.read_text(encoding='utf-8').splitlines(), 1):
        if not line.strip():
            continue
        try:
            out.append((json.loads(line), line))
        except json.JSONDecodeError as e:
            raise ValueError(f'{p}:{i}: not valid JSON ({e})') from e
    return out


# ------------------------------------------------------------------ migration

def migrate(data_dir, dry_run=False):
    """JSONL -> database. Idempotent: every insert is OR IGNORE on the natural
    key, so re-running adds only what is missing. Originals are NOT touched
    here — freezing them is a separate, explicit step (`--freeze`), so a
    half-finished migration never costs data."""
    con = init(data_dir)
    data = Path(data_dir)
    report = {}
    # customers first: a subscription_version row carries customer_id, and the
    # append-only trigger (correctly) forbids adding it afterwards
    sub_rel, sub_table = LEDGERS['subscriptions']
    sub_pairs = read_jsonl_lines(data / sub_rel)
    n_cust = _derive_customers(con, [r for r, _ in sub_pairs])
    for name, (rel, table) in LEDGERS.items():
        pairs = sub_pairs if name == 'subscriptions' else read_jsonl_lines(data / rel)
        if not pairs:
            report[name] = (0, 0, 'no source file')
            continue
        n = 0
        for row, line in pairs:
            extra = ({'customer_id': row.get('sub_id')}
                     if table == 'subscription_version' else None)
            n += insert(con, table, row, raw_line=line, extra=extra)
        report[name] = (len(pairs), n, table)
    if dry_run:
        con.rollback()
    else:
        con.commit()
    return report, n_cust


def _derive_customers(con, sub_rows):
    """One customer per sub_id, from the subscription versions.

    The JSONL carried `name`/`award_names` on every version, which implied a
    rename could be a market change. It cannot: identity belongs to the
    person, so it moves to `customer`, the newest spelling wins, and
    `created_at` is the earliest `effective_from` we ever saw for them.
    """
    newest, first_seen = {}, {}
    for d in sub_rows:
        sub_id = d.get('sub_id')
        if not sub_id:
            continue
        v = int(d.get('version') or 1)
        if sub_id not in newest or v >= newest[sub_id][0]:
            newest[sub_id] = (v, d)
        eff = str(d.get('effective_from') or '')
        if eff and (sub_id not in first_seen or eff < first_seen[sub_id]):
            first_seen[sub_id] = eff
    for sub_id, (_, d) in newest.items():
        award = d.get('award_names')
        con.execute(
            f'{IGNORE_PREFIX} customer'
            f' (customer_id, name, award_names, created_at) VALUES (?,?,?,?)',
            (sub_id, d.get('name'),
             json.dumps(award, ensure_ascii=False) if award else None,
             first_seen.get(sub_id, '')))
    return len(newest)


# --------------------------------------------------- subscriptions (phase 2/2)

def subscription_rows(con):
    """Subscription versions as the dicts every caller already expects.

    The market filter comes back from `raw` verbatim, so a caller cannot tell
    the difference between a database row and the ledger line it was migrated
    from. Identity — `name`, `award_names` — is overlaid from `customer`
    instead, because that is the whole point of the split: the filter is a
    frozen decision, the person's name is a correctable fact. Rename a customer
    and every past version reports the new name; their historical market stays
    exactly as it was.
    """
    cust = {r['customer_id']: r for r in con.execute('SELECT * FROM customer')}
    out = []
    for r in con.execute('SELECT sub_id, version, customer_id, raw '
                         'FROM subscription_version ORDER BY seq'):
        d = json.loads(unpack(r['raw']))
        c = cust.get(r['customer_id'] or r['sub_id'])
        if c is not None:
            if c['name'] is not None:
                d['name'] = c['name']
            if c['award_names']:
                d['award_names'] = json.loads(c['award_names'])
        out.append(d)
    return out


def put_subscriptions(data_dir, subs):
    """Create/extend subscription storage from validated dicts.

    Used by the sandbox path (`subscriptions.write_sandbox`), so a throwaway
    customer set exercises the same storage the real one uses instead of a
    second format that only sandboxes know about.
    """
    con = init(data_dir)
    _derive_customers(con, subs)
    for s in subs:
        insert(con, 'subscription_version', s,
               extra={'customer_id': s.get('sub_id')})
    con.commit()
    return len(subs)


def subscription_versions_present(con):
    """{(sub_id, version)} already stored — lets a caller detect a source file
    that has drifted ahead of the database."""
    return {(r['sub_id'], int(r['version'])) for r in
            con.execute('SELECT sub_id, version FROM subscription_version')}


def export_jsonl(data_dir, out_dir):
    """Regenerate the text ledgers from the database.

    This is what keeps the promise that an auditor sees exactly what we see —
    and what makes `cat`, `grep` and `git diff` work again after the ledgers
    become a binary file. Rows come out in append order (the explicit `seq`
    column, not SQLite's rowid — see the schema comment).
    """
    con = connect(data_dir, create=False)
    if con is None:
        raise SystemExit(f'[db] no database at {path_for(data_dir)}')
    out = Path(out_dir)
    written = {}
    for name, (rel, table) in LEDGERS.items():
        dest = out / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        lines = [unpack(r['raw']) for r in
                 con.execute(f'SELECT raw FROM {table} ORDER BY seq')]
        dest.write_text(''.join(l + '\n' for l in lines), encoding='utf-8')
        written[name] = len(lines)
    return written


def stale_tables(data_dir):
    """[(ledger, rows_in_db, rows_in_file)] for tables whose SOURCE FILE is
    ahead — records the database has not taken in.

    Not an error by itself: a ledger nobody reads from the database yet is
    allowed to lag. It matters when something reports on the database's contents,
    because a report on a table that is behind must say so rather than look
    current.
    """
    con = connect(data_dir, create=False)
    if con is None:
        return []
    out = []
    for name, (rel, table) in LEDGERS.items():
        f = Path(data_dir) / rel
        if not f.exists():
            continue
        n_file = sum(1 for line in f.read_text(encoding='utf-8').splitlines()
                     if line.strip())
        n_db = con.execute(f'SELECT COUNT(*) AS n FROM {table}').fetchone()['n']
        if n_file > n_db:
            out.append((name, n_db, n_file))
    return out


def verify(data_dir):
    """Export to a temporary directory and compare against the source files.

    The migration's whole claim is that nothing was lost. This proves it by
    comparison — every original line present, in order — rather than by
    asserting it in a docstring.
    """
    import tempfile
    con = connect(data_dir, create=False)
    if con is None:
        raise SystemExit(f'[db] no database at {path_for(data_dir)}')
    data = Path(data_dir)
    tmp = Path(tempfile.mkdtemp())
    export_jsonl(data_dir, tmp)
    ok = True
    for name, (rel, table) in LEDGERS.items():
        src, dst = data / rel, tmp / rel
        orig = [l for l in (src.read_text(encoding='utf-8').splitlines()
                            if src.exists() else []) if l.strip()]
        back = [l for l in dst.read_text(encoding='utf-8').splitlines() if l.strip()]
        if orig == back:
            print(f'  {name:14s} {len(orig):7d} lines  identical')
            continue
        # The check is CONTAINMENT, not equality. Once a ledger's writes go to
        # the database the file stops growing, so the table legitimately holds
        # more than the file — and demanding equality would report every
        # migrated-and-since-written ledger as broken. What must never happen is
        # a line in the file that the table does not have: that would be a lost
        # record. Sets built ONCE: the first version rebuilt them inside a
        # comprehension, which turned the first real mismatch into a hang.
        set_orig, set_back = set(orig), set(back)
        missing = [l for l in orig if l not in set_back]
        if not missing and len(back) >= len(orig):
            print(f'  {name:14s} {len(orig):7d} lines  all present; database '
                  f'has {len(back) - len(orig)} newer row(s)')
            continue
        if sorted(orig) == sorted(back):
            # same records, different sequence — the append order is itself a
            # record, so this is still a failure
            print(f'  {name:14s} {len(orig):7d} lines  SAME SET, ORDER DIFFERS')
        else:
            extra = len([l for l in back if l not in set_orig])
            print(f'  {name:14s} MISMATCH: {len(orig)} in the file, {len(back)} '
                  f'exported, {len(missing)} MISSING, {extra} unexpected')
        ok = False
    return ok


def status(data_dir):
    con = connect(data_dir, create=False)
    if con is None:
        print(f'[db] no database at {path_for(data_dir)}')
        return
    v = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    print(f'[db] {path_for(data_dir)} (schema {v["value"] if v else "?"})')
    for t in ('customer',) + LEDGER_TABLES:
        n = con.execute(f'SELECT COUNT(*) AS n FROM {t}').fetchone()['n']
        print(f'  {t:22s} {n:7d} rows')


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--data-dir', default=config.data_root())
    ap.add_argument('--init', action='store_true', help='create an empty database')
    ap.add_argument('--migrate', action='store_true', help='JSONL -> database')
    ap.add_argument('--dry-run', action='store_true',
                    help='with --migrate: roll back instead of committing')
    ap.add_argument('--verify', action='store_true',
                    help='export and diff against the source ledgers')
    ap.add_argument('--export-jsonl', metavar='DIR', default=None,
                    help='regenerate the text ledgers into DIR')
    ap.add_argument('--status', action='store_true')
    args = ap.parse_args()

    did = False
    if args.init:
        init(args.data_dir)
        print(f'[db] initialised {path_for(args.data_dir)}')
        did = True
    if args.migrate:
        report, n_cust = migrate(args.data_dir, dry_run=args.dry_run)
        for name, (n_src, n_new, table) in report.items():
            print(f'  {name:14s} {n_src:7d} source lines -> {n_new:7d} '
                  f'inserted ({table})')
        print(f'  {"customers":14s} {n_cust:7d} derived from the newest version')
        if args.dry_run:
            print('[db] --dry-run: rolled back, nothing written')
        did = True
    if args.verify:
        print('[db] verifying the round trip against the source ledgers')
        did = True
        if not verify(args.data_dir):
            raise SystemExit('[db] VERIFY FAILED — do not freeze the originals')
        print('[db] round trip is byte-faithful')
    if args.export_jsonl:
        w = export_jsonl(args.data_dir, args.export_jsonl)
        for name, n in w.items():
            print(f'  {name:14s} {n:7d} lines')
        print(f'[db] exported to {args.export_jsonl}')
        did = True
    if args.status or not did:
        status(args.data_dir)


if __name__ == '__main__':
    main()
