"""Play one BrandMeister Hoseline talkgroup through mpv."""

import argparse
import datetime as dt
import json
import logging
import os
import queue
import re
import shutil
import struct
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import wave
from contextlib import closing
from pathlib import Path
from typing import IO, BinaryIO, NamedTuple

import msgpack  # type: ignore[import-untyped]
import socketio  # type: ignore[import-untyped]
import websocket

PAGE_URL = "https://hose.brandmeister.network/"
SPOTTER_URL = "wss://hose.brandmeister.network/spotter/"
LOGGER = logging.getLogger(__name__)
FRAME_BYTES = 960
FRAME_SECONDS = FRAME_BYTES / 8000
SILENCE_FRAME = b"\x00\x00" * FRAME_BYTES
LAST_HEARD_URL = "https://api.brandmeister.network?version=2"
DEFAULT_USERS_JSON = Path(__file__).with_name("data") / "users.json"
CALL_SEPARATOR = "=" * 60
PHONETIC_WORDS = {
    "A": "Alfa",
    "B": "Bravo",
    "C": "Charlie",
    "D": "Delta",
    "E": "Echo",
    "F": "Foxtrot",
    "G": "Golf",
    "H": "Hotel",
    "I": "India",
    "J": "Juliett",
    "K": "Kilo",
    "L": "Lima",
    "M": "Mike",
    "N": "November",
    "O": "Oscar",
    "P": "Papa",
    "Q": "Quebec",
    "R": "Romeo",
    "S": "Sierra",
    "T": "Tango",
    "U": "Uniform",
    "V": "Victor",
    "W": "Whiskey",
    "X": "X-ray",
    "Y": "Yankee",
    "Z": "Zulu",
    "/": "Slash",
}


class Operator(NamedTuple):
    """Public operator details used for a live call display."""

    callsign: str
    name: str
    city: str
    country: str


def phonetic_callsign(callsign: str) -> str:
    """Spell letters with the radio alphabet while keeping digits as written."""
    return " ".join(PHONETIC_WORDS.get(character, character) for character in callsign.upper())


def filename_callsign(callsign: str) -> str:
    """Keep a callsign in one safe filename component."""
    return re.sub(r"[^A-Z0-9]+", "-", callsign.upper()).strip("-")[:24] or "UNKNOWN"


def clean_text(value: object, limit: int = 80) -> str:
    """Limit untrusted directory and feed text to one printable line."""
    if not isinstance(value, str):
        return ""
    return " ".join(
        "".join(character for character in value if character.isprintable() and character != "\x1b").split()
    )[:limit]


def load_users(path: Path) -> dict[int, Operator]:
    """Index the local BM user export by DMR radio ID."""
    try:
        with path.open(encoding="utf-8") as stream:
            records = json.load(stream)["users"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        LOGGER.warning("User directory unavailable (%s); using live metadata", type(exc).__name__)
        return {}
    if not isinstance(records, list):
        LOGGER.warning("User directory has an unexpected format; using live metadata")
        return {}
    users = {}
    for row in records:
        if not isinstance(row, dict) or not isinstance(row.get("radio_id"), int):
            continue
        users[row["radio_id"]] = Operator(
            clean_text(row.get("callsign"), 24),
            clean_text(row.get("name")),
            clean_text(row.get("city")),
            clean_text(row.get("country")),
        )
    LOGGER.info("Loaded %s user records from %s", len(users), path)
    return users


class LastHeardFeed:
    """Match the current Hoseline call with BM Last Heard metadata."""

    def __init__(self, talkgroup: int, users: dict[int, Operator]) -> None:
        """Prepare a separate Socket.IO connection for speaker metadata."""
        self.talkgroup = talkgroup
        self.users = users
        self.client = socketio.Client(reconnection=True, request_timeout=5)
        self.client.on("connect", handler=self._on_connect)
        self.client.on("mqtt", handler=self._on_mqtt)
        self.lock = threading.Lock()
        self.current: tuple[int, int] | None = None
        self.current_operator: Operator | None = None
        self.displayed = False
        self.recent: dict[tuple[int, int], tuple[Operator, float]] = {}
        self.thread = threading.Thread(target=self._run, name="bm-last-heard", daemon=True)

    def start(self) -> None:
        """Connect without delaying or stopping audio playback."""
        self.thread.start()

    def stop(self) -> None:
        """Disconnect the metadata feed and release its worker thread."""
        if self.client.connected:
            self.client.emit("leave", "everything")
            self.client.disconnect()
        self.thread.join(timeout=2)

    def start_call(self, source: object, destination: object) -> None:
        """Show cached metadata if it belongs to this active call."""
        LOGGER.info(CALL_SEPARATOR)
        if not isinstance(source, int) or not isinstance(destination, int):
            with self.lock:
                self.current = None
                self.current_operator = None
                self.displayed = False
            LOGGER.info("Call started; speaker unknown")
            return
        key = (source, destination)
        with self.lock:
            self.current = key
            self.displayed = False
            cached = self.recent.get(key)
            if source in self.users and self.users[source].callsign:
                self.displayed = True
                details = self.users[source]
            elif cached and time.monotonic() - cached[1] <= 10:
                self.displayed = True
                details = cached[0]
            else:
                details = None
            self.current_operator = details
        if details:
            self._show(details)
        else:
            LOGGER.info("Call started; waiting for speaker details")

    def finish_call(self) -> None:
        """Discard the current call so delayed events cannot relabel the next one."""
        with self.lock:
            self.current = None
            self.current_operator = None
            self.displayed = False

    def current_callsign(self) -> str:
        """Return the current operator's callsign for optional recording."""
        with self.lock:
            return self.current_operator.callsign if self.current_operator is not None else ""

    def _show(self, operator: Operator) -> None:
        """Display operator details from the local directory or Last Heard."""
        LOGGER.info(
            "TG %s: %s; name: %s; from: %s, %s\nPhonetic: %s",
            self.talkgroup,
            operator.callsign,
            operator.name or "Unknown",
            operator.city or "Unknown",
            operator.country or "Unknown",
            phonetic_callsign(operator.callsign),
        )

    def _on_connect(self) -> None:
        self.client.emit("join", "everything")
        self.client.emit("sendCurrentSessions")

    def _on_mqtt(self, data: object) -> None:
        if not isinstance(data, dict) or not isinstance(data.get("payload"), str):
            return
        try:
            call = json.loads(data["payload"])
        except (TypeError, ValueError):
            return
        if not isinstance(call, dict) or call.get("DestinationID") != self.talkgroup:
            return
        source = call.get("SourceID")
        callsign = call.get("SourceCall")
        if not isinstance(source, int) or not isinstance(callsign, str):
            return
        callsign = clean_text(callsign, 24)
        if not callsign or len(callsign) > 24 or not re.fullmatch(r"[A-Za-z0-9/]+", callsign):
            return
        operator = Operator(callsign, clean_text(call.get("SourceName")), "", "")
        key = (source, self.talkgroup)
        with self.lock:
            if len(self.recent) > 64:
                now = time.monotonic()
                self.recent = {
                    stored_key: details for stored_key, details in self.recent.items() if now - details[1] <= 10
                }
            if call.get("Event") == "Session-Stop":
                self.recent.pop(key, None)
            elif call.get("Event") in {"Session-Start", "Session-Update"}:
                self.recent[key] = (operator, time.monotonic())
            if self.current == key and not self.displayed:
                self.displayed = True
                self.current_operator = operator
                show = True
            else:
                show = False
        if show:
            self._show(operator)

    def _run(self) -> None:
        try:
            self.client.connect(
                LAST_HEARD_URL,
                socketio_path="lh",
                transports=["websocket"],
                wait_timeout=5,
            )
            self.client.wait()
        except (OSError, socketio.exceptions.ConnectionError):
            LOGGER.warning("Speaker metadata feed unavailable; audio continues")


class CallRecorder:
    """Write one mono PCM WAV file per call when recording is enabled."""

    def __init__(self, directory: Path, talkgroup: int) -> None:
        """Prepare a lazy recorder; no file is created before audio arrives."""
        self.directory = directory
        self.talkgroup = talkgroup
        self.writer: wave.Wave_write | None = None
        self.file: BinaryIO | None = None
        self.path: Path | None = None
        self.timestamp = ""
        self.callsign = "UNKNOWN"

    def write_audio(self, audio: bytes, callsign: str = "") -> None:
        """Record received speech without the mpv silence or jitter buffer."""
        if not audio:
            return
        if self.writer is None:
            self.directory.mkdir(parents=True, exist_ok=True)
            self.timestamp = dt.datetime.now(dt.UTC).strftime("%Y%m%d_%H%M%S%f")
            self.callsign = filename_callsign(callsign)
            self.path = self.directory / f"TG{self.talkgroup}_{self.callsign}_{self.timestamp}.wav"
            self.file = self.path.open("xb")
            self.writer = wave.open(self.file, "wb")
            self.writer.setnchannels(1)
            self.writer.setsampwidth(2)
            self.writer.setframerate(8000)
            LOGGER.info("Recording started")
        self.writer.writeframesraw(b"".join(PCM_TABLE[code] for code in audio))

    def finish_call(self, callsign: str = "") -> None:
        """Finalize the WAV file and add a callsign learned during the call."""
        if self.writer is not None:
            self.writer.close()
            self.writer = None
        if self.file is not None:
            self.file.close()
            self.file = None
        if self.path is not None:
            final_callsign = filename_callsign(callsign)
            if final_callsign != "UNKNOWN" and final_callsign != self.callsign:
                target = self.directory / f"TG{self.talkgroup}_{final_callsign}_{self.timestamp}.wav"
                try:
                    os.link(self.path, target)
                    self.path.unlink()
                    self.path = target
                except OSError:
                    LOGGER.warning("Could not add callsign to recording filename")
            LOGGER.info("Saved recording: %s", self.path)
            self.path = None


def decode_mulaw_sample(code: int) -> int:
    """Convert one G.711 mu-law code to a signed 16-bit PCM sample."""
    value = ~code & 0xFF
    magnitude = (((value & 0x0F) << 3) + 0x84) << ((value & 0x70) >> 4)
    sample = magnitude - 0x84
    return -sample if value & 0x80 else sample


PCM_TABLE = tuple(struct.pack("<h", decode_mulaw_sample(code)) for code in range(256))


class JitterBuffer:
    """Hold the start of a call before sending audio to the player."""

    def __init__(self, target_bytes: int) -> None:
        """Set the number of mu-law bytes to collect before playback."""
        self.target_bytes = target_bytes
        self.pending = bytearray()
        self.buffering = True

    def push(self, frame: bytes) -> bytes:
        """Return audio once the initial buffer is full, or empty bytes."""
        if not self.buffering:
            return frame
        self.pending.extend(frame)
        if len(self.pending) < self.target_bytes:
            return b""
        self.buffering = False
        audio = bytes(self.pending)
        self.pending.clear()
        return audio

    def finish_call(self) -> bytes:
        """Return any short-call audio and prepare for the next call."""
        audio = bytes(self.pending)
        self.pending.clear()
        self.buffering = True
        return audio


class AudioPlayout:
    """Keep mpv supplied with PCM audio or silence at a steady rate."""

    def __init__(self, stream: IO[bytes]) -> None:
        """Create a bounded queue for the player's input stream."""
        self.stream = stream
        self.frames: queue.Queue[bytes] = queue.Queue(maxsize=32)
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, name="hoseline-audio", daemon=True)
        self.error: OSError | None = None

    def start(self) -> None:
        """Start writing fixed-duration frames to the player."""
        self.thread.start()

    def enqueue(self, audio: bytes) -> None:
        """Queue audio frames, dropping the oldest frame if the player falls behind."""
        for offset in range(0, len(audio), FRAME_BYTES):
            frame = audio[offset : offset + FRAME_BYTES]
            frame += b"\xff" * (FRAME_BYTES - len(frame))
            pcm = b"".join(PCM_TABLE[code] for code in frame)
            try:
                self.frames.put_nowait(pcm)
            except queue.Full:
                try:
                    self.frames.get_nowait()
                except queue.Empty:
                    pass
                self.frames.put_nowait(pcm)

    def next_frame(self) -> bytes:
        """Return queued audio, or silence when no call is being played."""
        try:
            return self.frames.get_nowait()
        except queue.Empty:
            return SILENCE_FRAME

    def stop(self) -> None:
        """Ask the writer to stop and wait briefly for it."""
        self.stop_event.set()
        self.thread.join(timeout=1)

    def _run(self) -> None:
        """Write one 120 ms frame per tick to keep the audio device active."""
        next_write = time.monotonic()
        while not self.stop_event.is_set():
            try:
                self.stream.write(self.next_frame())
                self.stream.flush()
            except OSError as exc:
                self.error = exc
                self.stop_event.set()
                return
            next_write = max(next_write + FRAME_SECONDS, time.monotonic())
            self.stop_event.wait(next_write - time.monotonic())


def get_spotter_token() -> str:
    """Return the site token, or an explicit environment override.

    Raises:
        RuntimeError: If the current Hoseline page does not expose its token.
        urllib.error.URLError: If the page cannot be fetched.
    """
    if token := os.getenv("BM_HOSELINE_TOKEN"):
        return token

    with urllib.request.urlopen(PAGE_URL, timeout=10) as response:  # noqa: S310
        page = response.read().decode("utf-8")

    client = re.search(
        r'new\s+\w+\("wss://hose\.brandmeister\.network/spotter/",(\w+)\)',
        page,
    )
    if client is None:
        raise RuntimeError("Could not find the Hoseline client token reference")

    assignment = re.search(rf'\b{re.escape(client.group(1))}="([^"]+)"', page)
    if assignment is None:
        raise RuntimeError("Could not find the Hoseline client token value")
    return assignment.group(1)


def play_talkgroup(
    talkgroup: int,
    token: str,
    buffer_ms: int,
    users: dict[int, Operator],
    record_dir: Path | None = None,
) -> None:
    """Subscribe to one talkgroup and pipe decoded PCM audio to mpv.

    Args:
        talkgroup: BrandMeister talkgroup number.
        token: Token published by Hoseline or supplied through the environment.
        buffer_ms: Initial audio buffer in milliseconds.
        users: Local BM user directory indexed by DMR radio ID.
        record_dir: Optional directory for one WAV file per call.

    Raises:
        RuntimeError: If mpv exits or the negotiated protocol is unexpected.
        websocket.WebSocketException: If the connection fails.
    """
    query = urllib.parse.urlencode({"token": token})
    url = f"{SPOTTER_URL}?{query}"
    mpv = [
        "mpv",
        "--no-config",
        "--no-video",
        "--cache=no",
        "--stream-buffer-size=4k",
        "--audio-channels=stereo",
        "--audio-samplerate=48000",
        "--demuxer=rawaudio",
        "--demuxer-rawaudio-format=s16le",
        "--demuxer-rawaudio-rate=8000",
        "--demuxer-rawaudio-channels=mono",
        "--term-status-msg=",
        "fd://0",
    ]

    with closing(
        websocket.create_connection(
            url,
            subprotocols=["spotter"],
            origin=PAGE_URL.rstrip("/"),
            timeout=10,
        )
    ) as socket:
        if socket.getsubprotocol() != "spotter":
            raise RuntimeError("Hoseline did not negotiate the spotter protocol")
        socket.settimeout(1)
        socket.send_binary(msgpack.packb([1, [talkgroup]]))
        LOGGER.info("Subscribed to TG %s; waiting for audio. Press Ctrl+C to stop.", talkgroup)

        with subprocess.Popen(mpv, stdin=subprocess.PIPE) as player:
            if player.stdin is None:
                raise RuntimeError("Could not open mpv input")
            playout = AudioPlayout(player.stdin)
            playout.start()
            metadata = LastHeardFeed(talkgroup, users)
            metadata.start()
            recorder = CallRecorder(record_dir, talkgroup) if record_dir is not None else None
            audio_announced = False
            jitter_buffer = JitterBuffer(buffer_ms * 8)
            try:
                while True:
                    if playout.error is not None or player.poll() is not None:
                        raise RuntimeError("mpv stopped during playback")
                    try:
                        raw = socket.recv()
                    except websocket.WebSocketTimeoutException:
                        continue
                    if not raw:
                        break
                    if not isinstance(raw, bytes):
                        continue
                    message = msgpack.unpackb(raw, raw=False)
                    if not isinstance(message, list) or not message:
                        continue
                    if message[0] == 11:
                        tail = jitter_buffer.finish_call()
                        if tail:
                            playout.enqueue(tail)
                        if recorder is not None:
                            recorder.finish_call(metadata.current_callsign())
                        metadata.finish_call()
                        metadata.start_call(
                            message[2] if len(message) > 2 else None,
                            message[3] if len(message) > 3 else None,
                        )
                        audio_announced = False
                    elif message[0] == 13:
                        LOGGER.info("Call ended")
                        tail = jitter_buffer.finish_call()
                        if tail:
                            playout.enqueue(tail)
                        if recorder is not None:
                            recorder.finish_call(metadata.current_callsign())
                        metadata.finish_call()
                    elif message[0] == 20 and len(message) > 1 and isinstance(message[1], bytes):
                        if not audio_announced:
                            LOGGER.info("Audio packets received; buffering %s ms", buffer_ms)
                            audio_announced = True
                        audio = jitter_buffer.push(message[1])
                        if audio:
                            playout.enqueue(audio)
                        if recorder is not None:
                            recorder.write_audio(message[1], metadata.current_callsign())
            finally:
                metadata.stop()
                if recorder is not None:
                    recorder.finish_call(metadata.current_callsign())
                playout.stop()
                if playout.thread.is_alive():
                    player.terminate()
                    playout.thread.join(timeout=2)
                player.stdin.close()


def main() -> None:
    """Parse the talkgroup and run the Hoseline player."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("talkgroup", type=int, nargs="?", default=91)
    parser.add_argument("--buffer-ms", type=int, default=1000)
    parser.add_argument("--users-json", type=Path, default=DEFAULT_USERS_JSON)
    parser.add_argument("--record", action="store_true", help="Save each call as a WAV file")
    parser.add_argument("--record-dir", type=Path, default=Path(__file__).with_name("recordings"))
    args = parser.parse_args()
    if args.talkgroup <= 90:
        parser.error("talkgroup must be greater than 90")
    if not 0 <= args.buffer_ms <= 3000:
        parser.error("--buffer-ms must be between 0 and 3000")
    if shutil.which("mpv") is None:
        parser.error("mpv is required")

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        users = load_users(args.users_json)
        record_dir = args.record_dir if args.record else None
        play_talkgroup(args.talkgroup, get_spotter_token(), args.buffer_ms, users, record_dir)
    except KeyboardInterrupt:
        LOGGER.info("Stopped")
    except (OSError, RuntimeError, ValueError, websocket.WebSocketException) as exc:
        LOGGER.error("Player stopped: %s", type(exc).__name__)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
