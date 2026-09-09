#!/usr/bin/env python3
"""Split a long mix into sample-exact tracks using a pasted tracklist.

    mp3split mix.mp3 --tracklist list.txt -d tracks/
    pbpaste | mp3split mix.mp3 --tracklist - -d tracks/ --album "Midnight Memories"

The tracklist is the kind of thing you copy out of a YouTube description:
one line per track, each containing a timestamp. Numbering, bullets and
brackets around the timestamp are ignored.
"""

import argparse
import os
import re
import subprocess
import sys

from id3 import build_tag, safe_filename
from mp3frames import (
    Mp3Error,
    average_bitrate,
    cut,
    describe_ffmpeg_error,
    fmt_samples,
    index_frames,
    reencode,
)

# hh:mm:ss(.mmm) or mm:ss(.mmm), not glued to other digits.
TIMESTAMP = re.compile(r"(?<!\d)(?:(\d{1,3}):)?(\d{1,2}):(\d{2})(?:\.(\d{1,3}))?(?!\d)")

# Leading "1.", "01)", "- ", bullets; and separators left behind after removing them.
LEADING_NUMBER = re.compile(r"^\s*\d{1,3}\s*[.)\]]\s*")
EDGE_JUNK = re.compile(r"^[\s\-–—:;,.*•|\[\]()]+|[\s\-–—:;,*•|\[\]()]+$")

# " - ", " – ", " — ", " ~ " between artist and title.
ARTIST_SPLIT = re.compile(r"\s+[-–—~]\s+")


def fmt_hms(seconds):
    h, rem = divmod(float(seconds), 3600)
    m, sec = divmod(rem, 60)
    if h:
        return "%d:%02d:%06.3f" % (int(h), int(m), sec)
    return "%d:%06.3f" % (int(m), sec)


def parse_tracklist(text):
    """Return (entries, skipped).

    entries are (seconds, label, line_number) in the order they were written;
    skipped are (line_number, text) for non-empty lines carrying no timestamp,
    so a mistyped track cannot vanish without anyone noticing.
    """
    out = []
    skipped = []
    for lineno, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        m = TIMESTAMP.search(line)
        if not m:
            skipped.append((lineno, line))
            continue
        hours, minutes, seconds, millis = m.groups()
        total = int(minutes) * 60 + int(seconds)
        if hours:
            total += int(hours) * 3600
        if millis:
            total += int(millis.ljust(3, "0")) / 1000.0

        label = line[: m.start()] + " " + line[m.end() :]
        label = LEADING_NUMBER.sub("", label.strip())
        label = EDGE_JUNK.sub("", label)
        label = " ".join(label.split())
        out.append((total, label, lineno))
    return out, skipped


def split_label(label, default_artist=None):
    """'Artist - Title' -> ('Artist', 'Title'); otherwise the whole thing is a title."""
    if not label:
        return default_artist, None
    parts = ARTIST_SPLIT.split(label, maxsplit=1)
    if len(parts) == 2 and parts[0] and parts[1]:
        return parts[0].strip(), parts[1].strip()
    return default_artist, label


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="mp3split", description="Split a mix into sample-exact tracks from a tracklist."
    )
    ap.add_argument("input")
    ap.add_argument(
        "-t", "--tracklist", required=True, help="tracklist file, or - to read standard input"
    )
    ap.add_argument("-d", "--outdir", default="tracks", help="output directory (default: tracks)")
    ap.add_argument("--album", help="album tag (defaults to the input filename)")
    ap.add_argument("--artist", help="fallback artist when a line has no 'Artist - Title'")
    ap.add_argument("--year")
    ap.add_argument("--genre")
    ap.add_argument(
        "--no-tags", action="store_true", help="write plain files with no ID3 tag at all"
    )
    ap.add_argument(
        "-n", "--dry-run", action="store_true", help="show the split without writing files"
    )
    ap.add_argument(
        "--mode",
        choices=("lossless", "reencode"),
        default="lossless",
        help="lossless keeps the original frames (default); reencode avoids the "
        "bit-reservoir warm-up at a cut point, at one lossy generation",
    )
    ap.add_argument(
        "--sort",
        action="store_true",
        help="accept a tracklist that is not in time order, and sort it",
    )
    ap.add_argument("-q", "--quiet", action="store_true")
    args = ap.parse_args(argv)

    text = (
        sys.stdin.read() if args.tracklist == "-" else open(args.tracklist, encoding="utf-8").read()
    )
    entries, skipped = parse_tracklist(text)
    if not entries:
        sys.exit("mp3split: no timestamps found in the tracklist")

    # A tracklist is authored in play order, so a timestamp that goes backwards
    # is a typo in the source, not a list that wants sorting. Sorting it
    # silently would swap two tracks and hand back plausible-looking files.
    problems = []
    for (t0, l0, n0), (t1, l1, n1) in zip(entries, entries[1:]):
        if t1 == t0:
            problems.append(
                "  line %d (%s) and line %d (%s) both start at %s"
                % (n0, l0 or "?", n1, l1 or "?", fmt_hms(t0))
            )
        elif t1 < t0:
            problems.append(
                "  line %d (%s) starts at %s, before line %d (%s) at %s"
                % (n1, l1 or "?", fmt_hms(t1), n0, l0 or "?", fmt_hms(t0))
            )
    if problems and not args.sort:
        sys.exit(
            "mp3split: tracklist is not in ascending time order:\n"
            + "\n".join(problems)
            + "\nFix the timestamp(s), or pass --sort to order by time anyway."
        )
    if args.sort:
        entries.sort(key=lambda e: e[0])
        for (t0, l0, n0), (t1, l1, n1) in zip(entries, entries[1:]):
            if t0 == t1:
                sys.exit(
                    "mp3split: line %d (%s) and line %d (%s) both start at %s"
                    % (n0, l0 or "?", n1, l1 or "?", fmt_hms(t0))
                )

    if skipped and not args.quiet:
        print("skipped %d line(s) with no timestamp:" % len(skipped))
        for lineno, line in skipped:
            print("  line %d: %s" % (lineno, line[:70]))
        print()

    with open(args.input, "rb") as fh:
        data = fh.read()
    try:
        frames, hdr, xing = index_frames(data)
    except Mp3Error as exc:
        sys.exit("mp3split: %s" % exc)

    sr = hdr["sample_rate"]
    music_len = (
        len(frames) * hdr["spf"] - (xing["delay"] if xing else 0) - (xing["padding"] if xing else 0)
    )

    avg_kbps = average_bitrate(frames, hdr)
    stem = os.path.splitext(os.path.basename(args.input))[0]
    album = args.album or stem

    # Each track runs to the next one's start; the last runs to the end of the mix.
    starts = [int(round(sec * sr)) for sec, _, _ in entries]
    bounds = starts[1:] + [music_len]
    if starts[0] >= music_len:
        sys.exit("mp3split: the first timestamp is past the end of the audio")

    if not args.quiet:
        print(
            "input : %s  (%s, %d Hz)"
            % (os.path.basename(args.input), fmt_samples(music_len, sr), sr)
        )
        print("tracks: %d\n" % len(entries))

    exit_code = 0
    written = 0
    for i, ((_, label, _), start, end) in enumerate(zip(entries, starts, bounds), 1):
        artist, title = split_label(label)
        title = title or "Track %02d" % i
        # Only an artist named on the line belongs in the filename; --artist is
        # a tagging fallback, not evidence that the line carried an artist.
        name = "%02d - %s.mp3" % (
            i,
            safe_filename("%s - %s" % (artist, title) if artist else title),
        )
        tag_artist = artist or args.artist
        dst = os.path.join(args.outdir, name)

        if end > music_len:
            end = music_len
        if start >= end:
            print("mp3split: skipping track %d (%s): zero length" % (i, title), file=sys.stderr)
            exit_code = 1
            continue

        if not args.quiet:
            print("%2d. %s -> %s  %s" % (i, fmt_samples(start, sr), fmt_samples(end, sr), title))
            if artist:
                print("    artist: %s" % artist)
            print("    %s" % dst)

        if args.dry_run:
            continue

        tag = b""
        if not args.no_tags:
            tag = build_tag(
                title=title,
                artist=tag_artist,
                album=album,
                album_artist=args.artist,
                track=(i, len(entries)),
                year=args.year,
                genre=args.genre,
            )

        os.makedirs(args.outdir, exist_ok=True)

        if args.mode == "reencode":
            # ffmpeg writes the file itself, so tag it afterwards.
            try:
                reencode(args.input, dst, start, end, sr, max(32, avg_kbps), strip_tags=True)
                if tag:
                    with open(dst, "rb") as fh:
                        body = fh.read()
                    with open(dst, "wb") as fh:
                        fh.write(tag + body)
            except (OSError, subprocess.SubprocessError) as exc:
                # Fail this track the way the lossless branch does: say which one,
                # drop whatever was half-written, and keep going with the rest.
                print("mp3split: track %d: %s" % (i, describe_ffmpeg_error(exc)), file=sys.stderr)
                if os.path.exists(dst):
                    os.remove(dst)
                exit_code = 1
                continue
            written += 1
            continue

        try:
            blob, info = cut(data, frames, hdr, xing, start, end, keep_tags=False)
        except Mp3Error as exc:
            print("mp3split: track %d: %s" % (i, exc), file=sys.stderr)
            exit_code = 1
            continue

        with open(dst, "wb") as fh:
            fh.write(tag + blob)
        written += 1
        if not args.quiet and info["warning"]:
            print("    warning: %s" % info["warning"])

    if not args.quiet:
        if args.dry_run:
            print("\ndry run: nothing written")
        else:
            print("\nwrote %d track(s) to %s" % (written, args.outdir))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
