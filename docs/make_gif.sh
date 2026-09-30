#!/usr/bin/env bash
# Record the live viewer and assemble it into the README's GIF.
#
#   docs/make_gif.sh [url] [out.gif]
#
# The recorder writes JPEG frames; the assembly is the part that is easy to get wrong, so it is
# written down here rather than left in someone's shell history. A default palette over a dark UI
# bands badly — `stats_mode=diff` with a Bayer dither is what keeps the panels readable. Frames are
# captured at FRAME_MS=120 and played back at 8 fps, which is what the committed asset does; the
# recorder's own defaults already match the 900x1000 viewport the detail panel needs to be visible.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
URL="${1:-https://toolgate.srv1567269.hstgr.cloud/}"
OUT="${2:-$ROOT/docs/viewer-demo.gif}"
FRAMES="${FRAMES:-/tmp/toolgate-gif/frames}"

rm -rf "$FRAMES" && mkdir -p "$FRAMES"
VIEWPORT_W="${VIEWPORT_W:-900}" VIEWPORT_H="${VIEWPORT_H:-1000}" SCROLL_Y="${SCROLL_Y:-0}" \
    node "$ROOT/docs/record_viewer.mjs" "$URL" "$FRAMES"

# 8 fps: the recorder sampled every 120 ms, and the committed GIF holds that pace.
ffmpeg -y -loglevel error -framerate 8 -i "$FRAMES/f%04d.jpg" \
    -vf "split[s0][s1];[s0]palettegen=stats_mode=diff[p];[s1][p]paletteuse=dither=bayer:bayer_scale=3:diff_mode=rectangle" \
    -loop 0 "$OUT"

ffprobe -v error -select_streams v:0 -count_frames \
    -show_entries stream=nb_read_frames,width,height -of default=nw=1 "$OUT" | sed 's/^/  /'
ls -la "$OUT" | awk '{print "  bytes: "$5}'
