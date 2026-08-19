"""The one rewind engine — a materialised past you can trust. REFACTOR.md
phase 5.

A library, not a program: it answers no question and has no CLI. Given the
full store and a cutoff D, `World` hands back the world as it stood before D,
plus the artifacts every rewind question needs — a gate config calibrated
inside that world, a firm's pre-D references, a champion trained pre-D. The
three rewind programs (every cutoff / one win / one customer's Monday) are
readers over this; the zoom levels are theirs, the time isolation is here,
written once.

Time isolation is by construction: one working directory whose store parquets
are rewritten per cutoff (pyarrow filter — preserves the per-column role
metadata `load_with_roles` depends on; a pandas round-trip drops it), and
every component (calibration, trust list, profile, model) reads only from it.

The embedding sidecar is the exception, and it is a deliberate one: it is
copied whole, once, because embedding every cutoff over again would dominate
the run. It is therefore a per-notice cache that outruns the world it sits
in, and anything reading it must intersect with the store first —
`calibrate.calibrate` does that with its `world` row set. Two draws there
once indexed the sidecar by raw row number and so estimated the cohesion
baseline (hence which CPV codes are trusted) and the admitted-volume pool
partly from notices published after the cutoff; fixed 2026-08-10. What
crossed was aggregate text distribution, never an outcome — no bid count,
winner or award row has ever reached the as-of side.

Remaining disclosed residual: a notice's embedding vector is looked up from
that shared sidecar (a per-notice function of its own text, so no other
notice's existence changes it), and the system's design postdates any
replayed period.

**Grading is deliberately not here.** Outcomes (bid counts, winners) live in
the full store, and the callers join to it after the as-of work is done. The
engine never reads a publication dated on or after its cutoff, and it never
reads an outcome at all — that split is what makes it auditable.

Scratch homes: one parent, one subdirectory per program (`data/asof/all`,
`data/asof/win`, `data/asof/report`), so two rewinds can run at once and a
half-hour run is never lost to a collision. `cycle.py`'s housekeeping sweeps
the parent by age.
"""

import json
import shutil
from pathlib import Path

import pyarrow.compute as pc
import pyarrow.parquet as pq

import relevance as rel
import single_bidder as sb
from calibrate import calibrate as run_calibration

TRUST_NAME = 'trusted_codes_asof.json'


def refs_for_firm(gate, awards, firm):
    """A firm's resolvable wins in `awards`, as profile references — the
    publication numbers of the winning tenders' notices. Fed an as-of awards
    frame this is "the references the firm could have cited at the cutoff";
    an empty list means the firm had no citable win yet, which callers must
    treat as "the gate never ran", not as a miss (the new-customer
    bootstrap). Module-level because the bar-dropping and bar-setting
    policies around it differ per program and stay at the call sites."""
    aw = awards[awards['winner_names'].apply(
        lambda x: x is not None and firm in list(x))]
    return sorted({gate.rows[gate.by_key[(p, l)]]['publication_number']
                   for p, l in zip(aw['procedure_id'], aw['lot_id'])
                   if (p, l) in gate.by_key})


class World:
    """A rewindable copy of the store, plus everything built inside it.

    Usage (the shape all three rewind programs share):

        w = asof.World(data_dir, Path(data_dir) / 'asof' / 'all')
        w.rewind(D)                       # the world now stands at D
        cfg = w.calibrated_config('F')    # calibrated strictly inside
        gate = w.gate(cfg)
        refs = w.refs_for_firm(gate, 'Firma GmbH')
        model = w.model()                 # trained on pre-D labels only

    Frames (`tenders`, `roles`, `awards`, `data`, `aw`) are loaded lazily and
    cached; `rewind()` drops every cache. The calibration is NOT dropped by
    `rewind()` — how often to recalibrate is the caller's cadence decision
    (the all-lots rewind recalibrates every eighth cutoff, the single-moment
    ones once), so going stale must be a choice the call site can read, not a
    surprise the engine springs.
    """

    def __init__(self, data_dir, work_dir):
        self.full = Path(data_dir)
        self.work = Path(work_dir)
        (self.work / 'store').mkdir(parents=True, exist_ok=True)
        if not (self.work / 'embeddings').exists():
            shutil.copytree(self.full / 'embeddings', self.work / 'embeddings')
        self.cutoff = None
        self._frames = {}
        self._calibration = None

    # ------------------------------------------------------------ the rewind

    def rewind(self, cutoff):
        """Rewrite the working store to contain only pre-cutoff publications.

        Written to a temp file and renamed, so an interrupted run leaves the
        previous cutoff's world rather than a half-written parquet. Not for
        concurrency — a work dir has one writer — but because a rewind that
        is Ctrl-C'd mid-write used to poison the directory for every LATER
        run: the next start died on "Parquet magic bytes not found in
        footer", and the only repair was knowing to delete the file by hand.
        (Hit for real on 2026-08-11.)
        """
        for name in ('tenders', 'awards'):
            tab = pq.read_table(self.full / 'store' / f'{name}.parquet')
            mask = pc.less(tab.column('publication_date'), cutoff.date())
            tmp = self.work / 'store' / f'{name}.parquet.partial'
            pq.write_table(tab.filter(mask), tmp)
            tmp.replace(self.work / 'store' / f'{name}.parquet')
        self.cutoff = cutoff
        self._frames = {}
        return self

    # ------------------------------------------------- the as-of ingredients

    def _frame(self, key, build):
        if key not in self._frames:
            self._frames[key] = build()
        return self._frames[key]

    def _load(self):
        t, roles = sb.load_with_roles(self.work / 'store' / 'tenders.parquet')
        a, _ = sb.load_with_roles(self.work / 'store' / 'awards.parquet')
        return t, roles, a

    @property
    def tenders(self):
        return self._frame('load', self._load)[0]

    @property
    def roles(self):
        return self._frame('load', self._load)[1]

    @property
    def awards(self):
        return self._frame('load', self._load)[2]

    def _assemble(self):
        data, aw, _ = sb.assemble(self.tenders, self.awards)
        return data, aw

    @property
    def data(self):
        """The labeled training frame — every resolvable pre-cutoff lot."""
        return self._frame('assemble', self._assemble)[0]

    @property
    def aw(self):
        """The latest award revision per pre-cutoff lot."""
        return self._frame('assemble', self._assemble)[1]

    def calibrate(self):
        """Run the gate calibration strictly inside the world, and write the
        as-of trust list next to the store it was derived from.

        -> the raw calibration result. `WorldTooThin` propagates: what to do
        about a cutoff the store barely reaches back to is policy per
        program (the statistic skips it, the single-moment rewinds stop with
        advice), never the engine's.
        """
        # hand over the frames this world already holds rather than let the
        # calibration read the same two parquets a second time
        r = run_calibration(str(self.work), tenders=self.tenders,
                            awards=self.awards)
        (self.work / TRUST_NAME).write_text(json.dumps(
            {'baseline': r['baseline'], 'cut': r['trust_cut'],
             'codes': {k: {'n': v['n'], 'cohesion': v['cohesion'],
                           'trusted': v['cohesion'] >= r['trust_cut']}
                       for k, v in r['cohesion'].items()}}), encoding='utf-8')
        self._calibration = r
        return r

    def calibrated_config(self, recipe):
        """The as-of gate config for a named recipe, calibrated inside the
        world — as a value, never by assigning to module globals (REFACTOR.md
        phase 3). Calibrates on first use; after that, reuses the standing
        calibration until the caller asks again via `calibrate()`.

        The recipe letters are `calibrate.py`'s own names. The two in live
        use, mapped exactly as their original call sites built them:

        * `'F'` (hard/soft codes + floor/consensus) — all three bars ride on
          the config. That is the all-lots rewind's convention: its
          `as_of_profile` drops a customer's own bars so the as-of
          calibration decides (see its docstring for why).
        * `'H'` (single bar + trade-read corroboration) — corroboration
          fields on the config, NO bars: the report rewind has always left
          the bar fields to `DEFAULT_CONFIG`. Preserved verbatim as a
          recorded difference (REFACTOR.md phase 5); unifying it, if ever,
          is its own decision.
        """
        r = self._calibration or self.calibrate()
        trust = self.work / TRUST_NAME
        if recipe == 'F':
            f = r['configs']['F hard/soft codes + floor/consensus']
            return rel.DEFAULT_CONFIG.replace(
                trusted_codes=trust,
                soft_floor=f['soft_floor'],
                soft_consensus=f['soft_consensus'],
                min_relevance=float(f['threshold']),
                min_code_hard=float(f['code_threshold']),
                min_code_soft=float(f['soft_threshold']))
        if recipe == 'H':
            h = r['configs']['H single bar + trade-read corroboration']
            return rel.DEFAULT_CONFIG.replace(
                trusted_codes=trust,
                soft_floor=h['soft_floor'],
                soft_consensus=h['soft_consensus'],
                trade_read_form=(h['corr_form'] if h['corr_form'] != 'off'
                                 else 'off'),
                trade_read_param=h['corr_param'])
        raise ValueError(f'unknown gate recipe {recipe!r} — '
                         f"the live ones are 'F' and 'H'")

    def gate(self, config):
        """A relevance gate over the as-of store."""
        return rel.Gate(str(self.work), config=config)

    def refs_for_firm(self, gate, firm):
        """`refs_for_firm` over this world's awards: the references the firm
        could have cited at the cutoff."""
        return refs_for_firm(gate, self.awards, firm)

    def model(self):
        """The champion, trained on pre-cutoff labels only — same features,
        same 1/k lot weighting, same training window (TRAIN_WINDOW_MONTHS,
        measured from the cutoff) as the live cycle."""
        data = sb.training_window(self.data, as_of=self.cutoff)
        X, cat_cols, _, _ = sb.build_features(data, self.roles,
                                              list_frame=self.tenders)
        k = data.groupby(sb.KEY)['label'].transform('size')
        return sb.train(X, data['label'].to_numpy(),
                        (1.0 / k).to_numpy(), cat_cols)
