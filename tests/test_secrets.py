"""docker/secrets.sh — the loops that read env files, line by line.

`read` returns nonzero on a final line that has no trailing newline, even
though it filled the variable: without `|| [ -n "$line" ]` that line is
silently skipped. Measured 2026-08-20 with mail.env: RESEND_API_KEY was
pushed and working, and invisible in the `list` receipt — and `diff` would
have called the key absent on whichever side had the newline-less copy.

The REMOTE_LIST and FINGERPRINT heredocs are lifted out of the shipped
script and run under bash, so this tests the code that runs and not a copy
of it."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

NEWLINE = chr(10)


def heredoc(script, name):
    """The body of NAME=$(cat <<'REMOTE' ... REMOTE) in secrets.sh."""
    marker = name + "=$(cat <<'REMOTE'" + NEWLINE
    start = script.index(marker) + len(marker)
    end = script.index(NEWLINE + 'REMOTE' + NEWLINE, start)
    return script[start:end]


class LastLineWithoutNewline(unittest.TestCase):

    def setUp(self):
        self.bash = shutil.which('bash')
        if not self.bash:
            self.skipTest('no bash on this machine')
        script = (Path(__file__).resolve().parent.parent
                  / 'docker' / 'secrets.sh').read_text(encoding='utf-8')
        self.remote_list = heredoc(script, 'REMOTE_LIST')
        self.fingerprint = heredoc(script, 'FINGERPRINT')
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def run_half(self, body, envdir):
        # D with forward slashes: MSYS bash on Windows takes C:/... everywhere.
        env = {**os.environ, 'D': str(envdir).replace(chr(92), '/')}
        out = subprocess.run([self.bash, '-s'], input=body.encode('utf-8'),
                             env=env, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL)
        return out.stdout.decode('utf-8')

    def write_env(self, name, text):
        # bytes, not text mode: nothing may append the newline under test
        (Path(self.dir) / name).write_bytes(text.encode('utf-8'))

    def test_list_shows_a_key_on_a_final_line_without_newline(self):
        self.write_env('mail.env',
                       'MAIL_FROM=post@murara.eu' + NEWLINE
                       + 'RESEND_API_KEY=re_abc123')  # no trailing newline
        out = self.run_half(self.remote_list, self.dir)
        self.assertIn('MAIL_FROM', out)
        self.assertIn('RESEND_API_KEY', out)
        row = next(l for l in out.splitlines() if 'RESEND_API_KEY' in l)
        self.assertIn(' set', row)
        self.assertIn('r...', row)

    def test_fingerprint_hashes_that_line_the_same_as_with_newline(self):
        # The value is identical either way; diff must say `same`, never
        # `only here` / `only on the server`, for a newline-less copy.
        self.write_env('mail.env', 'RESEND_API_KEY=re_abc123')
        bare = self.run_half(self.fingerprint, self.dir)
        self.write_env('mail.env', 'RESEND_API_KEY=re_abc123' + NEWLINE)
        with_nl = self.run_half(self.fingerprint, self.dir)
        self.assertIn('mail.env|RESEND_API_KEY ', bare)
        self.assertEqual(bare, with_nl)


if __name__ == '__main__':
    unittest.main()
