import base64

import pytest

from tinytorrent.daemon.magnet import MagnetLink, MagnetParseError, parse

INFO_HASH_BYTES = bytes(range(20))  # 000102...13
INFO_HASH_HEX = INFO_HASH_BYTES.hex()
INFO_HASH_B32 = base64.b32encode(INFO_HASH_BYTES).decode("ascii")


class TestBasicParsing:
    def test_hex_info_hash(self):
        link = parse(f"magnet:?xt=urn:btih:{INFO_HASH_HEX}")
        assert link.info_hash == INFO_HASH_BYTES

    def test_hex_info_hash_uppercase(self):
        link = parse(f"magnet:?xt=urn:btih:{INFO_HASH_HEX.upper()}")
        assert link.info_hash == INFO_HASH_BYTES

    def test_base32_info_hash(self):
        link = parse(f"magnet:?xt=urn:btih:{INFO_HASH_B32}")
        assert link.info_hash == INFO_HASH_BYTES

    def test_info_hash_hex_property(self):
        link = parse(f"magnet:?xt=urn:btih:{INFO_HASH_HEX}")
        assert link.info_hash_hex == INFO_HASH_HEX

    def test_display_name(self):
        link = parse(f"magnet:?xt=urn:btih:{INFO_HASH_HEX}&dn=Some%20Linux%20ISO")
        assert link.display_name == "Some Linux ISO"

    def test_no_display_name(self):
        link = parse(f"magnet:?xt=urn:btih:{INFO_HASH_HEX}")
        assert link.display_name is None

    def test_single_tracker(self):
        link = parse(
            f"magnet:?xt=urn:btih:{INFO_HASH_HEX}&tr=http%3A%2F%2Ftracker.example%3A80%2Fannounce"
        )
        assert link.trackers == ("http://tracker.example:80/announce",)

    def test_multiple_trackers_preserve_order(self):
        uri = (
            f"magnet:?xt=urn:btih:{INFO_HASH_HEX}"
            "&tr=http%3A%2F%2Fa.example%2Fannounce"
            "&tr=udp%3A%2F%2Fb.example%3A1337"
            "&tr=http%3A%2F%2Fc.example%2Fannounce"
        )
        link = parse(uri)
        assert link.trackers == (
            "http://a.example/announce",
            "udp://b.example:1337",
            "http://c.example/announce",
        )

    def test_no_trackers(self):
        link = parse(f"magnet:?xt=urn:btih:{INFO_HASH_HEX}")
        assert link.trackers == ()

    def test_full_realistic_link(self):
        uri = (
            f"magnet:?xt=urn:btih:{INFO_HASH_HEX}"
            "&dn=example.iso"
            "&tr=udp%3A%2F%2Ftracker.example%3A1337%2Fannounce"
            "&tr=udp%3A%2F%2Ftracker2.example%3A80"
        )
        link = parse(uri)
        assert link == MagnetLink(
            info_hash=INFO_HASH_BYTES,
            display_name="example.iso",
            trackers=(
                "udp://tracker.example:1337/announce",
                "udp://tracker2.example:80",
            ),
        )

    def test_unrelated_params_ignored(self):
        # 'kt' (keyword topic), 'ws' (web seed) and similar are legal
        # magnet params we don't use — they must not break parsing.
        link = parse(f"magnet:?xt=urn:btih:{INFO_HASH_HEX}&kt=foo&x.pe=1.2.3.4%3A6881")
        assert link.info_hash == INFO_HASH_BYTES


class TestErrors:
    def test_empty_string(self):
        with pytest.raises(MagnetParseError):
            parse("")

    def test_whitespace_only(self):
        with pytest.raises(MagnetParseError):
            parse("   ")

    def test_wrong_scheme(self):
        with pytest.raises(MagnetParseError):
            parse(f"http://example.com/?xt=urn:btih:{INFO_HASH_HEX}")

    def test_no_query(self):
        with pytest.raises(MagnetParseError):
            parse("magnet:")

    def test_missing_xt(self):
        with pytest.raises(MagnetParseError):
            parse("magnet:?dn=no-hash-here")

    def test_invalid_hash_length(self):
        with pytest.raises(MagnetParseError):
            parse("magnet:?xt=urn:btih:deadbeef")

    def test_invalid_hash_characters(self):
        bad = "z" * 40
        with pytest.raises(MagnetParseError):
            parse(f"magnet:?xt=urn:btih:{bad}")

    def test_v2_only_link_rejected_with_clear_message(self):
        btmh = "1220" + "00" * 32  # multihash-style prefix + digest
        with pytest.raises(MagnetParseError, match="v2-only"):
            parse(f"magnet:?xt=urn:btmh:{btmh}")

    def test_unsupported_xt_scheme(self):
        with pytest.raises(MagnetParseError):
            parse("magnet:?xt=urn:sha1:somethingelse")


class TestHybridLinks:
    def test_prefers_v1_hash_when_both_present(self):
        btmh = "1220" + "00" * 32
        uri = f"magnet:?xt=urn:btih:{INFO_HASH_HEX}&xt=urn:btmh:{btmh}"
        link = parse(uri)
        assert link.info_hash == INFO_HASH_BYTES

    def test_prefers_v1_hash_regardless_of_order(self):
        btmh = "1220" + "00" * 32
        uri = f"magnet:?xt=urn:btmh:{btmh}&xt=urn:btih:{INFO_HASH_HEX}"
        link = parse(uri)
        assert link.info_hash == INFO_HASH_BYTES
