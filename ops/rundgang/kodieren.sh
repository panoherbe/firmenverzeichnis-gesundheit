#!/usr/bin/env bash
# Frames (frames.txt from rundgang.py) -> rundgang.mp4 (H.264, 1280x800, 25 fps, no audio), rundgang.gif, poster.png
#   kodieren.sh <aufnahme-dir> <ziel-dir> [gif-breite=800] [gif-fps=8] [poster-frame=f00000]
# Limits for the public repos: MP4 < 10 MB, GIF < 5 MB (checked at the end).
set -euo pipefail
A="$1"; Z="$2"; GB="${3:-800}"; GF="${4:-8}"; PF="${5:-f00000}"
mkdir -p "$Z"
ffmpeg -hide_banner -loglevel error -y -f concat -safe 0 -i "$A/frames.txt" \
  -vf "scale=1280:800:flags=lanczos,fps=25,format=yuv420p" \
  -c:v libx264 -preset slow -crf 22 -profile:v high -movflags +faststart -an "$Z/rundgang.mp4"
ffmpeg -hide_banner -loglevel error -y -i "$Z/rundgang.mp4" \
  -vf "fps=$GF,scale=$GB:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=128:stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle" \
  -loop 0 "$Z/rundgang.gif"
ffmpeg -hide_banner -loglevel error -y -i "$A/frames/$PF.png" -vf "scale=1280:800:flags=lanczos" "$Z/poster.png"
mp4=$(stat -c %s "$Z/rundgang.mp4"); gif=$(stat -c %s "$Z/rundgang.gif")
dauer=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$Z/rundgang.mp4")
echo "mp4 $((mp4 / 1024)) KB, gif $((gif / 1024)) KB, Dauer ${dauer} s"
[ "$mp4" -lt 10000000 ] || { echo "MP4 zu groß"; exit 2; }
[ "$gif" -lt 5000000 ] || { echo "GIF zu groß"; exit 3; }
