#!/usr/bin/env bash
# Record the live viewer once, and cut both motion assets from that single take.
#
#   docs/make_motion.sh [url]
#
#   docs/viewer-demo.gif    for the README, which cannot play an MP4
#   docs/viewer-demo.mp4    for LinkedIn, which cannot animate a GIF
#
# The recorder writes JPEG frames; the assembly is the part that is easy to get wrong, so it is
# written down here rather than left in someone's shell history.
#
#   GIF  a default palette over a dark UI bands badly, so this uses `stats_mode=diff` with a Bayer
#        dither, at 8 fps — the pace the committed asset holds.
#   MP4  LinkedIn refuses anything under 10 fps, and the recorder samples every 120 ms (~8.3), so
#        the output is resampled to 30. crf 14 keeps the detail-panel text sharp and still lands
#        about 1.5x above LinkedIn's 192 Kbps floor, with the 30 Mbps ceiling 100x away.
#
# The recorder's own defaults already match the 900x1000 viewport the detail panel needs to be on
# screen, so nothing here overrides them.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
URL="${1:-https://toolgate.srv1567269.hstgr.cloud/}"
FRAMES="${FRAMES:-/tmp/toolgate-gif/frames}"
GIF="${GIF:-$ROOT/docs/viewer-demo.gif}"
MP4="${MP4:-$ROOT/docs/viewer-demo.mp4}"

rm -rf "$FRAMES" && mkdir -p "$FRAMES"
VIEWPORT_W="${VIEWPORT_W:-900}" VIEWPORT_H="${VIEWPORT_H:-1000}" SCROLL_Y="${SCROLL_Y:-0}" \
    node "$ROOT/docs/record_viewer.mjs" "$URL" "$FRAMES"

# 8 fps: the recorder sampled every 120 ms, and the committed GIF holds that pace.
ffmpeg -y -loglevel error -framerate 8 -i "$FRAMES/f%04d.jpg" \
    -vf "split[s0][s1];[s0]palettegen=stats_mode=diff[p];[s1][p]paletteuse=dither=bayer:bayer_scale=3:diff_mode=rectangle" \
    -loop 0 "$GIF"

# yuv420p and +faststart because both are what a web uploader expects; no audio track, which
# LinkedIn does not require.
ffmpeg -y -loglevel error -framerate 25/3 -i "$FRAMES/f%04d.jpg" \
    -vf "fps=30,format=yuv420p" -c:v libx264 -preset slow -crf 14 \
    -profile:v high -level 4.0 -movflags +faststart -an "$MP4"

for f in "$GIF" "$MP4"; do
    echo "  $(basename "$f")"
    ffprobe -v error -select_streams v:0 -count_frames \
        -show_entries stream=nb_read_frames,width,height -of default=nw=1 "$f" | sed 's/^/    /'
    ls -la "$f" | awk '{print "    bytes: "$5}'
done
