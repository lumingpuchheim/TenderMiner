"""Firm identity tests — when two winner names are one company.

    python -m unittest discover -t . -s tests     # from the repository root
    python tests/test_firms.py                    # or directly

Every case here is a real pair out of the awards store, because the rule is
only worth what it does to the names buyers actually typed. The properties
worth naming:

* the same firm typed in two ways is merged, and its spellings survive the
  merge — a letter must be able to quote what TED published,
* a registration number is the only field allowed to keep two firms apart,
  and it does: `Siemens AG` never becomes `Siemens Healthineers AG`,
* one mistyped digit does not split a firm, and one branch office in another
  town does not either,
* a sentence in the name box is not a company.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import firms


def firm(name, *notices):
    """A winner spelling and the (registration number, postcode) its notices
    carried."""
    f = firms.Firm(name)
    for national_id, zone in notices or [(None, None)]:
        f.saw(national_id, zone)
    return f


class NotAFirm(unittest.TestCase):
    def test_the_anonymisation_sentence_is_not_a_company(self):
        # Five BITMARCK lots, publication 00241114-2026: the winners are
        # lawfully unpublished and TED has no field for that.
        self.assertFalse(firms.is_firm_name(
            'Keine Angabe - der Zuschlag erging für die Lose an mehrere Bieter, '
            'die weiterhin über Einzelrealisationswettbewerbe im Wettbewerb '
            'zueinander stehen.'))

    def test_placeholders_are_not_companies(self):
        for text in ('vertraulich', 'wird nicht veröffentlicht', 'keine Angabe',
                     '-', '   ', 'k.A.', ''):
            self.assertFalse(firms.is_firm_name(text), text)

    def test_an_ordinary_firm_is_a_firm(self):
        for text in ('Jebsen GmbH', 'SVA System Vertrieb Alexander GmbH',
                     'Kurre Metallbau GmbH § CO KG'):
            self.assertTrue(firms.is_firm_name(text), text)

    def test_pasted_boilerplate_leaves_the_real_name(self):
        self.assertEqual(
            firms.clean_name('Impressum  Angaben gemäß § 5 DDG  Hans Andritter GmbH'),
            'Hans Andritter GmbH')

    def test_a_glued_address_is_not_part_of_the_name(self):
        self.assertEqual(
            firms.clean_name('SVA System Vertrieb Alexander GmbH, Borsigstr. 26, '
                             '65205 Wiesbaden'),
            'SVA System Vertrieb Alexander GmbH')

    def test_a_dropped_name_never_reaches_a_cluster(self):
        clusters, _ = firms.resolve([firm('vertraulich', ('DE123456789', '10115')),
                                     firm('Jebsen GmbH', ('DE123456789', '10115'))])
        self.assertEqual([c.name for c in clusters], ['Jebsen GmbH'])


class TheRegistrationNumber(unittest.TestCase):
    def test_a_vat_number_survives_any_spacing(self):
        self.assertEqual(firms.normalise_id('DE 141 62 68 67'), ('vat', '141626867'))
        self.assertEqual(firms.normalise_id('DE141626867'), ('vat', '141626867'))

    def test_the_registering_court_is_dropped(self):
        self.assertEqual(firms.normalise_id('HRB 39150 Leipzig'), ('reg', 'HRB39150'))
        self.assertEqual(firms.normalise_id('HRB39150'), ('reg', 'HRB39150'))

    def test_junk_in_the_box_is_no_number_at_all(self):
        for text in ('Keine Angabe', '-', '0204:994-DOEVD-83', '11122',
                     '04d56724-64e8-4dd8-ba24-7ce1e5e21dc3', 't:0221 221', None):
            self.assertIsNone(firms.normalise_id(text), text)

    def test_a_vat_number_and_an_hrb_number_do_not_contradict(self):
        # SoftwareOne gives its VAT number in one notice and its HRB in the next.
        a = firm('SoftwareONE Deutschland GmbH', ('DE141626867', '04329'))
        b = firm('SoftwareOne Deutschland GmbH', ('HRB 39150', '04329'))
        self.assertIsNone(firms.id_verdict(a, b))


class OneCompanyTypedTwice(unittest.TestCase):
    def test_case_only_difference_merges(self):
        clusters, _ = firms.resolve([
            firm('PROFI Engineering Systems AG', ('DE 158 119 098', '64293')),
            firm('Profi Engineering Systems AG', ('DE158119098', '44801'))])
        self.assertEqual(len(clusters), 1)
        self.assertEqual(sorted(clusters[0].spellings),
                         ['PROFI Engineering Systems AG', 'Profi Engineering Systems AG'])

    def test_an_abbreviation_merges_when_the_number_agrees(self):
        clusters, _ = firms.resolve([
            firm('SVA GmbH', ('DE 185 176 948', '65205')),
            firm('SVA System Vertrieb Alexander GmbH', ('DE185176948', '65205'))])
        self.assertEqual(len(clusters), 1)
        self.assertTrue(clusters[0].proven)

    def test_a_branch_office_in_another_town_still_merges(self):
        # XERVON / Xervon were held apart by a postcode until the postcode was
        # demoted to supporting evidence.
        clusters, _ = firms.resolve([firm('XERVON GmbH', (None, '45899')),
                                     firm('Xervon GmbH', (None, '13403'))])
        self.assertEqual(len(clusters), 1)

    def test_one_mistyped_digit_does_not_split_a_firm(self):
        clusters, _ = firms.resolve([
            firm('Hidalgo Bau GmbH', ('DE349570513', '10115')),
            firm('HIDALGO Bau GmbH', ('DE349507513', '10115'))])
        self.assertEqual(len(clusters), 1)

    def test_the_commonest_spelling_names_the_company(self):
        big = firms.Firm('SVA System Vertrieb Alexander GmbH')
        for _ in range(233):
            big.saw('DE185176948', '65205')
        small = firm('SVA GmbH', ('DE185176948', '65205'))
        clusters, _ = firms.resolve([small, big])
        self.assertEqual(clusters[0].name, 'SVA System Vertrieb Alexander GmbH')
        self.assertEqual(clusters[0].wins, 234)


class TwoCompaniesThatLookAlike(unittest.TestCase):
    def test_the_number_keeps_a_subsidiary_apart(self):
        clusters, blocked = firms.resolve([
            firm('Siemens AG', ('DE129274202', '80333')),
            firm('Siemens Healthineers AG', ('DE321281763', '91052'))])
        self.assertEqual(len(clusters), 2)

    def test_a_shared_first_word_is_not_evidence(self):
        clusters, _ = firms.resolve([
            firm('Becker GmbH & Co. KG', ('DE111111111', '10115')),
            firm('Becker & Partner Baugesellschaft mbH', ('DE222222222', '10115'))])
        self.assertEqual(len(clusters), 2)

    def test_one_name_two_companies_stay_apart_when_numbers_differ(self):
        # Matthäi really is several regional companies under one name.
        a = firm('Matthäi Bremen', ('DE114624504', '28309'))
        b = firm('Matthäi Langenhagen', ('DE291062630', '30855'))
        self.assertEqual(firms.compare(a, b)[0], 'block')

    def test_a_blocked_pair_is_reported_for_a_person_to_read(self):
        _, blocked = firms.resolve([
            firm('Lehmann Ausbau GmbH', ('DE342674754', '01067')),
            firm('LEHMANN Ausbau GmbH', ('DE262259004', '01067'))])
        self.assertEqual(len(blocked), 1)
        self.assertIn('registration numbers differ', blocked[0][2])

    def test_a_different_legal_form_is_a_different_company(self):
        # Bechtle AG is the parent, Bechtle GmbH a subsidiary; STRABAG AG and
        # STRABAG GmbH likewise. The name alone must not merge them.
        for other in ('Bechtle AG', 'Bechtle GmbH & Co. KG'):
            clusters, _ = firms.resolve([firm('Bechtle GmbH'), firm(other)])
            self.assertEqual(len(clusters), 2, other)

    def test_a_matching_number_still_merges_across_legal_forms(self):
        # ... but when the notices carry the same registration number, the firm
        # simply typed its own form differently, and the number wins.
        clusters, _ = firms.resolve([
            firm('Bechtle GmbH', ('DE145104053', '74172')),
            firm('Bechtle AG', ('DE145104053', '74172'))])
        self.assertEqual(len(clusters), 1)

    def test_a_missing_legal_form_is_not_a_disagreement(self):
        clusters, _ = firms.resolve([firm('SoftwareONE Deutschland GmbH'),
                                     firm('SoftwareONE Deutschland')])
        self.assertEqual(len(clusters), 1)

    def test_a_short_name_never_swallows_a_longer_one(self):
        clusters, _ = firms.resolve([firm('Bau GmbH'), firm('Baumann GmbH')])
        self.assertEqual(len(clusters), 2)


class OneBadLinkDoesNotWeldTwoFirms(unittest.TestCase):
    """Chaining is how the first run of this module put `Otis GmbH` inside
    `STRABAG AG`: both were blocked against each other, and a third spelling
    matched both."""

    def test_the_example_number_from_the_form_identifies_nobody(self):
        # 37 unrelated firms filled in DE123456789, KPMG among them.
        self.assertIsNone(firms.normalise_id('DE123456789'))
        self.assertIsNone(firms.normalise_id('DE 111 111 111'))
        clusters, _ = firms.resolve([firm('Andic GmbH', ('DE123456789', '10115')),
                                     firm('KPMG AG WPG', ('DE123456789', '10115'))])
        self.assertEqual(len(clusters), 2)

    def test_someone_elses_number_is_dropped_by_majority(self):
        # One notice gives an Otis lot STRABAG's VAT number. STRABAG holds it
        # 150 times, so it stays STRABAG's and Otis does not keep it.
        strabag = firms.Firm('STRABAG AG')
        for _ in range(150):
            strabag.saw('DE137223649', '50679')
        otis = firm('Otis GmbH', ('DE137223649', '13403'), ('DE136593436', '13403'))
        firms.discount_stray_ids([strabag, otis])
        self.assertNotIn('137223649', otis.ids['vat'])
        self.assertIn('137223649', strabag.ids['vat'])

    def test_a_third_spelling_cannot_merge_two_blocked_firms(self):
        # The spelling in the middle carries no number, so it merges with both
        # neighbours on the name alone — and the two neighbours are firms a
        # registration number says are different. The merge that would put them
        # under one roof is refused, and reported.
        a = firm('Planotec Innenausbau GmbH', ('DE111222333', '73037'))
        bridge = firm('Planotec Innenausbau Gmbh')
        c = firm('Planotec Innenausbau GmbH Nord', ('DE444555666', '20095'))
        clusters, blocked = firms.resolve([a, bridge, c])
        together = {frozenset(cl.spellings) for cl in clusters}
        self.assertEqual(len(clusters), 2)
        self.assertTrue(any({'Planotec Innenausbau GmbH',
                             'Planotec Innenausbau Gmbh'} == set(g) for g in together))
        self.assertTrue(any('different registration number' in why
                            for _, _, why in blocked))

    def test_a_form_less_spelling_cannot_chain_two_legal_forms(self):
        # "Bechtle" states no legal form, so it merges with both "Bechtle GmbH"
        # and "Bechtle AG" — and welded all three into one company the first
        # time this ran against the live store.
        clusters, blocked = firms.resolve([firm('Bechtle GmbH'), firm('Bechtle AG'),
                                           firm('Bechtle')])
        self.assertEqual(len(clusters), 2)
        self.assertTrue(any('different legal forms' in why for _, _, why in blocked))

    def test_a_number_still_beats_the_legal_form(self):
        clusters, _ = firms.resolve([firm('Bechtle GmbH', ('DE145104053', '74172')),
                                     firm('Bechtle AG', ('DE145104053', '74172')),
                                     firm('Bechtle', ('DE145104053', '74172'))])
        self.assertEqual(len(clusters), 1)

    def test_branch_spellings_still_gather_under_one_company(self):
        head = firms.Firm('STRABAG AG')
        for _ in range(20):
            head.saw('DE137223649', '50679')
        branches = [firm(f'STRABAG AG, Direktion {place}', ('DE137223649', '50679'))
                    for place in ('Nord', 'Sachsen/Thüringen', 'Bayern Süd')]
        clusters, _ = firms.resolve([head] + branches)
        self.assertEqual(len(clusters), 1)
        self.assertEqual(len(clusters[0].spellings), 4)


class FromTheStore(unittest.TestCase):
    def test_the_award_columns_are_folded_into_firms(self):
        import pandas as pd
        awards = pd.DataFrame([
            {'winner_names': ['SVA GmbH'],
             'winner_national_ids': ['DE 185 176 948'],
             'winner_postal_zones': ['65205']},
            {'winner_names': ['SVA System Vertrieb Alexander GmbH', 'Jebsen GmbH'],
             'winner_national_ids': ['DE185176948', None],
             'winner_postal_zones': ['65205', '22525']},
        ])
        clusters, _ = firms.resolve(firms.from_awards(awards))
        self.assertEqual(len(clusters), 2)
        top = clusters[0]
        self.assertEqual(top.wins, 2)
        self.assertEqual(len(top.spellings), 2)

    def test_a_store_without_the_new_columns_still_works(self):
        import pandas as pd
        awards = pd.DataFrame([{'winner_names': ['Jebsen GmbH']},
                               {'winner_names': ['jebsen GmbH']}])
        clusters, _ = firms.resolve(firms.from_awards(awards))
        self.assertEqual(len(clusters), 1)
        self.assertFalse(clusters[0].proven)


if __name__ == '__main__':
    unittest.main(verbosity=2)
