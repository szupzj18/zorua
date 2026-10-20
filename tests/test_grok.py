"""Grok CLI accounts: registry, sign-in reading, usage, doctor. Isolated HOME/config, made-up
accounts, and no network: grok's billing endpoint is always mocked."""
import base64
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
import urllib.error
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SECRET = 'grok-refresh-secret-123'


def bearer(exp):
    b = lambda o: base64.urlsafe_b64encode(json.dumps(o).encode()).decode().rstrip('=')
    return b({'alg': 'none'}) + '.' + b({'exp': exp, 'tier': 1}) + '.sig'


class Answer(io.BytesIO):
    """What urlopen returns, enough for json.load."""


class Grok(unittest.TestCase):
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
                    'GROK_AUTH', 'GROK_AUTH_PATH', 'ZORUA_AUTO_GROK', 'ZORUA_EVAL_FILE'):
            os.environ.pop(var, None)
        spec = importlib.util.spec_from_file_location('grok_test_core', ROOT / 'zorua_core.py')
        self.c = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.c)
        self.c.init_registry()
        self.c.write_tsv(self.c.ACCOUNT_FILE, [])
        self.requests = []

    def grok(self, name, email='grok.user@example.com', exp=None, signed=True):
        home = self.root / ('.grok-' + name)
        home.mkdir(mode=0o700)
        if signed:
            entry = {'key': bearer(exp or time.time() + 3600), 'refresh_token': SECRET, 'email': email,
                     'expires_at': '2030-01-01T00:00:00.000000Z'}
            (home / 'auth.json').write_text(json.dumps({'https://auth.example.invalid::0000': entry}))
            (home / 'auth.json').chmod(0o600)
        self.c.write_tsv(self.c.GROK_FILE, self.c.grok_accounts() + [(name, str(home))])
        return home

    def billing(self, config):
        def urlopen(req, timeout=None):
            self.requests.append(req)
            return Answer(json.dumps({'config': config}).encode())
        return patch.object(self.c.urllib.request, 'urlopen', side_effect=urlopen)

    def run_cmd(self, *args, config=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out), self.billing(config or {}):
            rc = self.c.main(list(args))
        return rc, out.getvalue()

    def period(self, days_left=3, **extra):
        now = time.time()
        iso = lambda t: time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(t)) + '.679519+00:00'
        return dict({'currentPeriod': {'type': 'USAGE_PERIOD_TYPE_WEEKLY', 'start': iso(now - (7 - days_left) * 86400),
                                       'end': iso(now + days_left * 86400)},
                     'onDemandCap': {'val': 0}, 'prepaidBalance': {'val': 0}}, **extra)

    # ---- registry / ls ----------------------------------------------------------------

    def test_ls_lists_grok_accounts_in_their_own_section_masked(self):
        self.grok('gx', 'grok.user@example.com')
        self.grok('gy', signed=False)
        rc, out = self.run_cmd('ls')
        self.assertEqual(rc, 0)
        self.assertNotIn('Grok', out)            # headings only when more than one agent is listed
        self.c.write_tsv(self.c.ACCOUNT_FILE, [('default', str(self.root / '.codex'))])
        rc, out = self.run_cmd('ls')
        self.assertIn(' Codex\n', out)
        self.assertIn(' Grok\n', out)
        self.assertIn('gro***r@example.com', out)
        self.assertIn('(not signed in)', out)
        self.assertNotIn('grok.user@example.com', out)
        self.assertNotIn(SECRET, out)
        self.assertEqual(self.requests, [], 'ls must not call out')

    def test_json_adds_grok_accounts_without_changing_the_shape(self):
        self.c.write_tsv(self.c.ACCOUNT_FILE, [('default', str(self.root / '.codex'))])
        _, before = self.run_cmd('ls', '--json')
        self.grok('gx')
        rc, raw = self.run_cmd('ls', '--json')
        self.assertEqual(rc, 0)
        d, b = json.loads(raw), json.loads(before)
        self.assertEqual(set(d), set(b))
        codex = {a['name']: a for a in d['accounts']}['default']
        self.assertEqual(codex, b['accounts'][0])
        g = {a['name']: a for a in d['accounts']}['gx']
        self.assertEqual(set(g), set(codex))
        self.assertEqual((g['agent'], g['state'], g['email'], g['plan']), ('grok', 'ok', 'gro***r@example.com', None))
        self.assertEqual(set(g['usage']), set(codex['usage']))
        self.assertIsNone(g['usage']['relay'])
        self.assertNotIn(SECRET, raw)
        self.assertNotIn('"key"', raw)
        self.assertNotIn('grok.user@example.com', raw)
        self.assertIn('grok.user@example.com', self.run_cmd('ls', '--json', '--full')[1])

    def test_sign_in_states(self):
        home = self.grok('gx')
        self.assertEqual(self.c.read_grok_info(str(home))['state'], 'ok')
        (home / 'auth.json').write_text('{}')
        self.assertEqual(self.c.read_grok_info(str(home))['state'], 'none')
        (home / 'auth.json').write_text('not json')
        self.assertEqual(self.c.read_grok_info(str(home))['state'], 'bad')
        (home / 'auth.json').unlink()
        self.assertEqual(self.c.read_grok_info(str(home))['state'], 'none')

    def test_names_are_shared_with_codex_and_claude(self):
        self.c.write_tsv(self.c.ACCOUNT_FILE, [('work', str(self.root / '.codex-work'))])
        rc, out = self.run_cmd('add', '--grok', 'work', '--no-login')
        self.assertEqual(rc, 1)
        self.assertIn('already exists', out)
        self.assertEqual(self.run_cmd('add', '--grok', '--claude', 'x', '--no-login')[0], 1)
        self.assertEqual(self.run_cmd('add', '--claude', 'x', '--device-auth')[0], 1)

    def test_add_signs_in_through_grok_login_in_its_own_home(self):
        calls = []
        def call(argv, env=None):
            calls.append((argv, env))
            Path(env['GROK_HOME'], 'auth.json').write_text(json.dumps({'s': {'key': bearer(1), 'email': 'new@example.com'}}))
            return 0
        os.environ['GROK_AUTH_PATH'] = str(self.root / 'elsewhere.json')
        with patch.object(self.c.subprocess, 'call', side_effect=call):
            rc, out = self.run_cmd('add', '--grok', 'fresh', '--device-auth')
        self.assertEqual(rc, 0, out)
        home = str(self.root / '.grok-fresh')
        self.assertEqual(calls[0][0], ['grok', 'login', '--device-auth'])
        self.assertEqual(calls[0][1]['GROK_HOME'], home)
        self.assertNotIn('GROK_AUTH_PATH', calls[0][1])
        self.assertEqual(self.c.grok_accounts(), [('fresh', home)])
        self.assertIn('added grok account: fresh', out)

    def test_failed_login_registers_nothing(self):
        with patch.object(self.c.subprocess, 'call', return_value=1):
            rc, _ = self.run_cmd('add', '--grok', 'nope')
        self.assertEqual(rc, 1)
        self.assertEqual(self.c.grok_accounts(), [])

    def test_use_sets_grok_home_and_warns_about_overrides(self):
        home = self.grok('gx')
        os.environ['GROK_AUTH'] = '{"inline": true}'
        rc, out = self.run_cmd('use', 'gx')
        self.assertEqual(rc, 0)
        self.assertIn('GROK_AUTH is set in this shell', out)
        self.assertIn("export GROK_HOME=%s" % home, self.c.EMIT.lines)
        self.assertIn("ZORUA_PROMPT_TEXT='[grok:gx]'", self.c.EMIT.lines)

    def test_one_shot_and_env_drop_other_sign_ins(self):
        home = self.grok('gx')
        with patch.dict(os.environ, {'GROK_AUTH': 'x', 'GROK_AUTH_PATH': 'y', 'XAI_API_KEY': 'kept'}):
            env = self.c.grok_env(str(home))
        self.assertEqual(env['GROK_HOME'], str(home))
        self.assertNotIn('GROK_AUTH', env)
        self.assertNotIn('GROK_AUTH_PATH', env)
        self.assertEqual(env['XAI_API_KEY'], 'kept')
        with patch.object(self.c.os, 'execvpe', side_effect=FileNotFoundError) as ex:
            self.run_cmd('gx', '-p', 'hi')
        argv0, argv, env = ex.call_args[0]
        self.assertEqual((argv0, argv, env['GROK_HOME']), ('grok', ['grok', '-p', 'hi'], str(home)))

    def test_rm_and_binding(self):
        home = self.grok('gx')
        proj = self.root / 'proj'
        proj.mkdir()
        os.environ['PWD'] = str(proj)
        with patch.object(self.c.os, 'getcwd', return_value=str(proj)):
            self.assertEqual(self.run_cmd('bind', 'gx')[0], 0)
        self.assertEqual(self.c.binding_for(str(proj / 'sub'), 'grok'), 'gx')
        self.assertIn("ZORUA_AUTO_GROK=gx", self.c.EMIT.lines)
        self.assertEqual(self.run_cmd('rm', 'gx', '--purge')[0], 0)
        self.assertEqual(self.c.grok_accounts(), [])
        self.assertEqual(self.c.read_tsv(self.c.BINDING_FILE), [])
        self.assertFalse(home.exists())

    def test_purge_never_deletes_the_default_grok_home(self):
        default = self.root / '.grok'
        (default / 'bin').mkdir(parents=True)
        self.c.write_tsv(self.c.GROK_FILE, [('main', str(default))])
        rc, out = self.run_cmd('rm', 'main', '--purge')
        self.assertEqual(rc, 0)
        self.assertEqual(self.c.grok_accounts(), [])
        self.assertTrue((default / 'bin').is_dir())
        self.assertIn('data directory kept', out)

    # ---- usage --------------------------------------------------------------------------

    def test_usage_shows_the_weekly_period(self):
        self.grok('gx')
        rc, out = self.run_cmd('usage', config=self.period(3, creditUsagePercent=42.4))
        self.assertEqual(rc, 0)
        self.assertIn(' 42%', out)
        self.assertIn('2d2', out)       # resets in 2d23h (or 2d22h on a slow run)
        req = self.requests[0]
        self.assertEqual(req.full_url, self.c.GROK_BILLING_URL)
        self.assertTrue(req.get_header('Authorization').startswith('Bearer '))
        self.assertEqual(req.get_header('X-xai-token-auth'), 'xai-grok-cli')

    def test_unreported_share_is_unknown_not_zero(self):
        self.grok('gx')
        rc, raw = self.run_cmd('usage', '--json', config=self.period(3))
        w = json.loads(raw)['accounts'][0]['usage']['windows']
        self.assertEqual(len(w), 1)
        self.assertIsNone(w[0]['used_percent'])
        self.assertEqual(w[0]['window_seconds'], 7 * 86400)
        self.assertTrue(2 * 86400 < w[0]['reset_after_seconds'] <= 3 * 86400)
        os.unlink(self.cfg / 'usage-cache.json')
        rc, out = self.run_cmd('usage', config=self.period(3))
        self.assertNotIn('  0%', out)
        self.assertIn('not how much of it is used', out)
        os.unlink(self.cfg / 'usage-cache.json')
        rc, out = self.run_cmd('usage', '-v', config=self.period(3))
        self.assertIn('used share not reported', out)

    def test_expired_bearer_asks_for_grok_and_sends_nothing(self):
        self.grok('gx', exp=1000000000)
        rc, out = self.run_cmd('usage', config=self.period())
        self.assertIn('token expired (run grok once to refresh)', out)
        self.assertEqual(self.requests, [])

    def test_http_errors(self):
        self.grok('gx')
        def denied(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 401, 'no', {}, None)
        with patch.object(self.c.urllib.request, 'urlopen', side_effect=denied):
            self.assertEqual(self.c.fetch_grok_usage({'access_token': bearer(time.time() + 60)})[1],
                             'token expired (run grok once to refresh)')
        with patch.object(self.c.urllib.request, 'urlopen', return_value=Answer(b'[]')):
            self.assertEqual(self.c.fetch_grok_usage({'access_token': bearer(time.time() + 60)}), ({'rate_limit': {}}, None))
        with patch.object(self.c.urllib.request, 'urlopen', return_value=Answer(b'<html>')):
            self.assertIn('unexpected answer', self.c.fetch_grok_usage({'access_token': bearer(time.time() + 60)})[1])

    def test_one_request_per_email_and_separate_from_codex(self):
        self.grok('ga', 'same@example.com')
        self.grok('gb', 'same@example.com')
        self.grok('gc', 'other@example.com')
        codex = self.root / '.codex-same'
        codex.mkdir()
        claims = base64.urlsafe_b64encode(json.dumps({'email': 'same@example.com'}).encode()).decode().rstrip('=')
        (codex / 'auth.json').write_text(json.dumps({'tokens': {'id_token': 'x.' + claims + '.x', 'access_token': 'a'}}))
        self.c.write_tsv(self.c.ACCOUNT_FILE, [('cx', str(codex))])
        codex_usage = ({'rate_limit': {'primary_window': {'used_percent': 90, 'limit_window_seconds': 18000, 'reset_after_seconds': 60}}}, None)
        with patch.object(self.c, 'fetch_codex_usage', return_value=codex_usage) as fc:
            rc, raw = self.run_cmd('usage', '--json', config=self.period(3, creditUsagePercent=5))
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(fc.call_count, 1)
        used = {a['name']: [w['used_percent'] for w in a['usage']['windows']] for a in json.loads(raw)['accounts']}
        self.assertEqual(used, {'cx': [90], 'ga': [5], 'gb': [5], 'gc': [5]})

    def test_period_parsing(self):
        w = self.c.grok_windows({'currentPeriod': {'start': '2026-10-18T16:08:13Z', 'end': '2026-10-25T16:08:13+00:00'},
                                 'creditUsagePercent': 140})
        self.assertEqual(w['rate_limit']['primary_window']['limit_window_seconds'], 7 * 86400)
        self.assertEqual(w['rate_limit']['primary_window']['used_percent'], 100)
        self.assertEqual(self.c._iso_seconds('2026-10-18T18:08:13.5+02:00'), self.c._iso_seconds('2026-10-18T16:08:13Z'))
        for bad in ({}, {'currentPeriod': {'start': 'soon', 'end': 'later'}},
                    {'currentPeriod': {'start': '2026-10-25T00:00:00Z', 'end': '2026-10-18T00:00:00Z'}}):
            self.assertEqual(self.c.grok_windows(bad), {'rate_limit': {}})
        self.assertIsNone(self.c.grok_windows(dict(self.period(), creditUsagePercent=True))['rate_limit']['primary_window']['used_percent'])

    # ---- doctor -------------------------------------------------------------------------

    def doctor(self, *names):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch.object(self.c.shutil, 'which', return_value=None):
            rc = self.c.main(['doctor'] + list(names) + ['--json'])
        return rc, out.getvalue()

    def test_doctor_checks_grok_accounts_locally(self):
        self.grok('gx', exp=1000000000)          # an expired bearer is fine: grok renews it
        rc, raw = self.doctor('gx')
        checks = {c['check']: c['status'] for c in json.loads(raw)['accounts'][0]['checks']}
        self.assertEqual(rc, 0, raw)
        self.assertEqual(checks, {'directory': 'ok', 'permissions': 'ok', 'permissions:auth.json': 'ok', 'login': 'ok'})
        self.assertNotIn(SECRET, raw)
        self.assertEqual(self.requests, [])
        home = self.grok('gy', signed=False)
        rc, raw = self.doctor('gy')
        self.assertEqual(rc, 1)
        self.assertIn('zorua login gy', raw)
        (home / 'auth.json').write_text('{}')
        (home / 'auth.json').chmod(0o644)
        checks = {c['check']: c['status'] for c in json.loads(self.doctor('gy')[1])['accounts'][0]['checks']}
        self.assertEqual((checks['login'], checks['permissions:auth.json']), ('fail', 'fail'))

    def test_doctor_reads_herdr_registration_from_the_hooks_dir(self):
        home = self.grok('gx')
        seen = {}
        class Done:
            stdout = 'grok: current (v1) (%s/hooks/herdr-agent-state.sh)\n' % home
        def run(argv, env=None, **kw):
            seen['env'] = env
            return Done()
        def report():
            out = io.StringIO()
            with contextlib.redirect_stdout(out), patch.object(self.c.shutil, 'which', side_effect=lambda t: '/bin/' + t if t == 'herdr' else None), \
                    patch.object(self.c.subprocess, 'run', side_effect=run):
                self.c.main(['doctor', 'gx', '--json'])
            return {c['check']: c for c in json.loads(out.getvalue())['accounts'][0]['checks']}['herdr']
        self.assertEqual(report()['status'], 'warn')
        self.assertIn('GROK_HOME=', report()['fix'])
        (home / 'hooks').mkdir()
        (home / 'hooks' / 'herdr.json').write_text('{"hooks": {"SessionStart": [{"command": "./herdr-agent-state.sh"}]}}')
        self.assertEqual(report()['status'], 'ok')
        self.assertEqual(seen['env']['GROK_HOME'], str(home))


if __name__ == '__main__':
    unittest.main()
