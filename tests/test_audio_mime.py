"""Audio MIME sniffing, extension mapping and media kind. No browser or network."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine import MuseEngine, _sniff_mime, media_kind  # noqa: E402

WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\0" * 24
MP3 = b"ID3\x03\x00\x00\x00\x00\x00\x0f" + b"\xff\xfb\x90\x00" * 8
AAC = b"\xff\xf1" + b"\0" * 16
OGG = b"OggS" + b"\0" * 8 + b"OpusHead"
FLAC = b"fLaC" + b"\0" * 16
M4A = (8 + 12).to_bytes(4, "big") + b"ftyp" + b"M4A " + b"\0\0\2\0" + b"M4A mp42"


def main():
    assert _sniff_mime(WAV) == "audio/wav"
    assert _sniff_mime(MP3) == "audio/mpeg"
    assert _sniff_mime(AAC) == "audio/aac"
    assert _sniff_mime(OGG) == "audio/opus"
    assert _sniff_mime(FLAC) == "audio/flac"
    assert _sniff_mime(M4A) == "audio/mp4"

    pick = MuseEngine._pick_ext
    assert pick("audio/mpeg", "blob:x", "audio") == ".mp3"
    assert pick("audio/mp3", "blob:x", "audio") == ".mp3"
    assert pick("audio/wav", "blob:x", "audio") == ".wav"
    assert pick("audio/mp4", "blob:x", "audio") == ".m4a"
    assert pick("audio/ogg", "blob:x", "audio") == ".ogg"
    assert pick("audio/flac", "blob:x", "audio") == ".flac"
    assert pick("", "https://x/voice.opus", "audio") == ".opus"
    assert pick("", "blob:x", "audio") == ".mp3"

    assert media_kind(".mp3") == "audio"
    assert media_kind(".M4A") == "audio"
    assert media_kind(".mp4") == "video"
    assert media_kind(".webp") == "image"

    print("PASS: audio sniffing, extension mapping and media kind")


if __name__ == "__main__":
    main()
