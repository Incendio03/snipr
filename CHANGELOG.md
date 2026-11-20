# Changelog

Each milestone in `docs/design.md` gets a version bump and a git tag: milestone N is `v0.N.0`.

## v0.1.0 — milestone 1: VOD link in, transcript and candidate clips out

- `snipr clip <vod link>` downloads the audio, reads Twitch chat replay, transcribes with word timings and cuts candidate clips.
- Works on Twitch VODs and YouTube stream replays. Twitch gives game chapters and chat; YouTube gives neither.
- Game filter (`--game "GTA 6"`): only the matching chapters of a stream are processed, or the VOD is skipped.
- Only the audio is downloaded for analysis. The picture is fetched per clip with 20 s of padding, instead of the whole VOD at 1080p.
- Measured on the RTX 2050: `medium` at `int8_float16` transcribes about 30x real time (12 minutes in 24 s).
- Clip cuts are frame-exact against the transcript timeline (checked by audio alignment, 0.00 s error on four clips).
- Without an LLM key, Highlights picks on chat and loudness spikes alone.
- Project scaffold: `uv` environment on Python 3.12, dependencies, design document.
