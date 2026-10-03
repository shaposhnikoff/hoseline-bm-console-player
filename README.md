# Hoseline BM Console Player

Play a BrandMeister talkgroup in the terminal with `mpv`, display the active
operator, and optionally save each call as a WAV file.

[Hoseline playback flow diagram](https://github.com/shaposhnikoff/hoseline-bm-console-player/blob/main/docs/diagrams/hoseline-playback-flow.md)

## Play Hoseline audio

Install `mpv` so it is on your `PATH`, then run:

```bash
uv run --package bm-monitor python hoseline_player.py 91
```

The talkgroup defaults to 91 if omitted. The player reads the current Hoseline
client token from its public page, joins that talkgroup through the `spotter`
WebSocket, and plays incoming G.711 mu-law audio. Set `BM_HOSELINE_TOKEN` to
override automatic token discovery. Press Ctrl+C to stop. The player exits if
the audio connection closes. It collects one second of audio before playback to
smooth irregular packet arrival. It decodes G.711 mu-law to PCM and continuously
feeds `mpv`, inserting silence between calls so the audio device stays active.
Increase the delay with `--buffer-ms 1500` if audio still crackles. The incoming
stream is fixed at 64 kbit/s G.711 mu-law.

At call start, the player matches the Hoseline source DMR ID with
`data/users.json` and displays the registered callsign, name, city, and country.
The next console line spells the callsign using the radio phonetic alphabet,
keeping digits as digits (for example, `UT3UDX` becomes
`Uniform Tango 3 Uniform Delta X-ray`).
Each new call starts with a horizontal separator in the console.
Use `--users-json /path/to/users.json` for another export. The local JSON is
not included in this repository. It is read at startup, so restart the player
after updating it. If a record is absent,
the player uses the BM Last Heard feed for a callsign and name when they arrive;
the city and country then show as `Unknown`. If that feed fails, audio still plays.
Directory locations are registration data, not a live position.

Recording is off by default. To save each call as a separate 8 kHz mono WAV:

```bash
uv run --package bm-monitor python hoseline_player.py 91 --record
```

Files go to the ignored `recordings/` directory. Set `--record-dir /path/to/dir`
to choose another location. Filenames use
`TG<group>_<callsign>_<YYYYMMDD>_<HHMMSSffffff>.wav` in UTC, for example
`TG91_UT3UDX_20261003_153000123456.wav`. A slash in a portable callsign is
replaced with a hyphen. If the callsign is unavailable, the filename contains
`UNKNOWN`; a callsign learned during the call is added when the WAV closes.
Silence between calls is not recorded.
