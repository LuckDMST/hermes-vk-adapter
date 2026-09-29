"""Single-owner VK Bots Long Poll adapter for Hermes Agent.

Only private text messages from VK_ALLOWED_USERS are passed to Hermes. The
Long Poll cursor file stores transport state only; conversation history and
session identity remain owned by Hermes.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import secrets
import tempfile
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from agent.secret_scope import UnscopedSecretError, get_secret as scoped_get_secret
from gateway.config import Platform
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, SendResult
from hermes_cli.config import get_hermes_home


logger = logging.getLogger(__name__)
VK_API_VERSION = "5.199"
VK_LONG_POLL_VERSION = "3"
VK_MESSAGE_LIMIT = 4096
VK_LONG_POLL_WAIT = 25
VK_API_TIMEOUT = 20
VK_LONG_POLL_TIMEOUT = 35
_MAX_RECENT_EVENTS = 2048
_BACKOFF_INITIAL = 1.0
_BACKOFF_MAX = 60.0
# Read-only migration source for releases that hardcoded /opt/data, even when HERMES_HOME differs.
_LEGACY_STATE_DIR = Path("/opt/data")


class VkTransportError(Exception):
    """Transport failure without URL, request body, or provider response text."""


class VkApiError(Exception):
    """VK API error identified by numeric code only."""

    def __init__(self, code: int):
        self.code = int(code)
        super().__init__(f"VK API error {self.code}")


@dataclass(frozen=True)
class IncomingVkMessage:
    user_id: str
    text: str
    message_id: Optional[str]
    event_id: Optional[str]


def _secret(name: str) -> str:
    try:
        value = scoped_get_secret(name)
    except UnscopedSecretError:
        value = os.getenv(name)
    return str(value or "").strip()


def parse_allowed_users(value: Optional[str]) -> set[str]:
    """Return positive numeric user IDs; invalid entries are denied."""
    if not value:
        return set()
    result = set()
    for part in value.split(","):
        candidate = part.strip()
        if candidate.isdecimal() and int(candidate) > 0:
            result.add(str(int(candidate)))
    return result


def split_vk_message(text: str, limit: int = VK_MESSAGE_LIMIT) -> list[str]:
    """Split by Unicode code points while preserving every input character."""
    if limit < 1:
        raise ValueError("limit must be positive")
    if not text:
        return []
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + limit, len(text))
        if end < len(text):
            newline = text.rfind("\n", start + 1, end)
            space = text.rfind(" ", start + 1, end)
            boundary = max(newline, space)
            if boundary > start:
                end = boundary + 1
        chunks.append(text[start:end])
        start = end
    return chunks


def normalize_message_update(update: Any, group_id: str) -> Optional[IncomingVkMessage]:
    """Parse current Bots Long Poll updates and legacy array message_new events."""
    if isinstance(update, dict):
        if update.get("type") != "message_new":
            return None
        event_id = update.get("event_id")
        obj = update.get("object")
        message = obj.get("message", obj) if isinstance(obj, dict) else None
        if not isinstance(message, dict):
            return None
        if message.get("out") in (1, True, "1"):
            return None
        user_id = message.get("from_id", message.get("user_id"))
        peer_id = message.get("peer_id")
        message_id = message.get("id") or message.get("conversation_message_id")
        text = message.get("text")
    elif isinstance(update, (list, tuple)) and len(update) >= 6 and update[0] == 4:
        # Legacy Bots Long Poll: [4, id, flags, peer_id, date, text, ...].
        message_id, flags, peer_id, text = update[1], update[2], update[3], update[5]
        if not isinstance(flags, int) or flags & 2:
            return None
        event_id = str(message_id) if message_id is not None else None
        user_id = peer_id
        # Legacy group-conversation peer IDs are in the 2,000,000,000 range.
        if isinstance(peer_id, int) and peer_id >= 2_000_000_000:
            return None
    else:
        return None

    try:
        user = int(user_id)
        peer = int(peer_id)
        community = int(group_id)
    except (TypeError, ValueError):
        return None
    if user <= 0 or peer != user or user == community:
        return None
    if not isinstance(text, str) or not text.strip():
        return None
    return IncomingVkMessage(
        user_id=str(user),
        text=text,
        message_id=str(message_id) if message_id is not None else None,
        event_id=str(event_id) if event_id is not None else None,
    )


def _post_request(method: str, params: dict[str, Any], token: str) -> Request:
    """Build a POST request so the access token never appears in a URL."""
    body = {**params, "access_token": token, "v": VK_API_VERSION}
    encoded = urlencode(body).encode("utf-8")
    return Request(
        f"https://api.vk.com/method/{method}",
        data=encoded,
        headers={"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"},
        method="POST",
    )


def _read_json_response(response: Any) -> Any:
    try:
        raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            raise VkTransportError("response_too_large")
        return json.loads(raw.decode("utf-8"))
    except VkTransportError:
        raise
    except Exception:
        raise VkTransportError("invalid_json_response") from None


def vk_api_call(method: str, params: dict[str, Any], token: str) -> Any:
    request = _post_request(method, params, token)
    try:
        with urlopen(request, timeout=VK_API_TIMEOUT) as response:
            payload = _read_json_response(response)
    except HTTPError as error:
        raise VkTransportError(f"http_{error.code}") from None
    except VkTransportError:
        raise
    except Exception as error:
        raise VkTransportError(type(error).__name__) from None
    if not isinstance(payload, dict):
        raise VkTransportError("invalid_api_envelope")
    error = payload.get("error")
    if isinstance(error, dict):
        try:
            raise VkApiError(int(error.get("error_code", 0)))
        except (TypeError, ValueError):
            raise VkApiError(0) from None
    if "response" not in payload:
        raise VkTransportError("missing_api_response")
    return payload["response"]


def _checked_long_poll_server(server: str) -> str:
    parsed = urlsplit(server)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not (host == "vk.com" or host.endswith((".vk.com", ".vk.ru"))):
        raise VkTransportError("unexpected_long_poll_host")
    return server


def vk_long_poll_call(server: str, key: str, ts: str) -> dict[str, Any]:
    server = _checked_long_poll_server(server)
    query = urlencode({
        "act": "a_check",
        "key": key,
        "ts": ts,
        "wait": VK_LONG_POLL_WAIT,
        "version": VK_LONG_POLL_VERSION,
    })
    separator = "&" if "?" in server else "?"
    request = Request(f"{server}{separator}{query}", headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=VK_LONG_POLL_TIMEOUT) as response:
            payload = _read_json_response(response)
    except HTTPError as error:
        raise VkTransportError(f"http_{error.code}") from None
    except VkTransportError:
        raise
    except Exception as error:
        raise VkTransportError(type(error).__name__) from None
    if not isinstance(payload, dict):
        raise VkTransportError("invalid_long_poll_envelope")
    return payload


class VkAdapter(BasePlatformAdapter):
    """Single-owner private DM adapter using VK group Bots Long Poll."""

    splits_long_messages = True

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        """Return Hermes' minimal DM metadata for an allowlisted VK user."""
        user_id = str(chat_id).strip()
        if not user_id.isdecimal() or user_id not in self.allowed_users:
            return {"chat_id": user_id, "chat_type": "dm", "platform": "vk"}
        return {"chat_id": user_id, "chat_type": "dm", "platform": "vk", "user_id": user_id}

    def __init__(self, config: Any):
        super().__init__(config=config, platform=Platform("vk"))
        self.token = _secret("VK_TOKEN")
        self.group_id = os.getenv("VK_GROUP_ID", "").strip()
        self.allowed_users = parse_allowed_users(os.getenv("VK_ALLOWED_USERS"))
        self._server = ""
        self._key = ""
        self._ts = ""
        self._poll_task: Optional[asyncio.Task] = None
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._typing_disabled = False
        self._message_event_handler = None

    def set_message_event_handler(self, handler) -> None:
        """Register a trusted application callback; transport does not own ACK state."""
        self._message_event_handler = handler

    async def message_event_enabled(self) -> bool:
        """Read the community's current Bots Long Poll callback-event setting."""
        if not self.token or not self.group_id.isdecimal():
            return False
        settings = await asyncio.to_thread(vk_api_call, "groups.getLongPollSettings", {"group_id": int(self.group_id)}, self.token)
        events = settings.get("events") if isinstance(settings, dict) else None
        return bool(isinstance(events, dict) and events.get("message_event"))

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if self._poll_task and not self._poll_task.done():
            return True
        if not self.token or not self.group_id or not self.group_id.isdecimal():
            logger.error("VK adapter disabled: VK_TOKEN and numeric VK_GROUP_ID are required")
            return False
        if not self.allowed_users:
            logger.error("VK adapter disabled: VK_ALLOWED_USERS must contain at least one numeric user ID")
            return False
        try:
            info = await asyncio.to_thread(self._get_long_poll_server)
            persisted_ts = await asyncio.to_thread(self._load_ts)
            self._server, self._key = info["server"], info["key"]
            self._ts = persisted_ts or info["ts"]
        except VkApiError as error:
            logger.error("VK connection failed with API code %s", error.code)
            return False
        except Exception as error:
            logger.error("VK connection failed (%s)", type(error).__name__)
            return False
        self._poll_task = asyncio.create_task(self._poll_loop(), name="hermes-vk-long-poll")
        self._mark_connected()
        logger.info("VK Bots Long Poll connected")
        return True

    async def disconnect(self) -> None:
        task, self._poll_task = self._poll_task, None
        if task and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=2.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
        logger.info("VK Bots Long Poll disconnected")

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> SendResult:
        if not self.token:
            return SendResult(success=False, error="VK_TOKEN is not configured")
        try:
            peer_id = int(chat_id)
        except (TypeError, ValueError):
            return SendResult(success=False, error="Invalid VK private chat ID")
        if peer_id <= 0 or str(peer_id) not in self.allowed_users:
            return SendResult(success=False, error="VK target is not in VK_ALLOWED_USERS")
        chunks = split_vk_message(content)
        metadata = metadata or {}
        keyboard = metadata.get("unified_vk_keyboard")
        fixed_random_id = metadata.get("unified_vk_random_id")
        if keyboard is not None and not isinstance(keyboard, dict):
            return SendResult(success=False, error="Invalid VK keyboard")
        if fixed_random_id is not None and (not isinstance(fixed_random_id, int) or not 0 < fixed_random_id < 2_147_483_648):
            return SendResult(success=False, error="Invalid VK random_id")
        if (keyboard is not None or fixed_random_id is not None) and len(chunks) != 1:
            return SendResult(success=False, error="VK callback messages must fit in one chunk")
        message_ids = []
        for index, chunk in enumerate(chunks):
            params = {"peer_id": peer_id, "random_id": fixed_random_id or secrets.randbelow(2_147_483_647) + 1, "message": chunk}
            if keyboard is not None:
                params["keyboard"] = json.dumps(keyboard, ensure_ascii=False, separators=(",", ":"))
            try:
                response = await asyncio.to_thread(
                    vk_api_call,
                    "messages.send",
                    params,
                    self.token,
                )
            except VkApiError as error:
                return SendResult(success=False, error=f"VK API error {error.code}", raw_response={"delivered_chunks": len(message_ids), "total_chunks": len(chunks)})
            except Exception as error:
                return SendResult(success=False, error=f"VK transport error ({type(error).__name__})", raw_response={"delivered_chunks": len(message_ids), "total_chunks": len(chunks)})
            message_ids.append(str(response) if response is not None else "")
            if index + 1 < len(chunks):
                await asyncio.sleep(0.05)
        return SendResult(
            success=True,
            message_id=message_ids[-1] if message_ids else None,
            raw_response={"chunk_ids": message_ids} if len(message_ids) > 1 else None,
        )

    async def edit_message(self, chat_id: str, message_id: str, content: str, *, clear_keyboard: bool = False) -> bool:
        if str(chat_id) not in self.allowed_users or not str(message_id).isdecimal() or not self.token:
            return False
        params: dict[str, Any] = {"peer_id": int(chat_id), "message_id": int(message_id), "message": content}
        if clear_keyboard:
            params["keyboard"] = json.dumps({"inline": True, "buttons": []}, separators=(",", ":"))
        try:
            return bool(await asyncio.to_thread(vk_api_call, "messages.edit", params, self.token))
        except (VkApiError, VkTransportError):
            logger.warning("VK message edit failed")
            return False

    async def delete_message(self, chat_id: str, message_id: str) -> bool:
        if str(chat_id) not in self.allowed_users or not str(message_id).isdecimal() or not self.token:
            return False
        try:
            result = await asyncio.to_thread(vk_api_call, "messages.delete", {"message_ids": message_id, "delete_for_all": 1}, self.token)
            return isinstance(result, dict) and str(result.get(str(message_id))) == "1"
        except (VkApiError, VkTransportError):
            logger.warning("VK message delete failed")
            return False

    async def send_typing(self, chat_id: str, metadata: Optional[dict[str, Any]] = None) -> None:
        if self._typing_disabled or not self.token:
            return
        try:
            await asyncio.to_thread(
                vk_api_call,
                "messages.setActivity",
                {"peer_id": int(chat_id), "type": "typing", "group_id": int(self.group_id)},
                self.token,
            )
        except VkApiError as error:
            self._typing_disabled = True
            logger.debug("VK typing indicator unavailable (API code %s)", error.code)
        except Exception as error:
            logger.debug("VK typing indicator unavailable (%s)", type(error).__name__)

    def _get_long_poll_server(self) -> dict[str, str]:
        response = vk_api_call("groups.getLongPollServer", {"group_id": int(self.group_id)}, self.token)
        if not isinstance(response, dict):
            raise VkTransportError("invalid_long_poll_server_response")
        server = _checked_long_poll_server(str(response.get("server", "")))
        key = response.get("key")
        ts = response.get("ts")
        if not isinstance(key, str) or not key or ts is None:
            raise VkTransportError("incomplete_long_poll_server_response")
        return {"server": server, "key": key, "ts": str(ts)}

    def _state_path(self) -> str:
        override = os.getenv("VK_STATE_DIR", "").strip()
        directory = Path(override).expanduser() if override else Path(get_hermes_home()) / "plugin-data" / "vk-platform"
        if not directory.is_absolute():
            raise ValueError("VK_STATE_DIR must be an absolute path")
        return str(directory / f"vk-long-poll-{int(self.group_id)}.json")

    def _load_ts(self) -> str:
        paths = [self._state_path()]
        if not os.getenv("VK_STATE_DIR", "").strip():
            filename = f"vk-long-poll-{int(self.group_id)}.json"
            for directory in (Path(get_hermes_home()), _LEGACY_STATE_DIR):
                candidate = str(directory / filename)
                if candidate not in paths:
                    paths.append(candidate)
        for path in paths:
            try:
                with open(path, "r", encoding="utf-8") as state_file:
                    state = json.load(state_file)
                ts = str(state.get("ts", ""))
                return ts if ts.isdecimal() else ""
            except FileNotFoundError:
                continue
            except (OSError, ValueError, TypeError):
                return ""
        return ""

    def _save_ts(self) -> None:
        if not self._ts.isdecimal():
            return
        path = self._state_path()
        directory = os.path.dirname(path)
        os.makedirs(directory, mode=0o700, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".vk-long-poll-", dir=directory)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as state_file:
                json.dump({"group_id": self.group_id, "ts": self._ts}, state_file)
                state_file.flush()
                os.fsync(state_file.fileno())
            os.replace(temporary, path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    async def _recover_long_poll(self, failed: int, payload: dict[str, Any]) -> None:
        if failed == 1 and payload.get("ts") is not None:
            new_ts = str(payload["ts"])
            if new_ts.isdecimal():
                self._ts = new_ts
                await asyncio.to_thread(self._save_ts)
            return
        old_ts = self._ts
        info = await asyncio.to_thread(self._get_long_poll_server)
        self._server, self._key = info["server"], info["key"]
        # Failed=2 means the key expired: preserve the event cursor. Failed=3
        # means VK discarded the queue, so accept the newly issued cursor.
        self._ts = info["ts"] if failed == 3 else (old_ts or info["ts"])
        await asyncio.to_thread(self._save_ts)
        if failed == 3:
            logger.warning("VK Long Poll queue was reset by VK; resumed from the new cursor")

    async def _dispatch_update(self, update: Any) -> None:
        if isinstance(update, dict) and update.get("type") == "message_event":
            await self._dispatch_message_event(update)
            return
        incoming = normalize_message_update(update, self.group_id)
        if incoming is None or incoming.user_id not in self.allowed_users:
            return
        event_key = incoming.event_id or incoming.message_id
        if not event_key:
            event_key = hashlib.sha256(f"{incoming.user_id}\0{incoming.text}".encode("utf-8")).hexdigest()
        if event_key in self._seen:
            return
        source = self.build_source(
            chat_id=incoming.user_id,
            chat_type="dm",
            user_id=incoming.user_id,
            message_id=incoming.message_id,
        )
        event = MessageEvent(
            text=incoming.text,
            message_type=MessageType.TEXT,
            user_id=incoming.user_id,
            source=source,
            message_id=incoming.message_id,
            metadata={"vk_event_id": incoming.event_id} if incoming.event_id else {},
        )
        await self.handle_message(event)
        self._seen[event_key] = None
        self._seen.move_to_end(event_key)
        if len(self._seen) > _MAX_RECENT_EVENTS:
            self._seen.popitem(last=False)

    async def _dispatch_message_event(self, update: dict[str, Any]) -> None:
        event = update.get("object")
        if not isinstance(event, dict) or self._message_event_handler is None:
            return
        user_id, peer_id = str(event.get("user_id", "")), str(event.get("peer_id", ""))
        event_id = str(event.get("event_id", ""))
        if not user_id.isdecimal() or user_id not in self.allowed_users or peer_id != user_id or not event_id:
            return
        payload = event.get("payload")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (TypeError, ValueError):
                return
        if not isinstance(payload, dict) or not isinstance(payload.get("hui"), str):
            return
        callback_data = f"hui:{payload['hui']}"
        conversation_id = str(event.get("conversation_message_id", ""))
        if not conversation_id.isdecimal():
            return
        try:
            resolved = await asyncio.to_thread(vk_api_call, "messages.getByConversationMessageId", {"peer_id": int(peer_id), "conversation_message_ids": conversation_id}, self.token)
            items = resolved.get("items", []) if isinstance(resolved, dict) else []
            message_id = str(items[0].get("id", "")) if items else ""
            if not message_id.isdecimal():
                return
            handled = await self._message_event_handler(callback_data, peer_id=peer_id, user_id=user_id, message_id=message_id)
        except Exception:
            logger.exception("VK message event handler failed")
            return
        if handled:
            try:
                await asyncio.to_thread(vk_api_call, "messages.sendMessageEventAnswer", {"event_id": event_id, "user_id": int(user_id), "peer_id": int(peer_id), "event_data": json.dumps({"type": "show_snackbar", "text": "Принято"}, ensure_ascii=False)}, self.token)
            except (VkApiError, VkTransportError):
                logger.warning("VK callback acknowledgement transport failed")

    async def _poll_loop(self) -> None:
        delay = _BACKOFF_INITIAL
        consecutive_errors = 0
        while True:
            try:
                payload = await asyncio.to_thread(vk_long_poll_call, self._server, self._key, self._ts)
                failed = payload.get("failed")
                if failed is not None:
                    await self._recover_long_poll(int(failed), payload)
                    consecutive_errors = 0
                    delay = _BACKOFF_INITIAL
                    continue
                updates = payload.get("updates", [])
                if not isinstance(updates, list):
                    raise VkTransportError("invalid_updates_list")
                for update in updates:
                    await self._dispatch_update(update)
                new_ts = payload.get("ts")
                if new_ts is not None and str(new_ts).isdecimal():
                    self._ts = str(new_ts)
                    await asyncio.to_thread(self._save_ts)
                consecutive_errors = 0
                delay = _BACKOFF_INITIAL
            except asyncio.CancelledError:
                raise
            except VkApiError as error:
                consecutive_errors += 1
                logger.warning("VK Long Poll API error %s; reconnecting", error.code)
                await self._reconnect_after_error(consecutive_errors, delay)
                delay = min(delay * 2, _BACKOFF_MAX)
            except Exception as error:
                consecutive_errors += 1
                logger.warning("VK Long Poll request failed (%s); reconnecting", type(error).__name__)
                await self._reconnect_after_error(consecutive_errors, delay)
                delay = min(delay * 2, _BACKOFF_MAX)

    async def _reconnect_after_error(self, errors: int, delay: float) -> None:
        await asyncio.sleep(random.uniform(delay / 2, delay))
        if errors < 3:
            return
        try:
            info = await asyncio.to_thread(self._get_long_poll_server)
            self._server, self._key = info["server"], info["key"]
            self._ts = self._ts or info["ts"]
        except Exception as error:
            logger.debug("VK Long Poll server refresh failed (%s)", type(error).__name__)
            return
        errors = 0


def _env_enablement() -> Optional[dict[str, Any]]:
    group_id = os.getenv("VK_GROUP_ID", "").strip()
    allowed_users = parse_allowed_users(os.getenv("VK_ALLOWED_USERS"))
    if not _secret("VK_TOKEN") or not group_id.isdecimal() or not allowed_users:
        return None
    return {"group_id": group_id, "allowed_users": sorted(allowed_users)}


def _validate_config(config: Any) -> bool:
    extra = getattr(config, "extra", {}) or {}
    group_id = str(extra.get("group_id") or os.getenv("VK_GROUP_ID", "")).strip()
    allowed = parse_allowed_users(os.getenv("VK_ALLOWED_USERS"))
    if not allowed:
        allowed = parse_allowed_users(",".join(map(str, extra.get("allowed_users", []))))
    return bool(_secret("VK_TOKEN") and group_id.isdecimal() and allowed)


def register(ctx: Any) -> None:
    """Register the VK adapter through Hermes' supported platform plugin API."""
    ctx.register_platform(
        name="vk",
        label="VK",
        adapter_factory=VkAdapter,
        check_fn=lambda: True,
        validate_config=_validate_config,
        required_env=["VK_TOKEN", "VK_GROUP_ID"],
        env_enablement_fn=_env_enablement,
        allowed_users_env="VK_ALLOWED_USERS",
        max_message_length=VK_MESSAGE_LIMIT,
    )
