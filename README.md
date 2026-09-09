# mp3cut

Sample-exact MP3 tools that do not re-encode and do not drift. Built because mp3cut.net and similar tools drift — the output is not the audio you selected.

| tool | what it does |
| --- | --- |
| `mp3cut` | cut one or more exact ranges out of a file |
| `mp3split` | split a mix into tagged tracks from a pasted tracklist |
| `mp3join` | join files back together, trimming as much padding as MP3 allows |
| `mp3info` | inspect frames, gapless metadata and structural integrity |

All four share one engine, `mp3frames.py`, which parses every MPEG frame directly.

## Why other cutters drift

Three separate problems stack up, and none of them is a rounding bug you can just tighten:

**MP3 frames are atomic.** A frame holds 1152 samples (576 for MPEG2/2.5) and cannot be split. At 48 kHz that is 24 ms per frame, so a naive lossless cut lands on a frame boundary and is off by up to ±24 ms at each end, every time.

**VBR seeking by average bitrate.** In a variable-bitrate file there is no fixed bytes-per-second, so converting a timestamp to a byte offset with the average bitrate is wrong nearly everywhere, and the error grows with position. An hour into a VBR mix that can be seconds, not milliseconds.

**Ignored encoder delay and padding.** Encoders prepend priming samples and append padding, recorded in the Xing/LAME header. A decoder skips `delay + 529` samples at the front and trims `padding - 529` at the end. A cutter that ignores this shifts everything by roughly 23 ms, and computes the wrong total duration too.

## How this tool gets it exact

It parses every frame header in the file, so timestamps map to frames with no bitrate estimation — VBR and CBR are handled identically.

It then cuts on the frame boundaries *outside* the requested range and writes a fresh Xing/LAME header whose delay and padding fields tell the decoder exactly how many samples to discard at each end. The frame boundary problem does not go away; it gets hidden by the same gapless mechanism encoders already use. Output is sample-exact while the audio frames stay byte-for-byte identical to the source.

It also carries the **bit reservoir**. MP3 frames can store their main data in earlier frames, so a frame at a cut point may reference bytes that the cut would remove. The tool reads `main_data_begin` from the side info and includes exactly the earlier frames needed — plus one more so the MDCT overlap into the first kept frame is correct — then hides those priming frames with the same delay field.

## Setup

Needs [uv](https://docs.astral.sh/uv/) and [Task](https://taskfile.dev); ffmpeg is needed only for verifying and for `--mode reencode`. The cutter itself is pure standard library, so `python3 mp3cut.py ...` also works with no environment at all.

```
task sync            # create the uv environment
task --list          # see everything
task install         # optional: install `mp3cut` as a global CLI
```

## mp3cut — exact ranges

```
task cut -- input.mp3 -c 1:20-2:45 -o out.mp3
```

Split a long mix into tracks in one pass:

```
task cut -- mix.mp3 -c 0:00-3:47.512 -c 3:47.512-8:11.003 -d tracks/
```

Everything after `--` goes straight to the tool, so `uv run mp3cut.py <args>` is equivalent, as is `mp3cut <args>` once installed.

## mp3split — a mix into tagged tracks

Paste the tracklist out of a YouTube description into a file and point at it:

```
task split -- mix.mp3 -t list.txt -d tracks/ --album "Midnight Memories"
```

Lines are matched on whatever timestamp they contain, so the usual mess all parses: `00:00 Artist - Title`, `1. 03:47.512 Title`, `[08:11] Artist — Title`, `0:15:02 Title`. Track numbering, bullets and brackets are stripped, lines without a timestamp are ignored, and `Artist - Title` is split on `-`, `–`, `—` or `~`. Each track runs to the next one's start, the last runs to the end of the mix.

Every track gets its own ID3v2.3 tag — title, artist, album, album artist, track number, optional year and genre — written in UTF-16 so CJK and Vietnamese titles survive. Filenames are sanitized for macOS, Linux and Windows. Use `-n` to preview the split before writing anything, and `-t -` to read the tracklist from standard input.

## mp3join — put them back together

```
task join -- 01.mp3 02.mp3 03.mp3 -o whole.mp3
```

Inputs must share a sample rate, channel mode and MPEG version; mismatches are refused rather than silently producing a broken file.

**This is not gapless, and cannot be.** A single MP3 carries only one encoder delay / padding pair, in its Xing header, so only the very start and the very end of the result can be trimmed to the sample. At each interior seam the best available without re-encoding is to drop whole frames, which leaves under two frames — measured at exactly one frame, 24 ms, when joining files this toolkit produced. The tool prints the real cost of every seam rather than hiding it.

That still beats `cat` or mp3wrap, which keep the full priming silence at every seam and leave a duration no player computes correctly. If you need a truly seamless join, the seam has to be re-encoded.

One deliberate trade-off: after a seam the first frame may decode from a short bit reservoir. Carrying the reservoir frames instead would re-introduce ~87 ms of duplicated audio at every join, which is far more audible than the alternative.

## mp3info — what is actually in the file

```
task info -- suspect.mp3
task info -- suspect.mp3 --frames 20
```

Reports format, VBR/CBR and average bitrate, frame count, decoded versus gapless-trimmed duration, the delay and padding values a player will act on, and structural problems: a Xing frame count that disagrees with reality, resync gaps, unexpected trailing data, padding below the decoder delay. The useful move is pointing it at some *other* tool's output to see where that tool went wrong.

Timestamps accept `83`, `83.5`, `1:23.5`, `01:02:03.456`, or `#<sample number>` for exact sample positions.

`--mode reencode` decodes and re-encodes through ffmpeg instead. It is sample-exact in time for any player, at the cost of one generation of lossy re-encoding. Use it only for the low-bitrate case noted below, or when targeting a player that ignores gapless metadata.

`--no-tags` skips copying the source ID3v2 tag.

## Verifying

`verify.py` measures the error rather than asserting correctness:

```
task verify -- source.mp3 cut.mp3 --start 1:20 --end 2:45
```

Ground truth is the source decoded from sample 0 and trimmed with ffmpeg's `atrim`. That detail matters: ffmpeg's own `-ss` seek begins decoding with a cold bit reservoir, so seek-based extraction is itself slightly wrong near the cut point and makes a poor reference. Measured against `-ss`, a correct cut looks like it has a 0.13% error; measured against a warm decode, it is bit-identical.

`task test` generates fixtures across CBR/VBR, 320 to 32 kbps, mono, MPEG2, and a file with no Xing header, then runs a matrix of cuts including file start, file end, sub-frame offsets and a 9-sample cut, followed by split, join and info checks. Current result: 56 cut cases (46 bit-exact, 10 exact-in-time), split verified per track plus tag read-back, join within two frames per seam, info clean on all 8 fixtures — 0 failures.

## Known limitation

The gapless delay field is 12 bits, so at most 4095 + 529 samples of priming can be hidden. At roughly 40 kbps and below, frames are small enough that satisfying the bit reservoir needs more lead than that. In those cases the tool drops the excess priming frames and warns.

**Timing stays exact** when this happens — the length and both cut points are still sample-accurate. Only the first few tens of milliseconds decode from a short reservoir and may differ slightly from the source. Use `--mode reencode` if that matters. Above ~64 kbps this never triggers; every fixture at 96 kbps and up is bit-exact.
