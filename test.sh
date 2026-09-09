#!/usr/bin/env bash
# Regenerate fixtures and run the cut/verify matrix. Requires ffmpeg and uv.
set -u
cd "$(dirname "$0")"
FX="${FX:-./fixtures}"
mkdir -p "$FX"

SIG="aevalsrc='0.35*sin(2*PI*(180+140*sin(2*PI*0.31*t))*t)+0.25*sin(2*PI*(900+700*sin(2*PI*0.13*t))*t)+0.12*random(0)|0.35*sin(2*PI*(210+120*sin(2*PI*0.23*t))*t)+0.12*random(1):s=%d:d=30'"

if [ ! -f "$FX/cbr441.mp3" ] || [ "${1:-}" = "--fixtures-only" ]; then
  echo "generating fixtures in $FX ..."
  ffmpeg -v error -y -f lavfi -i "$(printf "$SIG" 44100)" -c:a libmp3lame -b:a 320k "$FX/cbr320.mp3"
  ffmpeg -v error -y -f lavfi -i "$(printf "$SIG" 44100)" -c:a libmp3lame -b:a 128k "$FX/cbr441.mp3"
  ffmpeg -v error -y -f lavfi -i "$(printf "$SIG" 44100)" -c:a libmp3lame -b:a  40k "$FX/cbr40.mp3"
  ffmpeg -v error -y -f lavfi -i "$(printf "$SIG" 44100)" -c:a libmp3lame -q:a 4   "$FX/vbr441.mp3"
  ffmpeg -v error -y -f lavfi -i "$(printf "$SIG" 44100)" -c:a libmp3lame -q:a 9   "$FX/vbrlow.mp3"
  ffmpeg -v error -y -f lavfi -i "$(printf "$SIG" 32000)" -ac 1 -c:a libmp3lame -b:a 96k "$FX/mono32.mp3"
  ffmpeg -v error -y -f lavfi -i "$(printf "$SIG" 24000)" -c:a libmp3lame -b:a  32k "$FX/mpeg2_24k.mp3"
  ffmpeg -v error -y -f lavfi -i "$(printf "$SIG" 44100)" -c:a libmp3lame -b:a 128k -write_xing 0 "$FX/noxing.mp3"
fi
[ "${1:-}" = "--fixtures-only" ] && exit 0

RANGES=("0-3.5" "7.123-11.777" "1.0005-1.5015" "25.5-30" "0-0.05" "29.9-30" "12.9999-13.0001")
exact=0; warm=0; fail=0
for f in cbr320 cbr441 vbr441 vbrlow cbr40 mono32 mpeg2_24k noxing; do
  for r in "${RANGES[@]}"; do
    s="${r%-*}"; e="${r#*-}"
    if ! uv run mp3cut.py "$FX/$f.mp3" -c "$r" -o "$FX/_out.mp3" -q >/dev/null 2>&1; then
      printf "CUTFAIL %-11s %s\n" "$f" "$r"; fail=$((fail+1)); continue
    fi
    res=$(uv run verify.py "$FX/$f.mp3" "$FX/_out.mp3" --start "$s" --end "$e" 2>/dev/null | tail -1)
    case "$res" in
      "PASS - sample exact, losslessly") exact=$((exact+1));;
      PASS*) warm=$((warm+1));;
      *) fail=$((fail+1)); printf "FAIL %-11s %-18s %s\n" "$f" "$r" "$res";;
    esac
  done
done
rm -f "$FX/_out.mp3"
echo "bit-exact=$exact  exact-with-warmup=$warm  fail=$fail  (total $((exact+warm+fail)))"
[ "$fail" -eq 0 ]
