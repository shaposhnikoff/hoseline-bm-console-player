"""Serve a local Hoseline dashboard and relay one talkgroup to each browser."""

import argparse
import asyncio
import contextlib
import json
import logging
import os
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp
import msgpack  # type: ignore[import-untyped]
import socketio  # type: ignore[import-untyped]
from aiohttp import web
from hoseline_player import (
    DEFAULT_USERS_JSON,
    LAST_HEARD_URL,
    PAGE_URL,
    PCM_TABLE,
    SPOTTER_URL,
    CallRecorder,
    JitterBuffer,
    Operator,
    clean_text,
    load_users,
    phonetic_callsign,
)

LOGGER = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent
TALKGROUP_URL = "https://api.brandmeister.network/v2/talkgroup"
MAX_LISTENERS = 8


def operator_details(operator: Operator | None) -> dict[str, str]:
    """Return safe, display-ready fields for a known operator."""
    if operator is None:
        return {"callsign": "", "name": "", "city": "", "country": "", "phonetic": ""}
    return {
        "callsign": operator.callsign,
        "name": operator.name,
        "city": operator.city,
        "country": operator.country,
        "phonetic": phonetic_callsign(operator.callsign),
    }


class SessionStore:
    """Maintain the small recent-call view shown on the dashboard."""

    def __init__(self, users: dict[int, Operator]) -> None:
        self.users = users
        self.sessions: dict[str, dict[str, Any]] = {}
        self.connected = False

    def update(self, message: object) -> None:
        """Apply one Last Heard MQTT event without trusting its text fields."""
        if not isinstance(message, dict) or not isinstance(message.get("payload"), str):
            return
        try:
            call = json.loads(message["payload"])
        except (TypeError, ValueError):
            return
        if not isinstance(call, dict):
            return
        group = call.get("DestinationID")
        source = call.get("SourceID")
        if not isinstance(group, int) or group <= 90 or not isinstance(source, int):
            return
        session_id = call.get("SessionID")
        key = str(session_id) if isinstance(session_id, (int, str)) else f"{source}:{group}"
        now = time.monotonic()
        event = call.get("Event")
        if event == "Session-Stop":
            if key in self.sessions:
                self.sessions[key]["active"] = False
                self.sessions[key]["updated"] = now
            return
        if event not in {"Session-Start", "Session-Update"}:
            return
        registered = self.users.get(source)
        operator = operator_details(registered)
        operator["callsign"] = operator["callsign"] or clean_text(call.get("SourceCall"), 24)
        operator["name"] = operator["name"] or clean_text(call.get("SourceName"))
        operator["phonetic"] = phonetic_callsign(operator["callsign"])
        existing = self.sessions.get(key)
        self.sessions[key] = {
            "group": group,
            "source": source,
            "operator": operator,
            "alias": clean_text(call.get("TalkerAlias"), 80),
            "master": clean_text(str(call.get("Master", "")), 20),
            "active": True,
            "started": existing["started"] if existing and existing["active"] else now,
            "updated": now,
        }

    def snapshot(self) -> list[dict[str, Any]]:
        """Return one current or recent call per talkgroup, dropping stale entries."""
        now = time.monotonic()
        self.sessions = {
            key: call for key, call in self.sessions.items() if now - call["updated"] < (90 if call["active"] else 30)
        }
        by_group: dict[int, dict[str, Any]] = {}
        for call in self.sessions.values():
            previous = by_group.get(call["group"])
            if previous is None or (call["active"], call["updated"]) > (previous["active"], previous["updated"]):
                by_group[call["group"]] = call
        return [
            {**call, "elapsed": int(now - call["started"]), "age": int(now - call["updated"])}
            for call in sorted(by_group.values(), key=lambda item: item["group"])
        ]

    def find_operator(self, source: object, group: int) -> Operator | None:
        """Resolve a speaker from the local directory or recent live metadata."""
        if not isinstance(source, int):
            return None
        if source in self.users and self.users[source].callsign:
            return self.users[source]
        for call in reversed(list(self.sessions.values())):
            if call["source"] == source and call["group"] == group:
                details = call["operator"]
                return Operator(details["callsign"], details["name"], details["city"], details["country"])
        return None


@dataclass
class Runtime:
    """Own the shared feed, outbound client, caches, and counters."""

    store: SessionStore
    record_dir: Path
    buffer_ms: int
    http: aiohttp.ClientSession | None = None
    groups: dict[str, str] = field(default_factory=dict)
    groups_until: float = 0.0
    token: str = ""
    token_until: float = 0.0
    listeners: int = 0
    calls: int = 0
    audio_packets: int = 0
    sockets: set[web.WebSocketResponse] = field(default_factory=set)

    @property
    def client(self) -> aiohttp.ClientSession:
        """Return the live outbound session after application startup."""
        if self.http is None:
            raise RuntimeError("HTTP client is not ready")
        return self.http


RUNTIME = web.AppKey("runtime", Runtime)


async def monitor_last_heard(store: SessionStore, http: aiohttp.ClientSession) -> None:
    """Reconnect the shared Last Heard feed without blocking the web server."""
    while True:
        client = socketio.AsyncClient(
            reconnection=False,
            request_timeout=5,
            http_session=http,
            handle_sigint=False,
        )

        @client.event
        async def connect(active_client: socketio.AsyncClient = client) -> None:
            store.connected = True
            await active_client.emit("join", "everything")
            await active_client.emit("sendCurrentSessions")

        @client.event
        async def disconnect() -> None:
            store.connected = False

        client.on("mqtt", handler=store.update)
        try:
            await client.connect(LAST_HEARD_URL, socketio_path="lh", transports=["websocket"], wait_timeout=5)
            await client.wait()
        except asyncio.CancelledError:
            raise
        except (OSError, socketio.exceptions.ConnectionError) as exc:
            LOGGER.warning("Last Heard connection unavailable: %s", type(exc).__name__)
        finally:
            store.connected = False
            if client.connected:
                await client.disconnect()
        await asyncio.sleep(5)


async def get_spotter_token(app: web.Application) -> str:
    """Read and briefly cache the token published by the Hoseline page."""
    runtime = app[RUNTIME]
    if token := os.getenv("BM_HOSELINE_TOKEN"):
        return token
    if runtime.token and time.monotonic() < runtime.token_until:
        return runtime.token
    async with runtime.client.get(PAGE_URL, timeout=aiohttp.ClientTimeout(total=15)) as response:
        response.raise_for_status()
        page = await response.text()
    client = re.search(r'new\s+\w+\("wss://hose\.brandmeister\.network/spotter/",(\w+)\)', page)
    if client is None:
        raise RuntimeError("Hoseline token reference was not found")
    assignment = re.search(rf'\b{re.escape(client.group(1))}="([^"]+)"', page)
    if assignment is None:
        raise RuntimeError("Hoseline token value was not found")
    runtime.token = assignment.group(1)
    runtime.token_until = time.monotonic() + 900
    return runtime.token


async def get_talkgroups(app: web.Application) -> dict[str, str]:
    """Refresh BM talkgroup names at most once an hour."""
    runtime = app[RUNTIME]
    if time.monotonic() < runtime.groups_until:
        return runtime.groups
    runtime.groups_until = time.monotonic() + 60
    try:
        async with runtime.client.get(TALKGROUP_URL, timeout=aiohttp.ClientTimeout(total=15)) as response:
            response.raise_for_status()
            result = await response.json()
        if isinstance(result, dict):
            runtime.groups = {
                key: clean_text(value, 80)
                for key, value in result.items()
                if isinstance(key, str) and key.isdigit() and isinstance(value, str)
            }
            runtime.groups_until = time.monotonic() + 3600
    except (TimeoutError, aiohttp.ClientError, ValueError) as exc:
        LOGGER.warning("Talkgroup names unavailable: %s", type(exc).__name__)
    return runtime.groups


async def relay_audio(browser: web.WebSocketResponse, group: int, record: bool, app: web.Application) -> None:
    """Decode Hoseline packets and send PCM plus call events to one browser."""
    runtime = app[RUNTIME]
    token = await get_spotter_token(app)
    url = f"{SPOTTER_URL}?{urllib.parse.urlencode({'token': token})}"
    recorder = CallRecorder(runtime.record_dir, group) if record else None
    buffer = JitterBuffer(runtime.buffer_ms * 8)
    callsign = ""
    source: object = None
    try:
        async with runtime.client.ws_connect(
            url, protocols=("spotter",), origin=PAGE_URL.rstrip("/"), heartbeat=20
        ) as upstream:
            if upstream.protocol != "spotter":
                raise RuntimeError("Hoseline spotter protocol was not negotiated")
            await upstream.send_bytes(msgpack.packb([1, [group]]))
            await browser.send_json({"type": "status", "message": f"Listening to TG {group}"})
            async for packet in upstream:
                if browser.closed:
                    break
                if packet.type != aiohttp.WSMsgType.BINARY:
                    continue
                try:
                    message = msgpack.unpackb(packet.data, raw=False, max_bin_len=65536)
                except (ValueError, msgpack.exceptions.UnpackException):
                    continue
                if not isinstance(message, list) or not message:
                    continue
                if message[0] == 11:
                    tail = buffer.finish_call()
                    if tail:
                        await browser.send_bytes(b"".join(PCM_TABLE[code] for code in tail))
                    if recorder is not None:
                        recorder.finish_call(callsign)
                    source = message[2] if len(message) > 2 else None
                    details = runtime.store.find_operator(source, group)
                    callsign = details.callsign if details else ""
                    runtime.calls += 1
                    await browser.send_json(
                        {"type": "call-start", "source": source, "operator": operator_details(details)}
                    )
                elif message[0] == 13:
                    tail = buffer.finish_call()
                    if tail:
                        await browser.send_bytes(b"".join(PCM_TABLE[code] for code in tail))
                    if recorder is not None:
                        recorder.finish_call(callsign)
                    await browser.send_json({"type": "call-end"})
                    callsign = ""
                elif message[0] == 20 and len(message) > 1 and isinstance(message[1], bytes):
                    runtime.audio_packets += 1
                    audio = buffer.push(message[1])
                    if audio:
                        await browser.send_bytes(b"".join(PCM_TABLE[code] for code in audio))
                    if recorder is not None:
                        current = runtime.store.find_operator(source, group)
                        if current is not None:
                            callsign = current.callsign
                        recorder.write_audio(message[1], callsign)
    finally:
        if recorder is not None:
            recorder.finish_call(callsign)


async def audio_socket(request: web.Request) -> web.WebSocketResponse:
    """Open a local audio session for one selected talkgroup."""
    runtime = request.app[RUNTIME]
    origin = request.headers.get("Origin")
    if origin and origin != f"{request.scheme}://{request.host}":
        raise web.HTTPForbidden(text="Open the player from this server")
    try:
        group = int(request.query.get("tg", "91"))
    except ValueError as exc:
        raise web.HTTPBadRequest(text="Invalid talkgroup") from exc
    if not 90 < group <= 99999999:
        raise web.HTTPBadRequest(text="Invalid talkgroup")
    if runtime.listeners >= MAX_LISTENERS:
        raise web.HTTPServiceUnavailable(text="Too many listeners")
    record = request.query.get("record") == "1"
    browser = web.WebSocketResponse(heartbeat=30, max_msg_size=1024)
    await browser.prepare(request)
    runtime.listeners += 1
    runtime.sockets.add(browser)
    relay = asyncio.create_task(relay_audio(browser, group, record, request.app))
    incoming = asyncio.create_task(browser.receive())
    try:
        done, pending = await asyncio.wait({relay, incoming}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        if relay in done:
            try:
                await relay
            except (aiohttp.ClientError, OSError, RuntimeError, ValueError, msgpack.ExtraData) as exc:
                LOGGER.warning("Audio relay stopped: %s", type(exc).__name__)
                if not browser.closed:
                    await browser.send_json({"type": "error", "message": "Hoseline audio disconnected"})
    finally:
        relay.cancel()
        incoming.cancel()
        await asyncio.gather(relay, incoming, return_exceptions=True)
        runtime.listeners -= 1
        runtime.sockets.discard(browser)
        await browser.close()
    return browser


async def state(request: web.Request) -> web.Response:
    """Provide active calls and talkgroup names for the dashboard."""
    groups = await get_talkgroups(request.app)
    store = request.app[RUNTIME].store
    return web.json_response({"sessions": store.snapshot(), "talkgroups": groups, "feedConnected": store.connected})


async def health(_request: web.Request) -> web.Response:
    """Report that the local HTTP service is responsive."""
    return web.json_response({"status": "ok"})


async def metrics(request: web.Request) -> web.Response:
    """Expose simple Prometheus counters for the relay."""
    runtime = request.app[RUNTIME]
    body = (
        f"hoseline_web_listeners {runtime.listeners}\n"
        f"hoseline_web_calls_total {runtime.calls}\n"
        f"hoseline_web_audio_packets_total {runtime.audio_packets}\n"
    )
    return web.Response(text=body, content_type="text/plain")


async def resources(app: web.Application):  # type: ignore[no-untyped-def]
    """Own the outbound HTTP session and background metadata connection."""
    timeout = aiohttp.ClientTimeout(total=None, connect=5, sock_read=None)
    runtime = app[RUNTIME]
    async with aiohttp.ClientSession(timeout=timeout) as client:
        runtime.http = client
        task = asyncio.create_task(monitor_last_heard(runtime.store, client))
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            runtime.http = None


async def close_sockets(app: web.Application) -> None:
    """Close browser streams before the HTTP runner waits for handlers."""
    await asyncio.gather(*(socket.close() for socket in tuple(app[RUNTIME].sockets)), return_exceptions=True)


async def index(_request: web.Request) -> web.FileResponse:
    """Serve the local dashboard entry point."""
    return web.FileResponse(ROOT / "web" / "index.html")


def create_app(users_json: Path, record_dir: Path, buffer_ms: int = 1000) -> web.Application:
    """Build the local-only dashboard server."""
    app = web.Application(client_max_size=1024)
    app[RUNTIME] = Runtime(SessionStore(load_users(users_json)), record_dir, buffer_ms)
    app.cleanup_ctx.append(resources)
    app.on_shutdown.append(close_sockets)
    app.router.add_get("/", index)
    app.router.add_static("/static", ROOT / "web", show_index=False)
    app.router.add_get("/api/state", state)
    app.router.add_get("/ws/audio", audio_socket)
    app.router.add_get("/health", health)
    app.router.add_get("/metrics", metrics)
    return app


def main() -> None:
    """Run the dashboard on a selected local or container interface."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--users-json", type=Path, default=DEFAULT_USERS_JSON)
    parser.add_argument("--record-dir", type=Path, default=ROOT / "recordings")
    parser.add_argument("--buffer-ms", type=int, default=1000)
    args = parser.parse_args()
    if not 0 <= args.buffer_ms <= 3000:
        parser.error("--buffer-ms must be between 0 and 3000")
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    web.run_app(create_app(args.users_json, args.record_dir, args.buffer_ms), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
