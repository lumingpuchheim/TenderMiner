"""PARAMETERS.md 4.1 / 4.2: the gate fingerprint covers evidence.py's rules,
and the benchmark loader names its denominator.

No data dir, no network: everything here is module state and a temp file.
"""
import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import evidence as evd                                            # noqa: E402
import relevance as rel                                           # noqa: E402


class RulesRegister(unittest.TestCase):
    def test_every_module_constant_is_a_rule_or_excluded(self):
        """A new UPPER_CASE constant in evidence.py must be filed as a RULE
        (it moves the fingerprint) or listed in NOT_RULES (it does not);
        silently doing neither is how a knob escapes the stamp."""
        names = {n for n, v in vars(evd).items()
                 if n.isupper() and not n.startswith('_')
                 and not callable(v) and not hasattr(v, 'pattern')
                 and not isinstance(v, (list, dict))}
        filed = set(evd.RULES) | set(evd.NOT_RULES)
        self.assertEqual(names - filed, set(),
                         'unfiled evidence.py constants — add to RULES or NOT_RULES')
        self.assertEqual(set(evd.RULES) & set(evd.NOT_RULES), set())

    def test_rules_reflect_live_module_state(self):
        with mock.patch.object(evd, 'SYN_THRESHOLD', 0.99):
            self.assertEqual(evd.rules()['SYN_THRESHOLD'], 0.99)
        self.assertEqual(evd.rules()['SYN_THRESHOLD'], evd.SYN_THRESHOLD)


class HonestFingerprint(unittest.TestCase):
    def test_moving_an_evidence_rule_moves_the_gate_fingerprint(self):
        before = rel.GateConfig()
        with mock.patch.object(evd, 'SYN_THRESHOLD', 0.99):
            after = rel.GateConfig()
        self.assertNotEqual(before.fingerprint, after.fingerprint)
        self.assertNotEqual(before.rules_fingerprint, after.rules_fingerprint)
        # and only the rules moved — the gate's own bars are unchanged
        self.assertEqual(before.replace(evidence_rules=after.evidence_rules)
                         .fingerprint, after.fingerprint)

    def test_moving_a_non_rule_does_not(self):
        before = rel.GateConfig()
        with mock.patch.object(evd, 'SEED', 99):
            after = rel.GateConfig()
        self.assertEqual(before.fingerprint, after.fingerprint)

    def test_env_override_is_reflected(self):
        """The twenty env-driven switches read os.environ at import; the
        snapshot sees whatever the module resolved to, so a stray variable
        in cron's environment changes the stamp instead of hiding."""
        with mock.patch.object(evd, 'CORE_HYSTERESIS', True):
            self.assertIn(('CORE_HYSTERESIS', True), rel.GateConfig().evidence_rules)

    def test_as_dict_carries_the_rules_and_is_json_safe(self):
        import json
        d = rel.GateConfig().as_dict()
        self.assertIsInstance(d['evidence_rules'], dict)
        self.assertEqual(set(d['evidence_rules']), set(evd.RULES))
        json.dumps(d, default=str)

    def test_describe_names_the_rules_hash(self):
        cfg = rel.GateConfig()
        self.assertIn(f'rules={cfg.rules_fingerprint}', cfg.describe())

    def test_the_recorded_default_fingerprint(self):
        """The register's value lives in `knobs.EXPECTED_GATE_FINGERPRINT` —
        one constant, read here and by the cycle's gate guard. If this fails
        somebody moved a knob: update the constant's receipt, the register row
        and that value in the same commit — or revert (rule 8.2)."""
        import knobs
        cfg = rel.GateConfig()
        self.assertEqual(cfg.rules_fingerprint, 'a62e07fda4')
        self.assertEqual(cfg.fingerprint, knobs.EXPECTED_GATE_FINGERPRINT)
        self.assertEqual(cfg.fingerprint, '7931c8e9cd')


class BenchmarkDenominator(unittest.TestCase):
    def test_loader_announces_blob_hash_count_and_seed_once(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'bench.jsonl'
            raw = b'# comment\n{"a": 1}\n\n{"a": 2}\n'
            p.write_bytes(raw)
            # git's blob id: sha1("blob <len>\\0" + bytes)
            import hashlib
            blob = hashlib.sha1(b'blob %d\0' % len(raw) + raw).hexdigest()[:12]
            evd.benchmark_cases._announced = False
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                cases = evd.benchmark_cases(str(p))
                evd.benchmark_cases(str(p))          # second call: silent
            self.assertEqual(cases, [{'a': 1}, {'a': 2}])
            lines = [l for l in out.getvalue().splitlines() if l.startswith('[benchmark]')]
            self.assertEqual(len(lines), 1)
            self.assertIn(f'blob {blob}', lines[0])
            self.assertIn('cases 2', lines[0])
            self.assertIn(f'seed {evd.SEED}', lines[0])
            self.assertIn(f'rules {evd.rules_fingerprint()}', lines[0])


if __name__ == '__main__':
    unittest.main()
