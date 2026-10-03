# Local Hoseline web interface

- [x] Inspect the existing player, the original Hoseline interface, and project instructions.
- [x] Add a local web server for live sessions, talkgroup labels, and audio relay.
- [x] Add a browser player with active-call cards, filtering, theme, and recording control.
- [x] Select and mix up to four talkgroups in the browser player.
- [x] Add QRZ.com and RadioID profile icons beside the current callsign.
- [x] Package the web interface in Docker and document build, run, and storage.
- [x] Verify the application, audio protocol, tests, and container build.

## Acceptance

- The page opens at `http://127.0.0.1:8765` after `docker run`.
- The user can choose TG 91 or another talkgroup and hear subsequent calls in the browser.
- The user can monitor up to four talkgroups at once and remove any one without interrupting the others.
- Active calls, caller details, filters, theme, and optional WAV recording work.
- Profile icons follow the current callsign and disappear when it is unknown.
- The image runs without root, and local data and recordings stay outside the image.

## Review

All 14 tests pass, Ruff and JavaScript syntax checks pass, and the Docker
image builds with its standalone lockfile. The local container is healthy with
the mounted user export. Browser playback is connected to TG 91, and live audio
packets were received. Shutdown with an active browser stream completed without
session warnings. The browser showed the QRZ.com and RadioID URLs for VU36DR,
and hid both links without a callsign. The updated local container is healthy.
Subjective speaker output still requires a human listening check.

## Repository media

- [x] Capture light theme, dark theme, and player screenshots with sample data.
- [x] Record a short silent interface walkthrough and verify the video file.
- [x] Add the media to README and verify the repository diff before publishing.

The captures contain fictional demo records only. The MP4 is 14 seconds,
70 H.264 frames, 1266 x 1042 pixels, and has no audio track.
