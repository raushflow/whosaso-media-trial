# whosaso-media-trial

Bounded single-worker FREE-host demo runner for the Whosaso video qualification trial.
Synthetic fixtures only; no personal media, no external calls, no secrets, no connection to
the Whosaso app/backend. Runs the reviewer-qualified location-strip pipeline (46-assertion
suite) and one synthetic transcode probe, reporting wall time, child CPU and max RSS so the
fitness of a free host (0.1 CPU / 512MB shape) can be measured honestly.

- GET /health - liveness
- GET /trial - generate deterministic fixtures + run the full qualified suite (cached; ?fresh=1 re-runs)
- GET /trial/transcode - one 15s 1080p synthetic H264 generate + 360p transcode timing probe

Toolchain pinned to the qualification: ubuntu:22.04 (ffmpeg 4.4.2, exiftool 12.40, python 3.10).
Not production; not an admission GO; video stays OFF.
