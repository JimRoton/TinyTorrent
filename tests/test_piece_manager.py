import hashlib

import pytest

from tinytorrent.daemon.piece_manager import (
    BLOCK_SIZE,
    PieceAssembler,
    PieceManagerError,
    PieceStorage,
    iter_blocks,
)
from tinytorrent.daemon.torrent_info import parse_info_dict


def _hash(data: bytes) -> bytes:
    return hashlib.sha1(data).digest()


def _single_file_info(piece_length=10, length=25, name=b"file.txt"):
    num_pieces = -(-length // piece_length)
    piece_data = []
    for i in range(num_pieces):
        chunk_len = min(piece_length, length - i * piece_length)
        piece_data.append(bytes([ord("a") + i]) * chunk_len)
    pieces = b"".join(_hash(chunk) for chunk in piece_data)
    return parse_info_dict(
        {b"name": name, b"piece length": piece_length, b"pieces": pieces, b"length": length}
    ), piece_data


def _multi_file_info(piece_length=10, file_lengths=(15, 10), name=b"Multi"):
    total = sum(file_lengths)
    num_pieces = -(-total // piece_length)
    whole = bytes((i % 256) for i in range(total))
    pieces = b"".join(
        _hash(whole[i * piece_length: min((i + 1) * piece_length, total)]) for i in range(num_pieces)
    )
    files = [
        {b"length": length, b"path": [f"part{i}.bin".encode()]} for i, length in enumerate(file_lengths)
    ]
    info = parse_info_dict({b"name": name, b"piece length": piece_length, b"pieces": pieces, b"files": files})
    return info, whole


class TestIterBlocks:
    def test_exact_multiple(self):
        info, _ = _single_file_info(piece_length=BLOCK_SIZE * 2, length=BLOCK_SIZE * 2 + 5)
        blocks = iter_blocks(info, 0)
        assert blocks == [(0, BLOCK_SIZE), (BLOCK_SIZE, BLOCK_SIZE)]

    def test_partial_last_piece(self):
        info, _ = _single_file_info(piece_length=10, length=25)
        assert iter_blocks(info, 0) == [(0, 10)]
        assert iter_blocks(info, 2) == [(0, 5)]  # last piece is only 5 bytes


class TestPieceAssembler:
    def test_assembles_in_order_regardless_of_arrival_order(self):
        assembler = PieceAssembler(0, [(0, 4), (4, 4), (8, 2)])
        assembler.add_block(4, b"BBBB")
        assembler.add_block(0, b"AAAA")
        assert assembler.is_complete is False
        assembler.add_block(8, b"CC")
        assert assembler.is_complete is True
        assert assembler.assemble() == b"AAAABBBBCC"

    def test_bytes_received_tracks_progress(self):
        assembler = PieceAssembler(0, [(0, 4), (4, 4)])
        assert assembler.bytes_received == 0
        assembler.add_block(0, b"AAAA")
        assert assembler.bytes_received == 4

    def test_unexpected_block_rejected(self):
        assembler = PieceAssembler(0, [(0, 4)])
        with pytest.raises(PieceManagerError):
            assembler.add_block(0, b"AA")  # wrong length for this begin

    def test_assemble_before_complete_raises(self):
        assembler = PieceAssembler(0, [(0, 4), (4, 4)])
        assembler.add_block(0, b"AAAA")
        with pytest.raises(PieceManagerError):
            assembler.assemble()


class TestPieceStoragePreallocation:
    def test_single_file_created_with_correct_size(self, tmp_path):
        info, _ = _single_file_info()
        PieceStorage(info, tmp_path)
        path = tmp_path / "file.txt"
        assert path.exists()
        assert path.stat().st_size == 25

    def test_multi_file_created_under_torrent_dir(self, tmp_path):
        info, _ = _multi_file_info()
        PieceStorage(info, tmp_path)
        assert (tmp_path / "Multi" / "part0.bin").stat().st_size == 15
        assert (tmp_path / "Multi" / "part1.bin").stat().st_size == 10

    def test_reopening_existing_correctly_sized_file_does_not_wipe_it(self, tmp_path):
        info, piece_data = _single_file_info()
        storage = PieceStorage(info, tmp_path)
        storage.write_piece(0, piece_data[0])
        # Re-instantiate as if the daemon restarted.
        storage2 = PieceStorage(info, tmp_path)
        assert storage2.read_piece(0) == piece_data[0]


class TestPieceStorageReadWrite:
    def test_write_then_read_round_trip(self, tmp_path):
        info, piece_data = _single_file_info()
        storage = PieceStorage(info, tmp_path)
        storage.write_piece(1, piece_data[1])
        assert storage.read_piece(1) == piece_data[1]

    def test_write_wrong_length_rejected(self, tmp_path):
        info, _ = _single_file_info()
        storage = PieceStorage(info, tmp_path)
        with pytest.raises(PieceManagerError):
            storage.write_piece(0, b"tooshort")

    def test_read_unwritten_piece_is_zero_filled(self, tmp_path):
        info, _ = _single_file_info()
        storage = PieceStorage(info, tmp_path)
        assert storage.read_piece(0) == b"\x00" * 10

    def test_piece_spanning_two_files_round_trips(self, tmp_path):
        info, whole = _multi_file_info(piece_length=10, file_lengths=(15, 10))
        storage = PieceStorage(info, tmp_path)
        piece1 = whole[10:20]
        storage.write_piece(1, piece1)
        assert storage.read_piece(1) == piece1
        # Confirm it actually landed in both underlying files.
        with open(tmp_path / "Multi" / "part0.bin", "rb") as f:
            f.seek(10)
            assert f.read(5) == piece1[:5]
        with open(tmp_path / "Multi" / "part1.bin", "rb") as f:
            assert f.read(5) == piece1[5:]


class TestPieceStorageVerification:
    def test_verify_piece_true_for_matching_data(self, tmp_path):
        info, piece_data = _single_file_info()
        storage = PieceStorage(info, tmp_path)
        assert storage.verify_piece(0, piece_data[0]) is True

    def test_verify_piece_false_for_wrong_data(self, tmp_path):
        info, piece_data = _single_file_info()
        storage = PieceStorage(info, tmp_path)
        assert storage.verify_piece(0, b"z" * len(piece_data[0])) is False

    def test_hash_piece_on_disk_after_write(self, tmp_path):
        info, piece_data = _single_file_info()
        storage = PieceStorage(info, tmp_path)
        assert storage.hash_piece_on_disk(0) is False  # nothing written yet
        storage.write_piece(0, piece_data[0])
        assert storage.hash_piece_on_disk(0) is True

    def test_scan_completed_pieces(self, tmp_path):
        info, piece_data = _single_file_info()
        storage = PieceStorage(info, tmp_path)
        storage.write_piece(0, piece_data[0])
        storage.write_piece(2, piece_data[2])
        assert storage.scan_completed_pieces() == {0, 2}

    def test_resume_after_reopen_detects_prior_progress(self, tmp_path):
        info, piece_data = _single_file_info()
        storage = PieceStorage(info, tmp_path)
        storage.write_piece(0, piece_data[0])
        storage.write_piece(1, piece_data[1])

        resumed = PieceStorage(info, tmp_path)
        assert resumed.scan_completed_pieces() == {0, 1}


class TestPieceStorageDeletion:
    def test_delete_all_files_single_file(self, tmp_path):
        info, _ = _single_file_info()
        storage = PieceStorage(info, tmp_path)
        path = tmp_path / "file.txt"
        assert path.exists()
        storage.delete_all_files()
        assert not path.exists()

    def test_delete_all_files_multi_file_removes_dir(self, tmp_path):
        info, _ = _multi_file_info()
        storage = PieceStorage(info, tmp_path)
        storage.delete_all_files()
        assert not (tmp_path / "Multi").exists()

    def test_delete_is_idempotent(self, tmp_path):
        info, _ = _single_file_info()
        storage = PieceStorage(info, tmp_path)
        storage.delete_all_files()
        storage.delete_all_files()  # should not raise
