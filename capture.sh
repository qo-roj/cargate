#!/data/data/com.termux/files/usr/bin/bash
# Event-driven capture for the car alarm. Invoked over SSH (key auth).
# Photos (main + wide) + ~10s audio burst. Prints the output dir, one file per line.
set -u
TS=$(date +%Y%m%d-%H%M%S)
OUT="$HOME/captures/$TS"
mkdir -p "$OUT"

termux-camera-photo -c 0 "$OUT/cam0.jpg" >/dev/null 2>&1 || true
termux-camera-photo -c 2 "$OUT/cam2.jpg" >/dev/null 2>&1 || true

REC_LINE=$(termux-microphone-record -d -l 10 -e aac 2>&1 | head -1)
REC=$(echo "$REC_LINE" | grep -o '/storage/emulated/0/[^ ]*')
sleep 12
termux-microphone-record -q >/dev/null 2>&1 || true
if [ -n "$REC" ] && [ -f "$REC" ]; then
    cp "$REC" "$OUT/audio.m4a" 2>/dev/null || true
elif ls /storage/emulated/0/TermuxAudioRecording_*.m4a >/dev/null 2>&1; then
    NEWEST=$(ls -t /storage/emulated/0/TermuxAudioRecording_*.m4a | head -1)
    [ -n "$NEWEST" ] && cp "$NEWEST" "$OUT/audio.m4a" 2>/dev/null || true
fi

echo "DIR $OUT"
ls -1 "$OUT"/* 2>/dev/null