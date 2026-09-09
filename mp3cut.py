#!/usr/bin/env python3
"""Cut an MP3 at sample-exact timestamps without re-encoding.

mp3cut input.mp3 -c 1:20-2:45 -o out.mp3
mp3cut mix.mp3 -c 0:00-3:47.512 -c 3:47.512-8:11.003 -d tracks/
"""

import argparse
import os
import subprocess
import sys

from mp3frames import (
    DECODER_DELAY,
    Mp3Error,
    average_bitrate,
    cut,
    describe_ffmpeg_error,
    fmt_samples,
    index_frames,
    parse_time,
    reencode,
)


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="mp3cut",
        description="Cut MP3 files at sample-exact timestamps without re-encoding.",
    )
    ap.add_argument("input")
    ap.add_argument(
        "-c",
        "--cut",
        action="append",
        required=True,
        metavar="START-END",
        help="range to keep, e.g. 1:20-2:45 or 0:00-3:12.500 (repeat for multiple cuts)",
    )
    ap.add_argument("-o", "--output", help="output file (single cut only)")
    ap.add_argument("-d", "--outdir", default=".", help="output directory for multiple cuts")
    ap.add_argument(
        "--mode",
        choices=("lossless", "reencode"),
        default="lossless",
        help="lossless keeps the original frames (default); "
        "reencode is exact for players that ignore gapless tags",
    )
    ap.add_argument("--no-tags", action="store_true", help="do not copy the ID3v2 tag")
    ap.add_argument("-q", "--quiet", action="store_true")
    args = ap.parse_args(argv)

    with open(args.input, "rb") as fh:
        data = fh.read()

    try:
        frames, hdr, xing = index_frames(data)
    except Mp3Error as exc:
        sys.exit("mp3cut: %s" % exc)

    sr = hdr["sample_rate"]
    spf = hdr["spf"]
    music_len = (
        len(frames) * spf - (xing["delay"] if xing else 0) - (xing["padding"] if xing else 0)
    )

    if not args.quiet:
        vbr = "VBR" if xing and xing.get("kind") == b"Xing" else "CBR/unknown"
        print("input : %s" % os.path.basename(args.input))
        print(
            "        %d Hz, %s, %d frames, %s, duration %s"
            % (
                sr,
                ("mono" if hdr["channel_mode"] == 3 else "stereo"),
                len(frames),
                vbr,
                fmt_samples(music_len, sr),
            )
        )
        print(
            "        encoder delay %d + %d, padding %d"
            % (xing["delay"] if xing else 0, DECODER_DELAY, xing["padding"] if xing else 0)
        )

    ranges = []
    for spec in args.cut:
        if "-" not in spec:
            sys.exit("mp3cut: --cut needs START-END, got %r" % spec)
        s_txt, _, e_txt = spec.rpartition("-")
        try:
            ranges.append((parse_time(s_txt, sr), parse_time(e_txt, sr)))
        except ValueError as exc:
            sys.exit("mp3cut: bad timestamp in %r: %s" % (spec, exc))

    if args.output and len(ranges) > 1:
        sys.exit("mp3cut: -o works with a single --cut; use --outdir instead")

    stem = os.path.splitext(os.path.basename(args.input))[0]
    # frames and hdr do not change between cuts, so derive this once.
    avg_kbps = average_bitrate(frames, hdr)
    exit_code = 0
    for i, (s, e) in enumerate(ranges, 1):
        if args.output:
            dst = args.output
        else:
            os.makedirs(args.outdir, exist_ok=True)
            dst = os.path.join(args.outdir, "%s - %02d.mp3" % (stem, i))

        if args.mode == "reencode":
            try:
                reencode(args.input, dst, s, min(e, music_len), sr, max(32, avg_kbps))
            except (OSError, subprocess.SubprocessError) as exc:
                print("mp3cut: cut %d: %s" % (i, describe_ffmpeg_error(exc)), file=sys.stderr)
                if os.path.exists(dst):
                    os.remove(dst)
                exit_code = 1
                continue
            if not args.quiet:
                print(
                    "\ncut %d: %s -> %s  [re-encoded]"
                    % (i, fmt_samples(s, sr), fmt_samples(min(e, music_len), sr))
                )
                print("        %s" % dst)
            continue

        try:
            blob, info = cut(data, frames, hdr, xing, s, e, not args.no_tags)
        except Mp3Error as exc:
            print("mp3cut: cut %d: %s" % (i, exc), file=sys.stderr)
            exit_code = 1
            continue
        with open(dst, "wb") as fh:
            fh.write(blob)

        if not args.quiet:
            print(
                "\ncut %d: %s -> %s  (%s)"
                % (
                    i,
                    fmt_samples(s, sr),
                    fmt_samples(min(e, music_len), sr),
                    fmt_samples(info["out_samples"], sr),
                )
            )
            print(
                "        %s  (%d frames, %d priming, delay %d, padding %d)"
                % (dst, info["frames"], info["priming_frames"], info["delay"], info["padding"])
            )
            if info["warning"]:
                print("        warning: %s" % info["warning"])
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
