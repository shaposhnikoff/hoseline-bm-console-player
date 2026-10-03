"""Checks for Hoseline playback, operator display, and optional recording."""

import json
import logging
import re
import wave
from io import BytesIO
from pathlib import Path

import pytest
from hoseline_player import (
    CALL_SEPARATOR,
    PCM_TABLE,
    SILENCE_FRAME,
    AudioPlayout,
    CallRecorder,
    JitterBuffer,
    LastHeardFeed,
    Operator,
    decode_mulaw_sample,
    filename_callsign,
    load_users,
    phonetic_callsign,
)


def test_mulaw_reference_levels() -> None:
    """G.711 silence and peak codes decode to their expected PCM values."""
    assert decode_mulaw_sample(0xFF) == 0
    assert decode_mulaw_sample(0x7F) == 0
    assert decode_mulaw_sample(0x00) == -32124
    assert decode_mulaw_sample(0x80) == 32124


def test_phonetic_callsign_keeps_digits_and_spells_portable_suffix() -> None:
    """A callsign follows the user's radio alphabet example."""
    assert phonetic_callsign("UT3UDX") == "Uniform Tango 3 Uniform Delta X-ray"
    assert phonetic_callsign("ab1c/p") == "Alfa Bravo 1 Charlie Slash Papa"


def test_buffer_releases_frames_in_order_after_one_second() -> None:
    """Eight 120 ms packets remain queued until the 1 s target is reached."""
    jitter_buffer = JitterBuffer(8000)
    frames = [bytes([index]) * 960 for index in range(9)]

    assert [jitter_buffer.push(frame) for frame in frames[:8]] == [b""] * 8
    assert jitter_buffer.push(frames[8]) == b"".join(frames[:9])
    assert jitter_buffer.push(b"next") == b"next"


def test_short_call_is_flushed_and_next_call_buffers_again() -> None:
    """A call shorter than the target remains audible when it ends."""
    jitter_buffer = JitterBuffer(8000)

    assert jitter_buffer.push(b"first") == b""
    assert jitter_buffer.finish_call() == b"first"
    assert jitter_buffer.push(b"second") == b""
    assert jitter_buffer.finish_call() == b"second"


def test_playout_resumes_audio_after_silent_gap() -> None:
    """The audio queue accepts a later operator after an idle period."""
    playout = AudioPlayout(BytesIO())

    assert playout.next_frame() == SILENCE_FRAME
    playout.enqueue(b"\x00" * 960)
    assert playout.next_frame() == PCM_TABLE[0] * 960
    assert playout.next_frame() == SILENCE_FRAME
    playout.enqueue(b"\x80" * 960)
    assert playout.next_frame() == PCM_TABLE[128] * 960


def test_user_directory_matches_spotter_source_id(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The local BM export supplies callsign, name, city, and country."""
    path = tmp_path / "users.json"
    path.write_text(
        json.dumps(
            {
                "users": [
                    {
                        "radio_id": 1234567,
                        "callsign": "TEST1",
                        "name": "Test Operator",
                        "city": "Test City",
                        "country": "Test Country",
                    }
                ]
            }
        )
    )
    users = load_users(path)
    feed = LastHeardFeed(91, users)

    with caplog.at_level(logging.INFO):
        feed.start_call(1234567, 91)
        assert feed.current_callsign() == "TEST1"

    assert users[1234567] == Operator("TEST1", "Test Operator", "Test City", "Test Country")
    assert caplog.text.count(CALL_SEPARATOR) == 1
    assert "TG 91: TEST1; name: Test Operator; from: Test City, Test Country" in caplog.text
    caller_message = next(record.message for record in caplog.records if record.message.startswith("TG 91:"))
    assert caller_message.splitlines()[1] == "Phonetic: Tango Echo Sierra Tango 1"


def test_last_heard_fills_missing_directory_entry(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A matching live update can label a call absent from the JSON export."""
    feed = LastHeardFeed(91, {})
    with caplog.at_level(logging.INFO):
        feed.start_call(1234567, 91)
        assert feed.current_callsign() == ""
        feed._on_mqtt(
            {
                "payload": json.dumps(
                    {
                        "Event": "Session-Update",
                        "SourceID": 7654321,
                        "DestinationID": 91,
                        "SourceCall": "OTHER1",
                        "SourceName": "Other Operator",
                    }
                )
            }
        )
        feed._on_mqtt(
            {
                "payload": json.dumps(
                    {
                        "Event": "Session-Update",
                        "SourceID": 1234567,
                        "DestinationID": 91,
                        "SourceCall": "TEST1",
                        "SourceName": "Test Operator",
                    }
                )
            }
        )
        feed.finish_call()
        assert feed.current_callsign() == ""
        feed._on_mqtt(
            {
                "payload": json.dumps(
                    {
                        "Event": "Session-Update",
                        "SourceID": 1234567,
                        "DestinationID": 91,
                        "SourceCall": "TEST1",
                        "SourceName": "Test Operator",
                    }
                )
            }
        )

    assert caplog.text.count("TG 91: TEST1") == 1
    assert "OTHER1" not in caplog.text
    assert "name: Test Operator; from: Unknown, Unknown" in caplog.text


def test_optional_recorder_splits_calls_without_silence(tmp_path: Path) -> None:
    """WAV output has one file per call with its safe callsign and UTC time."""
    recorder = CallRecorder(tmp_path / "recordings", 91)
    assert not recorder.directory.exists()
    recorder.write_audio(b"\xff\x80", "UT3UDX")
    recorder.finish_call("UT3UDX")
    recorder.write_audio(b"\x00")
    recorder.finish_call("AB1C/P")

    paths = sorted(recorder.directory.glob("*.wav"))
    assert len(paths) == 2
    names = {path.name for path in paths}
    assert any(re.fullmatch(r"TG91_UT3UDX_\d{8}_\d{12}\.wav", name) for name in names)
    assert any(re.fullmatch(r"TG91_AB1C-P_\d{8}_\d{12}\.wav", name) for name in names)
    assert filename_callsign("../../AB1C/P") == "AB1C-P"
    assert not any("UNKNOWN" in name for name in names)
    first = next(path for path in paths if "UT3UDX" in path.name)
    second = next(path for path in paths if "AB1C-P" in path.name)
    with wave.open(str(first), "rb") as recording:
        assert recording.getframerate() == 8000
        assert recording.getnchannels() == 1
        assert recording.readframes(2) == PCM_TABLE[255] + PCM_TABLE[128]
    with wave.open(str(second), "rb") as recording:
        assert recording.readframes(1) == PCM_TABLE[0]
