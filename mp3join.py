#!/usr/bin/env python3
"""Join MP3 files losslessly, trimming as much encoder padding as MP3 allows.

    mp3join 01.mp3 02.mp3 03.mp3 -o whole.mp3

A single MP3 carries only ONE encoder delay / padding pair, in its Xing
header, so only the very start and the very end of the result can be trimmed
to the exact sample. At every interior seam the best that can be done without
re-encoding is to drop whole frames of priming and padding, which leaves under
one frame (26 ms at 44.1 kHz, 24 ms at 48 kHz) of silence at the join.

That is still better than `cat` or mp3wrap, which keep the full priming
silence at every seam and leave a duration that no player computes correctly.
It is not truly gapless: for that the seam has to be re-encoded.
"""

import argparse
import os
import sys

from mp3frames import (
    DECODER_DELAY,
    MAX_GAPLESS,
    Mp3Error,
    build_toc,
    build_xing_frame,
    fmt_samples,
    index_frames,
    priming_start,
)


def music_bounds(frames, hdr, xing, keep_priming):
    """Raw sample range holding real audio, and the frames covering it.

    `keep_priming` is only right for the first file, whose head is trimmed
    exactly by the output's delay field, so carrying extra reservoir frames
    costs nothing. For every later file those frames would become audible
    duplicated audio at the seam, so the reservoir is sacrificed instead: the
    first frame after a seam may decode from a short reservoir.
    """
    spf = hdr["spf"]
    total = len(frames) * spf
    delay = xing["delay"] if xing else 0
    padding = xing["padding"] if xing else 0
    raw_start = delay + DECODER_DELAY
    raw_end = total - padding + DECODER_DELAY
    raw_end = min(raw_end, total)

    first_music = raw_start // spf
    first = priming_start(frames, first_music) if keep_priming else first_music
    last = min((raw_end - 1) // spf, len(frames) - 1)
    return first, last, raw_start, raw_end


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="mp3join", description="Join MP3 files losslessly, trimming what MP3 allows."
    )
    ap.add_argument("inputs", nargs="+", help="files to join, in order")
    ap.add_argument("-o", "--output", required=True)
    ap.add_argument("-q", "--quiet", action="store_true")
    args = ap.parse_args(argv)

    if len(args.inputs) < 2:
        sys.exit("mp3join: need at least two inputs")

    parts = []
    base = None
    for path in args.inputs:
        with open(path, "rb") as fh:
            data = fh.read()
        try:
            frames, hdr, xing = index_frames(data)
        except Mp3Error as exc:
            sys.exit("mp3join: %s: %s" % (path, exc))

        key = (hdr["version"], hdr["sample_rate"], hdr["channel_mode"])
        if base is None:
            base = (key, hdr)
        elif key != base[0]:
            sys.exit(
                "mp3join: %s does not match the first file "
                "(%d Hz %s vs %d Hz %s); re-encode them to a common format first"
                % (
                    os.path.basename(path),
                    hdr["sample_rate"],
                    "mono" if hdr["channel_mode"] == 3 else "stereo",
                    base[1]["sample_rate"],
                    "mono" if base[1]["channel_mode"] == 3 else "stereo",
                )
            )
        first, last, raw_start, raw_end = music_bounds(frames, hdr, xing, keep_priming=not parts)
        parts.append(
            {
                "path": path,
                "data": data,
                "frames": frames,
                "hdr": hdr,
                "first": first,
                "last": last,
                "head_extra": raw_start - first * hdr["spf"],
                "tail_extra": (last + 1) * hdr["spf"] - raw_end,
                "count": last - first + 1,
            }
        )

    hdr = base[1]
    spf = hdr["spf"]
    sr = hdr["sample_rate"]

    # The delay field is 12 bits, so it can hide at most MAX_GAPLESS +
    # DECODER_DELAY samples of lead. If the first file's reservoir priming needs
    # more than that, drop frames until it fits. Clamping the field instead -
    # which is what this used to do - silently keeps audio that was meant to be
    # hidden, making the join longer than the sum of its parts with no warning.
    lead_warning = None
    lead = parts[0]["head_extra"]
    if lead - DECODER_DELAY > MAX_GAPLESS:
        drop = (lead - DECODER_DELAY - MAX_GAPLESS + spf - 1) // spf
        drop = min(drop, parts[0]["count"] - 1)
        parts[0]["first"] += drop
        parts[0]["count"] -= drop
        parts[0]["head_extra"] = lead - drop * spf
        lead_warning = (
            "dropped %d priming frame(s) from %s to fit the 12-bit delay field; "
            "the first ~%d ms may decode with a short bit-reservoir"
            % (drop, os.path.basename(parts[0]["path"]), drop * spf * 1000 // sr)
        )

    payload = bytearray()
    all_frames = []  # (offset in payload, size) for the TOC
    for p in parts:
        f = p["frames"]
        for i in range(p["first"], p["last"] + 1):
            all_frames.append((len(payload) + f[i].off - f[p["first"]].off, f[i].size))
        payload += p["data"][f[p["first"]].off : f[p["last"]].off + f[p["last"]].size]

    out_total = sum(p["count"] for p in parts) * spf
    n0 = parts[0]["head_extra"]
    n1 = out_total - parts[-1]["tail_extra"]

    delay = max(0, n0 - DECODER_DELAY)
    padding = parts[-1]["tail_extra"] + DECODER_DELAY
    # Both must now fit without truncation; the lead was made to fit above, and
    # tail_extra is under one frame by construction. If either still overflows
    # the field, the output length would be wrong, so say so rather than lie.
    if delay > MAX_GAPLESS or padding > MAX_GAPLESS:
        raise Mp3Error(
            "gapless fields overflow (delay %d, padding %d, max %d); "
            "the join would not be sample-exact" % (delay, padding, MAX_GAPLESS)
        )

    class _F:
        def __init__(self, off):
            self.off = off

    toc = build_toc([_F(off) for off, _ in all_frames], 0, len(all_frames) - 1, len(payload))
    header = build_xing_frame(hdr, len(all_frames), len(payload), toc, delay, padding)
    tag_p = 4 + hdr["side_len"] + 12
    header = header[:tag_p] + (len(payload) + len(header)).to_bytes(4, "big") + header[tag_p + 4 :]

    with open(args.output, "wb") as fh:
        fh.write(header)
        fh.write(payload)

    if not args.quiet:
        print("output: %s" % args.output)
        print(
            "        %d Hz, %s, %d frames, %s"
            % (
                sr,
                "mono" if hdr["channel_mode"] == 3 else "stereo",
                len(all_frames),
                fmt_samples(n1 - n0, sr),
            )
        )
        print("        delay %d, padding %d" % (delay, padding))
        if lead_warning:
            print("        warning: %s" % lead_warning)
        print()
        worst = 0
        for i, (a, b) in enumerate(zip(parts, parts[1:]), 1):
            gap = a["tail_extra"] + b["head_extra"]
            worst = max(worst, gap)
            print(
                "seam %d: %s | %s  +%d samples (%.1f ms) of silence"
                % (
                    i,
                    os.path.basename(a["path"])[:28],
                    os.path.basename(b["path"])[:28],
                    gap,
                    gap * 1000.0 / sr,
                )
            )
        if parts[1:]:
            print(
                "\nstart and end are sample-exact; worst seam is %.1f ms "
                "(a frame is %.1f ms, and a seam spans at most two)"
                % (worst * 1000.0 / sr, spf * 1000.0 / sr)
            )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Mp3Error as exc:
        sys.exit("mp3join: %s" % exc)
