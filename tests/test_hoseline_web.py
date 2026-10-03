"""Checks for the local dashboard, active-call view, and browser audio relay."""

import asyncio
import json
import wave
from pathlib import Path

import hoseline_web
import msgpack
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer
from hoseline_player import PCM_TABLE, Operator
from hoseline_web import SessionStore


def call_event(event: str, session: int, source: int, group: int) -> dict[str, str]:
    """Build one Last Heard event with no external service dependency."""
    return {
        "payload": json.dumps(
            {
                "Event": event,
                "SessionID": session,
                "SourceID": source,
                "DestinationID": group,
                "SourceCall": f"TEST{source}",
                "SourceName": "Live Name",
                "TalkerAlias": "Radio alias",
            }
        )
    }


def test_session_store_prefers_active_call_and_registered_operator() -> None:
    """One group card tracks the current call rather than duplicating history."""
    store = SessionStore({123: Operator("AB1C", "Registered", "City", "Country")})
    store.update(call_event("Session-Start", 1, 111, 91))
    store.update(call_event("Session-Stop", 1, 111, 91))
    store.update(call_event("Session-Start", 2, 123, 91))

    current = store.snapshot()
    assert len(current) == 1
    assert current[0]["active"]
    assert current[0]["operator"]["callsign"] == "AB1C"
    assert current[0]["operator"]["country"] == "Country"
    assert store.find_operator(123, 91) == Operator("AB1C", "Registered", "City", "Country")


def test_local_websocket_relays_short_call_and_records_wav(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Browser receives PCM even when the call ends before the jitter target."""

    async def idle_monitor(_store: SessionStore, _http: object) -> None:
        await asyncio.Event().wait()

    async def token(_app: web.Application) -> str:
        return "test-token"

    async def groups(_app: web.Application) -> dict[str, str]:
        return {"91": "World-wide"}

    async def spotter(request: web.Request) -> web.WebSocketResponse:
        assert request.query["token"] == "test-token"
        upstream = web.WebSocketResponse(protocols=("spotter",))
        await upstream.prepare(request)
        joined = await upstream.receive(timeout=2)
        assert msgpack.unpackb(joined.data) == [1, [91]]
        await upstream.send_bytes(msgpack.packb([11, 0, 123, 91]))
        await upstream.send_bytes(msgpack.packb([20, b"\xff" * 960]))
        await upstream.send_bytes(msgpack.packb([13]))
        await upstream.send_bytes(msgpack.packb([11, 0, 456, 91]))
        await upstream.send_bytes(msgpack.packb([20, b"\x80" * 960]))
        await upstream.send_bytes(msgpack.packb([13]))
        await asyncio.sleep(0.1)
        await upstream.close()
        return upstream

    async def run() -> None:
        upstream_app = web.Application()
        upstream_app.router.add_get("/spotter/", spotter)
        upstream_server = TestServer(upstream_app)
        await upstream_server.start_server()
        monkeypatch.setattr(
            hoseline_web, "SPOTTER_URL", str(upstream_server.make_url("/spotter/")).replace("http:", "ws:")
        )
        monkeypatch.setattr(hoseline_web, "monitor_last_heard", idle_monitor)
        monkeypatch.setattr(hoseline_web, "get_spotter_token", token)
        monkeypatch.setattr(hoseline_web, "get_talkgroups", groups)
        app = hoseline_web.create_app(tmp_path / "missing.json", tmp_path / "recordings")
        app[hoseline_web.RUNTIME].store.users[123] = Operator("AB1C", "Test", "City", "Country")
        app[hoseline_web.RUNTIME].store.users[456] = Operator("XY2Z", "Next", "Town", "Country")
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            async with client.get("/health") as response:
                assert response.status == 200
                assert (await response.json())["status"] == "ok"
            async with client.get("/api/state") as response:
                assert (await response.json())["talkgroups"]["91"] == "World-wide"
            async with client.get("/ws/audio?tg=91", headers={"Origin": "https://wrong.example"}) as response:
                assert response.status == 403
            async with client.ws_connect("/ws/audio?tg=91&record=1") as browser:
                assert (await browser.receive_json(timeout=2))["type"] == "status"
                started = await browser.receive_json(timeout=2)
                assert started["operator"]["callsign"] == "AB1C"
                audio = await browser.receive(timeout=2)
                assert audio.type == WSMsgType.BINARY
                assert audio.data == PCM_TABLE[255] * 960
                assert (await browser.receive_json(timeout=2))["type"] == "call-end"
                next_call = await browser.receive_json(timeout=2)
                assert next_call["operator"]["callsign"] == "XY2Z"
                next_audio = await browser.receive(timeout=2)
                assert next_audio.type == WSMsgType.BINARY
                assert next_audio.data == PCM_TABLE[128] * 960
                assert (await browser.receive_json(timeout=2))["type"] == "call-end"
            paths = list((tmp_path / "recordings").glob("*.wav"))
            assert len(paths) == 2
            first = next(path for path in paths if "AB1C" in path.name)
            second = next(path for path in paths if "XY2Z" in path.name)
            with wave.open(str(first), "rb") as recording:
                assert recording.readframes(960) == PCM_TABLE[255] * 960
            with wave.open(str(second), "rb") as recording:
                assert recording.readframes(960) == PCM_TABLE[128] * 960
        finally:
            await client.close()
            await upstream_server.close()

    asyncio.run(run())
