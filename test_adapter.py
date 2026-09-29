"""Hermes-independent tests for the VK protocol and filtering helpers."""

import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs


ROOT = Path(__file__).parent


def _install_import_stubs():
    agent = types.ModuleType("agent")
    secret_scope = types.ModuleType("agent.secret_scope")

    class UnscopedSecretError(Exception):
        pass

    secret_scope.UnscopedSecretError = UnscopedSecretError
    secret_scope.get_secret = lambda _name: None
    agent.secret_scope = secret_scope
    sys.modules["agent"] = agent
    sys.modules["agent.secret_scope"] = secret_scope

    hermes_cli = types.ModuleType("hermes_cli")
    hermes_cli.__path__ = []
    hermes_config = types.ModuleType("hermes_cli.config")
    hermes_config.get_hermes_home = lambda: Path("/tmp/hermes-test-home")
    hermes_cli.config = hermes_config
    sys.modules["hermes_cli"] = hermes_cli
    sys.modules["hermes_cli.config"] = hermes_config

    gateway = types.ModuleType("gateway")
    gateway.__path__ = []
    config = types.ModuleType("gateway.config")
    config.Platform = lambda value: value
    platforms = types.ModuleType("gateway.platforms")
    platforms.__path__ = []
    base = types.ModuleType("gateway.platforms.base")

    class MessageType(Enum):
        TEXT = "text"

    @dataclass
    class MessageEvent:
        text: str
        message_type: MessageType
        user_id: str
        source: object
        message_id: str | None = None
        metadata: dict | None = None

    @dataclass
    class SendResult:
        success: bool
        message_id: str | None = None
        error: str | None = None
        raw_response: object = None

    class BasePlatformAdapter:
        def __init__(self, config, platform):
            self.config = config
            self.platform = platform
            self.events = []

        def build_source(self, **kwargs):
            return types.SimpleNamespace(platform=self.platform, **kwargs)

        async def handle_message(self, event):
            self.events.append(event)

        async def get_chat_info(self, chat_id):
            return {"chat_id": str(chat_id), "chat_type": "dm", "platform": "vk"}

        def _mark_connected(self):
            pass

    base.BasePlatformAdapter = BasePlatformAdapter
    base.MessageEvent = MessageEvent
    base.MessageType = MessageType
    base.SendResult = SendResult
    gateway.config = config
    gateway.platforms = platforms
    platforms.base = base
    sys.modules.update({
        "gateway": gateway,
        "gateway.config": config,
        "gateway.platforms": platforms,
        "gateway.platforms.base": base,
    })


_install_import_stubs()
spec = importlib.util.spec_from_file_location("hermes_vk_adapter", ROOT / "adapter.py")
adapter = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = adapter
spec.loader.exec_module(adapter)


class VkProtocolTests(unittest.TestCase):
    def test_cursor_uses_hermes_home_and_round_trips(self):
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"VK_STATE_DIR": ""}), patch.object(adapter, "get_hermes_home", return_value=Path(home)):
            instance = adapter.VkAdapter(types.SimpleNamespace(extra={}))
            instance.group_id = "987654321"
            expected = Path(home) / "plugin-data" / "vk-platform" / "vk-long-poll-987654321.json"
            self.assertEqual(Path(instance._state_path()), expected)
            instance._ts = "123456"
            instance._save_ts()
            self.assertTrue(expected.is_file())
            self.assertEqual(instance._load_ts(), "123456")

    def test_cursor_absolute_override_round_trips_and_relative_is_rejected(self):
        with tempfile.TemporaryDirectory() as home, patch.object(adapter, "get_hermes_home", return_value=Path(home) / "unused"):
            instance = adapter.VkAdapter(types.SimpleNamespace(extra={}))
            instance.group_id = "987654321"
            override = str(Path(home) / "custom-state")
            with patch.dict(os.environ, {"VK_STATE_DIR": override}):
                expected = Path(override) / "vk-long-poll-987654321.json"
                self.assertEqual(Path(instance._state_path()), expected)
                instance._ts = "654321"
                instance._save_ts()
                self.assertEqual(instance._load_ts(), "654321")
            with patch.dict(os.environ, {"VK_STATE_DIR": "relative-state"}), self.assertRaisesRegex(ValueError, "absolute path"):
                instance._state_path()
            with patch.dict(os.environ, {"VK_STATE_DIR": "~/vk-state"}):
                self.assertEqual(Path(instance._state_path()).parent, Path.home() / "vk-state")

    def test_legacy_cursor_is_read_once_from_hermes_home_and_new_writes_use_plugin_data(self):
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"VK_STATE_DIR": ""}), patch.object(adapter, "get_hermes_home", return_value=Path(home)):
            instance = adapter.VkAdapter(types.SimpleNamespace(extra={}))
            instance.group_id = "987654321"
            legacy = Path(home) / "vk-long-poll-987654321.json"
            legacy.write_text('{"ts":"111"}', encoding="utf-8")
            self.assertEqual(instance._load_ts(), "111")
            instance._ts = "222"
            instance._save_ts()
            self.assertEqual(instance._load_ts(), "222")
            self.assertEqual(legacy.read_text(encoding="utf-8"), '{"ts":"111"}')
            self.assertTrue((Path(home) / "plugin-data" / "vk-platform" / legacy.name).is_file())

    def test_fixed_legacy_cursor_migrates_when_hermes_home_differs(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"VK_STATE_DIR": ""}):
            home = Path(root) / "home"
            legacy_dir = Path(root) / "old-opt-data"
            home.mkdir()
            legacy_dir.mkdir()
            with patch.object(adapter, "get_hermes_home", return_value=home), patch.object(adapter, "_LEGACY_STATE_DIR", legacy_dir):
                instance = adapter.VkAdapter(types.SimpleNamespace(extra={}))
                instance.group_id = "987654321"
                legacy = legacy_dir / "vk-long-poll-987654321.json"
                legacy.write_text('{"ts":"111"}', encoding="utf-8")
                self.assertEqual(instance._load_ts(), "111")
                instance._ts = "222"
                instance._save_ts()
                self.assertEqual(instance._load_ts(), "222")
                self.assertEqual(legacy.read_text(encoding="utf-8"), '{"ts":"111"}')
                self.assertTrue((home / "plugin-data" / "vk-platform" / legacy.name).is_file())

    def test_same_home_and_fixed_legacy_path_is_read_once(self):
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"VK_STATE_DIR": ""}), patch.object(adapter, "get_hermes_home", return_value=Path(home)), patch.object(adapter, "_LEGACY_STATE_DIR", Path(home)):
            instance = adapter.VkAdapter(types.SimpleNamespace(extra={}))
            instance.group_id = "987654321"
            with patch("builtins.open", side_effect=FileNotFoundError) as reader:
                self.assertEqual(instance._load_ts(), "")
            self.assertEqual(reader.call_count, 2)

    def test_override_does_not_read_legacy_cursor_from_hermes_home(self):
        with tempfile.TemporaryDirectory() as home, patch.object(adapter, "get_hermes_home", return_value=Path(home)), patch.object(adapter, "_LEGACY_STATE_DIR", Path(home) / "old-opt-data"):
            instance = adapter.VkAdapter(types.SimpleNamespace(extra={}))
            instance.group_id = "987654321"
            (Path(home) / "vk-long-poll-987654321.json").write_text('{"ts":"111"}', encoding="utf-8")
            adapter._LEGACY_STATE_DIR.mkdir()
            (adapter._LEGACY_STATE_DIR / "vk-long-poll-987654321.json").write_text('{"ts":"333"}', encoding="utf-8")
            with patch.dict(os.environ, {"VK_STATE_DIR": str(Path(home) / "override")}):
                self.assertEqual(instance._load_ts(), "")

    def test_split_preserves_unicode_text_and_respects_limit(self):
        text = ("Привет 😀 мир\n" * 9) + ("я" * 31)
        chunks = adapter.split_vk_message(text, limit=17)
        self.assertEqual("".join(chunks), text)
        self.assertTrue(all(len(chunk) <= 17 for chunk in chunks))

    def test_empty_message_has_no_chunks(self):
        self.assertEqual(adapter.split_vk_message(""), [])

    def test_allowed_users_are_positive_numeric_ids_only(self):
        self.assertEqual(adapter.parse_allowed_users("123456789, 00123, bad, -7, 0"), {"123456789", "123"})
        self.assertEqual(adapter.parse_allowed_users(" , "), set())

    def test_private_message_new_normalizes_and_preserves_unicode(self):
        update = {
            "type": "message_new",
            "event_id": "evt-1",
            "object": {"message": {"id": 9, "peer_id": 123456789, "from_id": 123456789, "out": 0, "text": "Привет, Hermes 👋"}},
        }
        parsed = adapter.normalize_message_update(update, "987654321")
        self.assertEqual(parsed, adapter.IncomingVkMessage("123456789", "Привет, Hermes 👋", "9", "evt-1"))

    def test_ignores_groups_bot_echoes_unknown_events_and_empty_text(self):
        base = {"type": "message_new", "object": {"message": {"peer_id": 123456789, "from_id": 123456789, "text": "hi"}}}
        self.assertIsNone(adapter.normalize_message_update({**base, "type": "wall_post_new"}, "987654321"))
        self.assertIsNone(adapter.normalize_message_update({"type": "message_new", "object": {"message": {"peer_id": 2_000_000_001, "from_id": 123456789, "text": "hi"}}}, "987654321"))
        self.assertIsNone(adapter.normalize_message_update({"type": "message_new", "object": {"message": {"peer_id": 123456789, "from_id": -987654321, "out": 1, "text": "hi"}}}, "987654321"))
        self.assertIsNone(adapter.normalize_message_update({"type": "message_new", "object": {"message": {"peer_id": 123456789, "from_id": 123456789, "text": " "}}}, "987654321"))

    def test_legacy_private_event_is_supported_and_group_peer_is_rejected(self):
        self.assertEqual(adapter.normalize_message_update([4, 12, 0, 123456789, 1, "Привет"], "987654321").text, "Привет")
        self.assertIsNone(adapter.normalize_message_update([4, 12, 0, 2_000_000_001, 1, "group"], "987654321"))

    def test_token_is_post_body_not_url(self):
        token = "secret-token-for-test"
        request = adapter._post_request("messages.send", {"peer_id": 123456789}, token)
        self.assertNotIn(token, request.full_url)
        self.assertEqual(parse_qs(request.data.decode("utf-8"))["access_token"], [token])

    def test_dispatch_ignores_non_allowlisted_users_and_duplicate_events(self):
        instance = adapter.VkAdapter(types.SimpleNamespace(extra={}))
        instance.group_id = "987654321"
        instance.allowed_users = {"123456789"}

        async def dispatch(update):
            await instance._dispatch_update(update)

        allowed = {"type": "message_new", "event_id": "evt-1", "object": {"message": {"id": 9, "peer_id": 123456789, "from_id": 123456789, "text": "Привет"}}}
        denied = {"type": "message_new", "event_id": "evt-2", "object": {"message": {"id": 10, "peer_id": 8675309, "from_id": 8675309, "text": "Не обрабатывай"}}}
        asyncio.run(dispatch(allowed))
        asyncio.run(dispatch(allowed))
        asyncio.run(dispatch(denied))
        self.assertEqual(len(instance.events), 1)
        self.assertEqual(instance.events[0].source.chat_id, "123456789")
        self.assertEqual(instance.events[0].source.user_id, "123456789")

    def test_send_splits_text_and_refuses_unallowlisted_target(self):
        instance = adapter.VkAdapter(types.SimpleNamespace(extra={}))
        instance.token = "test-token"
        instance.allowed_users = {"123456789"}
        calls = []

        async def inline_thread(function, *args, **kwargs):
            calls.append((function, args, kwargs))
            return function(*args, **kwargs)

        with patch.object(adapter.asyncio, "to_thread", new=inline_thread), patch.object(adapter, "vk_api_call", side_effect=[101, 102]) as api_call:
            result = asyncio.run(instance.send("123456789", "я" * 5000))
            denied = asyncio.run(instance.send("8675309", "не отправлять"))

        self.assertTrue(result.success)
        self.assertEqual(len(api_call.call_args_list), 2)
        self.assertEqual([len(call.args[1]["message"]) for call in api_call.call_args_list], [4096, 904])
        self.assertEqual(denied.success, False)
        self.assertEqual(len(api_call.call_args_list), 2)

    def test_unified_keyboard_uses_fixed_random_id(self):
        instance = adapter.VkAdapter(types.SimpleNamespace(extra={}))
        instance.token = "test-token"
        instance.allowed_users = {"123456789"}
        keyboard = {"inline": True, "buttons": [[{"action": {"type": "callback", "label": "Принял", "payload": "{}"}}]]}
        with patch.object(adapter, "vk_api_call", return_value=321) as api_call:
            result = asyncio.run(instance.send("123456789", "notice", metadata={"unified_vk_keyboard": keyboard, "unified_vk_random_id": 99}))
        self.assertTrue(result.success)
        self.assertEqual(result.message_id, "321")
        self.assertEqual(api_call.call_args.args[1]["random_id"], 99)
        self.assertEqual(json.loads(api_call.call_args.args[1]["keyboard"]), keyboard)

    def test_message_event_setting_is_read_from_community(self):
        instance = adapter.VkAdapter(types.SimpleNamespace(extra={}))
        instance.token = "test-token"
        instance.group_id = "987654321"
        with patch.object(adapter, "vk_api_call", return_value={"events": {"message_event": 1}}) as api_call:
            self.assertTrue(asyncio.run(instance.message_event_enabled()))
        self.assertEqual(api_call.call_args.args[0], "groups.getLongPollSettings")
        self.assertEqual(api_call.call_args.args[1]["group_id"], 987654321)
        with patch.object(adapter, "vk_api_call", return_value={"events": {"message_event": 0}}):
            self.assertFalse(asyncio.run(instance.message_event_enabled()))

    def test_message_event_resolves_global_message_id_before_ack(self):
        instance = adapter.VkAdapter(types.SimpleNamespace(extra={}))
        instance.token = "test-token"
        instance.allowed_users = {"123456789"}
        callbacks = []
        async def handle(data, **kwargs):
            callbacks.append((data, kwargs))
            return True
        instance.set_message_event_handler(handle)
        event = {"type": "message_event", "object": {"user_id": 123456789, "peer_id": 123456789, "event_id": "evt", "conversation_message_id": 17, "payload": {"hui": "reminder"}}}
        with patch.object(adapter, "vk_api_call", side_effect=[{"items": [{"id": 321}]}, 1]) as api_call:
            asyncio.run(instance._dispatch_update(event))
        self.assertEqual(callbacks, [("hui:reminder", {"peer_id": "123456789", "user_id": "123456789", "message_id": "321"})])
        self.assertEqual([call.args[0] for call in api_call.call_args_list], ["messages.getByConversationMessageId", "messages.sendMessageEventAnswer"])

    def test_long_poll_rejects_non_vk_or_plain_http_servers(self):
        for server in ("http://lp.vk.com/wh1", "https://example.org/longpoll", "https://vk.com.evil.test/wh1"):
            with self.subTest(server=server), self.assertRaises(adapter.VkTransportError):
                adapter._checked_long_poll_server(server)
        self.assertEqual(adapter._checked_long_poll_server("https://lp.vk.com/wh1"), "https://lp.vk.com/wh1")


if __name__ == "__main__":
    unittest.main()
