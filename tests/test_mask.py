"""Email masking in ls / usage / pick, with an isolated HOME/config and a mocked Claude CLI."""
import base64
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


class Mask(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = self.root / 'cfg'
        self.cfg.mkdir()
        env = patch.dict(os.environ, {'HOME': str(self.root), 'ZORUA_CONFIG_DIR': str(self.cfg)})
        env.start()
        self.addCleanup(env.stop)
        for var in ('ZORUA_FULL_EMAIL', 'ZORUA_COLOR', 'CX_COLOR', 'CODEX_HOME', 'CLAUDE_CONFIG_DIR'):
            os.environ.pop(var, None)
        spec = importlib.util.spec_from_file_location('mask_test_core', ROOT / 'zorua_core.py')
        self.c = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.c)
        self.c.init_registry()
        self.c.write_tsv(self.c.ACCOUNT_FILE, [])
        self.status = {}

    def codex(self, name, email=None):
        home = self.root / name
        home.mkdir()
        if email:
            claims = base64.urlsafe_b64encode(json.dumps({'email': email}).encode()).decode().rstrip('=')
            (home / 'auth.json').write_text(json.dumps({'tokens': {'id_token': 'x.' + claims + '.x', 'access_token': 'fake-' + name}}))
        self.c.write_tsv(self.c.ACCOUNT_FILE, self.c.accounts() + [(name, str(home))])

    def claude(self, name, status):
        home = self.root / ('claude-' + name)
        home.mkdir()
        self.status[str(home)] = status
        self.c.write_tsv(self.c.CLAUDE_FILE, self.c.claude_accounts() + [(name, str(home))])

    def run_cmd(self, *args):
        out = io.StringIO()
        usage = ({'rate_limit': {'secondary_window': {'used_percent': 10, 'limit_window_seconds': 604800, 'reset_after_seconds': 1000}}}, None)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out), \
                patch.object(self.c, 'claude_status', side_effect=lambda home: self.status[home]), \
                patch.object(self.c, 'fetch_codex_usage', return_value=usage):
            rc = self.c.main(list(args))
        return rc, out.getvalue()

    def emails(self, *args):
        rc, raw = self.run_cmd(*args)
        self.assertEqual(rc, 0)
        return {a['name']: a['email'] for a in json.loads(raw)['accounts']}

    def test_rule_keeps_domain_and_hides_the_middle(self):
        cases = {
            'jonathan.smith@gmail.com': 'jon***h@gmail.com',     # 8+ characters: first 3 and last
            'alice@example.edu': 'al***e@example.edu',      # 5-7: first 2 and last
            'work@example.com': 'w***k@example.com',              # 4 or fewer: at most half shown
            'abc@example.com': 'a***@example.com',
            'ab@example.com': 'a***@example.com',
            'a@example.com': '***@example.com',
        }
        for email, masked in cases.items():
            self.assertEqual(self.c.mask_email(email), masked)
            self.assertEqual(self.c.email_masker([email])(email), masked)

    def test_lookalikes_reveal_more_until_they_differ(self):
        hide = self.c.email_masker(['dev.alpha1@example.com', 'dev.bravo1@example.com', 'dev.bravo1@example.com',
                                    'dev.charlie1@other.org', 'alice@example.edu'])
        self.assertEqual(hide('dev.alpha1@example.com'), 'dev.a***1@example.com')
        self.assertEqual(hide('dev.bravo1@example.com'), 'dev.b***1@example.com')
        self.assertEqual(hide('dev.charlie1@other.org'), 'dev***1@other.org')     # the domain already tells it apart
        self.assertEqual(hide('alice@example.edu'), 'al***e@example.edu')
        short = self.c.email_masker(['ab@x.io', 'ac@x.io', 'abc@x.io', 'abd@x.io'])
        masks = [short(e) for e in ('ab@x.io', 'ac@x.io', 'abc@x.io', 'abd@x.io')]
        self.assertEqual(len(set(masks)), 4)
        self.assertTrue(all('***@x.io' in m for m in masks))

    def test_names_that_are_not_emails_pass_through(self):
        values = ['(signed in)', 'Acme Corp', 'google-oauth2|1234567890', 'claude auth status failed: timeout', None, '']
        hide = self.c.email_masker(values)
        for s in values:
            self.assertEqual(hide(s), s)
        self.assertEqual(hide("alice@example.edu's Organization"), "al***e@example.edu's Organization")

    def test_full_flag_and_environment_variable(self):
        email = 'jonathan.smith@gmail.com'
        self.assertEqual(self.c.email_masker([email], full=True)(email), email)
        with patch.dict(os.environ, {'ZORUA_FULL_EMAIL': '1'}):
            self.assertEqual(self.c.email_masker([email])(email), email)

    def test_ls_masks_both_agents_and_keeps_placeholders(self):
        self.codex('work', 'jonathan.smith@gmail.com')
        self.codex('spare')
        self.claude('official', {'loggedIn': True, 'email': 'alice@example.edu', 'subscriptionType': 'max'})
        self.claude('mayday', {'loggedIn': True, 'email': 'alice@example.edu', 'subscriptionType': 'max'})
        self.claude('signedout', {'loggedIn': False})
        self.claude('org', {'loggedIn': True, 'orgName': 'Acme Corp'})
        rc, out = self.run_cmd('ls')
        self.assertEqual(rc, 0)
        self.assertIn('jon***h@gmail.com', out)
        self.assertEqual(out.count('al***e@example.edu'), 2)     # one address keeps one mask
        self.assertEqual(out.count('(not signed in)'), 2)
        self.assertIn('Acme Corp', out)
        self.assertNotIn('jonathan.smith', out)
        self.assertNotIn('alice@', out)
        rc, out = self.run_cmd('ls', '-v')
        self.assertIn('jon***h@gmail.com', out)
        self.assertNotIn('jonathan.smith', out)
        rc, out = self.run_cmd('ls', '--full')
        self.assertIn('jonathan.smith@gmail.com', out)
        self.assertIn('alice@example.edu', out)

    def test_lookalikes_across_codex_and_claude_in_one_list(self):
        self.codex('a', 'dev.alpha1@example.com')
        self.claude('b', {'loggedIn': True, 'email': 'dev.bravo1@example.com'})
        rc, out = self.run_cmd('ls')
        self.assertIn('dev.a***1@example.com', out)
        self.assertIn('dev.b***1@example.com', out)

    def test_json_is_masked_unless_full(self):
        self.codex('work', 'jonathan.smith@gmail.com')
        self.codex('spare')
        self.claude('mayday', {'loggedIn': True, 'email': 'alice@example.edu'})
        masked = {'work': 'jon***h@gmail.com', 'spare': None, 'mayday': 'al***e@example.edu'}
        self.assertEqual(self.emails('ls', '--json'), masked)
        self.assertEqual(self.emails('usage', '--json'), masked)
        full = {'work': 'jonathan.smith@gmail.com', 'spare': None, 'mayday': 'alice@example.edu'}
        self.assertEqual(self.emails('ls', '--json', '--full'), full)
        with patch.dict(os.environ, {'ZORUA_FULL_EMAIL': '1'}):
            self.assertEqual(self.emails('usage', '--json'), full)

    def test_pick_pools_on_the_real_address_and_masks_output(self):
        self.codex('work', 'jonathan.smith@gmail.com')
        self.codex('side', 'alice@example.edu')
        self.codex('copy', 'alice@example.edu')
        rc, raw = self.run_cmd('pick', '--kind', 'codex', '--json')
        d = json.loads(raw)
        self.assertEqual(rc, 0)
        shown = [c['email'] for c in [d['recommendation']] + d['alternatives']]
        self.assertEqual(sorted(shown), ['al***e@example.edu', 'jon***h@gmail.com'])
        self.assertTrue(any('shared email' in ' '.join(x['reasons']) for x in d['excluded']))
        self.assertNotIn('alice@', raw)
        rc, raw = self.run_cmd('pick', '--kind', 'codex', '--json', '--full')
        self.assertIn('jonathan.smith@gmail.com', raw)

    def test_unknown_option_mentions_full(self):
        rc, out = self.run_cmd('ls', '--bogus')
        self.assertEqual(rc, 1)
        self.assertIn('--full', out)


if __name__ == '__main__':
    unittest.main()
