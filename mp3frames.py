"""MPEG-1/2/2.5 Layer III frame parsing and sample-exact frame surgery.

Shared engine behind the mp3cut / mp3split / mp3join / mp3info tools.

MP3 frames are atomic (24-26 ms), so a lossless edit can only land on a frame
boundary. The trick used throughout is to keep the frames just *outside* the
wanted range and then write a fresh Xing/LAME gapless header whose encoder
delay and padding fields tell the decoder exactly how many samples to discard
at each end. That is sample-exact while the audio frames stay byte-identical.

Frames may also store their main data in earlier frames (the bit reservoir),
so cutting reads `main_data_begin` from the side info and carries the earlier
frames that a cut point depends on.
"""

# ---------------------------------------------------------------- MPEG tables

MPEG1, MPEG2, MPEG25 = 3, 2, 0
LAYER3 = 1

SAMPLE_RATES = {
    MPEG1: (44100, 48000, 32000),
    MPEG2: (22050, 24000, 16000),
    MPEG25: (11025, 12000, 8000),
}
BITRATES = {
    MPEG1: (None, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, None),
    MPEG2: (None, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, None),
}
BITRATES[MPEG25] = BITRATES[MPEG2]

# Samples per frame, layer III
SPF = {MPEG1: 1152, MPEG2: 576, MPEG25: 576}

# Every MP3 decoder introduces this fixed delay; the LAME tag's delay field is
# stored relative to it, so players skip (delay + 529) samples. ffmpeg uses
# 528 + 1. Matching it exactly is what keeps us aligned with the reference.
DECODER_DELAY = 529

MAX_GAPLESS = 4095  # delay/padding fields are 12 bits each


class Mp3Error(Exception):
    pass


class Frame:
    """One MPEG audio frame located in the source file."""

    __slots__ = ("off", "size", "main_begin", "main_size")

    def __init__(self, off, size, main_begin, main_size):
        self.off = off
        self.size = size
        self.main_begin = main_begin
        self.main_size = main_size


def side_info_len(version, channel_mode):
    mono = channel_mode == 3
    if version == MPEG1:
        return 17 if mono else 32
    return 9 if mono else 17


def parse_header(buf, off):
    """Decode a 4-byte frame header. Returns a dict, or None if not a frame."""
    if off + 4 > len(buf):
        return None
    b0, b1, b2, b3 = buf[off], buf[off + 1], buf[off + 2], buf[off + 3]
    if b0 != 0xFF or (b1 & 0xE0) != 0xE0:
        return None

    version = (b1 >> 3) & 0x03
    if version == 1:  # reserved
        return None
    layer = (b1 >> 1) & 0x03
    if layer != LAYER3:  # we only handle Layer III
        return None
    crc = not (b1 & 0x01)

    br_idx = (b2 >> 4) & 0x0F
    sr_idx = (b2 >> 2) & 0x03
    if br_idx in (0, 15) or sr_idx == 3:  # free-format / reserved
        return None

    bitrate = BITRATES[version][br_idx]
    sample_rate = SAMPLE_RATES[version][sr_idx]
    padding = (b2 >> 1) & 0x01
    channel_mode = (b3 >> 6) & 0x03
    spf = SPF[version]

    size = (spf // 8) * bitrate * 1000 // sample_rate + padding
    if size <= 4:
        return None

    return {
        "version": version,
        "crc": crc,
        "br_idx": br_idx,
        "sr_idx": sr_idx,
        "bitrate": bitrate,
        "sample_rate": sample_rate,
        "padding": padding,
        "channel_mode": channel_mode,
        "spf": spf,
        "size": size,
        "side_len": side_info_len(version, channel_mode),
        "b3": b3,
    }


def id3v2_size(buf):
    """Length of a leading ID3v2 tag, or 0."""
    if len(buf) < 10 or buf[0:3] != b"ID3":
        return 0
    flags = buf[5]
    size = 0
    for b in buf[6:10]:
        size = (size << 7) | (b & 0x7F)
    total = 10 + size
    if flags & 0x10:  # footer present
        total += 10
    return total


# ------------------------------------------------------------- Xing/LAME tag


def read_xing(buf, off, hdr):
    """Parse a Xing/Info header frame at `off`. Returns dict or None."""
    tag_off = off + 4 + (2 if hdr["crc"] else 0) + hdr["side_len"]
    if tag_off + 8 > len(buf):
        return None
    magic = buf[tag_off : tag_off + 4]
    if magic not in (b"Xing", b"Info"):
        # Some encoders write a VBRI header instead (Fraunhofer).
        if buf[off + 4 + 32 : off + 4 + 36] == b"VBRI":
            return {
                "delay": 0,
                "padding": 0,
                "kind": b"VBRI",
                "frame_count": None,
                "byte_count": None,
            }
        return None

    p = tag_off + 4
    flags = int.from_bytes(buf[p : p + 4], "big")
    p += 4
    frame_count = byte_count = None
    if flags & 0x01:
        frame_count = int.from_bytes(buf[p : p + 4], "big")
        p += 4
    if flags & 0x02:
        byte_count = int.from_bytes(buf[p : p + 4], "big")
        p += 4
    if flags & 0x04:
        p += 100  # TOC
    if flags & 0x08:
        p += 4  # quality

    delay = padding = 0
    version_str = buf[p : p + 9]
    if version_str[:4] in (b"LAME", b"Lavc", b"Lavf") and p + 24 <= len(buf):
        d = buf[p + 21 : p + 24]
        delay = (d[0] << 4) | (d[1] >> 4)
        padding = ((d[1] & 0x0F) << 8) | d[2]

    return {
        "delay": delay,
        "padding": padding,
        "kind": magic,
        "frame_count": frame_count,
        "byte_count": byte_count,
    }


def build_xing_frame(hdr, frame_count, byte_count, toc, delay, padding):
    """Create a fresh Xing header frame carrying our gapless values."""
    version = hdr["version"]
    side_len = hdr["side_len"]
    tag_len = 4 + 4 + 4 + 4 + 100 + 36  # magic+flags+frames+bytes+TOC+LAME
    needed = 4 + side_len + tag_len

    # Smallest bitrate whose frame is large enough to hold the tag.
    br_idx = None
    for i in range(1, 15):
        br = BITRATES[version][i]
        if br is None:
            continue
        size = (hdr["spf"] // 8) * br * 1000 // hdr["sample_rate"]
        if size >= needed:
            br_idx, frame_size = i, size
            break
    if br_idx is None:
        raise Mp3Error("cannot fit a Xing header at this sample rate")

    b1 = 0xE0 | (version << 3) | (LAYER3 << 1) | 0x01  # no CRC
    b2 = (br_idx << 4) | (hdr["sr_idx"] << 2)
    out = bytearray(frame_size)
    out[0] = 0xFF
    out[1] = b1
    out[2] = b2
    out[3] = hdr["b3"]  # keep channel mode

    p = 4 + side_len
    out[p : p + 4] = b"Xing"
    out[p + 4 : p + 8] = (0x0007).to_bytes(4, "big")  # frames | bytes | TOC
    out[p + 8 : p + 12] = frame_count.to_bytes(4, "big")
    out[p + 12 : p + 16] = byte_count.to_bytes(4, "big")
    out[p + 16 : p + 116] = bytes(toc)

    lame = bytearray(36)
    lame[0:9] = b"LAME3.100"
    lame[21] = (delay >> 4) & 0xFF
    lame[22] = ((delay & 0x0F) << 4) | ((padding >> 8) & 0x0F)
    lame[23] = padding & 0xFF
    out[p + 116 : p + 152] = lame
    return bytes(out)


def build_silent_frame(hdr):
    """A valid all-zero Layer III frame: side info and main data of zeros
    decode to silence, and the decoder's overlap buffer starts at zero anyway,
    so prepending one is inaudible."""
    version = hdr["version"]
    for i in range(1, 15):
        br = BITRATES[version][i]
        if br is None:
            continue
        size = (hdr["spf"] // 8) * br * 1000 // hdr["sample_rate"]
        if size > 4 + hdr["side_len"]:
            out = bytearray(size)
            out[0] = 0xFF
            out[1] = 0xE0 | (version << 3) | (LAYER3 << 1) | 0x01
            out[2] = (i << 4) | (hdr["sr_idx"] << 2)
            out[3] = hdr["b3"]
            return bytes(out)
    raise Mp3Error("cannot build a silent lead frame")


def build_toc(frames, first, last, total_bytes):
    """100-entry seek table over the frames we are about to write."""
    n = last - first + 1
    base = frames[first].off
    toc = bytearray(100)
    for i in range(100):
        target = first + (i * n) // 100
        rel = frames[target].off - base
        toc[i] = min(255, rel * 255 // total_bytes if total_bytes else 0)
    return toc


# --------------------------------------------------------------- frame index


def index_frames(data):
    """Scan the whole file and return (frames, header, xing)."""
    pos = id3v2_size(data)
    n = len(data)

    # Find the first valid frame.
    hdr = None
    while pos < n - 4:
        hdr = parse_header(data, pos)
        if hdr and parse_header(data, pos + hdr["size"]):
            break
        hdr = None
        pos += 1
    if hdr is None:
        raise Mp3Error("no MPEG audio frames found")

    xing = read_xing(data, pos, hdr)
    if xing:
        pos += hdr["size"]  # the Xing frame is metadata, not audio
        h2 = parse_header(data, pos)
        if h2:
            hdr = h2

    frames = []
    base = hdr
    while pos + 4 <= n:
        h = parse_header(data, pos)
        if h is None:
            # Trailing ID3v1/APE tag, or a corrupt byte: try to resync once.
            nxt = data.find(b"\xff", pos + 1)
            if nxt < 0 or nxt > pos + 8192:
                break
            pos = nxt
            continue
        if pos + h["size"] > n:
            break
        hlen = 4 + (2 if h["crc"] else 0)
        si = pos + hlen
        if h["version"] == MPEG1:
            main_begin = (data[si] << 1) | (data[si + 1] >> 7)  # 9 bits
        else:
            main_begin = data[si]  # 8 bits
        main_size = h["size"] - hlen - h["side_len"]
        frames.append(Frame(pos, h["size"], main_begin, main_size))
        pos += h["size"]

    if not frames:
        raise Mp3Error("no audio frames after the header frame")
    return frames, base, xing


def priming_start(frames, idx):
    """First frame index needed so that `idx` decodes bit-exactly.

    Walks back far enough to satisfy the bit reservoir of `idx` and of every
    frame in between, and always includes at least one extra frame so the
    MDCT overlap into `idx` is correct.
    """
    if idx == 0:
        return 0
    prev = frames[idx - 1]
    # Bytes that must precede `idx`: enough for its own reservoir, and enough
    # that the frame before it also decodes, so the MDCT overlap is right.
    need = max(frames[idx].main_begin, prev.main_begin + prev.main_size)
    acc = 0
    i = idx - 1
    while i >= 0:
        acc += frames[i].main_size
        if acc >= need:
            return i
        i -= 1
    return 0


# ----------------------------------------------------------------- time input


def parse_time(text, sample_rate):
    """Accept 83, 83.5, 1:23.5, 01:02:03.456, or #<samples>."""
    text = text.strip()
    if not text:
        raise ValueError("empty timestamp")
    if text.startswith("#"):
        return int(text[1:])
    parts = text.split(":")
    if len(parts) > 3:
        raise ValueError("too many ':' in %r" % text)
    seconds = 0.0
    for part in parts:
        seconds = seconds * 60 + float(part)
    return int(round(seconds * sample_rate))


def fmt_samples(s, sr):
    t = s / sr
    h, rem = divmod(t, 3600)
    m, sec = divmod(rem, 60)
    return "%d:%02d:%06.3f" % (int(h), int(m), sec)


# ---------------------------------------------------------------------- cut


def cut(data, frames, hdr, xing, start_sample, end_sample, keep_tags, quiet=False):
    """Return the bytes of a new MP3 containing exactly [start, end) samples."""
    spf = hdr["spf"]
    sr = hdr["sample_rate"]
    total = len(frames) * spf

    # Music time 0 sits `delay + 529` samples into the raw decoded stream.
    # Without a LAME tag a reference decoder outputs from raw sample 0.
    src_delay = (xing["delay"] + DECODER_DELAY) if (xing and xing.get("kind") != b"VBRI") else 0
    # A player skips (delay + 529) at the front and trims (padding - 529) at
    # the end, so the two 529s cancel in the total.
    music_len = total - (xing["delay"] if xing else 0) - (xing["padding"] if xing else 0)

    if end_sample <= start_sample:
        raise Mp3Error("end must be after start")
    if start_sample >= music_len:
        raise Mp3Error("start is past the end of the audio (%s)" % fmt_samples(music_len, sr))
    end_sample = min(end_sample, music_len)

    raw_start = start_sample + src_delay
    raw_end = end_sample + src_delay

    first_audio = raw_start // spf
    last_audio = min((raw_end - 1) // spf, len(frames) - 1)

    first = priming_start(frames, first_audio)
    # One trailing frame so the final MDCT overlap is complete.
    last = min(last_audio + 1, len(frames) - 1)

    n0 = raw_start - first * spf  # samples to drop at the front
    out_total = (last - first + 1) * spf
    n1 = raw_end - first * spf  # last sample we keep

    delay = n0 - DECODER_DELAY
    padding = (out_total - n1) + DECODER_DELAY

    warning = None
    if delay > MAX_GAPLESS:
        # Too much priming to hide in 12 bits: drop priming frames until it
        # fits. Costs a few ms of reservoir accuracy at the very first frame.
        drop = (delay - MAX_GAPLESS + spf - 1) // spf
        first += drop
        n0 = raw_start - first * spf
        out_total = (last - first + 1) * spf
        n1 = raw_end - first * spf
        delay = n0 - DECODER_DELAY
        padding = (out_total - n1) + DECODER_DELAY
        warning = (
            "dropped %d priming frame(s) to fit the 12-bit delay field; "
            "timing stays exact, but the first ~%d ms may decode with a "
            "short bit-reservoir; --mode reencode removes it, at the cost "
            "of re-encoding the whole track" % (drop, drop * spf * 1000 // sr)
        )
    lead_silence = 0
    if delay < 0:
        # Decoders always skip (delay + 529), so we need at least 529 samples
        # of lead before the first sample we want to keep. Borrow a real frame
        # if one exists; at the very start of the file, prepend a silent one.
        if first > 0:
            first -= 1
        else:
            lead_silence = 1
        n0 = raw_start - first * spf + lead_silence * spf
        out_total = (last - first + 1 + lead_silence) * spf
        n1 = raw_end - first * spf + lead_silence * spf
        delay = n0 - DECODER_DELAY
        padding = (out_total - n1) + DECODER_DELAY
    padding = max(0, min(padding, MAX_GAPLESS))

    payload = data[frames[first].off : frames[last].off + frames[last].size]
    if lead_silence:
        payload = build_silent_frame(hdr) + payload
    frame_count = last - first + 1 + lead_silence
    toc = build_toc(frames, first, last, len(payload))
    header_frame = build_xing_frame(hdr, frame_count, len(payload), toc, delay, padding)
    # Now that the header frame's size is known, correct the byte count.
    tag_p = 4 + hdr["side_len"] + 12  # magic(4) + flags(4) + frames(4)
    header_frame = (
        header_frame[:tag_p]
        + (len(payload) + len(header_frame)).to_bytes(4, "big")
        + header_frame[tag_p + 4 :]
    )

    out = bytearray()
    if keep_tags:
        tag_len = id3v2_size(data)
        if tag_len:
            out += data[:tag_len]
    out += header_frame
    out += payload

    info = {
        "frames": frame_count,
        "delay": delay,
        "padding": padding,
        "priming_frames": first_audio - first,
        "out_samples": n1 - n0,
        "warning": warning,
    }
    return bytes(out), info


# ------------------------------------------------------------------ reencode


def average_bitrate(frames, hdr):
    """Mean kbps over the whole stream.

    The header of frame 0 only describes frame 0, which in a VBR file says
    little about the rest, so re-encoding targets this instead.
    """
    if not frames:
        return 0
    audio_bytes = sum(f.size for f in frames)
    samples = len(frames) * hdr["spf"]
    return int(round(audio_bytes * 8 * hdr["sample_rate"] / (samples * 1000.0)))


def describe_ffmpeg_error(exc):
    """One sentence for a failed reencode(), whatever went wrong."""
    import subprocess

    if isinstance(exc, FileNotFoundError):
        return "ffmpeg not found; --mode reencode needs it on PATH"
    if isinstance(exc, subprocess.CalledProcessError):
        return "ffmpeg exited %s (its own error is above)" % exc.returncode
    return str(exc)


def reencode(src, dst, start_sample, end_sample, sr, bitrate_kbps, strip_tags=False):
    """Decode, trim to an exact sample range, and re-encode.

    Exact in time for any player, including ones that ignore gapless metadata,
    at the cost of one lossy generation. `strip_tags` leaves the file without
    an ID3 header so a caller can prepend its own.
    """
    import subprocess

    filt = "atrim=start_sample=%d:end_sample=%d,asetpts=N/SR/TB" % (start_sample, end_sample)
    cmd = ["ffmpeg", "-v", "error", "-y", "-i", src, "-af", filt]
    if strip_tags:
        cmd += ["-map_metadata", "-1", "-id3v2_version", "0", "-write_id3v1", "0"]
    cmd += ["-c:a", "libmp3lame", "-b:a", "%dk" % bitrate_kbps, dst]
    subprocess.run(cmd, check=True)
