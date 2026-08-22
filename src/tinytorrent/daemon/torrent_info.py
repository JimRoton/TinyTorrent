"""Parses a fetched info dict (from metadata exchange) into a usable model.

This is also where path safety is enforced: a multi-file torrent's info
dict supplies file paths, and a malicious peer controls what bytes end up
in that dict. Without validation, a crafted path (``..``, an absolute
path, embedded separators) could write outside the intended download
directory. Every path segment is checked before it's trusted.
"""

from __future__ import annotations

from dataclasses import dataclass


class TorrentInfoError(Exception):
    """Raised when an info dict is missing required fields or is malformed."""


@dataclass(frozen=True)
class FileEntry:
    path: str  # relative path within the torrent's directory, '/'-separated
    length: int
    offset: int  # byte offset within the concatenated torrent data space


@dataclass(frozen=True)
class TorrentInfo:
    name: str
    piece_length: int
    piece_hashes: tuple[bytes, ...]
    files: tuple[FileEntry, ...]
    total_length: int
    is_multi_file: bool

    @property
    def num_pieces(self) -> int:
        return len(self.piece_hashes)


def parse_info_dict(info: dict) -> TorrentInfo:
    if not isinstance(info, dict):
        raise TorrentInfoError("info dict must be a dict")

    name_raw = info.get(b"name")
    if not isinstance(name_raw, bytes) or not name_raw:
        raise TorrentInfoError("info dict missing 'name'")
    name = name_raw.decode("utf-8", errors="replace")
    if name in (".", "..") or "/" in name or "\\" in name:
        raise TorrentInfoError(f"unsafe torrent name {name!r}")

    piece_length = info.get(b"piece length")
    if not isinstance(piece_length, int) or piece_length <= 0:
        raise TorrentInfoError("info dict missing/invalid 'piece length'")

    pieces_raw = info.get(b"pieces")
    if not isinstance(pieces_raw, bytes) or len(pieces_raw) == 0 or len(pieces_raw) % 20 != 0:
        raise TorrentInfoError("info dict missing/invalid 'pieces'")
    piece_hashes = tuple(pieces_raw[i:i + 20] for i in range(0, len(pieces_raw), 20))

    files_raw = info.get(b"files")
    if files_raw is not None:
        if not isinstance(files_raw, list) or not files_raw:
            raise TorrentInfoError("'files' must be a non-empty list")
        files = _parse_multi_file(files_raw)
        is_multi_file = True
    else:
        length = info.get(b"length")
        if not isinstance(length, int) or length <= 0:
            raise TorrentInfoError("info dict missing/invalid 'length' (single-file torrent)")
        files = (FileEntry(path=name, length=length, offset=0),)
        is_multi_file = False

    total_length = sum(f.length for f in files)
    if total_length <= 0:
        raise TorrentInfoError("torrent has zero total length")

    expected_pieces = -(-total_length // piece_length)  # ceil division
    if len(piece_hashes) != expected_pieces:
        raise TorrentInfoError(
            f"'pieces' has {len(piece_hashes)} hashes but total length "
            f"({total_length}) with piece length ({piece_length}) implies {expected_pieces}"
        )

    return TorrentInfo(
        name=name,
        piece_length=piece_length,
        piece_hashes=piece_hashes,
        files=files,
        total_length=total_length,
        is_multi_file=is_multi_file,
    )


def _parse_multi_file(files_raw: list) -> tuple[FileEntry, ...]:
    files = []
    seen_paths: set[str] = set()
    offset = 0
    for entry in files_raw:
        if not isinstance(entry, dict):
            raise TorrentInfoError("each entry in 'files' must be a dict")

        length = entry.get(b"length")
        if not isinstance(length, int) or length < 0:
            raise TorrentInfoError("file entry missing/invalid 'length'")

        path_segments_raw = entry.get(b"path")
        if not isinstance(path_segments_raw, list) or not path_segments_raw:
            raise TorrentInfoError("file entry missing/invalid 'path'")

        path = _sanitize_path(path_segments_raw)
        if path in seen_paths:
            raise TorrentInfoError(f"duplicate file path in torrent: {path!r}")
        seen_paths.add(path)

        files.append(FileEntry(path=path, length=length, offset=offset))
        offset += length
    return tuple(files)


def _sanitize_path(path_segments_raw: list) -> str:
    segments = []
    for segment_raw in path_segments_raw:
        if not isinstance(segment_raw, bytes):
            raise TorrentInfoError("path segment must be a byte string")
        segment = segment_raw.decode("utf-8", errors="replace")
        if segment in ("", ".", ".."):
            raise TorrentInfoError(f"unsafe path segment {segment!r}")
        if "/" in segment or "\\" in segment or "\x00" in segment:
            raise TorrentInfoError(f"path segment contains a separator or null byte: {segment!r}")
        segments.append(segment)
    return "/".join(segments)


def piece_span(info: TorrentInfo, piece_index: int) -> list[tuple[FileEntry, int, int]]:
    """Return ``[(file, offset_in_file, length), ...]`` spans covering one piece.

    A piece can span multiple files in a multi-file torrent whose
    individual files are smaller than the piece length.
    """
    if not 0 <= piece_index < info.num_pieces:
        raise TorrentInfoError(f"piece index {piece_index} out of range")

    piece_start = piece_index * info.piece_length
    piece_end = min(piece_start + info.piece_length, info.total_length)

    spans = []
    for file in info.files:
        file_start = file.offset
        file_end = file.offset + file.length
        overlap_start = max(piece_start, file_start)
        overlap_end = min(piece_end, file_end)
        if overlap_start < overlap_end:
            spans.append((file, overlap_start - file_start, overlap_end - overlap_start))
    return spans


def to_info_dict(info: TorrentInfo) -> dict:
    """Serialize a ``TorrentInfo`` back into a raw bencode-style info dict.

    The inverse of ``parse_info_dict`` — used to persist metadata that's
    already been fetched from peers, so the daemon doesn't need to
    re-fetch it from scratch after every restart.
    """
    result: dict = {
        b"name": info.name.encode("utf-8"),
        b"piece length": info.piece_length,
        b"pieces": b"".join(info.piece_hashes),
    }
    if info.is_multi_file:
        result[b"files"] = [
            {b"length": f.length, b"path": [seg.encode("utf-8") for seg in f.path.split("/")]}
            for f in info.files
        ]
    else:
        result[b"length"] = info.files[0].length
    return result


def piece_length_for(info: TorrentInfo, piece_index: int) -> int:
    """The actual length of a piece — equal to piece_length except (usually) the last."""
    if not 0 <= piece_index < info.num_pieces:
        raise TorrentInfoError(f"piece index {piece_index} out of range")
    piece_start = piece_index * info.piece_length
    piece_end = min(piece_start + info.piece_length, info.total_length)
    return piece_end - piece_start
