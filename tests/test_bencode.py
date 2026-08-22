import pytest

from tinytorrent.daemon.bencode import (
    BencodeDecodeError,
    BencodeEncodeError,
    decode,
    decode_from,
    encode,
)


class TestEncodeIntegers:
    def test_positive(self):
        assert encode(42) == b"i42e"

    def test_zero(self):
        assert encode(0) == b"i0e"

    def test_negative(self):
        assert encode(-13) == b"i-13e"

    def test_bool_rejected(self):
        with pytest.raises(BencodeEncodeError):
            encode(True)


class TestEncodeStrings:
    def test_bytes(self):
        assert encode(b"spam") == b"4:spam"

    def test_str_utf8(self):
        assert encode("spam") == b"4:spam"

    def test_empty(self):
        assert encode(b"") == b"0:"


class TestEncodeLists:
    def test_simple(self):
        assert encode([b"spam", b"eggs"]) == b"l4:spam4:eggse"

    def test_empty(self):
        assert encode([]) == b"le"

    def test_nested(self):
        assert encode([1, [b"a"]]) == b"li1el1:aee"


class TestEncodeDicts:
    def test_keys_sorted(self):
        # Keys must come out sorted regardless of insertion order.
        assert encode({b"z": 1, b"a": 2}) == b"d1:ai2e1:zi1ee"

    def test_str_keys_coerced_to_bytes(self):
        assert encode({"cow": "moo", "spam": "eggs"}) == (
            b"d3:cow3:moo4:spam4:eggse"
        )

    def test_empty(self):
        assert encode({}) == b"de"

    def test_non_str_bytes_key_rejected(self):
        with pytest.raises(BencodeEncodeError):
            encode({1: "x"})


class TestDecodeIntegers:
    def test_positive(self):
        assert decode(b"i42e") == 42

    def test_negative(self):
        assert decode(b"i-13e") == -13

    def test_zero(self):
        assert decode(b"i0e") == 0

    def test_leading_zero_rejected(self):
        with pytest.raises(BencodeDecodeError):
            decode(b"i042e")

    def test_negative_zero_rejected(self):
        with pytest.raises(BencodeDecodeError):
            decode(b"i-0e")

    def test_unterminated(self):
        with pytest.raises(BencodeDecodeError):
            decode(b"i42")


class TestDecodeStrings:
    def test_simple(self):
        assert decode(b"4:spam") == b"spam"

    def test_empty(self):
        assert decode(b"0:") == b""

    def test_length_exceeds_data(self):
        with pytest.raises(BencodeDecodeError):
            decode(b"10:short")


class TestDecodeLists:
    def test_simple(self):
        assert decode(b"l4:spam4:eggse") == [b"spam", b"eggs"]

    def test_empty(self):
        assert decode(b"le") == []

    def test_unterminated(self):
        with pytest.raises(BencodeDecodeError):
            decode(b"l4:spam")


class TestDecodeDicts:
    def test_simple(self):
        assert decode(b"d3:cow3:moo4:spam4:eggse") == {b"cow": b"moo", b"spam": b"eggs"}

    def test_nested(self):
        data = encode({"info": {"length": 100, "name": "file.txt"}})
        assert decode(data) == {b"info": {b"length": 100, b"name": b"file.txt"}}

    def test_non_bytes_key_rejected(self):
        with pytest.raises(BencodeDecodeError):
            decode(b"di1e3:vale")


class TestDecodeMisc:
    def test_trailing_data_rejected(self):
        with pytest.raises(BencodeDecodeError):
            decode(b"i42eextra")

    def test_invalid_marker(self):
        with pytest.raises(BencodeDecodeError):
            decode(b"x")

    def test_empty_input(self):
        with pytest.raises(BencodeDecodeError):
            decode(b"")

    def test_decode_from_leaves_remainder(self):
        value, offset = decode_from(b"i42eREST", 0)
        assert value == 42
        assert b"i42eREST"[offset:] == b"REST"


class TestRoundTrip:
    @pytest.mark.parametrize(
        "value",
        [
            0,
            1,
            -1,
            123456789,
            b"",
            b"hello world",
            [],
            [1, 2, 3],
            {},
            {b"a": 1, b"b": [1, 2, {b"c": b"d"}]},
        ],
    )
    def test_round_trip(self, value):
        assert decode(encode(value)) == value
