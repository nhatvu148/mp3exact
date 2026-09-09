"""Minimal ID3v2.3 tag writer.

Only what splitting needs: a handful of text frames, written as ID3v2.3 with
UTF-16LE payloads. v2.3 rather than v2.4 because more players read it, and
UTF-16 rather than Latin-1 so CJK and Vietnamese titles survive intact.
"""

# Friendly name -> ID3v2.3 frame id. Ordered so tags read sensibly in a hex dump.
FRAMES = [
    ("title", "TIT2"),
    ("artist", "TPE1"),
    ("album", "TALB"),
    ("album_artist", "TPE2"),
    ("track", "TRCK"),
    ("year", "TYER"),
    ("genre", "TCON"),
    ("comment", "COMM"),
]

ENC_UTF16 = 1  # UTF-16 with a byte order mark
BOM = b"\xff\xfe"


def _utf16(text):
    return BOM + str(text).encode("utf-16-le")


def _syncsafe(n):
    """28-bit size, 7 bits per byte, as used by the ID3v2 tag header."""
    return bytes(((n >> 21) & 0x7F, (n >> 14) & 0x7F, (n >> 7) & 0x7F, n & 0x7F))


def _frame(frame_id, payload):
    # v2.3 frame sizes are plain big-endian, unlike the syncsafe tag header.
    return frame_id.encode("ascii") + len(payload).to_bytes(4, "big") + b"\x00\x00" + payload


def _text_frame(frame_id, text):
    return _frame(frame_id, bytes([ENC_UTF16]) + _utf16(text) + b"\x00\x00")


def _comment_frame(text):
    # COMM is encoding, 3-byte language, terminated short description, then text.
    payload = bytes([ENC_UTF16]) + b"eng" + _utf16("") + b"\x00\x00" + _utf16(text) + b"\x00\x00"
    return _frame("COMM", payload)


def build_tag(padding=0, **tags):
    """Build an ID3v2.3 tag. Unknown or empty values are skipped.

    build_tag(title="Nightfall", artist="Someone", track=(3, 12))
    """
    body = b""
    for name, frame_id in FRAMES:
        value = tags.get(name)
        if value is None or value == "":
            continue
        if name == "track" and isinstance(value, (tuple, list)):
            value = "%d/%d" % (value[0], value[1]) if value[1] else str(value[0])
        if frame_id == "COMM":
            body += _comment_frame(value)
        else:
            body += _text_frame(frame_id, value)

    unknown = set(tags) - {n for n, _ in FRAMES}
    if unknown:
        raise ValueError("unknown tag(s): %s" % ", ".join(sorted(unknown)))
    if not body:
        return b""

    body += b"\x00" * padding
    return b"ID3" + b"\x03\x00" + b"\x00" + _syncsafe(len(body)) + body


def safe_filename(text, fallback="track", maxlen=120):
    """Turn a track title into something safe on macOS, Linux and Windows."""
    text = str(text).strip()
    for bad in '/\\:*?"<>|':
        text = text.replace(bad, "-")
    text = " ".join(text.split())  # collapse whitespace, including newlines
    text = text.strip(". ")  # Windows dislikes trailing dots and spaces
    if len(text) > maxlen:
        text = text[:maxlen].rstrip()
    return text or fallback
