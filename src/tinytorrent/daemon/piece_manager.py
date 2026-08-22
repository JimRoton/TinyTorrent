"""Piece storage, block assembly, and verification.

Handles mapping torrent pieces to files on disk, reading/writing piece
data, buffering in-flight blocks until a piece is fully received, and
SHA-1 verification against the info dict's piece hashes. This module has
no opinion about which peer a piece came from or what order pieces are
requested in — see torrent_session.py for that.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from tinytorrent.daemon.torrent_info import FileEntry, TorrentInfo, piece_length_for, piece_span

BLOCK_SIZE = 16384  # 16 KiB, the conventional block size for piece requests


class PieceManagerError(Exception):
    pass


def iter_blocks(info: TorrentInfo, piece_index: int) -> list[tuple[int, int]]:
    """Return ``[(begin, length), ...]`` block offsets for requesting a piece."""
    length = piece_length_for(info, piece_index)
    blocks = []
    begin = 0
    while begin < length:
        block_len = min(BLOCK_SIZE, length - begin)
        blocks.append((begin, block_len))
        begin += block_len
    return blocks


class PieceAssembler:
    """Buffers incoming blocks for one piece until it's fully received."""

    def __init__(self, piece_index: int, expected_blocks: list[tuple[int, int]]):
        self.piece_index = piece_index
        self._expected = list(expected_blocks)
        self._expected_set = set(expected_blocks)
        self._received: dict[int, bytes] = {}

    def add_block(self, begin: int, data: bytes) -> None:
        if (begin, len(data)) not in self._expected_set:
            raise PieceManagerError(
                f"unexpected block for piece {self.piece_index}: begin={begin} length={len(data)}"
            )
        self._received[begin] = data

    def has_block(self, begin: int) -> bool:
        return begin in self._received

    @property
    def is_complete(self) -> bool:
        return len(self._received) == len(self._expected_set)

    @property
    def bytes_received(self) -> int:
        return sum(len(data) for data in self._received.values())

    def assemble(self) -> bytes:
        if not self.is_complete:
            raise PieceManagerError(f"piece {self.piece_index} is not fully received yet")
        return b"".join(self._received[begin] for begin, _length in self._expected)


class PieceStorage:
    """Owns the on-disk files for one torrent; reads, writes, and verifies pieces."""

    def __init__(self, info: TorrentInfo, download_dir: Path):
        self.info = info
        self.download_dir = Path(download_dir)
        self._ensure_files_exist()

    def _file_path(self, file: FileEntry) -> Path:
        if self.info.is_multi_file:
            return self.download_dir / self.info.name / file.path
        return self.download_dir / file.path

    def _ensure_files_exist(self) -> None:
        for file in self.info.files:
            path = self._file_path(file)
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                path.touch()
            if path.stat().st_size != file.length:
                # Preallocate (or correct) the file to its declared length.
                # On common filesystems this creates a sparse file rather
                # than actually writing `length` zero bytes to disk.
                with open(path, "r+b") as f:
                    f.truncate(file.length)

    def write_piece(self, piece_index: int, data: bytes) -> None:
        expected_len = piece_length_for(self.info, piece_index)
        if len(data) != expected_len:
            raise PieceManagerError(
                f"piece {piece_index} data length {len(data)} != expected {expected_len}"
            )
        pos = 0
        for file, offset, length in piece_span(self.info, piece_index):
            path = self._file_path(file)
            with open(path, "r+b") as f:
                f.seek(offset)
                f.write(data[pos:pos + length])
            pos += length

    def read_piece(self, piece_index: int) -> bytes:
        """Read a piece's current bytes from disk, whether or not it's complete."""
        buf = bytearray()
        for file, offset, length in piece_span(self.info, piece_index):
            path = self._file_path(file)
            with open(path, "rb") as f:
                f.seek(offset)
                chunk = f.read(length)
            if len(chunk) < length:
                chunk += b"\x00" * (length - len(chunk))
            buf += chunk
        return bytes(buf)

    def verify_piece(self, piece_index: int, data: bytes) -> bool:
        return hashlib.sha1(data).digest() == self.info.piece_hashes[piece_index]

    def hash_piece_on_disk(self, piece_index: int) -> bool:
        """Re-read a piece from disk and check it against the expected hash."""
        return self.verify_piece(piece_index, self.read_piece(piece_index))

    def scan_completed_pieces(self) -> set[int]:
        """Determine which pieces are already correctly downloaded on disk.

        Used at startup/resume: rather than trust a separate serialized
        progress record that could drift out of sync with what's actually
        on disk, completeness is derived directly from the files
        themselves by re-hashing every piece. Simple and robust; the
        tradeoff is O(torrent size) disk reads at startup.
        """
        return {i for i in range(self.info.num_pieces) if self.hash_piece_on_disk(i)}

    def delete_all_files(self) -> None:
        """Remove all downloaded data for this torrent (``purge --with-data``)."""
        for file in self.info.files:
            self._file_path(file).unlink(missing_ok=True)
        if self.info.is_multi_file:
            self._remove_empty_dirs()

    def _remove_empty_dirs(self) -> None:
        torrent_root = self.download_dir / self.info.name
        if not torrent_root.exists():
            return
        for dirpath, _dirnames, _filenames in os.walk(torrent_root, topdown=False):
            try:
                os.rmdir(dirpath)
            except OSError:
                pass  # not empty (unexpected extra files) — leave it alone
