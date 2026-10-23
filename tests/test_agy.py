"""Antigravity CLI (agy): one global sign-in that ls / usage only show. Isolated HOME/config,
made-up files, no network: agy has no usage interface zorua reads."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SECRET = 'agy-token-secret-123'


class Agy(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = self.root / 'cfg'
        self.cfg.mkdir()
        env = patch.dict(os.environ, {'HOME': str(self.root), 'ZORUA_CONFIG_DIR': str(self.cfg)})
        env.start()
        self.addCleanup(env.stop)
        for var in ('ZORUA_FULL_EMAIL', 'ZORUA_COLOR', 'CX_COLOR', 'CODEX_HOME', 'CLAUDE_CONFIG_DIR', 'GROK_HOME',
                    'ZORUA_AUTO_GROK', 'ZORUA_EVAL_FILE', 'ZORUA_USAGE_MODE'):
            os.environ.pop(var, None)
        spec = importlib.util.spec_from_file_location('agy_test_core', ROOT / 'zorua_core.py')
        self.c = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.c)
        self.c.init_registry()
        self.c.write_tsv(self.c.ACCOUNT_FILE, [])
        self.agy_dir = self.root / '.gemini' / 'antigravity-cli'
        self.requests = []

    def install(self, signed=True):
        self.agy_dir.mkdir(parents=True)
        if signed:
            token = self.agy_dir / 'antigravity-oauth-token'
            token.write_text(json.dumps({'auth_method': 'oauth', 'id_token': SECRET, 'token': {'access_token': SECRET}}))
            token.chmod(0o600)

    def run_cmd(self, *args):
        def urlopen(req, timeout=None):
            self.requests.append(req)
            raise AssertionError('no network expected')
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out), \
                patch.object(self.c.urllib.request, 'urlopen', side_effect=urlopen):
            rc = self.c.main(list(args))
        return rc, out.getvalue()

    def rows(self, out):
        return [l for l in out.splitlines() if l.strip().startswith('agy ')]

    # ---- discovery ----------------------------------------------------------------------

    def test_state_follows_only_the_sign_in_file(self):
        self.assertIsNone(self.c.agy_state())
        self.install(signed=False)
        self.assertEqual(self.c.agy_state(), 'none')
        (self.agy_dir / 'antigravity-oauth-token').write_text('not json at all')
        self.assertEqual(self.c.agy_state(), 'ok')

    def test_sign_in_file_is_never_opened(self):
        self.install()
        real_open = open
        token = str(self.agy_dir / 'antigravity-oauth-token')

        def guarded(path, *a, **k):
            self.assertNotEqual(os.path.realpath(str(path)), os.path.realpath(token), 'zorua must not read the agy token')
            return real_open(path, *a, **k)
        with patch('builtins.open', guarded):
            for cmd in (('ls',), ('usage',), ('ls', '-v'), ('usage', '-v')):
                rc, out = self.run_cmd(*cmd)
                self.assertEqual(rc, 0)
                self.assertNotIn(SECRET, out)

    # ---- ls / usage -------------------------------------------------------------------

    def test_ls_shows_the_current_sign_in_with_no_plan(self):
        self.install()
        rc, out = self.run_cmd('ls')
        self.assertEqual(rc, 0)
        self.assertNotIn('Antigravity', out)     # headings only when more than one agent is listed
        self.assertEqual([r.split() for r in self.rows(out)], [['agy', '(current', 'sign-in)', '–']])
        self.assertIn('agy: one global sign-in; zorua shows it but does not switch it', out)
        self.assertNotIn(SECRET, out)
        self.c.write_tsv(self.c.ACCOUNT_FILE, [('default', str(self.root / '.codex'))])
        rc, out = self.run_cmd('ls')
        self.assertIn(' Codex\n', out)
        self.assertIn(' Antigravity\n', out)

    def test_ls_says_when_agy_is_signed_out(self):
        self.install(signed=False)
        _, out = self.run_cmd('ls')
        self.assertEqual([r.split() for r in self.rows(out)], [['agy', '(not', 'signed', 'in)', '–']])

    def test_usage_has_no_numbers_for_agy(self):
        self.install()
        for args, header in ((('usage',), '5H  7D'), (('usage', '--remaining'), '5H LEFT  7D LEFT')):
            rc, out = self.run_cmd(*args)
            self.assertEqual(rc, 0)
            self.assertIn(header, out)
            self.assertEqual([r.split() for r in self.rows(out)], [['agy', '–', '–', '–', '–']])
            self.assertNotIn('%', out)
        rc, out = self.run_cmd('usage', '-v')
        self.assertIn('usage not available (agy has no readable usage interface)', out)
        self.assertIn('~/.gemini/antigravity-cli', out)
        self.assertEqual(self.requests, [], 'agy usage must not call out')

    def test_usage_signed_out(self):
        self.install(signed=False)
        _, out = self.run_cmd('usage')
        self.assertEqual([r.split() for r in self.rows(out)], [['agy', '–', '(not', 'signed', 'in)']])

    # ---- not an account ---------------------------------------------------------------

    def test_agy_is_not_an_account(self):
        self.install()
        self.assertNotIn('agy', self.c.all_names())
        self.assertIsNone(self.c.kind_of('agy'))
        rc, out = self.run_cmd('ls', '--json')
        self.assertEqual(rc, 0)
        self.assertEqual([a['agent'] for a in json.loads(out)['accounts']], [])
        self.assertNotIn(SECRET, out)
        rc, _ = self.run_cmd('use', 'agy')
        self.assertNotEqual(rc, 0)

    def test_help_says_agy_is_only_shown(self):
        _, out = self.run_cmd('help')
        self.assertIn('agy (Antigravity CLI)', out)
        self.assertIn('never switches it', out)

    # ---- without agy --------------------------------------------------------------------

    def test_without_agy_other_sections_are_unchanged(self):
        self.c.write_tsv(self.c.ACCOUNT_FILE, [('default', str(self.root / '.codex'))])
        before = {cmd: self.run_cmd(*cmd)[1] for cmd in (('ls',), ('usage',), ('ls', '-v'), ('ls', '--json'))}
        for out in before.values():
            self.assertNotIn('agy', out)
            self.assertNotIn('Antigravity', out)
        self.install()
        after = self.run_cmd('ls')[1]
        self.assertTrue(after.startswith('\n Codex\n'))
        codex_table = before[('ls',)].split('\n\n')[0].lstrip('\n')          # the codex rows, as printed without agy
        self.assertIn('\n Codex\n' + codex_table + '\n', after)
        self.assertEqual(json.loads(self.run_cmd('ls', '--json')[1])['accounts'],
                         json.loads(before[('ls', '--json')])['accounts'])


if __name__ == '__main__':
    unittest.main()
