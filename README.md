# mp3cut

Cuts MP3 files at start/end timestamps that are actually exact, without re-encoding. Built because mp3cut.net and similar tools drift — the output is not the audio you selected.

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

## Usage

```
task cut -- input.mp3 -c 1:20-2:45 -o out.mp3
```

Split a long mix into tracks in one pass:

```
task cut -- mix.mp3 -c 0:00-3:47.512 -c 3:47.512-8:11.003 -d tracks/
```

Everything after `--` goes straight to the tool, so `uv run mp3cut.py <args>` is equivalent, as is `mp3cut <args>` once installed.

Timestamps accept `83`, `83.5`, `1:23.5`, `01:02:03.456`, or `#<sample number>` for exact sample positions.

`--mode reencode` decodes and re-encodes through ffmpeg instead. It is sample-exact in time for any player, at the cost of one generation of lossy re-encoding. Use it only for the low-bitrate case noted below, or when targeting a player that ignores gapless metadata.

`--no-tags` skips copying the source ID3v2 tag.

## Verifying

`verify.py` measures the error rather than asserting correctness:

```
task verify -- source.mp3 cut.mp3 --start 1:20 --end 2:45
```

Ground truth is the source decoded from sample 0 and trimmed with ffmpeg's `atrim`. That detail matters: ffmpeg's own `-ss` seek begins decoding with a cold bit reservoir, so seek-based extraction is itself slightly wrong near the cut point and makes a poor reference. Measured against `-ss`, a correct cut looks like it has a 0.13% error; measured against a warm decode, it is bit-identical.

`task test` generates fixtures across CBR/VBR, 320 to 32 kbps, mono, MPEG2, and a file with no Xing header, then runs a matrix of cuts including file start, file end, sub-frame offsets and a 9-sample cut. Current result: 46 bit-exact, 10 exact-in-time, 0 failures across 56 cases.

## Known limitation

The gapless delay field is 12 bits, so at most 4095 + 529 samples of priming can be hidden. At roughly 40 kbps and below, frames are small enough that satisfying the bit reservoir needs more lead than that. In those cases the tool drops the excess priming frames and warns.

**Timing stays exact** when this happens — the length and both cut points are still sample-accurate. Only the first few tens of milliseconds decode from a short reservoir and may differ slightly from the source. Use `--mode reencode` if that matters. Above ~64 kbps this never triggers; every fixture at 96 kbps and up is bit-exact.
