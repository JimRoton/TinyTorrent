import hashlib

import pytest

from tinytorrent.daemon.torrent_info import (
    TorrentInfoError,
    parse_info_dict,
    piece_length_for,
    piece_span,
    to_info_dict,
)


def _hash(data: bytes) -> bytes:
    return hashlib.sha1(data).digest()


def _single_file_info(*, piece_length=10, length=25, name=b"file.txt"):
    pieces = _hash(b"a" * piece_length) + _hash(b"a" * piece_length) + _hash(b"a" * (length - 2 * piece_length))
    return {
        b"name": name,
        b"piece length": piece_length,
        b"pieces": pieces,
        b"length": length,
    }


def _multi_file_info(*, piece_length=10, file_lengths=(15, 10), name=b"MyTorrent"):
    total = sum(file_lengths)
    num_pieces = -(-total // piece_length)
    pieces = b"".join(_hash(bytes([i])) for i in range(num_pieces))  # hashes don't need to be "real" for parsing
    files = [{b"length": length, b"path": [f"part{i}.bin".encode()]} for i, length in enumerate(file_lengths)]
    return {
        b"name": name,
        b"piece length": piece_length,
        b"pieces": pieces,
        b"files": files,
    }


class TestSingleFileParsing:
    def test_basic_fields(self):
        info = parse_info_dict(_single_file_info())
        assert info.name == "file.txt"
        assert info.piece_length == 10
        assert info.total_length == 25
        assert info.is_multi_file is False
        assert info.num_pieces == 3
        assert len(info.files) == 1
        assert info.files[0].path == "file.txt"
        assert info.files[0].length == 25
        assert info.files[0].offset == 0

    def test_missing_name(self):
        d = _single_file_info()
        del d[b"name"]
        with pytest.raises(TorrentInfoError):
            parse_info_dict(d)

    def test_missing_length(self):
        d = _single_file_info()
        del d[b"length"]
        with pytest.raises(TorrentInfoError):
            parse_info_dict(d)

    def test_bad_piece_length(self):
        d = _single_file_info()
        d[b"piece length"] = 0
        with pytest.raises(TorrentInfoError):
            parse_info_dict(d)

    def test_pieces_not_multiple_of_20(self):
        d = _single_file_info()
        d[b"pieces"] = d[b"pieces"][:-1]
        with pytest.raises(TorrentInfoError):
            parse_info_dict(d)

    def test_piece_count_mismatch(self):
        d = _single_file_info()
        d[b"pieces"] = d[b"pieces"] + _hash(b"extra")
        with pytest.raises(TorrentInfoError):
            parse_info_dict(d)

    def test_unsafe_name_rejected(self):
        d = _single_file_info(name=b"../evil")
        with pytest.raises(TorrentInfoError):
            parse_info_dict(d)

    def test_not_a_dict(self):
        with pytest.raises(TorrentInfoError):
            parse_info_dict([1, 2, 3])


class TestMultiFileParsing:
    def test_basic_fields(self):
        info = parse_info_dict(_multi_file_info())
        assert info.name == "MyTorrent"
        assert info.is_multi_file is True
        assert info.total_length == 25
        assert [f.path for f in info.files] == ["part0.bin", "part1.bin"]
        assert info.files[0].offset == 0
        assert info.files[1].offset == 15

    def test_path_traversal_rejected(self):
        d = _multi_file_info()
        d[b"files"][0][b"path"] = [b"..", b"etc", b"passwd"]
        with pytest.raises(TorrentInfoError):
            parse_info_dict(d)

    def test_absolute_like_segment_rejected(self):
        d = _multi_file_info()
        d[b"files"][0][b"path"] = [b"sub/dir", b"file.txt"]
        with pytest.raises(TorrentInfoError):
            parse_info_dict(d)

    def test_empty_segment_rejected(self):
        d = _multi_file_info()
        d[b"files"][0][b"path"] = [b""]
        with pytest.raises(TorrentInfoError):
            parse_info_dict(d)

    def test_duplicate_paths_rejected(self):
        d = _multi_file_info()
        d[b"files"][1][b"path"] = d[b"files"][0][b"path"]
        with pytest.raises(TorrentInfoError):
            parse_info_dict(d)

    def test_nested_path_allowed(self):
        d = _multi_file_info()
        d[b"files"][0][b"path"] = [b"subdir", b"nested.bin"]
        info = parse_info_dict(d)
        assert info.files[0].path == "subdir/nested.bin"

    def test_empty_files_list_rejected(self):
        d = _multi_file_info()
        d[b"files"] = []
        with pytest.raises(TorrentInfoError):
            parse_info_dict(d)


class TestPieceSpan:
    def test_single_file_piece_within_bounds(self):
        info = parse_info_dict(_single_file_info(piece_length=10, length=25))
        spans = piece_span(info, 0)
        assert len(spans) == 1
        file, offset, length = spans[0]
        assert offset == 0
        assert length == 10

    def test_last_piece_is_shorter(self):
        info = parse_info_dict(_single_file_info(piece_length=10, length=25))
        spans = piece_span(info, 2)
        _file, offset, length = spans[0]
        assert offset == 20
        assert length == 5
        assert piece_length_for(info, 2) == 5

    def test_piece_spans_multiple_files(self):
        # piece_length=10; files are 15 and 10 bytes -> piece 1 (bytes 10-19)
        # covers the tail of file0 (bytes 10-14) and the start of file1 (bytes 0-4).
        info = parse_info_dict(_multi_file_info(piece_length=10, file_lengths=(15, 10)))
        spans = piece_span(info, 1)
        assert len(spans) == 2
        (file0, offset0, len0), (file1, offset1, len1) = spans
        assert file0.path == "part0.bin"
        assert offset0 == 10
        assert len0 == 5
        assert file1.path == "part1.bin"
        assert offset1 == 0
        assert len1 == 5

    def test_out_of_range_piece(self):
        info = parse_info_dict(_single_file_info())
        with pytest.raises(TorrentInfoError):
            piece_span(info, 999)


class TestToInfoDictRoundTrip:
    def test_single_file_round_trips(self):
        original_dict = _single_file_info()
        info = parse_info_dict(original_dict)
        reparsed = parse_info_dict(to_info_dict(info))
        assert reparsed == info

    def test_multi_file_round_trips(self):
        original_dict = _multi_file_info()
        info = parse_info_dict(original_dict)
        reparsed = parse_info_dict(to_info_dict(info))
        assert reparsed == info

    def test_nested_multi_file_path_round_trips(self):
        d = _multi_file_info()
        d[b"files"][0][b"path"] = [b"subdir", b"nested.bin"]
        info = parse_info_dict(d)
        reparsed = parse_info_dict(to_info_dict(info))
        assert reparsed.files[0].path == "subdir/nested.bin"
        assert reparsed == info
