"""One-process contract smoke test against the installed Hermes source."""

import asyncio
import importlib.util
import os
import sys
from pathlib import Path
from types import SimpleNamespace

from gateway.config import PlatformConfig
from gateway.platform_registry import PlatformEntry, platform_registry
from gateway.session import build_session_key


PLUGIN_DIR = Path(__file__).parent
spec = importlib.util.spec_from_file_location("hermes_vk_smoke", PLUGIN_DIR / "adapter.py")
plugin = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = plugin
spec.loader.exec_module(plugin)


class Context:
    def register_platform(self, **kwargs):
        platform_registry.register(PlatformEntry(**kwargs))


plugin.register(Context())
assert platform_registry.is_registered("vk"), "VK platform entry did not register"
os.environ["VK_TOKEN"] = "smoke-test-only"
os.environ["VK_GROUP_ID"] = "987654321"
os.environ["VK_ALLOWED_USERS"] = "123456789"
adapter = plugin.VkAdapter(PlatformConfig(enabled=True, extra={}))
assert adapter.platform.value == "vk", "VK enum value did not resolve through platform registry"
events = []


async def capture(event):
    events.append(event)


adapter.handle_message = capture
first = {"type": "message_new", "event_id": "smoke-1", "object": {"message": {"id": 1, "peer_id": 123456789, "from_id": 123456789, "text": "Привет"}}}
second = {"type": "message_new", "event_id": "smoke-2", "object": {"message": {"id": 2, "peer_id": 123456789, "from_id": 123456789, "text": "Снова"}}}
asyncio.run(adapter._dispatch_update(first))
asyncio.run(adapter._dispatch_update(second))
assert len(events) == 2, "VK events did not reach Hermes adapter dispatch"
assert events[0].source.platform.value == "vk"
assert events[0].source.chat_type == "dm"
assert events[0].source.chat_id == events[1].source.chat_id == "123456789"
assert build_session_key(events[0].source) == build_session_key(events[1].source), "same VK DM must resolve to one Hermes session"
print("Hermes plugin registration, actual BasePlatformAdapter source, allowlist dispatch, and stable DM session: PASS")
