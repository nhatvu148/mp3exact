#!/usr/bin/env python3
"""Prove a cut MP3 contains exactly the samples it was supposed to contain.

Ground truth is the source decoded from sample 0 and trimmed with ffmpeg's
`atrim` filter. That matters: ffmpeg's own `-ss` seek starts decoding with a
cold bit reservoir, so seek-based extraction is itself slightly wrong near the
cut point and makes a bad reference.

    uv run verify.py source.mp3 cut.mp3 --start 1:20 --end 2:45
"""

import argparse
import subprocess
import sys

import numpy as np


def parse_time(text):
    seconds = 0.0
    for part in str(text).strip().split(":"):
        seconds = seconds * 60 + float(part)
    return seconds


def probe_rate(path):
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=sample_rate",
            "-of",
            "default=nk=1:nw=1",
            path,
        ],
        check=True,
        stdout=subprocess.PIPE,
    ).stdout
    return int(out.split()[0])


def decode(path, sr, channels, filt=None):
    cmd = ["ffmpeg", "-v", "error", "-i", path]
    if filt:
        cmd += ["-af", filt]
    cmd += [
        "-map",
        "0:a:0",
        "-ac",
        str(channels),
        "-ar",
        str(sr),
        "-f",
        "s16le",
        "-acodec",
        "pcm_s16le",
        "-",
    ]
    raw = subprocess.run(cmd, check=True, stdout=subprocess.PIPE).stdout
    return np.frombuffer(raw, dtype="<i2")


def best_lag(ref, test, max_lag):
    """Lag in samples that best aligns `test` onto `ref`, via FFT correlation."""
    size = 1 << (len(ref) + len(test) - 1).bit_length()
    fa = np.fft.rfft(ref.astype(np.float32) - ref.mean(), size)
    fb = np.fft.rfft(test.astype(np.float32) - test.mean(), size)
    corr = np.fft.irfft(fa * np.conj(fb), size)
    window = np.concatenate([corr[-max_lag:], corr[: max_lag + 1]])
    peak = int(np.argmax(window))
    denom = float(np.linalg.norm(ref) * np.linalg.norm(test)) or 1.0
    return peak - max_lag, float(window[peak]) / denom


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("source")
    ap.add_argument("cut")
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--sr", type=int, help="defaults to the source sample rate")
    args = ap.parse_args()

    sr = args.sr or probe_rate(args.source)
    s0 = int(round(parse_time(args.start) * sr))
    s1 = int(round(parse_time(args.end) * sr))
    want = s1 - s0

    truth = decode(args.source, sr, 2, "atrim=start_sample=%d:end_sample=%d" % (s0, s1))
    ours = decode(args.cut, sr, 2)

    got = len(ours) // 2
    print("expected : %d samples (%.6f s)" % (want, want / sr))
    print("actual   : %d samples (%.6f s)" % (got, got / sr))
    err = got - want
    print("length   : %+d samples (%+.3f ms)" % (err, err * 1000.0 / sr))

    n = min(len(truth), len(ours))
    if n == 0:
        print("\nFAIL - empty decode")
        return 1
    diff = np.abs(truth[:n].astype(np.int32) - ours[:n].astype(np.int32))
    ndiff = int(np.count_nonzero(diff))

    if ndiff == 0 and err == 0:
        print("content  : bit identical to the source over the whole range")
        print("\nPASS - sample exact, losslessly")
        return 0

    print(
        "content  : %d of %d samples differ (%.5f%%), max delta %d"
        % (ndiff, n, 100.0 * ndiff / n, int(diff.max()))
    )

    where = np.nonzero(diff)[0] // 2  # de-interleave to frame indices
    first_ms = where[0] * 1000.0 / sr
    last_ms = where[-1] * 1000.0 / sr
    print("         : differences span %.1f ms .. %.1f ms from the start" % (first_ms, last_ms))

    # Too short to correlate meaningfully: the length check is the whole story.
    if n // 2 < int(0.05 * sr):
        if err == 0:
            print(
                "\nPASS - exact length; too short (%.1f ms) to correlate timing"
                % (n / 2.0 * 1000.0 / sr)
            )
            return 0
        print("\nDRIFT detected")
        return 1

    # Measure timing drift on a region past any bit-reservoir warm-up, so a
    # damaged head cannot masquerade as drift.
    mono_t = truth[::2]
    mono_o = ours[::2]
    skip = min(int(0.15 * sr), len(mono_t) // 4)
    body_t, body_o = mono_t[skip:], mono_o[skip:]
    win = min(2 * sr, len(body_t), len(body_o))
    max_lag = min(int(0.25 * sr), win // 2)
    lag, conf = best_lag(body_t[:win], body_o[:win], max_lag)
    print(
        "timing   : %+d samples (%+.3f ms) past the first %.0f ms, confidence %.3f"
        % (lag, lag * 1000.0 / sr, skip * 1000.0 / sr, conf)
    )

    if err != 0 or lag != 0:
        print("\nDRIFT detected")
        return 1

    # Timing is exact. Decide whether the content difference is a confined
    # reservoir warm-up at the head or a wholesale change (re-encoding).
    if last_ms <= 150.0:
        print(
            "\nPASS - sample exact in time; first %.1f ms differ (bit-reservoir warm-up)" % last_ms
        )
    else:
        print(
            "\nPASS - sample exact in time; content differs throughout (expected when re-encoded)"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
