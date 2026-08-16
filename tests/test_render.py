"""render.py — the two customer documents and the delivery rows.

REFACTOR.md phase 4b. Before the split these were unreachable without running
a cycle: the report, the annex and the receipts were one 200-line function
body inside `delivering.deliver`. Each test below is a promise made to a customer
in SUBSCRIPTIONS.md or a decision recorded there, and none of them needed a
store, a model or a database to write.
"""
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import render                                                  # noqa: E402
import selection                                               # noqa: E402

TODAY = date(2026, 8, 14)


def lot(pid='p1', lid='LOT-0000', **kw):
    r = {'procedure_id': pid, 'lot_id': lid, 'title': 'Dacharbeiten',
         'buyer_name': 'Stadt Musterhausen', 'deadline_date': '2026-09-30',
         'publication_number': '123456-2026', 'score': 0.9, 'model': 'm1',
         'why_lonely': ['kurze Frist'], 'flag': True}
    r.update(kw)
    return r


def sel_of(market=(), ranked=(), picks=(), borderline=(), judged=None):
    return selection.SliceResult(market=list(market), ranked=list(ranked),
                                 picks=list(picks), borderline=list(borderline),
                                 judged=judged or {})


def report_of(sub, sel, **kw):
    kw.setdefault('profile', None)
    kw.setdefault('receipts', '')
    kw.setdefault('tier_high', 0.10)
    kw.setdefault('tier_medium', 0.20)
    kw.setdefault('ts', '2026-08-14T08:15:00+00:00')
    kw.setdefault('already', set())
    return render.customer_report(sub, sel, today=TODAY, **kw)


SUB = {'sub_id': 'testfirm', 'name': 'Test Bau GmbH', 'version': 3,
       'cpv': ['45'], 'country': 'DEU'}


class NothingToSay(unittest.TestCase):
    """Decision 2026-08-06: no picks and no graded outcome -> no report at
    all, rather than a page telling the customer we found nothing."""

    def test_no_picks_no_receipts_writes_no_report(self):
        page, rows = report_of(SUB, sel_of())
        self.assertIsNone(page)
        self.assertEqual(rows, [])

    def test_receipts_alone_still_earn_a_report(self):
        page, rows = report_of(SUB, sel_of(), receipts='<ul><li>x</li></ul>')
        self.assertIsNotNone(page)
        self.assertIn('Rückblick', page)
        self.assertEqual(rows, [])

    def test_picks_without_receipts_promise_the_retrospective(self):
        page, _ = report_of(SUB, sel_of(ranked=[lot()], picks=[lot()]))
        self.assertIn('sobald der Zuschlag', page)


class WhatTheCustomerSees(unittest.TestCase):
    def test_single_pick_reads_singular(self):
        page, _ = report_of(SUB, sel_of(ranked=[lot()], picks=[lot()]))
        self.assertIn('Diese Ausschreibung passt', page)

    def test_two_picks_read_plural(self):
        two = [lot(), lot(lid='LOT-0001')]
        page, _ = report_of(SUB, sel_of(ranked=two, picks=two))
        self.assertIn('Diese 2 Ausschreibungen passen', page)

    def test_the_haystack_is_never_quoted(self):
        """Decision 2026-08-05: how many lots we checked is our business."""
        many = [lot(lid=f'LOT-{i:04d}') for i in range(40)]
        page, _ = report_of(SUB, sel_of(market=many, ranked=many, picks=many[:3]))
        self.assertNotIn('40', page)

    def test_why_mine_column_only_for_a_gated_subscription(self):
        one = [lot()]
        plain, _ = report_of(SUB, sel_of(ranked=one, picks=one))
        self.assertNotIn('warum Ihr Geschäft', plain)
        judged = {selection.lot_key(one[0]): (0.8, None, ('ref', 'Hallenbau'))}
        gated, _ = report_of(SUB, sel_of(ranked=one, picks=one, judged=judged),
                             profile={'version': 2, 'config': None})
        self.assertIn('warum Ihr Geschäft', gated)
        self.assertIn('ähnelt Ihrem Auftrag', gated)

    def test_a_lot_without_a_ted_number_is_not_a_broken_link(self):
        one = [lot(publication_number=None)]
        page, _ = report_of(SUB, sel_of(ranked=one, picks=one))
        self.assertIn('Dacharbeiten', page)
        self.assertNotIn('<a href="https://ted.europa.eu/de/notice/-/detail/None"', page)


class DeliveryRows(unittest.TestCase):
    """The frozen record of what this customer actually saw."""

    def test_one_row_per_pick_with_rank_and_slice_size(self):
        three = [lot(lid=f'LOT-{i}') for i in range(3)]
        _, rows = report_of(SUB, sel_of(ranked=three, picks=three))
        self.assertEqual([r['slice_rank'] for r in rows], [1, 2, 3])
        self.assertEqual({r['slice_size'] for r in rows}, {3})
        self.assertEqual({r['sub_version'] for r in rows}, {3})

    def test_a_lot_already_delivered_today_is_not_written_twice(self):
        one = [lot()]
        already = {('testfirm', 'p1', 'LOT-0000', '2026-08-14')}
        page, rows = report_of(SUB, sel_of(ranked=one, picks=one),
                               already=already)
        self.assertEqual(rows, [])
        self.assertIn('Dacharbeiten', page,
                      'the pick still appears in the report; only the row is skipped')

    def test_ungated_rows_carry_no_gate_stamp(self):
        one = [lot()]
        _, rows = report_of(SUB, sel_of(ranked=one, picks=one))
        self.assertNotIn('relevance_score', rows[0])


class Annex(unittest.TestCase):
    def test_every_market_lot_appears_sorted_by_deadline(self):
        rows = [lot(lid='LOT-A', deadline_date='2026-10-01', title='Spaeter'),
                lot(lid='LOT-B', deadline_date='2026-09-01', title='Frueher')]
        _, html = render.market_annex(SUB, sel_of(market=rows), today=TODAY,
                                      profile=None, top_slice=0.2)
        self.assertLess(html.index('Frueher'), html.index('Spaeter'))

    def test_the_filename_carries_the_cycle_date(self):
        name, _ = render.market_annex(SUB, sel_of(), today=TODAY, profile=None,
                                      top_slice=0.2)
        self.assertEqual(name, 'annex_2026-08-14.html')

    def test_flagged_lots_are_green_and_the_tail_is_red(self):
        rows = [lot(lid=f'LOT-{i}', flag=(i == 0), score=1.0 - i / 10)
                for i in range(5)]
        _, html = render.market_annex(SUB, sel_of(market=rows), today=TODAY,
                                      profile=None, top_slice=0.2)
        self.assertIn('v-green', html)
        self.assertIn('v-red', html)

    def test_borderline_band_appears_only_when_there_are_near_misses(self):
        _, without = render.market_annex(SUB, sel_of(market=[lot()]),
                                         today=TODAY, profile=None, top_slice=0.2)
        self.assertNotIn('Knapp aussortiert', without)
        _, with_band = render.market_annex(
            SUB, sel_of(market=[lot()], borderline=[lot(lid='LOT-9')]),
            today=TODAY, profile=None, top_slice=0.2)
        self.assertIn('Knapp aussortiert', with_band)


class Receipts(unittest.TestCase):
    def _grade(self, label=True, n=None):
        return {'procedure_id': 'p1', 'lot_id': 'LOT-0000', 'label': label,
                'award_pub': '2026-08-01', 'n_tenders': n,
                'award_publication_number': '999-2026'}

    def _delivery(self, **kw):
        d = {'ts': '2026-07-01T00:00:00+00:00', 'procedure_id': 'p1',
             'lot_id': 'LOT-0000', 'kind': 'pick', 'title': 'Dacharbeiten',
             'buyer_name': 'Stadt Musterhausen'}
        d.update(kw)
        return d

    def test_no_graded_outcome_means_no_section(self):
        self.assertEqual(render.receipt_html([], [self._delivery()], {}), '')

    def test_a_hit_and_a_miss_both_render(self):
        hit = render.receipt_html([self._grade(True, 1)], [self._delivery()], {})
        miss = render.receipt_html([self._grade(False, 7)], [self._delivery()], {})
        self.assertIn('kaum Wettbewerb', hit)
        self.assertIn('class="ok"', hit)
        self.assertIn('doch umkämpft', miss)
        self.assertIn('class="miss"', miss)

    def test_the_ted_link_is_the_proof(self):
        html = render.receipt_html([self._grade(True, 1)], [self._delivery()], {})
        self.assertIn('ted.europa.eu/de/notice/-/detail/999-2026', html)

    def test_long_histories_are_capped_and_the_rest_counted(self):
        grades, deliveries = [], []
        for i in range(render.MAX_RECEIPTS + 4):
            grades.append({'procedure_id': f'p{i}', 'lot_id': 'L', 'label': True,
                           'award_pub': f'2026-08-{i % 28 + 1:02d}',
                           'n_tenders': 1, 'award_publication_number': f'{i}-2026'})
            deliveries.append(self._delivery(procedure_id=f'p{i}', lot_id='L'))
        html = render.receipt_html(grades, deliveries, {})
        self.assertEqual(html.count('<li>'), render.MAX_RECEIPTS)
        self.assertIn('4 weitere', html)


class Escaping(unittest.TestCase):
    def test_a_buyer_name_with_markup_cannot_reach_the_page(self):
        one = [lot(buyer_name='<script>alert(1)</script>')]
        page, _ = report_of(SUB, sel_of(ranked=one, picks=one))
        self.assertNotIn('<script>', page)
        self.assertIn('&lt;script&gt;', page)

    def test_a_title_with_newlines_does_not_break_the_table(self):
        one = [lot(title='Dach\narbeiten\n\nLos 2')]
        page, _ = report_of(SUB, sel_of(ranked=one, picks=one))
        self.assertIn('Dach arbeiten Los 2', page)


if __name__ == '__main__':
    unittest.main()
