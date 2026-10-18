"""No real credentials: temporary HOME/config and a PATH security double on every platform."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import zorua_providers as zp


class ProviderKeys(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = self.root / 'cfg'
        self.cfg.mkdir()
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        shutil.copy(ROOT / 'tests/fake-security', self.bin / 'security')
        (self.bin / 'security').chmod(0o755)
        self.env = patch.dict(os.environ, {
            'HOME': str(self.root), 'ZORUA_CONFIG_DIR': str(self.cfg),
            'ZORUA_TEST_KEYCHAIN': str(self.root / 'keychain.json'),
            'PATH': str(self.bin) + os.pathsep + os.environ['PATH'],
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.platform = patch.object(sys, 'platform', 'darwin')
        self.platform.start()
        self.addCleanup(self.platform.stop)
        self.key = 'dummy-provider-credential-123456789'
        self.claude = {'env': zp.build_env('https://relay.example', self.key, models=['default=m'])}
        self.codex = zp.build_codex('https://relay.example', self.key, 'm')

    def test_store_both_agents_and_lazy_load(self):
        for name, p in [('cc', self.claude), ('cx', self.codex)]:
            stored = zp.store_key(name, p, self.key)
            self.assertNotIn(self.key, json.dumps(stored))
            self.assertEqual(stored['key_ref'], 'keychain')
            self.assertEqual(zp.secret(zp.resolve_key(name, stored)), self.key)
            zp.save(str(self.cfg), {name: stored})
            with patch.object(zp, '_security', side_effect=AssertionError('listing must be lazy')):
                self.assertEqual(zp.load(str(self.cfg))[name], stored)
                self.assertNotIn(self.key, json.dumps(zp.document(stored, reveal=True)))

    def test_store_key_keeps_the_key_out_of_every_argv(self):
        key = 'k ey "q" \'s\' \\back\\ $HOME `x` ;#*!'
        calls = []
        real = subprocess.run
        def run(argv, **kw):
            calls.append((argv, kw.get('input')))
            return real(argv, **kw)
        with patch.object(subprocess, 'run', side_effect=run):
            for name, p in [('cc', self.claude), ('cx', self.codex)]:
                stored = zp.store_key(name, p, key)
                self.assertEqual(zp.secret(zp.resolve_key(name, stored)), key)
        self.assertEqual(len(calls), 4)
        for argv, stdin in calls:
            joined = ' '.join(argv)
            for secret in (key, key.encode().hex(), self.key):
                self.assertNotIn(secret, joined)
        self.assertEqual(calls[0][0], ['security', '-i'])
        self.assertIn(key.encode().hex(), calls[0][1])

    def test_keys_the_keychain_tool_cannot_round_trip_are_refused(self):
        for key in ('caf\u00e9-dummy-key', 'x' * 2000):
            with patch.object(zp, '_security', side_effect=AssertionError('must not reach security')):
                with self.assertRaises(RuntimeError) as cm:
                    zp.store_key('relay', self.codex, key)
            self.assertNotIn(key, str(cm.exception))

    def test_unavailable_and_linux_fallback_are_private(self):
        for target, val in [('platform', 'linux'), ('platform', 'darwin')]:
            with patch.object(sys, target, val), patch.dict(os.environ, {'ZORUA_TEST_KEYCHAIN_FAIL': '1'}):
                p = zp.store_key('relay', self.claude, self.key)
                self.assertEqual(zp.secret(p), self.key)
                self.assertNotIn('key_ref', p)
                zp.save(str(self.cfg), {'relay': p})
                self.assertEqual((self.cfg / 'providers.json').stat().st_mode & 0o777, 0o600)
                self.assertIn('providers.json', zp.key_storage(p))

    def test_existing_public_temp_file_cannot_keep_bad_permissions(self):
        (self.cfg / 'providers.json.tmp').write_text('old')
        (self.cfg / 'providers.json.tmp').chmod(0o666)
        zp.save(str(self.cfg), {'relay': self.codex})
        self.assertEqual((self.cfg / 'providers.json').stat().st_mode & 0o777, 0o600)

    def test_locked_reference_fails_closed(self):
        stored = zp.store_key('relay', self.codex, self.key)
        with patch.dict(os.environ, {'ZORUA_TEST_KEYCHAIN_FAIL': '1'}):
            result = zp.check_stored('relay', stored)
            self.assertEqual(result['status'], 'fail')
            self.assertNotIn(self.key, json.dumps(result))

    def test_migration_dry_run_backup_and_idempotence(self):
        cfg = str(self.cfg)
        zp.save(cfg, {'cc': self.claude, 'cx': self.codex})
        original = (self.cfg / 'providers.json').read_bytes()
        self.assertEqual(zp.migrate_keys(cfg, True), ['cc', 'cx'])
        self.assertEqual((self.cfg / 'providers.json').read_bytes(), original)
        self.assertFalse((self.root / 'keychain.json').exists())
        self.assertFalse(list(self.cfg.glob('*.bak')))
        self.assertEqual(zp.migrate_keys(cfg), ['cc', 'cx'])
        backup, = self.cfg.glob('*.bak')
        self.assertEqual(backup.read_bytes(), original)
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(self.key, (self.cfg / 'providers.json').read_text())
        self.assertEqual(zp.migrate_keys(cfg), [])
        self.assertEqual(backup.read_bytes(), original)

    def test_failed_migration_preserves_original(self):
        zp.save(str(self.cfg), {'cc': self.claude})
        original = (self.cfg / 'providers.json').read_bytes()
        with patch.dict(os.environ, {'ZORUA_TEST_KEYCHAIN_FAIL': '1'}):
            with self.assertRaisesRegex(RuntimeError, 'original file kept'):
                zp.migrate_keys(str(self.cfg))
        self.assertEqual((self.cfg / 'providers.json').read_bytes(), original)

    def core(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('keys_test_core', ROOT / 'zorua_core.py')
        core = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(core)
        core.init_registry()
        return core

    def test_commands_never_print_keys_even_reveal_json_and_echoed_errors(self):
        c = self.core()
        zp.save(str(self.cfg), {'cc': self.claude, 'cx': self.codex})
        cmds = [[], ['ls', '--json'], ['usage', '--json'], ['provider', 'ls'],
                ['provider', 'show', 'cc'], ['provider', 'show', 'cx'],
                ['provider', 'get', 'cc', '--reveal'], ['provider', 'get', 'cx', '--reveal'],
                ['provider', 'models', 'cc'], ['provider', 'migrate-keys', '--dry-run'],
                ['provider', 'check', '--json'], ['provider', 'check'], ['use', 'cc']]
        with patch.object(c, 'claude_status', return_value={'loggedIn': False}), \
             patch.object(zp, '_send', return_value=(402, 'Insufficient balance ' + self.key, 1)):
            for cmd in cmds:
                out = io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                    c.main(cmd)
                self.assertNotIn(self.key, out.getvalue(), cmd)

    def test_set_key_from_stdin_then_check_new_key(self):
        c = self.core()
        zp.save(str(self.cfg), {'cc': self.claude})
        seen = []
        def send(req, timeout):
            seen.append(req.get_header('Authorization'))
            return 200, '{}', 1
        with patch.object(sys, 'stdin', io.StringIO('rotated-dummy-key\n')), patch.object(zp, '_send', side_effect=send), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(c.main(['provider', 'set-key', 'cc']), 0)
        self.assertEqual(seen, ['Bearer rotated-dummy-key'])
        self.assertNotIn('rotated-dummy-key', (self.cfg / 'providers.json').read_text())

    def test_interactive_key_uses_getpass(self):
        c = self.core()
        with patch.object(sys.stdin, 'isatty', return_value=True), patch('getpass.getpass', return_value=self.key):
            self.assertEqual(c.read_key('', ''), self.key)

    def test_edit_masked_key_and_launch_hydration(self):
        c = self.core()
        p = zp.store_key('cc', self.claude, self.key)
        zp.save(str(self.cfg), {'cc': p})
        doc = zp.document(p)
        doc['env']['ANTHROPIC_BASE_URL'] = 'https://new.example'
        with patch.object(sys, 'stdin', io.StringIO(json.dumps(doc))), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(c.main(['provider', 'put', 'cc']), 0)
        stored = zp.load(str(self.cfg))['cc']
        self.assertNotIn(self.key, json.dumps(stored))
        self.assertEqual(zp.secret(zp.resolve_key('cc', stored)), self.key)
        with patch.object(c, '_exec_agent', return_value=0) as launch:
            c.launch_claude('cc', [])
        self.assertEqual(launch.call_args[0][2]['ANTHROPIC_AUTH_TOKEN'], self.key)
        self.assertEqual((self.cfg / 'run/cc.settings.json').stat().st_mode & 0o777, 0o600)

    def test_edit_can_switch_auth_variable_without_plaintext(self):
        c = self.core()
        p = zp.store_key('cc', self.claude, self.key)
        zp.save(str(self.cfg), {'cc': p})
        doc = zp.document(p)
        doc['env'].pop('ANTHROPIC_AUTH_TOKEN')
        doc['env']['ANTHROPIC_API_KEY'] = 'replacement-dummy-api-key'
        with patch.object(sys, 'stdin', io.StringIO(json.dumps(doc))), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(c.main(['provider', 'put', 'cc']), 0)
        raw = (self.cfg / 'providers.json').read_text()
        self.assertNotIn('replacement-dummy-api-key', raw)
        stored = zp.load(str(self.cfg))['cc']
        self.assertEqual(stored['key_var'], 'ANTHROPIC_API_KEY')
        self.assertEqual(zp.secret(zp.resolve_key('cc', stored)), 'replacement-dummy-api-key')

    def test_two_auth_keys_cannot_leave_an_extra_plaintext_key(self):
        p = self.claude
        p['env']['ANTHROPIC_API_KEY'] = 'other-dummy-key'
        with self.assertRaisesRegex(RuntimeError, 'two auth keys'):
            zp.store_key('cc', p, self.key)

    def test_use_resolves_into_private_shell_environment_and_off_clears(self):
        c = self.core()
        zp.save(str(self.cfg), {'cx': zp.store_key('cx', self.codex, self.key)})
        path = self.root / 'shell-eval'
        path.write_text('')
        path.chmod(0o644)
        out = io.StringIO()
        with patch.dict(os.environ, {'ZORUA_EVAL_FILE': str(path)}), contextlib.redirect_stdout(out):
            self.assertEqual(c.main(['use', 'cx']), 0)
        self.assertIn('ZORUA_CODEX_KEY=', path.read_text())
        self.assertIn(self.key, path.read_text())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(self.key, out.getvalue())
        c.EMIT.lines.clear()
        with contextlib.redirect_stdout(io.StringIO()):
            c.main(['off'])
        self.assertIn('unset ZORUA_CODEX_KEY', c.EMIT.lines)

    def test_force_import_deduplicates_sanitized_names_before_storage(self):
        c = self.core()
        db = self.root / 'dummy.db'
        db.touch()
        first = zp.build_env('https://relay.example', self.key)
        second = zp.build_env('https://relay.example', 'second-dummy-key')
        with patch.object(zp, 'cc_switch_providers', return_value=[('Relay A', first), ('Relay-A', second)]), patch.object(zp, 'cc_switch_codex_providers', return_value=[]), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(c.main(['provider', 'import', 'cc-switch', '--force', '--db', str(db)]), 0)
        stored = zp.load(str(self.cfg))['relay-a']
        self.assertEqual(zp.secret(zp.resolve_key('relay-a', stored)), 'second-dummy-key')
        self.assertNotIn('second-dummy-key', (self.cfg / 'providers.json').read_text())

    def test_redaction_precedes_truncation(self):
        raw = 'x' * 110 + self.key
        self.assertNotIn(self.key[:10], zp._reply_message(raw, self.codex))

    def test_default_model_and_settings_survive_a_document_round_trip(self):
        p = dict(self.claude, settings={'enableArtifact': False})
        self.assertEqual(zp.set_default_model(p, 'vendor/new'), 'new')
        self.assertEqual(zp.default_model(p), 'vendor/new')
        self.assertIn('vendor/new', zp.models(p).values())
        back = zp.from_document(p, zp.document(p))
        self.assertEqual(back['settings'], {'enableArtifact': False})
        self.assertEqual(zp.secret(back), self.key)
        self.assertEqual(zp.set_default_model(back, ''), '')
        self.assertNotIn('ANTHROPIC_MODEL', back['env'])
        with self.assertRaises(ValueError):
            zp.set_default_model(dict(self.codex), '')



@unittest.skipUnless(sys.platform == 'darwin' and os.path.exists('/usr/bin/security'), 'needs the macOS security tool')
class RealKeychain(unittest.TestCase):
    """End to end against /usr/bin/security, in a throwaway keychain file. Creating a keychain that is not
    login/System leaves the search list alone (Security's StorageManager); this checks it did, too."""

    def security(self, *args):
        return subprocess.run(['/usr/bin/security'] + list(args), capture_output=True, text=True, timeout=30)

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.chain = os.path.join(tmp.name, 'zorua-e2e.keychain')
        before = (self.security('list-keychains', '-d', 'user').stdout, self.security('default-keychain', '-d', 'user').stdout)
        r = self.security('create-keychain', '-p', 'zorua-e2e-dummy', self.chain)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.addCleanup(lambda: self.assertEqual(
            (self.security('list-keychains', '-d', 'user').stdout, self.security('default-keychain', '-d', 'user').stdout),
            before, 'the user keychain search list / default keychain changed'))
        self.addCleanup(self.security, 'delete-keychain', self.chain)
        env = patch.dict(os.environ, {'ZORUA_TEST_KEYCHAIN_FILE': self.chain, 'PATH': '/usr/bin:/bin'})
        env.start()
        self.addCleanup(env.stop)

    def test_write_read_rotate_with_special_characters(self):
        calls = []
        real = subprocess.run
        def run(argv, **kw):
            calls.append((argv, kw.get('input') or ''))
            return real(argv, **kw)
        keys = ['dummy-plain-key-0123456789',
                'dummy key "dq" \'sq\' \\bs\\ $HOME ${X} `id` ;|&<>#*?![]{}~^%= end\\',
                ' dummy-surrounded-by-spaces ']
        with patch.object(subprocess, 'run', side_effect=run):
            p = zp.build_codex('https://relay.example', keys[0], 'm')
            for key in keys:            # the first write adds the item; later ones update it (-U)
                p = zp.store_key('e2e-relay', p, key, require_keychain=True)
                self.assertEqual(p['key_ref'], 'keychain')
                self.assertNotIn('key', p)
                self.assertEqual(zp.secret(zp.resolve_key('e2e-relay', p)), key)
        self.assertEqual(len(calls), 2 * len(keys))
        for argv, stdin in calls:
            self.assertIn(self.chain, ' '.join(argv) + stdin)
            for key in keys:
                self.assertNotIn(key.strip(), ' '.join(argv))
                self.assertNotIn(key.encode().hex(), ' '.join(argv))
        self.assertEqual(self.security('find-generic-password', '-s', 'zorua', '-a', 'e2e-relay', self.chain).returncode, 0)
        # Only the throwaway keychain has it: the user's own keychains were never written.
        self.assertNotEqual(self.security('find-generic-password', '-s', 'zorua', '-a', 'e2e-relay').returncode, 0)

    def test_missing_item_fails_closed(self):
        with self.assertRaisesRegex(RuntimeError, 'Keychain key unavailable'):
            zp.resolve_key('e2e-missing', {'kind': 'codex', 'key_ref': 'keychain'})

if __name__ == '__main__':
    unittest.main()
