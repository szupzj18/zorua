"""Recommendation checks use an isolated HOME/config and mocked usage/Claude CLI."""
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


class Pick(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = self.root / 'cfg'
        self.cfg.mkdir()
        env = patch.dict(os.environ, {'HOME': str(self.root), 'ZORUA_CONFIG_DIR': str(self.cfg)})
        env.start()
        self.addCleanup(env.stop)
        spec = importlib.util.spec_from_file_location('pick_test_core', ROOT / 'zorua_core.py')
        self.c = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.c)
        self.c.init_registry()
        self.c.write_tsv(self.c.ACCOUNT_FILE, [])

    def jwt(self, email, exp=None):
        claims = {'email': email}
        if exp is not None:
            claims['exp'] = exp
        return 'x.' + base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=') + '.x'

    def account(self, name, email=None, exp=None):
        home = self.root / name
        home.mkdir()
        if email:
            (home / 'auth.json').write_text(json.dumps({'tokens': {'id_token': self.jwt(email, exp), 'access_token': 'fake-' + name}}))
        self.c.write_tsv(self.c.ACCOUNT_FILE, self.c.accounts() + [(name, str(home))])
        return home

    def usage(self, weekly, short=10):
        return {'rate_limit': {'primary_window': {'used_percent': short, 'limit_window_seconds': 18000, 'reset_after_seconds': 1000},
                               'secondary_window': {'used_percent': weekly, 'limit_window_seconds': 604800, 'reset_after_seconds': 2000}}}, None

    def run_cmd(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = self.c.main(list(args))
        return rc, out.getvalue()

    def pick(self, *args):
        rc, raw = self.run_cmd('pick', '--kind', 'codex', '--json', *args)
        return rc, json.loads(raw)

    def test_tag_changes_persist_and_validation_is_atomic(self):
        self.account('work')
        self.assertEqual(self.run_cmd('tag', 'work', '+internal', '+browser')[0], 0)
        self.run_cmd('tag', 'work', '-browser', '+internal')
        self.assertIn('work: internal', self.run_cmd('tag', 'work')[1])
        before = (self.cfg / 'tags.json').read_bytes()
        self.assertEqual(self.run_cmd('tag', 'work', '+external', 'bad')[0], 1)
        self.assertEqual((self.cfg / 'tags.json').read_bytes(), before)
        self.assertEqual(self.run_cmd('tag', 'missing', '+internal')[0], 1)
        self.assertEqual((self.cfg / 'tags.json').stat().st_mode & 0o777, 0o600)

    def test_weekly_then_short_ranking_and_shared_email_pool(self):
        for n, e in [('one', 'same@example.com'), ('duplicate', 'SAME@example.com'), ('two', 'two@example.com'), ('three', 'three@example.com')]:
            self.account(n, e)
        def fetch(tokens):
            name = tokens['access_token'][5:]
            return self.usage(20, 30 if name == 'two' else 10) if name != 'three' else self.usage(30, 0)
        with patch.object(self.c, 'fetch_codex_usage', side_effect=fetch) as req:
            rc, d = self.pick()
        self.assertEqual(rc, 0)
        self.assertEqual(d['recommendation']['name'], 'duplicate')
        self.assertEqual([a['name'] for a in d['alternatives']], ['two', 'three'])
        self.assertEqual(req.call_count, 3)
        self.assertTrue(any('shared email' in ' '.join(a['reasons']) for a in d['excluded']))

    def test_tags_and_kind_filter_before_requests(self):
        self.account('work', 'work@example.com')
        self.account('personal', 'personal@example.com')
        self.run_cmd('tag', 'work', '+internal', '+browser')
        self.run_cmd('tag', 'personal', '+internal', '+external')
        self.c.write_tsv(self.c.CLAUDE_FILE, [('claude1', str(self.root))])
        with patch.object(self.c, 'fetch_codex_usage', return_value=self.usage(30)) as req, patch.object(self.c, 'claude_status') as status:
            rc, d = self.pick('--needs', 'internal,browser', '--avoid', 'external')
        self.assertEqual(d['recommendation']['name'], 'work')
        self.assertEqual(req.call_count, 1)
        status.assert_not_called()
        self.assertEqual(len(d['excluded']), 2)

    def test_unsigned_expired_and_exhausted_excluded(self):
        self.account('unsigned')
        self.account('expired', 'old@example.com', time.time() - 10)
        self.account('empty', 'empty@example.com')
        with patch.object(self.c, 'fetch_codex_usage', return_value=self.usage(100)):
            rc, d = self.pick()
        self.assertEqual(rc, 1)
        self.assertIsNone(d['recommendation'])
        self.assertEqual({x['name'] for x in d['excluded']}, {'unsigned', 'expired', 'empty'})
        self.assertIn('token expired', json.dumps(d))

    def test_unknown_quota_ranks_last_and_failure_reasons_text(self):
        self.account('known', 'known@example.com')
        self.account('unknown', 'unknown@example.com')
        with patch.object(self.c, 'fetch_codex_usage', side_effect=lambda tok: self.usage(95) if tok['access_token'] == 'fake-known' else (None, 'offline')):
            rc, d = self.pick()
        self.assertEqual(d['recommendation']['name'], 'known')
        self.assertIsNone(d['alternatives'][0]['remaining']['7d'])
        rc, text = self.run_cmd('pick', '--kind', 'codex', '--needs', 'missing')
        self.assertEqual(rc, 1)
        self.assertIn('known: missing tags: missing', text)
        self.assertIn('unknown: missing tags: missing', text)

    def test_provider_model_prefix_and_no_extra_requests(self):
        p = self.c.zp.build_codex('https://relay.example', 'fake-provider-key', 'vendor/model-a')
        p['models'] = {'a': 'vendor/model-a', 'b': 'vendor/model-b'}
        self.c.zp.save(str(self.cfg), {'relay': p})
        self.run_cmd('tag', 'relay', '+external')
        with patch.object(self.c.zp, 'fetch_models', side_effect=AssertionError('no requests')), patch.object(self.c.zp, 'check', side_effect=AssertionError('no requests')):
            rc, d = self.pick('--model', 'vendor/model-a', '--needs', 'external')
            self.assertEqual(d['recommendation']['name'], 'relay:vendor/model-a')
            self.assertNotIn('fake-provider-key', json.dumps(d))
            self.assertEqual(self.pick('--model', 'vendor/model-')[0], 1)
            self.assertEqual(self.pick('--model', 'a')[0], 0)
            self.assertEqual(self.pick('--avoid', 'external')[0], 1)

    def test_account_model_catalog_and_missing_model(self):
        home = self.account('work', 'work@example.com')
        (home / 'models_cache.json').write_text(json.dumps({'models': [{'slug': 'gpt-example'}]}))
        with patch.object(self.c, 'fetch_codex_usage', return_value=self.usage(10)):
            self.assertEqual(self.pick('--model', 'gpt-ex')[0], 0)
            self.assertEqual(self.pick('--model', 'no-such')[0], 1)
        (home / 'models_cache.json').unlink()
        (home / 'config.toml').write_text('model = "gpt-configured"\n')
        with patch.object(self.c, 'fetch_codex_usage', return_value=self.usage(10)):
            self.assertEqual(self.pick('--model', 'gpt-conf')[0], 0)

    def test_pick_and_usage_share_cache_and_redact_tokens(self):
        self.account('work', 'work@example.com')
        with patch.object(self.c, 'fetch_codex_usage', return_value=self.usage(15)) as fetch:
            self.pick()
            self.run_cmd('usage', '--json')
            self.pick()
            self.assertEqual(fetch.call_count, 1)
        raw = (self.cfg / 'usage-cache.json').read_text()
        self.assertNotIn('fake-work', raw)
        self.assertNotIn('id_token', raw)
        data = json.loads(raw)
        for cached in data.values():
            cached['at'] -= 31
        (self.cfg / 'usage-cache.json').write_text(json.dumps(data))
        with patch.object(self.c, 'fetch_codex_usage', return_value=self.usage(15)) as fetch:
            self.pick()
            self.assertEqual(fetch.call_count, 1)

    def test_claude_usage_cache_expired_window_and_login(self):
        h = self.root / 'claude-home'
        h.mkdir()
        self.c.write_tsv(self.c.CLAUDE_FILE, [('alt', str(h))])
        (h / '.zorua-usage.json').write_text(json.dumps({'updated_at': time.time(), 'five_hour': {'used_percentage': 100, 'resets_at': time.time()-1}, 'seven_day': {'used_percentage': 15, 'resets_at': time.time()+1000}}))
        with patch.object(self.c, 'claude_status', return_value={'loggedIn': True, 'email': 'claude@example.com'}), patch.object(self.c, 'fetch_codex_usage', side_effect=AssertionError('no network')):
            rc, text = self.run_cmd('pick', '--kind', 'claude', '--json')
        d = json.loads(text)
        self.assertEqual(rc, 0)
        self.assertEqual(d['recommendation']['remaining'], {'7d': 85, '5h': None})
        with patch.object(self.c, 'claude_status', return_value={'loggedIn': False}):
            self.assertEqual(self.run_cmd('pick', '--kind', 'claude', '--json')[0], 1)

    def test_removed_names_do_not_pass_labels_to_new_accounts(self):
        self.account('work')
        self.run_cmd('tag', 'work', '+internal')
        self.assertEqual(self.run_cmd('rm', 'work')[0], 0)
        self.assertNotIn('work', self.c.load_tags())
        p = self.c.zp.build_codex('https://relay.example', 'dummy', 'm')
        self.c.zp.save(str(self.cfg), {'relay': p})
        self.run_cmd('tag', 'relay', '+external')
        self.assertEqual(self.run_cmd('provider', 'rm', 'relay')[0], 0)
        self.assertNotIn('relay', self.c.load_tags())

    def test_pick_has_no_shell_side_effect_and_strict_options(self):
        for args in [('pick',), ('pick', '--kind', 'x'), ('pick', '--kind', 'codex', '--bogus'), ('pick', '--kind')]:
            self.assertEqual(self.run_cmd(*args)[0], 1)
        self.assertFalse(self.c.EMIT.lines)
        self.assertFalse(self.c.valid_name('pick'))
        self.assertFalse(self.c.valid_name('tag'))


if __name__ == '__main__':
    unittest.main()
