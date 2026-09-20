"""Offline regression checks. Only temporary profiles; no network or real credentials.

Needs a Hermes Agent checkout on PYTHONPATH (validated with 0.21.x). The
plugin's declared ``test`` extra does NOT install Hermes and ``testpaths`` is
``tests``, so on a plugin-only checkout this module skips itself — importing
``hermes_constants`` unconditionally would abort collection for the whole suite.
See docs/multiplex-profile-isolation.md for the run command.
"""
import asyncio, contextlib, importlib.util, os, pathlib, sys, tempfile, unittest
from types import SimpleNamespace
from unittest.mock import patch
PLUGIN = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN))
try:
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from agent.secret_scope import set_secret_scope, reset_secret_scope, set_multiplex_active, load_env_file
except ImportError as exc:  # pragma: no cover - environment gate
    _REASON = f"needs a Hermes Agent checkout on PYTHONPATH ({exc})"
    try:
        import pytest
    except ImportError:
        print(f"SKIP: {_REASON}")
        raise SystemExit(0) from None
    pytest.skip(_REASON, allow_module_level=True)
from clawchat_gateway import storage, terminal_send
from clawchat_gateway.config import ClawChatConfig, _get_env
from clawchat_gateway.connection import ClawChatConnection

@contextlib.contextmanager
def scope(home):
    h = set_hermes_home_override(str(home)); s = set_secret_scope(load_env_file(home / '.env'))
    try: yield
    finally: reset_secret_scope(s); reset_hermes_home_override(h)

@contextlib.contextmanager
def home_only(home):
    """Home override with NO secret scope: how the DEFAULT profile runs under multiplexing."""
    h = set_hermes_home_override(str(home))
    try: yield
    finally: reset_hermes_home_override(h)

class OfflineConnection(ClawChatConnection):
    def _warn_if_device_id_volatile(self): pass
    def _build_refresh_manager(self): return object()
    async def _supervisor(self): await asyncio.Event().wait()

class Sender:
    def __init__(self, name): self.name = name
    async def send_mention_message(self, **kw): return self.name
    async def send_reaction_message(self, **kw): return self.name
    async def send(self, **kw): return SimpleNamespace(success=True, message_id=self.name)

class Isolation(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.a = pathlib.Path(self.tmp.name) / '.hermes'; self.b = self.a / 'profiles/coder'
        for home, name in [(self.a, 'A'), (self.b, 'B')]:
            home.mkdir(parents=True)
            (home / '.env').write_text(f'CLAWCHAT_TOKEN=token-{name}\nCLAWCHAT_REFRESH_TOKEN=refresh-{name}\n')
            (home / 'config.yaml').write_text('{}\n')
        self.env = patch.dict(os.environ, {'HERMES_HOME': str(self.a), 'CLAWCHAT_TOKEN': 'ambient-A'})
        self.env.start(); self.addCleanup(self.env.stop)
        set_multiplex_active(True); self.addCleanup(set_multiplex_active, False)
        if hasattr(storage, '_stores'): storage._stores.clear()
        if hasattr(storage, '_store'): storage._store = None
        ClawChatConnection._live_supervisors.clear()
        if hasattr(terminal_send, '_senders'): terminal_send._senders.clear()
    def connection(self, home, name):
        with scope(home):
            c = OfflineConnection(ClawChatConfig(websocket_url='wss://invalid.example', token=f'token-{name}', refresh_token=f'refresh-{name}', user_id=name, owner_user_id='owner'), on_message=lambda x: None)
            c._store.update_activation_tokens(platform='hermes', account_id='default', access_token=f'token-{name}', refresh_token=f'refresh-{name}', seed_user_id=name, seed_owner_user_id='owner')
            return c
    async def test_database_and_startup_identity_a_b_a(self):
        a=self.connection(self.a,'A'); b=self.connection(self.b,'B')
        a._load_startup_activation_credentials(); b._load_startup_activation_credentials()
        self.assertEqual((a._cfg.user_id,b._cfg.user_id),('A','B'))
        with scope(self.a): self.assertIs(storage.get_clawchat_store(),a._store)
        self.assertIsNot(a._store,b._store)
    async def test_connections_coexist_and_same_profile_replacement(self):
        a=self.connection(self.a,'A'); b=self.connection(self.b,'B'); replacement=self.connection(self.a,'A')
        try:
            await a.start(); await b.start()
            self.assertFalse(a._stopping)
            await replacement.start()
            self.assertTrue(a._stopping); self.assertFalse(b._stopping)
            await a.stop()
            self.assertIn(replacement,ClawChatConnection._live_supervisors.values())
            await b.stop(); self.assertFalse(replacement._stopping)
        finally:
            for c in [a,b,replacement]: await c.stop()
    async def test_sender_and_media_routing(self):
        a=Sender('A'); b=Sender('B')
        with scope(self.a): terminal_send.set_clawchat_mention_sender(a)
        with scope(self.b): terminal_send.set_clawchat_mention_sender(b)
        spec=importlib.util.spec_from_file_location('delivery_plugin',PLUGIN/'__init__.py',submodule_search_locations=[str(PLUGIN)])
        plugin=importlib.util.module_from_spec(spec);spec.loader.exec_module(plugin)
        for home, name in [(self.a,'A'),(self.b,'B'),(self.a,'A')]:
            with scope(home):
                self.assertEqual(await terminal_send.send_clawchat_mention_message(chat_id='chat',mentions=[]),name)
                self.assertEqual(await terminal_send.send_clawchat_reaction_message(chat_id='chat',emoji='ok'),name)
                result=await plugin._send_clawchat_media_via_live_adapter('clawchat',SimpleNamespace(extra={}), 'chat','test',media_files=[])
                self.assertEqual(result['message_id'],name)
        with scope(self.b): terminal_send.clear_clawchat_mention_sender(a)
        with scope(self.b): self.assertEqual(await terminal_send.send_clawchat_reaction_message(chat_id='chat',emoji='ok'),'B')
    async def test_scoped_missing_secret_never_uses_default_environment(self):
        (self.b/'.env').write_text('')
        with scope(self.b): self.assertEqual(_get_env('CLAWCHAT_TOKEN'),'')

    async def test_default_profile_under_multiplex_keeps_env_fallback(self):
        """Multiplex on, no scope installed = the DEFAULT profile. os.environ is
        then its OWN value, so cutting the fallback would strip credentials from
        env-injected deployments (systemd / `op run` / a container)."""
        (self.a/'.env').write_text('')
        with home_only(self.a): self.assertEqual(_get_env('CLAWCHAT_TOKEN'),'ambient-A')

    async def test_scope_without_multiplex_keeps_env_fallback(self):
        """A scope outside multiplexing is a `.env` overlay, not a blindfold —
        Hermes' own get_secret falls through here, and so must we (cron 401s)."""
        set_multiplex_active(False)
        (self.b/'.env').write_text('')
        with scope(self.b): self.assertEqual(_get_env('CLAWCHAT_TOKEN'),'ambient-A')

    async def test_logout_tombstone_beats_the_stale_scope_snapshot(self):
        """The secret scope is frozen at gateway start. Deleting the .env line
        would leave that snapshot holding the token we just revoked, so the next
        adapter reads it back and retries auth forever; an empty value must win."""
        b = self.connection(self.b, 'B')
        with scope(self.b):
            self.assertEqual(_get_env('CLAWCHAT_TOKEN'), 'token-B')
            await b._persist_auth_logout('test')
            self.assertEqual(_get_env('CLAWCHAT_TOKEN'), '')
            self.assertEqual(_get_env('CLAWCHAT_REFRESH_TOKEN'), '')
            self.assertEqual(load_env_file(self.b/'.env').get('CLAWCHAT_TOKEN'), '')

    async def test_only_revoked_credential_keys_are_tombstoned(self):
        """An empty value is ordinary outside the two credential keys: activation
        always writes CLAWCHAT_HOME_CHANNEL_THREAD_ID= when it records a home
        channel. Treating that as a logout would strip optional settings — and,
        via the alias loop, a second alias too — of their env fallback."""
        (self.a/'.env').write_text('CLAWCHAT_HOME_CHANNEL_THREAD_ID=\nCLAWCHAT_WEBSOCKET_URL=\n')
        env = {'CLAWCHAT_HOME_CHANNEL_THREAD_ID': 'thread-A', 'CLAWCHAT_WS_URL': 'wss://alias.example'}
        with patch.dict(os.environ, env), home_only(self.a):
            self.assertEqual(_get_env('CLAWCHAT_HOME_CHANNEL_THREAD_ID'), 'thread-A')
            self.assertEqual(_get_env('CLAWCHAT_WEBSOCKET_URL', 'CLAWCHAT_WS_URL'), 'wss://alias.example')

    async def test_device_override_is_profile_scoped(self):
        from clawchat_gateway.device_id import get_device_id
        for home, name in [(self.a, 'A'), (self.b, 'B')]:
            with (home / '.env').open('a') as f: f.write(f'CLAWCHAT_DEVICE_ID=hermes-{name}\n')
        for home, name in [(self.a, 'A'), (self.b, 'B'), (self.a, 'A')]:
            with scope(home): self.assertEqual(get_device_id(), f'hermes-{name}')

    async def test_generated_device_ids_keep_profile_suffix_and_paired_identity(self):
        from clawchat_gateway.device_id import get_device_id, resolve_paired_device_id
        with scope(self.a): default_id = get_device_id()
        with scope(self.b):
            coder_id = get_device_id()
            self.assertNotEqual(coder_id, default_id)
            self.assertTrue(coder_id.startswith(default_id + '-p'))
            self.assertEqual(resolve_paired_device_id(stored=default_id, token='existing'), default_id)
        with scope(self.a): self.assertEqual(get_device_id(), default_id)

    async def test_refresh_and_logout_keep_owner_profile_from_wrong_context(self):
        a=self.connection(self.a,'A'); b=self.connection(self.b,'B')
        with scope(self.a):
            self.assertTrue(await b._persist_rotated_tokens('rotated-B','refresh2-B'))
        self.assertEqual(load_env_file(self.a/'.env')['CLAWCHAT_TOKEN'],'token-A')
        self.assertEqual(load_env_file(self.b/'.env')['CLAWCHAT_TOKEN'],'rotated-B')
        self.assertEqual(a._store.get_activation_credentials(platform='hermes',account_id='default').access_token,'token-A')
        self.assertEqual(b._store.get_activation_credentials(platform='hermes',account_id='default').access_token,'rotated-B')
        with scope(self.a): await b._persist_auth_logout('test')
        self.assertEqual(load_env_file(self.a/'.env')['CLAWCHAT_TOKEN'],'token-A')
        self.assertEqual(load_env_file(self.b/'.env').get('CLAWCHAT_TOKEN'),'')
        self.assertIsNone(b._store.get_activation_credentials(platform='hermes',account_id='default'))

if __name__ == '__main__': unittest.main(verbosity=2)
