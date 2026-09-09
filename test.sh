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

RANGES=("0-3.5" "7.123-11.777" "1.0005-1.5015" "25.5-30" "0-0.05" "29.9-30")
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
echo "cut:   bit-exact=$exact  exact-with-warmup=$warm  fail=$fail  (total $((exact+warm+fail)))"

# ---------------------------------------------------------------- mp3split
printf '00:00 Alpha - One\n0:08 Beta - Two\n[0:19.5] Gamma - Three\n' > "$FX/_list.txt"
rm -rf "$FX/_tracks"
uv run mp3split.py "$FX/cbr441.mp3" -t "$FX/_list.txt" -d "$FX/_tracks" -q >/dev/null 2>&1
sfail=0; sok=0
i=1
for range in "0 8" "8 19.5" "19.5 30"; do
  set -- $range
  f=$(ls "$FX/_tracks/0$i - "*.mp3 2>/dev/null | head -1)
  if [ -z "$f" ]; then sfail=$((sfail+1)); echo "SPLIT missing track $i"; i=$((i+1)); continue; fi
  res=$(uv run verify.py "$FX/cbr441.mp3" "$f" --start "$1" --end "$2" 2>/dev/null | tail -1)
  case "$res" in PASS*) sok=$((sok+1));; *) sfail=$((sfail+1)); echo "SPLIT track $i: $res";; esac
  i=$((i+1))
done
# The tag must survive the round trip.
t=$(ffprobe -v error -show_entries format_tags=title -of default=nk=1:nw=1 "$FX/_tracks/02 - Beta - Two.mp3" 2>/dev/null)
if [ "$t" = "Two" ]; then sok=$((sok+1)); else sfail=$((sfail+1)); echo "SPLIT tag readback got '$t', wanted 'Two'"; fi
echo "split: ok=$sok fail=$sfail"

# ---------------------------------------------------------------- mp3join
for r in "2-7" "7-12" "12-17"; do
  uv run mp3cut.py "$FX/cbr441.mp3" -c "$r" -o "$FX/_j${r%%-*}.mp3" -q >/dev/null 2>&1
done
uv run mp3join.py "$FX/_j2.mp3" "$FX/_j7.mp3" "$FX/_j12.mp3" -o "$FX/_joined.mp3" -q >/dev/null 2>&1
# 15 s of audio, plus at most two frames of seam silence per join.
jfail=$(uv run python3 -c "
import subprocess, sys
# Decoded samples, not ffprobe's duration field: ffmpeg 6 reports that raw and
# ffmpeg 9 reports it gapless-adjusted, so it measures the tool inconsistently.
raw = subprocess.run(['ffmpeg','-v','error','-i','$FX/_joined.mp3','-map','0:a:0',
                      '-ac','2','-ar','44100','-f','s16le','-'],
                     capture_output=True).stdout
got = len(raw) // 4
want, frame = 15 * 44100, 1152
slack = 4 * frame
print(0 if want <= got <= want + slack else 1)
sys.stderr.write('joined %d samples, wanted %d..%d\n' % (got, want, want + slack))
" 2>/dev/null)
if [ "$jfail" = "0" ]; then echo "join:  ok (seams within two frames each)"; else echo "join:  FAIL length out of range"; fi

# ------------------------------------------------------- sub-frame cut math
# A cut shorter than one frame is where the gapless arithmetic is tightest. It
# is checked on the file's own header rather than by decoding: skip and trim
# then discard all but 8 of 4608 samples, and whether a given decoder honours
# that is its business, not this tool's (ffmpeg 9 does, ffmpeg 6.1 does not).
uv run mp3cut.py "$FX/cbr441.mp3" -c 12.9999-13.0001 -o "$FX/_tiny.mp3" -q >/dev/null 2>&1
subfail=$(uv run python3 -c "
import sys; sys.path.insert(0, '.')
from mp3frames import index_frames, DECODER_DELAY
data = open('$FX/_tiny.mp3', 'rb').read()
frames, hdr, xing = index_frames(data)
total = len(frames) * hdr['spf']
kept = total - (xing['delay'] + DECODER_DELAY) - max(0, xing['padding'] - DECODER_DELAY)
want = round(13.0001 * 44100) - round(12.9999 * 44100)
print(0 if kept == want else 1)
sys.stderr.write('sub-frame cut keeps %d sample(s), wanted %d\\n' % (kept, want))
" 2>/dev/null)
if [ "$subfail" = "0" ]; then
  echo "subfrm: ok (header arithmetic exact for a sub-frame cut)"
else
  echo "subfrm: FAIL header arithmetic wrong"; subfail=1
fi
rm -f "$FX/_tiny.mp3"

# ------------------------------------------------ mp3split order validation
# The point of this PR: a tracklist that is out of order, or has two tracks at
# the same timestamp, must be refused rather than silently sorted into a
# plausible-but-wrong split - and --sort must still be able to accept it.
printf '00:00 One\n0:19 Three\n0:08 Two\n' > "$FX/_bad.txt"
printf '00:00 One\n0:08 Two\n0:08 Dup\n'   > "$FX/_tie.txt"
ordfail=0
rm -rf "$FX/_ord"
if uv run mp3split.py "$FX/cbr441.mp3" -t "$FX/_bad.txt" -d "$FX/_ord" -q >/dev/null 2>&1; then
  echo "ORDER: out-of-order tracklist was accepted"; ordfail=$((ordfail+1))
fi
msg=$(uv run mp3split.py "$FX/cbr441.mp3" -t "$FX/_bad.txt" -d "$FX/_ord" 2>&1 >/dev/null)
case "$msg" in
  *"line 3"*) : ;;
  *) echo "ORDER: error did not name the offending line: $msg"; ordfail=$((ordfail+1)) ;;
esac
if uv run mp3split.py "$FX/cbr441.mp3" -t "$FX/_tie.txt" -d "$FX/_ord" -q >/dev/null 2>&1; then
  echo "ORDER: tied timestamps were accepted"; ordfail=$((ordfail+1))
fi
rm -rf "$FX/_ord"
if uv run mp3split.py "$FX/cbr441.mp3" -t "$FX/_bad.txt" -d "$FX/_ord" --sort -q >/dev/null 2>&1; then
  n=$(ls "$FX/_ord" 2>/dev/null | wc -l | tr -d ' ')
  [ "$n" = "3" ] || { echo "ORDER: --sort wrote $n file(s), wanted 3"; ordfail=$((ordfail+1)); }
else
  echo "ORDER: --sort refused a merely unordered tracklist"; ordfail=$((ordfail+1))
fi
if [ "$ordfail" = "0" ]; then
  echo "order: ok (out-of-order and ties refused, --sort accepts)"
else
  echo "order: FAIL ($ordfail)"
fi
rm -rf "$FX/_ord" "$FX/_bad.txt" "$FX/_tie.txt"

# ------------------------------------------------- mp3split reencode failure
# A broken ffmpeg must fail the affected track and continue, not abort the run
# or leave a half-written file behind.
FAKE="$FX/_fakebin"; mkdir -p "$FAKE"
printf '#!/bin/sh\nexit 1\n' > "$FAKE/ffmpeg"; chmod +x "$FAKE/ffmpeg"
printf '00:00 One\n0:08 Two\n0:19 Three\n' > "$FX/_t3.txt"
rm -rf "$FX/_ft"
PATH="$FAKE:$PATH" uv run mp3split.py "$FX/cbr441.mp3" -t "$FX/_t3.txt" -d "$FX/_ft" \
  --mode reencode -q >/dev/null 2>&1
rc=$?
left=$(ls "$FX/_ft" 2>/dev/null | wc -l | tr -d ' ')
if [ "$rc" = "1" ] && [ "$left" = "0" ]; then
  echo "rterr: ok (broken ffmpeg reported, no partial files)"; rterr=0
else
  echo "rterr: FAIL exit=$rc partial_files=$left"; rterr=1
fi
rm -rf "$FAKE" "$FX/_t3.txt" "$FX/_ft"

# ---------------------------------------------------------------- mp3info
ifail=0
for f in cbr320 cbr441 vbr441 vbrlow cbr40 mono32 mpeg2_24k noxing; do
  if ! uv run mp3info.py "$FX/$f.mp3" 2>/dev/null | grep -q "problems  : none found"; then
    ifail=$((ifail+1)); echo "INFO $f reported problems"
  fi
done
echo "info:  ok=$((8-ifail)) fail=$ifail"

rm -rf "$FX/_tracks" "$FX/_list.txt" "$FX/_j"*.mp3 "$FX/_joined.mp3"
total=$((fail + sfail + ifail + rterr + ordfail + subfail))
[ "$jfail" = "0" ] || total=$((total+1))
echo
echo "TOTAL FAILURES: $total"
[ "$total" -eq 0 ]
