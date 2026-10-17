"""Doctor is read-only even on first run; all accounts and tools here are synthetic."""
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
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class Doctor(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = self.root / 'cfg'
        env = patch.dict(os.environ, {'HOME': str(self.root), 'ZORUA_CONFIG_DIR': str(self.cfg)})
        env.start()
        self.addCleanup(env.stop)
        spec = importlib.util.spec_from_file_location('doctor_test_core', ROOT / 'zorua_core.py')
        self.c = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.c)
        self.tools = patch.object(self.c.shutil, 'which', return_value=None)
        self.tools.start()
        self.addCleanup(self.tools.stop)

    def account(self, name='work', kind='codex', signed=True):
        h = self.root / name
        h.mkdir(mode=0o700)
        path = self.c.ACCOUNT_FILE if kind == 'codex' else self.c.CLAUDE_FILE
        self.c.write_tsv(path, self.c.read_tsv(path) + [(name, str(h))])
        if kind == 'codex' and signed:
            (h / 'auth.json').write_text(json.dumps({'tokens': {'access_token': 'dummy-secret-token'}}))
            (h / 'auth.json').chmod(0o600)
        return h

    def run_cmd(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = self.c.main(['doctor'] + list(args))
        return rc, out.getvalue()

    def report(self, *args):
        rc, raw = self.run_cmd(*args, '--json')
        return rc, json.loads(raw)

    def checks(self, report):
        return {c['check']: c for c in report['accounts'][0]['checks']}

    def snapshot(self):
        return {str(p.relative_to(self.root)): (p.read_bytes(), p.stat().st_mode) for p in self.root.rglob('*') if p.is_file()}

    def test_first_run_never_initializes_or_migrates(self):
        legacy = self.root / 'codex-switch'
        legacy.mkdir()
        (legacy / 'accounts.tsv').write_text('legacy\t/tmp/unused\n')
        before = self.snapshot()
        rc, d = self.report()
        self.assertEqual(rc, 1)
        self.assertEqual(d['accounts'][0]['name'], 'default')
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.cfg.exists())
        self.assertFalse(self.c.EMIT.lines)

    def test_healthy_account_and_no_secret_output(self):
        self.account()
        before = self.snapshot()
        rc, d = self.report('work')
        self.assertEqual(rc, 0)
        self.assertTrue(all(c['status'] == 'ok' for c in d['accounts'][0]['checks']))
        self.assertNotIn('dummy-secret-token', json.dumps(d))
        self.assertEqual(self.snapshot(), before)

    def test_missing_directory_and_unsigned_account(self):
        h = self.account(signed=False)
        h.rmdir()
        rc, d = self.report('work')
        self.assertEqual(rc, 1)
        checks = self.checks(d)
        self.assertEqual(checks['directory']['status'], 'fail')
        self.assertEqual(checks['login']['status'], 'fail')
        self.assertIn('zorua login work', checks['login']['fix'])

    def test_permissions_report_repairs_without_chmod(self):
        h = self.account()
        h.chmod(0o777)
        (h / 'auth.json').chmod(0o644)
        before = self.snapshot()
        rc, d = self.report('work')
        self.assertEqual(rc, 1)
        checks = self.checks(d)
        self.assertIn('chmod 700', checks['permissions']['fix'])
        self.assertIn('chmod 600', checks['permissions:auth.json']['fix'])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(h.stat().st_mode & 0o777, 0o777)

    def test_claude_proxy_must_be_in_account_settings(self):
        h = self.account('alt', 'claude')
        with patch.object(self.c, 'claude_status', return_value={'loggedIn': True}), patch.dict(os.environ, {'HTTPS_PROXY': 'http://shell-proxy'}):
            rc, d = self.report('alt')
            self.assertEqual(rc, 0)
            self.assertEqual(self.checks(d)['proxy']['status'], 'warn')
            (h / 'settings.json').write_text(json.dumps({'env': {'HTTPS_PROXY': 'http://dummy:fake-password@proxy'}}))
            (h / 'settings.json').chmod(0o600)
            rc, d = self.report('alt')
            self.assertEqual(self.checks(d)['proxy']['status'], 'ok')
            self.assertNotIn('fake-password', json.dumps(d))

    def test_claude_unsigned_and_bad_settings_do_not_echo_contents(self):
        h = self.account('alt', 'claude')
        (h / 'settings.json').write_text('{ broken dummy-secret-value')
        with patch.object(self.c, 'claude_status', return_value={'loggedIn': False}):
            rc, d = self.report('alt')
        self.assertEqual(rc, 1)
        self.assertEqual(self.checks(d)['settings']['status'], 'fail')
        self.assertNotIn('dummy-secret-value', json.dumps(d))

    def test_expired_codex_credentials(self):
        h = self.account()
        payload = base64.urlsafe_b64encode(json.dumps({'exp': time.time()-1}).encode()).decode().rstrip('=')
        (h / 'auth.json').write_text(json.dumps({'tokens': {'access_token': 'x.'+payload+'.x'}}))
        rc, d = self.report('work')
        self.assertEqual(rc, 1)
        self.assertIn('expired', self.checks(d)['login']['detail'])

    def test_hook_trust_and_changed_definition(self):
        h = self.account()
        hooks = {'hooks': {'SessionStart': [{'hooks': [{'type': 'command', 'command': 'echo ready'}]}]}}
        (h / 'hooks.json').write_text(json.dumps(hooks))
        rc, d = self.report('work')
        self.assertEqual(rc, 1)
        self.assertIn('1 untrusted', self.checks(d)['hooks']['detail'])
        # Canonical sorted JSON identity, following upstream config/fingerprint.rs.
        trusted = 'sha256:87c374b533ab1bbae9635a74b8ef964a50410f82bd6e1c2b9e7ae54bb2a87b99'
        key = str(h / 'hooks.json') + ':session_start:0:0'
        (h / 'config.toml').write_text('[hooks.state.'+json.dumps(key)+']\ntrusted_hash = "'+trusted+'"\n')
        rc, d = self.report('work')
        self.assertEqual(rc, 0)
        self.assertIn('trust hashes match', self.checks(d)['hooks']['detail'])
        hooks['hooks']['SessionStart'][0]['hooks'][0]['command'] = 'echo changed'
        (h / 'hooks.json').write_text(json.dumps(hooks))
        rc, d = self.report('work')
        self.assertEqual(rc, 1)
        self.assertIn('1 modified', self.checks(d)['hooks']['detail'])

    def test_disabled_hook_needs_no_trust_and_malformed_hook_fails(self):
        h = self.account()
        (h / 'hooks.json').write_text(json.dumps({'hooks': {'Stop': [{'hooks': [{'type': 'command', 'command': 'echo stop'}]}]}}))
        key = str(h / 'hooks.json') + ':stop:0:0'
        (h / 'config.toml').write_text('[hooks.state.'+json.dumps(key)+']\nenabled = false\n')
        self.assertEqual(self.report('work')[0], 0)
        (h / 'hooks.json').write_text('not json')
        self.assertEqual(self.report('work')[0], 1)

    def test_optional_herdr_and_browser_check_correct_home(self):
        h = self.account()
        # A notify hook registers Herdr without requiring hook trust.
        (h / 'config.toml').write_text('notify = ["herdr-agent-state.sh"]\n[plugins."browser@openai"]\nenabled = true\n')
        self.tools.stop()
        with patch.object(self.c.shutil, 'which', return_value='/fake/tool'), patch.object(self.c.subprocess, 'run', return_value=types.SimpleNamespace(returncode=0, stdout='codex: current (v8) (dummy)\n')) as run:
            rc, d = self.report('work')
        self.assertEqual(self.checks(d)['herdr']['status'], 'ok')
        self.assertEqual(self.checks(d)['browser']['status'], 'ok')
        self.assertEqual(run.call_args[0][0], ['herdr', 'integration', 'status'])
        self.assertEqual(run.call_args[1]['env']['CODEX_HOME'], str(h))
        self.tools.start()

    def test_provider_reuses_check_and_keeps_config_unchanged(self):
        self.cfg.mkdir()
        self.c.zp.save(str(self.cfg), {'relay': self.c.zp.build_codex('https://relay.example', 'dummy-provider-key', 'm')})
        before = self.snapshot()
        result = {'status': 'fail', 'detail': 'key rejected (HTTP 401)', 'ms': 1, 'http': 401, 'via': 'GET /models'}
        with patch.object(self.c.zp, 'check', return_value=result) as check:
            rc, d = self.report('relay')
        self.assertEqual(rc, 1)
        self.assertEqual(check.call_count, 1)
        self.assertNotIn('dummy-provider-key', json.dumps(d))
        self.assertEqual(self.snapshot(), before)

    def test_claude_provider_following_an_unlisted_model_warns(self):
        self.cfg.mkdir()
        p = {'env': self.c.zp.build_env('https://relay.example', 'dummy-provider-key', models=[]),
             'models': {'opus': 'relay/opus'}}
        self.c.zp.save(str(self.cfg), {'relay': p})
        claude_dir = self.root / '.claude'
        claude_dir.mkdir()
        ok = {'status': 'ok', 'detail': 'key accepted', 'ms': 1, 'http': 200, 'via': 'GET /models'}
        with patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(claude_dir)}), \
                patch.object(self.c.zp, 'check', return_value=ok):
            p['env']['ANTHROPIC_DEFAULT_SONNET_MODEL'] = 'relay/sonnet'
            self.c.zp.save(str(self.cfg), {'relay': p})
            for saved, expect in [('claude-elsewhere', 'warn'), ('opus', None), ('sonnet[1m]', None), ('haiku', 'warn')]:
                (claude_dir / 'settings.json').write_text(json.dumps({'model': saved}))
                rc, d = self.report('relay')
                self.assertEqual(rc, 0)
                self.assertEqual(self.checks(d).get('model', {}).get('status'), expect)
            p['env']['ANTHROPIC_MODEL'] = 'relay/opus'       # a provider default ignores /model
            self.c.zp.save(str(self.cfg), {'relay': p})
            (claude_dir / 'settings.json').write_text(json.dumps({'model': 'claude-elsewhere'}))
            self.assertNotIn('model', self.checks(self.report('relay')[1]))

    def test_matcher_hash_respects_event_semantics_and_whitespace(self):
        handler = {'type': 'command', 'command': 'echo ready'}
        # Stop ignores matchers upstream; tool events preserve the declared pattern.
        plain = self.c._doctor_hook_hash('Stop', {}, handler)
        self.assertEqual(plain, self.c._doctor_hook_hash('Stop', {'matcher': 'Bash'}, handler))
        self.assertNotEqual(self.c._doctor_hook_hash('PreToolUse', {'matcher': ' Bash '}, handler),
                            self.c._doctor_hook_hash('PreToolUse', {'matcher': 'Bash'}, handler))

    def test_named_selection_unknown_and_text_repairs(self):
        self.account('one')
        self.account('two', signed=False)
        rc, d = self.report('one')
        self.assertEqual([r['name'] for r in d['accounts']], ['one'])
        self.assertEqual(self.run_cmd('missing')[0], 1)
        self.assertEqual(self.run_cmd('--bogus')[0], 1)
        rc, text = self.run_cmd('two')
        self.assertEqual(rc, 1)
        self.assertIn('✗ login:', text)
        self.assertIn('fix: zorua login two', text)
        self.assertFalse(self.c.valid_name('doctor'))


if __name__ == '__main__':
    unittest.main()
