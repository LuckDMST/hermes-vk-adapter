"""Hermes-independent tests for the VK protocol and filtering helpers."""

import asyncio
import importlib.util
import sys
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

    def test_long_poll_rejects_non_vk_or_plain_http_servers(self):
        for server in ("http://lp.vk.com/wh1", "https://example.org/longpoll", "https://vk.com.evil.test/wh1"):
            with self.subTest(server=server), self.assertRaises(adapter.VkTransportError):
                adapter._checked_long_poll_server(server)
        self.assertEqual(adapter._checked_long_poll_server("https://lp.vk.com/wh1"), "https://lp.vk.com/wh1")


if __name__ == "__main__":
    unittest.main()
