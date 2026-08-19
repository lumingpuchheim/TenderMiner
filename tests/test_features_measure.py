"""`features._measure_days` — a DurationMeasure is a period, not any integer.

2026-08-19: the 2023-11..2024-07 backfill carried a validity measure of
11,333,988,760 days. It went straight into an int32 parquet column and the
whole store build died at the write (`ArrowInvalid: Value ... too large`).
A measure beyond a century is a typo and is nulled, like `duration_days`
already does at twenty years.
"""

import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import features                                                   # noqa: E402

NS = 'xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"'


def node(value, unit='DAY'):
    return ET.fromstring(f'<root {NS}><cbc:DurationMeasure unitCode="{unit}">'
                         f'{value}</cbc:DurationMeasure></root>')


class MeasureDays(unittest.TestCase):
    def test_a_plain_period_is_days(self):
        days, raw, unit = features._measure_days(node(90), 'cbc:DurationMeasure')
        self.assertEqual((days, raw, unit), (90, 90.0, 'DAY'))
        days, _, _ = features._measure_days(node(6, 'MON'), 'cbc:DurationMeasure')
        self.assertEqual(days, 180)

    def test_a_typo_beyond_a_century_is_nulled_not_stored(self):
        days, raw, unit = features._measure_days(node(11333988760),
                                                 'cbc:DurationMeasure', 'validity')
        self.assertIsNone(days)
        self.assertEqual(raw, 11333988760.0)          # the raw value is kept
        self.assertEqual(unit, 'DAY')
        days, _, _ = features._measure_days(node(10_000_000, 'MON'),
                                            'cbc:DurationMeasure')
        self.assertIsNone(days)
        # and a century itself still passes
        days, _, _ = features._measure_days(node(100, 'YEAR'), 'cbc:DurationMeasure')
        self.assertEqual(days, 36500)


if __name__ == '__main__':
    unittest.main(verbosity=2)
