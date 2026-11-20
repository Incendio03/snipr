# Changelog

Each milestone in `docs/design.md` gets a version bump and a git tag: milestone N is `v0.N.0`.

## v0.3.0 — milestone 3: vertical reframing, headline and captions

- Clips render at 1080 x 1920: Fill frame (one 9:16 crop, centre by default) or Stacked (facecam 1080 x 608 above gameplay 1080 x 1312).
- Headline plate and word-by-word captions drawn to the Designer's clip format spec, with the current word on the purple box. Chakra Petch and Inter are bundled.
- Crop rectangles are stored as fractions of the frame and fitted to the slot's aspect ratio, so they work at any source resolution.
- Checked on real footage in both layouts. A 56 s clip renders in about 37 s on the RTX 2050.
- Word fragments from the transcript ("y" + "'all") are joined before captioning.

## v0.2.0 — milestone 2: selection for all three modes

- Claude provider behind two functions, `select_segments` and `write_metadata`; model `claude-haiku-5-5`.
- Highlights: spikes nominate up to 15 windows, then one LLM request filters and trims them.
- Informative and Custom: the whole transcript goes in one request with per-minute chat and loudness figures. Transcripts too long for one request are split and merged.
- Content flags (slurs, sexual content, copyrighted music) come back with each segment.
- Headline, title, description and hashtags are written per clip.
- `snipr set-key` stores the API key in Windows Credential Manager.
- **Not yet run against the live API**: no key is stored on this machine. Prompt building, response validation and error handling are covered by tests with a stand-in client. The milestone's own bar ("at least half the candidates are ones you would post") still needs a real run and your judgement.

## v0.1.0 — milestone 1: VOD link in, transcript and candidate clips out

- `snipr clip <vod link>` downloads the audio, reads Twitch chat replay, transcribes with word timings and cuts candidate clips.
- Works on Twitch VODs and YouTube stream replays. Twitch gives game chapters and chat; YouTube gives neither.
- Game filter (`--game "GTA 6"`): only the matching chapters of a stream are processed, or the VOD is skipped.
- Only the audio is downloaded for analysis. The picture is fetched per clip with 20 s of padding, instead of the whole VOD at 1080p.
- Measured on the RTX 2050: `medium` at `int8_float16` transcribes about 30x real time (12 minutes in 24 s).
- Clip cuts are frame-exact against the transcript timeline (checked by audio alignment, 0.00 s error on four clips).
- Without an LLM key, Highlights picks on chat and loudness spikes alone.
- Project scaffold: `uv` environment on Python 3.12, dependencies, design document.
