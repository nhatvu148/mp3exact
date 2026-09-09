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
import sys

from id3 import build_tag, safe_filename
from mp3frames import Mp3Error, cut, fmt_samples, index_frames

# hh:mm:ss(.mmm) or mm:ss(.mmm), not glued to other digits.
TIMESTAMP = re.compile(r"(?<!\d)(?:(\d{1,3}):)?(\d{1,2}):(\d{2})(?:\.(\d{1,3}))?(?!\d)")

# Leading "1.", "01)", "- ", bullets; and separators left behind after removing them.
LEADING_NUMBER = re.compile(r"^\s*\d{1,3}\s*[.)\]]\s*")
EDGE_JUNK = re.compile(r"^[\s\-–—:;,.*•|\[\]()]+|[\s\-–—:;,*•|\[\]()]+$")

# " - ", " – ", " — ", " ~ " between artist and title.
ARTIST_SPLIT = re.compile(r"\s+[-–—~]\s+")


def parse_tracklist(text):
    """Yield (seconds, label) for every line that carries a timestamp."""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = TIMESTAMP.search(line)
        if not m:
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
        out.append((total, label))
    return out


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
    ap.add_argument("-q", "--quiet", action="store_true")
    args = ap.parse_args(argv)

    text = (
        sys.stdin.read() if args.tracklist == "-" else open(args.tracklist, encoding="utf-8").read()
    )
    entries = parse_tracklist(text)
    if not entries:
        sys.exit("mp3split: no timestamps found in the tracklist")

    entries.sort(key=lambda e: e[0])
    for (a, _), (b, _) in zip(entries, entries[1:]):
        if a == b:
            sys.exit("mp3split: two tracks start at the same time (%s)" % fmt_samples(a, 1))

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

    stem = os.path.splitext(os.path.basename(args.input))[0]
    album = args.album or stem

    # Each track runs to the next one's start; the last runs to the end of the mix.
    starts = [int(round(sec * sr)) for sec, _ in entries]
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
    for i, ((_, label), start, end) in enumerate(zip(entries, starts, bounds), 1):
        artist, title = split_label(label, args.artist)
        title = title or "Track %02d" % i
        name = "%02d - %s.mp3" % (
            i,
            safe_filename("%s - %s" % (artist, title) if artist else title),
        )
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

        try:
            blob, info = cut(data, frames, hdr, xing, start, end, keep_tags=False)
        except Mp3Error as exc:
            print("mp3split: track %d: %s" % (i, exc), file=sys.stderr)
            exit_code = 1
            continue

        if not args.no_tags:
            blob = (
                build_tag(
                    title=title,
                    artist=artist,
                    album=album,
                    album_artist=args.artist,
                    track=(i, len(entries)),
                    year=args.year,
                    genre=args.genre,
                )
                + blob
            )

        os.makedirs(args.outdir, exist_ok=True)
        with open(dst, "wb") as fh:
            fh.write(blob)
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
