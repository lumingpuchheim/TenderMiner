"""Storage tests — doc/STORAGE.md 6.3.

    python -m unittest discover -t . -s tests        # from the repository root
    python tests/test_storage.py                     # or directly

Every assertion here was previously a script written once, run once, and thrown
away; the proofs lived in commit messages and re-ran never. These are the same
checks, encoded so that breaking them is loud.

Deliberate properties of this file:

* **No real data.** Every test builds its own temporary directory. Nothing reads
  `data/`, nothing needs the 81 MB database, nothing touches the network, and a
  failure can never be "your store is in an odd state".
* **stdlib only.** `unittest`, because pytest is not installed here and a test
  suite that needs installing is a test suite that does not get run.
* **Behaviours, not implementations.** They assert what a caller is promised —
  identical rows from either storage, an idempotent append, a loud refusal —
  so the storage engine can be replaced underneath them. That is the point:
  these tests are what make the Postgres swap in doc/STORAGE.md 0 checkable
  rather than hopeful.
"""

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
import db
import ledger
import subscriptions as subs


def sub_row(sub_id='acme', version=1, **over):
    row = {'sub_id': sub_id, 'version': version,
           'effective_from': '2026-01-01', 'name': f'{sub_id} GmbH',
           'cpv_prefixes': ['45'], 'min_deadline_days': 14, 'max_picks': 5,
           'profile_refs': ['00000001-2026'], 'min_relevance': 0.7}
    row.update(over)
    return row


def delivery_row(sub_id='acme', lot='LOT-0001', ts='2026-08-01T00:00:00+00:00'):
    return {'ts': ts, 'sub_id': sub_id, 'sub_version': 1,
            'procedure_id': 'p-1', 'lot_id': lot, 'model': 'm-1',
            'score': 0.9, 'slice_rank': 1, 'slice_size': 10,
            'slice_tier': 'HIGH', 'kind': 'pick'}


class TempHome(unittest.TestCase):
    """A throwaway storage home per test."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix='tm-test-'))
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)

    def write_subs_file(self, rows, home=None):
        home = home or self.home
        home.mkdir(parents=True, exist_ok=True)
        (home / subs.FILE_NAME).write_text(
            ''.join(json.dumps(r) + '\n' for r in rows), encoding='utf-8')

    def write_ledger_file(self, name, rows, home=None):
        home = home or self.home
        p = ledger.file_path(home, name)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(''.join(json.dumps(r) + '\n' for r in rows), encoding='utf-8')


# --------------------------------------------------------- the validator

class Validation(TempHome):
    def test_unknown_field_raises(self):
        """A typo must never be silently ignored: that is discovered from a
        wrong report weeks later."""
        with self.assertRaises(subs.SubscriptionError) as e:
            subs.validate(sub_row(min_relevence=0.6))
        self.assertIn('min_relevence', str(e.exception))

    def test_retired_field_is_dropped_not_rejected(self):
        """The subscription record is append-only, so a historical line
        carrying a retired field must stay readable — refusing it would mean
        the system cannot read its own history."""
        out = subs.validate(sub_row(avoid_n=5, top_n=12), retired_out={})
        self.assertNotIn('avoid_n', out)
        self.assertNotIn('top_n', out)
        self.assertEqual(out['sub_id'], 'acme')

    def test_cpv_prefix_must_be_2_to_8_digits(self):
        for bad in ('4', '45abc', '453123456'):
            with self.subTest(prefix=bad), self.assertRaises(subs.SubscriptionError):
                subs.validate(sub_row(cpv_prefixes=[bad]))

    def test_out_of_range_and_wrong_type(self):
        for over in ({'min_relevance': 1.5}, {'max_picks': -1},
                     {'nuts_prefixes': 'DE6'}, {'version': 0}, {'sub_id': ''},
                     {'active': 'yes'}, {'effective_from': '08/2026'}):
            with self.subTest(**over), self.assertRaises(subs.SubscriptionError):
                subs.validate(sub_row(**over))

    def test_explicit_null_means_absent(self):
        """replay.py writes min_relevance: null for a firm with no resolvable
        win — an absence, not a bad value."""
        out = subs.validate(sub_row(min_relevance=None, profile_refs=None))
        self.assertIsNone(out['min_relevance'])


# ------------------------------------------------- as-of version resolution

class Resolution(TempHome):
    ROWS = [
        sub_row('acme', 1, effective_from='2026-01-01', max_picks=1),
        sub_row('acme', 2, effective_from='2026-06-01', max_picks=2),
        sub_row('beta', 1, effective_from='2026-03-01'),
        sub_row('gone', 1, effective_from='2026-01-01'),
        sub_row('gone', 2, effective_from='2026-05-01', active=False),
    ]

    def setUp(self):
        super().setUp()
        self.write_subs_file(self.ROWS)

    def test_newest_version_in_force_speaks(self):
        self.assertEqual(subs.one(self.home, '2026-05-31', 'acme')['max_picks'], 1)
        self.assertEqual(subs.one(self.home, '2026-06-01', 'acme')['max_picks'], 2)

    def test_future_versions_are_invisible(self):
        self.assertIsNone(subs.one(self.home, '2026-02-01', 'beta'))
        self.assertEqual(len(subs.load(self.home, '2026-02-01')), 2)  # acme, gone

    def test_deactivation_is_a_new_version_not_a_resurrection(self):
        """An inactive newest version deactivates the subscription. The older
        active one must NOT stand in for it — backtest.replay used to pick the
        last *active* line and would have resurrected a deactivated customer."""
        self.assertIsNotNone(subs.one(self.home, '2026-04-01', 'gone'))
        self.assertIsNone(subs.one(self.home, '2026-05-01', 'gone'))

    def test_missing_effective_from_is_always_in_force(self):
        """replay.py writes sandbox lines with no effective_from."""
        self.write_subs_file([sub_row('sandbox', 1, effective_from=None)])
        self.assertIsNotNone(subs.one(self.home, '2020-01-01', 'sandbox'))

    def test_absent_storage_is_empty_not_an_error(self):
        self.assertEqual(subs.load(self.home / 'nope', '2026-08-01'), [])


# ------------------------------------------------------- the market filter

class MarketFilter(unittest.TestCase):
    TODAY = date(2026, 8, 8)

    def test_cpv_prefix_deeper_than_three_digits_matches(self):
        """The bug fixed in bbb5292: the prefix was tested against the 3-char
        cpv3 slicing key, so '453'.startswith('453123') was False for every lot
        and the subscription addressed an empty market."""
        sub = sub_row(cpv_prefixes=['453123'])
        row = {'cpv_main': '45312310', 'cpv3': '453'}
        self.assertTrue(subs.in_market(sub, row))

    def test_cpv3_fallback_cannot_prove_a_deeper_prefix(self):
        """A row stamped before cpv_main existed can support a 2-3 digit prefix
        and nothing longer — the keyless-row rule."""
        row = {'cpv3': '453'}
        self.assertTrue(subs.in_market(sub_row(cpv_prefixes=['453']), row))
        self.assertFalse(subs.in_market(sub_row(cpv_prefixes=['453123']), row))

    def test_nan_is_treated_as_missing(self):
        """Store rows arrive from pandas, where NaN is truthy and would poison
        a bare `or ''` into the string 'nan'."""
        nan = float('nan')
        self.assertFalse(subs.in_market(sub_row(cpv_prefixes=['45']),
                                        {'cpv_main': nan, 'cpv3': None}))
        self.assertFalse(subs.in_market(sub_row(nuts_prefixes=['DE6']),
                                        {'cpv_main': '45000000',
                                         'place_nuts3': nan}))

    def test_omitted_filter_is_no_constraint(self):
        sub = subs.validate({'sub_id': 'x'})
        self.assertTrue(subs.in_market(sub, {'cpv_main': '99999999'}))

    def test_unknown_deadline_fails_a_deadline_promise(self):
        """"At least 14 days left" cannot be honoured for a lot whose notice
        does not state a deadline."""
        sub = sub_row(min_deadline_days=14)
        row = {'cpv_main': '45000000', 'deadline_date': None}
        self.assertFalse(subs.deadline_ok(sub, row, self.TODAY))
        self.assertTrue(subs.deadline_ok(sub, row, self.TODAY, days=0))

    def test_deadline_boundary(self):
        sub = sub_row(min_deadline_days=14)
        base = {'cpv_main': '45000000'}
        self.assertTrue(subs.deadline_ok(sub, {**base, 'deadline_date': '2026-08-22'}, self.TODAY))
        self.assertFalse(subs.deadline_ok(sub, {**base, 'deadline_date': '2026-08-21'}, self.TODAY))

    def test_accessors_default(self):
        self.assertEqual(subs.max_picks(subs.validate({'sub_id': 'x'})),
                         subs.DEFAULT_MAX_PICKS)
        self.assertEqual(subs.min_days(subs.validate({'sub_id': 'x'})), 0)


# ------------------------------ the same answer from either storage

class BothStorages(TempHome):
    """The property the whole migration rests on: a caller cannot tell which
    storage answered."""

    ROWS = [sub_row('acme', 1), sub_row('acme', 2, effective_from='2026-06-01'),
            sub_row('beta', 1, avoid_n=5)]

    def setUp(self):
        super().setUp()
        self.file_home = self.home / 'file'
        self.db_home = self.home / 'db'
        self.write_subs_file(self.ROWS, self.file_home)
        self.write_subs_file(self.ROWS, self.db_home)
        db.migrate(self.db_home)

    def test_storage_kinds_are_what_we_think(self):
        self.assertEqual(subs.storage(self.file_home)[0], 'jsonl')
        self.assertEqual(subs.storage(self.db_home)[0], 'db')

    def test_subscription_reads_are_identical(self):
        for as_of in ('2026-01-01', '2026-05-31', '2026-06-01', '2027-01-01'):
            with self.subTest(as_of=as_of):
                a = sorted(subs.load(self.file_home, as_of), key=lambda d: d['sub_id'])
                b = sorted(subs.load(self.db_home, as_of), key=lambda d: d['sub_id'])
                self.assertEqual(a, b)

    def test_read_all_preserves_append_order(self):
        key = lambda rows: [(r['sub_id'], r['version']) for r in rows]
        self.assertEqual(key(subs.read_all(self.file_home)),
                         key(subs.read_all(self.db_home)))

    def test_ledger_reads_and_appends_are_identical(self):
        rows = [delivery_row(lot='LOT-0001'), delivery_row(lot='LOT-0002')]
        for home in (self.file_home, self.db_home):
            self.write_ledger_file('deliveries', rows, home)
        db.migrate(self.db_home)
        self.assertEqual(ledger.read(self.file_home, 'deliveries'),
                         ledger.read(self.db_home, 'deliveries'))
        fresh = [delivery_row(lot='LOT-0003')]
        n_file = ledger.append(self.file_home, 'deliveries', fresh)
        n_db = ledger.append(self.db_home, 'deliveries', fresh)
        self.assertEqual((n_file, n_db), (1, 1))
        self.assertEqual(ledger.read(self.file_home, 'deliveries'),
                         ledger.read(self.db_home, 'deliveries'))

    def test_identity_comes_from_the_customer_table(self):
        """Renaming a customer must change every version's reported name while
        leaving each version's market filter frozen — the reason identity was
        split out of the append-only record."""
        con = db.connect(self.db_home)
        con.execute("UPDATE customer SET name='Renamed GmbH' WHERE customer_id='acme'")
        con.commit()
        rows = [r for r in subs.read_all(self.db_home) if r['sub_id'] == 'acme']
        self.assertEqual({r['name'] for r in rows}, {'Renamed GmbH'})
        self.assertEqual([r['max_picks'] for r in rows], [5, 5])


# ---------------------------------------------- idempotence and the guards

class Idempotence(TempHome):
    def setUp(self):
        super().setUp()
        self.write_subs_file([sub_row()])
        db.migrate(self.home)

    def test_migrate_twice_inserts_nothing_the_second_time(self):
        report, _ = db.migrate(self.home)
        self.assertEqual(report['subscriptions'][1], 0)

    def test_appending_the_same_row_twice_writes_once(self):
        """A re-run of a cycle must not duplicate a customer's record."""
        row = delivery_row()
        self.assertEqual(ledger.append(self.home, 'deliveries', [row]), 1)
        self.assertEqual(ledger.append(self.home, 'deliveries', [row]), 0)
        self.assertEqual(len(ledger.read(self.home, 'deliveries')), 1)

    def test_seq_is_dense_and_ordered(self):
        for i in range(5):
            ledger.append(self.home, 'deliveries', [delivery_row(lot=f'LOT-{i}')])
        con = db.connect(self.home, create=False)
        seqs = [r['seq'] for r in con.execute('SELECT seq FROM delivery ORDER BY seq')]
        self.assertEqual(seqs, list(range(1, 6)))


class Guards(TempHome):
    def test_subscription_file_ahead_of_database_raises(self):
        """Two sources with one silently preferred is how a customer ends up
        served from a stale market."""
        self.write_subs_file([sub_row('acme', 1)])
        db.migrate(self.home)
        self.write_subs_file([sub_row('acme', 1), sub_row('acme', 2,
                                                          effective_from='2026-07-01')])
        with self.assertRaises(subs.SubscriptionError) as e:
            subs.load(self.home, '2026-08-01')
        self.assertIn('db.py --migrate', str(e.exception))

    def test_ledger_file_ahead_of_database_raises(self):
        self.write_subs_file([sub_row()])
        self.write_ledger_file('deliveries', [delivery_row(lot='LOT-1')])
        db.migrate(self.home)
        self.write_ledger_file('deliveries', [delivery_row(lot='LOT-1'),
                                              delivery_row(lot='LOT-2')])
        with self.assertRaises(ledger.LedgerError) as e:
            ledger.read(self.home, 'deliveries')
        self.assertIn('db.py --migrate', str(e.exception))

    def test_a_storage_file_path_is_refused(self):
        """Reaching past the interface must fail loudly, not half-work."""
        self.write_subs_file([sub_row()])
        with self.assertRaises(subs.SubscriptionError):
            subs.load(self.home / subs.FILE_NAME, '2026-08-01')
        with self.assertRaises(ledger.LedgerError):
            ledger.read(self.home / 'x.jsonl', 'deliveries')

    def test_frozen_marker_beside_a_live_file_raises(self):
        self.write_subs_file([sub_row()])
        (self.home / (subs.FILE_NAME + '.migrated-2026-08-08')).write_text(
            '', encoding='utf-8')
        with self.assertRaises(subs.SubscriptionError):
            subs.load(self.home, '2026-08-01')

    def test_schema_downgrade_is_refused(self):
        """Reading a schema-1 database would export rows in an undefined order
        and look plausible."""
        self.write_subs_file([sub_row()])
        db.migrate(self.home)
        con = sqlite3.connect(db.path_for(self.home))
        con.execute("UPDATE meta SET value='1' WHERE key='schema_version'")
        con.commit()
        con.close()
        with self.assertRaises(RuntimeError) as e:
            db.connect(self.home, create=False)
        self.assertIn('--migrate', str(e.exception))


# ------------------------------------------- append-only, and what is not

class AppendOnly(TempHome):
    def setUp(self):
        super().setUp()
        self.write_subs_file([sub_row()])
        # EVERY ledger table needs a row: a DELETE or UPDATE against an empty
        # table fires no row trigger and therefore raises nothing, which the
        # first version of this test mistook for the guard being absent.
        self.write_ledger_file('deliveries', [delivery_row()])
        self.write_ledger_file('grades', [{
            'graded_at': '2026-08-01T00:00:00+00:00', 'procedure_id': 'p-1',
            'lot_id': 'LOT-0001', 'label': 1, 'n_tenders': 1,
            'award_pub': '2026-07-20', 'model': 'm-1', 'score': 0.9}])
        self.write_ledger_file('predictions', [{
            'ts': '2026-08-01T00:00:00+00:00', 'model': 'm-1',
            'procedure_id': 'p-1', 'lot_id': 'LOT-0001', 'score': 0.9}])
        self.write_ledger_file('learned_refs', [{
            'sub_id': 'acme', 'pub': '00000002-2026', 'learned_at': '2026-08-01'}])
        self.write_ledger_file('gate_configs', [{
            'fingerprint': 'abc1234567', 'first_seen': '2026-08-01T00:00:00+00:00',
            'mode': 'evidence'}])
        self.write_ledger_file('simulations', [{
            'ts': '2026-08-01T00:00:00+00:00', 'company': 'Acme GmbH',
            'procedure_id': 'p-1', 'lot_id': 'LOT-0001', 'model': 'm-1',
            'score': 0.8, 'cpv3': '452'}])
        db.migrate(self.home)
        self.con = db.connect(self.home)

    def test_every_ledger_table_has_a_row_to_test_against(self):
        """Guards the test above: an empty table would make it vacuous."""
        for table in db.LEDGER_TABLES:
            n = self.con.execute(f'SELECT COUNT(*) AS n FROM {table}').fetchone()['n']
            with self.subTest(table=table):
                self.assertGreater(n, 0)

    def test_ledger_rows_cannot_be_updated_or_deleted(self):
        for table in db.LEDGER_TABLES:
            for sql in (f'UPDATE {table} SET seq = seq',
                        f'DELETE FROM {table}'):
                with self.subTest(sql=sql), self.assertRaises(sqlite3.IntegrityError):
                    self.con.execute(sql)
                self.con.rollback()

    def test_customer_is_correctable_and_deletable(self):
        """Identity must stay mutable and the row deletable.

        This failed when written: `customer_id TEXT REFERENCES customer(...)`
        made `DELETE FROM customer` a foreign-key violation, so the erasure this
        table exists for was impossible. Schema 3 makes the reference soft.
        """
        self.con.execute("UPDATE customer SET contact_email='a@b.c'")
        self.con.execute('DELETE FROM customer')
        self.con.rollback()

    def test_deleting_a_customer_does_NOT_yet_erase_their_name(self):
        """The known gap, asserted so it cannot be forgotten.

        Deleting the customer row removes the identity the read path overlays —
        so `name` stops being reported. But the same name is still inside the
        append-only `raw` blob of every subscription version, because `raw` is
        the verbatim original line. So erasure is incomplete: this test asserts
        the CURRENT truth, not the desired one. See doc/STORAGE.md 5.5.
        """
        self.con.execute("DELETE FROM customer WHERE customer_id='acme'")
        self.con.commit()
        rows = subs.read_all(self.home)
        self.assertNotIn('name', {k for r in rows for k in r if r[k] is None} | set())
        # the overlay is gone from the returned dicts...
        self.assertTrue(all(r.get('name') == 'acme GmbH' for r in rows),
                        'name still comes from raw, which is the gap')
        # ...and demonstrably still present in storage
        raw = self.con.execute('SELECT raw FROM subscription_version').fetchone()[0]
        self.assertIn('acme GmbH', db.unpack(raw))


# --------------------------------------------------------- export fidelity

class Export(TempHome):
    def test_export_reproduces_the_source_lines_exactly(self):
        """`--verify`'s promise: the migration lost nothing, proved by
        comparison rather than assertion."""
        sub_rows = [sub_row('acme', 1), sub_row('acme', 2, avoid_n=5)]
        deliveries = [delivery_row(lot=f'LOT-{i}') for i in range(3)]
        self.write_subs_file(sub_rows)
        self.write_ledger_file('deliveries', deliveries)
        db.migrate(self.home)
        self.assertTrue(db.verify(self.home))
        out = self.home / 'export'
        db.export_jsonl(self.home, out)
        for name, rows in (('subscriptions', sub_rows), ('deliveries', deliveries)):
            src = (self.home / db.LEDGERS[name][0]).read_text(encoding='utf-8')
            dst = (out / db.LEDGERS[name][0]).read_text(encoding='utf-8')
            with self.subTest(name=name):
                self.assertEqual(src.splitlines(), dst.splitlines())

    def test_heterogeneous_rows_survive(self):
        """Rows written months apart carry different keys. A typed-only schema
        would invent values for the gaps; this is why every row keeps its
        verbatim line. The 12 delivery rows lost to `kind NOT NULL` were found
        exactly this way."""
        old = {'ts': '2026-07-01T00:00:00+00:00', 'sub_id': 'acme',
               'procedure_id': 'p-1', 'lot_id': 'LOT-9', 'model': 'm-0',
               'score': 0.5}                      # no kind, no gate_config
        self.write_subs_file([sub_row()])
        self.write_ledger_file('deliveries', [old, delivery_row()])
        db.migrate(self.home)
        self.assertTrue(db.verify(self.home))
        back = ledger.read(self.home, 'deliveries')
        self.assertEqual(len(back), 2)
        self.assertNotIn('kind', back[0])          # not invented on the way out

    def test_stale_tables_are_reported(self):
        self.write_subs_file([sub_row()])
        self.write_ledger_file('deliveries', [delivery_row(lot='LOT-1')])
        db.migrate(self.home)
        self.write_ledger_file('deliveries', [delivery_row(lot='LOT-1'),
                                              delivery_row(lot='LOT-2')])
        stale = dict((n, (d, f)) for n, d, f in db.stale_tables(self.home))
        self.assertEqual(stale['deliveries'], (1, 2))


# ------------------------------------------------------------ the sandbox

class Sandbox(TempHome):
    def test_write_sandbox_round_trips_and_validates(self):
        sandbox = self.home / 'sb'
        row = subs.override(sub_row(), min_deadline_days=0, version=9)
        subs.write_sandbox(sandbox, [row])
        self.assertEqual(subs.storage(sandbox)[0], 'db')
        self.assertEqual(subs.one(sandbox, '2026-08-01', 'acme'), row)

    def test_override_rejects_an_unknown_field(self):
        """A misspelled tryout --set used to render the unchanged report, which
        looks exactly like the setting making no difference."""
        with self.assertRaises(subs.SubscriptionError):
            subs.override(sub_row(), min_relevence=0.6)


# ------------------------------------------------------- where state lives

class Config(unittest.TestCase):
    def setUp(self):
        self.saved = {v: os.environ.get(v)
                      for v in (config.ENV_VAR, config.MODELS_ENV_VAR)}
        self.addCleanup(self._restore)
        for v in self.saved:
            os.environ.pop(v, None)

    def _restore(self):
        for var, val in self.saved.items():
            if val is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = val

    def test_resolution_order(self):
        self.assertEqual(config.data_root(), Path(config.FALLBACK))
        os.environ[config.ENV_VAR] = '/srv/state'
        self.assertEqual(config.data_root(), Path('/srv/state'))
        self.assertEqual(config.data_root('/explicit'), Path('/explicit'))

    def test_models_resolution_order(self):
        """The registry resolves like the data root and by its own variable —
        a container puts it under the mounted state, a laptop leaves it in the
        working directory (STORAGE.md 6.5)."""
        self.assertEqual(config.models_root(), Path(config.MODELS_FALLBACK))
        os.environ[config.MODELS_ENV_VAR] = '/srv/state/models'
        self.assertEqual(config.models_root(), Path('/srv/state/models'))
        self.assertEqual(config.models_root('/explicit'), Path('/explicit'))

    def test_the_two_roots_are_independent(self):
        """Setting the state root must not silently move the model registry:
        5.1 has not been decided, and a registry that relocates itself the day
        someone sets TM_DATA_DIR would take the promoted model with it."""
        os.environ[config.ENV_VAR] = '/srv/state'
        self.assertEqual(config.models_root(), Path(config.MODELS_FALLBACK))

    def test_state_inside_the_checkout_is_detected(self):
        self.assertTrue(config.inside_checkout(config.REPO / 'data'))
        self.assertFalse(config.inside_checkout(Path(tempfile.gettempdir())))


if __name__ == '__main__':
    unittest.main(verbosity=2)
