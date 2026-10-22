"""zorua usage used/remaining modes: isolated HOME/config, mocked usage requests and Claude CLI."""
import base64
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class UsageMode(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = self.root / 'cfg'
        self.cfg.mkdir()
        env = patch.dict(os.environ, {'HOME': str(self.root), 'ZORUA_CONFIG_DIR': str(self.cfg)})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop('NO_COLOR', None)
        os.environ.pop('ZORUA_COLOR', None)
        spec = importlib.util.spec_from_file_location('usage_mode_test_core', ROOT / 'zorua_core.py')
        self.c = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.c)
        self.c.init_registry()
        self.c.write_tsv(self.c.ACCOUNT_FILE, [])

    def jwt(self, email):
        return 'x.' + base64.urlsafe_b64encode(json.dumps({'email': email}).encode()).decode().rstrip('=') + '.x'

    def account(self, name, email):
        home = self.root / name
        home.mkdir()
        (home / 'auth.json').write_text(json.dumps({'tokens': {'id_token': self.jwt(email), 'access_token': 'fake-' + name}}))
        self.c.write_tsv(self.c.ACCOUNT_FILE, self.c.accounts() + [(name, str(home))])
        return home

    def usage(self, short, weekly):
        def win(used, secs, reset):
            return {'used_percent': used, 'limit_window_seconds': secs, 'reset_after_seconds': reset}
        return {'rate_limit': {'primary_window': win(short, 18000, 1000), 'secondary_window': win(weekly, 604800, 90000)}}, None

    def run_cmd(self, *args, usage=None):
        out = io.StringIO()
        (self.cfg / 'usage-cache.json').unlink(missing_ok=True)       # the 30-second cache would replay an earlier answer
        with patch.object(self.c, 'fetch_codex_usage', side_effect=AssertionError('no network') if usage is None else lambda tok: usage), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = self.c.main(list(args))
        return rc, out.getvalue()

    def row(self, text, name):
        return next(l for l in text.splitlines() if name in l)

    def test_remaining_percent_values(self):
        r = self.c.remaining_of
        self.assertEqual([r(0), r(30), r(100)], [100, 70, 0])
        self.assertEqual([r(120), r(-5)], [0, 100])           # out-of-range input stays on the scale
        self.assertIsNone(r(None))

    def test_used_is_the_default_and_unchanged(self):
        self.account('a', 'a@example.com')
        rc, text = self.run_cmd('usage', usage=self.usage(30, 80))
        self.assertEqual(rc, 0)
        head = next(l for l in text.splitlines() if 'NAME' in l)
        self.assertNotIn('LEFT', head)
        row = self.row(text, ' a ')
        self.assertIn('▓▓░░░░  30%', row)       # 1.8 cells, rounded up
        self.assertIn('▓▓▓▓▓░  80%', row)       # 4.8 cells

    def test_remaining_flag_draws_what_is_left(self):
        self.account('a', 'a@example.com')
        for flag in ('--remaining', '-r'):
            rc, text = self.run_cmd('usage', flag, usage=self.usage(30, 80))
            self.assertEqual(rc, 0)
            head = next(l for l in text.splitlines() if 'NAME' in l)
            self.assertIn('5H LEFT', head)
            self.assertIn('7D LEFT', head)
            row = self.row(text, ' a ')
            self.assertIn('▓▓▓▓▓░  70%', row)   # 70% left -> 4.2 cells, rounded up
            self.assertIn('▓▓░░░░  20%', row)   # 20% left -> 1.2 cells
            self.assertNotIn('30%', row)
            self.assertNotIn('80%', row)

    def test_extremes_fill_and_empty(self):
        self.account('a', 'a@example.com')
        _, text = self.run_cmd('usage', '-r', usage=self.usage(0, 100))
        row = self.row(text, ' a ')
        self.assertIn('▓▓▓▓▓▓ 100%', row)       # nothing used: full bar
        self.assertIn('░░░░░░   0%', row)       # everything used: empty bar

    def test_unknown_cells_stay_dashes(self):
        self.account('a', 'a@example.com')
        _, text = self.run_cmd('usage', '-r', usage=self.usage(None, None))
        row = self.row(text, ' a ')
        self.assertNotIn('%', row)
        self.assertNotIn('▓', row)
        self.assertNotIn('░', row)

    def test_signed_out_and_error_rows_are_not_a_hundred_percent(self):
        home = self.root / 'out'
        home.mkdir()
        self.c.write_tsv(self.c.ACCOUNT_FILE, [('out', str(home))])
        _, text = self.run_cmd('usage', '-r')
        self.assertIn('(not signed in)', text)
        self.assertNotIn('100%', text)

    def test_claude_without_cache_shows_dashes_in_remaining(self):
        home = self.root / 'cl'
        home.mkdir()
        self.c.write_tsv(self.c.CLAUDE_FILE, [('cl', str(home))])
        with patch.object(self.c, 'claude_status', return_value={'loggedIn': True, 'email': 'c@example.com', 'subscriptionType': 'max'}):
            _, text = self.run_cmd('usage', '-r')
        row = self.row(text, ' cl')
        self.assertEqual(row.split()[2:5], ['–', '–', '–'])
        self.assertNotIn('%', row)

    def test_claude_cache_windows(self):
        home = self.root / 'cl'
        home.mkdir()
        now = time.time()
        (home / '.zorua-usage.json').write_text(json.dumps({
            'updated_at': now, 'five_hour': {'used_percentage': 40, 'resets_at': now + 3600},
            'seven_day': {'used_percentage': 90, 'resets_at': now + 86400}}))
        self.c.write_tsv(self.c.CLAUDE_FILE, [('cl', str(home))])
        with patch.object(self.c, 'claude_status', return_value={'loggedIn': True, 'email': 'c@example.com', 'subscriptionType': 'max'}):
            _, text = self.run_cmd('usage', '-r')
        row = self.row(text, ' cl')
        self.assertIn('60%', row)
        self.assertIn('10%', row)

    def test_grok_unreported_share_stays_dash(self):
        home = self.root / 'gk'
        home.mkdir()
        self.c.write_tsv(self.c.GROK_FILE, [('gk', str(home))])
        grok = {'rate_limit': {'primary_window': None,
                               'secondary_window': {'used_percent': None, 'limit_window_seconds': 604800, 'reset_after_seconds': 5000}}}
        info = {'email': 'g@example.com', 'plan': None, 'until': None, 'tok': {'access_token': 'x'}, 'state': 'ok'}
        with patch.object(self.c, 'read_grok_info', return_value=info), patch.object(self.c, 'fetch_grok_usage', return_value=(grok, None)):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.c.main(['usage', '-r'])
        row = self.row(out.getvalue(), ' gk')
        self.assertNotIn('%', row)
        self.assertNotIn('▓', row)
        self.assertNotIn('░', row)

    def test_verbose_remaining(self):
        self.account('a', 'a@example.com')
        _, text = self.run_cmd('usage', '-v', '-r', usage=self.usage(25, 75))
        self.assertIn(' 75% left', text)
        self.assertIn(' 25% left', text)
        _, text = self.run_cmd('usage', '-v', usage=self.usage(25, 75))
        self.assertNotIn('left', text)

    def test_mode_persists_and_flags_override(self):
        self.account('a', 'a@example.com')
        rc, text = self.run_cmd('usage', 'mode')
        self.assertEqual((rc, text.strip()), (0, 'used'))
        self.assertFalse((self.cfg / 'prefs.json').exists())       # reading writes nothing
        rc, _ = self.run_cmd('usage', 'mode', 'remaining')
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads((self.cfg / 'prefs.json').read_text()), {'usage_mode': 'remaining'})
        self.assertEqual(self.run_cmd('usage', 'mode')[1].strip(), 'remaining')
        _, text = self.run_cmd('usage', usage=self.usage(30, 80))
        self.assertIn('5H LEFT', text)
        _, text = self.run_cmd('usage', '--used', usage=self.usage(30, 80))        # flag beats the saved default
        self.assertNotIn('LEFT', text)
        self.assertIn('30%', self.row(text, ' a '))
        self.run_cmd('usage', 'mode', 'used')
        _, text = self.run_cmd('usage', '-r', usage=self.usage(30, 80))             # and the other way round
        self.assertIn('5H LEFT', text)
        _, text = self.run_cmd('usage', usage=self.usage(30, 80))
        self.assertNotIn('LEFT', text)
        mode = (self.cfg / 'prefs.json').stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_mode_keeps_other_prefs_and_rejects_bad_input(self):
        (self.cfg / 'prefs.json').write_text(json.dumps({'other': 1}))
        self.run_cmd('usage', 'mode', 'remaining')
        self.assertEqual(json.loads((self.cfg / 'prefs.json').read_text()), {'other': 1, 'usage_mode': 'remaining'})
        rc, text = self.run_cmd('usage', 'mode', 'bogus')
        self.assertEqual(rc, 1)
        self.assertIn('usage: zorua usage mode', text)
        self.assertEqual(self.run_cmd('usage', 'mode')[1].strip(), 'remaining')    # unchanged
        self.assertEqual(self.run_cmd('usage', 'mode', 'used', 'extra')[0], 1)

    def test_corrupt_or_unknown_pref_falls_back_to_used(self):
        (self.cfg / 'prefs.json').write_text('{not json')
        self.assertEqual(self.run_cmd('usage', 'mode')[1].strip(), 'used')
        (self.cfg / 'prefs.json').write_text(json.dumps({'usage_mode': 'sideways'}))
        self.assertEqual(self.run_cmd('usage', 'mode')[1].strip(), 'used')
        (self.cfg / 'prefs.json').write_text('[]')
        self.assertEqual(self.run_cmd('usage', 'mode')[1].strip(), 'used')

    def test_json_adds_remaining_and_keeps_old_fields(self):
        self.account('a', 'a@example.com')
        for mode in ('used', 'remaining'):       # the saved mode never changes --json
            self.run_cmd('usage', 'mode', mode)
            _, raw = self.run_cmd('usage', '--json', usage=self.usage(30, 80))
            ws = json.loads(raw)['accounts'][0]['usage']['windows']
            self.assertEqual(ws[0], {'window_seconds': 18000, 'used_percent': 30, 'remaining_percent': 70, 'reset_after_seconds': 1000})
            self.assertEqual(ws[1], {'window_seconds': 604800, 'used_percent': 80, 'remaining_percent': 20, 'reset_after_seconds': 90000})
        _, raw = self.run_cmd('usage', '--used', '--json', usage=self.usage(None, 10))
        ws = json.loads(raw)['accounts'][0]['usage']['windows']
        self.assertIsNone(ws[0]['used_percent'])
        self.assertIsNone(ws[0]['remaining_percent'])
        self.assertEqual(ws[1]['remaining_percent'], 90)

    def test_ls_json_stays_compatible(self):
        self.account('a', 'a@example.com')
        rc, raw = self.run_cmd('ls', '--json')
        self.assertEqual(rc, 0)
        a = json.loads(raw)['accounts'][0]
        self.assertEqual(a['usage']['windows'], [])
        self.assertEqual(set(a), {'name', 'agent', 'home', 'state', 'email', 'plan', 'until', 'usage'})

    def test_plain_ls_ignores_mode(self):
        self.account('a', 'a@example.com')
        self.run_cmd('usage', 'mode', 'remaining')
        _, text = self.run_cmd('ls')
        self.assertNotIn('LEFT', text)
        self.assertIn('ACCOUNT', text)


if __name__ == '__main__':
    unittest.main()
