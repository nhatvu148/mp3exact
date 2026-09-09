#!/usr/bin/env python3
"""Inspect an MP3's frame layout, gapless metadata and structural integrity.

    mp3info file.mp3
    mp3info file.mp3 --frames 20     # dump the first frames

Useful for working out why some other tool's output is wrong: it shows the
gapless values a player will act on, whether the Xing frame count matches
reality, and where the bitstream had to resync.
"""

import argparse
import os
import sys
from collections import Counter

from mp3frames import DECODER_DELAY, Mp3Error, fmt_samples, id3v2_size, index_frames

VERSION_NAME = {3: "MPEG1", 2: "MPEG2", 0: "MPEG2.5"}
CHANNEL_NAME = {0: "stereo", 1: "joint stereo", 2: "dual channel", 3: "mono"}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="mp3info", description="Inspect MP3 structure.")
    ap.add_argument("input")
    ap.add_argument("--frames", type=int, default=0, metavar="N", help="dump the first N frames")
    args = ap.parse_args(argv)

    with open(args.input, "rb") as fh:
        data = fh.read()
    try:
        frames, hdr, xing = index_frames(data)
    except Mp3Error as exc:
        sys.exit("mp3info: %s" % exc)

    spf = hdr["spf"]
    sr = hdr["sample_rate"]
    total = len(frames) * spf
    delay = xing["delay"] if xing else 0
    padding = xing["padding"] if xing else 0
    music = total - delay - padding

    tag_len = id3v2_size(data)
    audio_bytes = sum(f.size for f in frames)

    print("file      : %s" % os.path.basename(args.input))
    print("size      : %d bytes" % len(data))
    print(
        "format    : %s Layer III, %d Hz, %s"
        % (VERSION_NAME.get(hdr["version"], "?"), sr, CHANNEL_NAME.get(hdr["channel_mode"], "?"))
    )

    sizes = Counter(f.size for f in frames)
    kind = "CBR" if len(sizes) <= 2 else "VBR"
    if xing:
        kind = "VBR (Xing)" if xing.get("kind") == b"Xing" else "CBR (Info)"
        if xing.get("kind") == b"VBRI":
            kind = "VBR (VBRI)"
    avg_kbps = audio_bytes * 8 * sr / (total * 1000.0) if total else 0
    print(
        "bitrate   : %s, average %.1f kbps, %d distinct frame sizes" % (kind, avg_kbps, len(sizes))
    )

    print("frames    : %d audio frames, %d samples per frame" % (len(frames), spf))
    print(
        "duration  : %s decoded, %s after gapless trim"
        % (fmt_samples(total, sr), fmt_samples(music, sr))
    )
    if xing:
        print(
            "gapless   : delay %d (+%d decoder = %d skipped), padding %d (%d trimmed)"
            % (
                delay,
                DECODER_DELAY,
                delay + DECODER_DELAY,
                padding,
                max(0, padding - DECODER_DELAY),
            )
        )
    else:
        print("gapless   : no Xing/LAME header - players decode every sample, including priming")
    print("id3v2     : %s" % ("%d bytes" % tag_len if tag_len else "none"))

    # Structural checks.
    problems = []
    if xing and xing.get("kind") in (b"Xing", b"Info"):
        declared = xing.get("frame_count")
        if declared and declared != len(frames):
            problems.append("Xing frame count says %d, found %d" % (declared, len(frames)))
    gaps = 0
    for a, b in zip(frames, frames[1:]):
        if a.off + a.size != b.off:
            gaps += 1
    if gaps:
        problems.append("%d resync gap(s): bytes between frames that are not frame data" % gaps)
    tail = frames[-1].off + frames[-1].size
    trailing = len(data) - tail
    if trailing > 0:
        kind_tail = "ID3v1" if data[tail : tail + 3] == b"TAG" else "trailing data"
        if trailing > 128 or kind_tail != "ID3v1":
            problems.append("%d bytes of %s after the last frame" % (trailing, kind_tail))
    if padding and padding < DECODER_DELAY:
        problems.append(
            "padding %d is below the %d-sample decoder delay" % (padding, DECODER_DELAY)
        )

    print()
    if problems:
        print("problems  :")
        for p in problems:
            print("  - %s" % p)
    else:
        print("problems  : none found")

    if args.frames:
        print("\n%-6s %-10s %-7s %-6s %s" % ("frame", "offset", "size", "resvr", "time"))
        for i, f in enumerate(frames[: args.frames]):
            print(
                "%-6d %-10d %-7d %-6d %s"
                % (i, f.off, f.size, f.main_begin, fmt_samples(i * spf, sr))
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
