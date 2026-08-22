import pytest

from tinytorrent.daemon.bencode import encode as bencode_encode
from tinytorrent.daemon.peer_wire.extension import (
    EXTENDED_HANDSHAKE_ID,
    ExtendedHandshake,
    build_extended_handshake,
    parse_extended_handshake,
)
from tinytorrent.daemon.peer_wire.messages import Extended, PeerProtocolError


class TestBuildExtendedHandshake:
    def test_has_extended_handshake_id(self):
        msg = build_extended_handshake(supported_extensions={"ut_metadata": 1})
        assert msg.extended_message_id == EXTENDED_HANDSHAKE_ID

    def test_payload_round_trips(self):
        msg = build_extended_handshake(supported_extensions={"ut_metadata": 1}, metadata_size=4096)
        parsed = parse_extended_handshake(msg)
        assert parsed.supported_extensions == {"ut_metadata": 1}
        assert parsed.metadata_size == 4096
        assert parsed.client_version == "TinyTorrent/0.1"

    def test_metadata_size_omitted_when_unknown(self):
        msg = build_extended_handshake(supported_extensions={"ut_metadata": 1})
        parsed = parse_extended_handshake(msg)
        assert parsed.metadata_size is None


class TestParseExtendedHandshake:
    def test_wrong_message_id_rejected(self):
        with pytest.raises(PeerProtocolError):
            parse_extended_handshake(Extended(extended_message_id=1, payload=b"de"))

    def test_non_dict_payload_rejected(self):
        payload = bencode_encode([1, 2, 3])
        with pytest.raises(PeerProtocolError):
            parse_extended_handshake(Extended(extended_message_id=0, payload=payload))

    def test_malformed_bencode_rejected(self):
        with pytest.raises(PeerProtocolError):
            parse_extended_handshake(Extended(extended_message_id=0, payload=b"not bencode"))

    def test_missing_m_defaults_to_empty(self):
        payload = bencode_encode({"v": "SomeClient/1.0"})
        parsed = parse_extended_handshake(Extended(extended_message_id=0, payload=payload))
        assert parsed.supported_extensions == {}
        assert parsed.client_version == "SomeClient/1.0"

    def test_ignores_non_int_extension_ids(self):
        payload = bencode_encode({"m": {"ut_metadata": 1, "weird": "not-an-int"}})
        parsed = parse_extended_handshake(Extended(extended_message_id=0, payload=payload))
        assert parsed.supported_extensions == {"ut_metadata": 1}

    def test_default_dataclass_values(self):
        assert ExtendedHandshake() == ExtendedHandshake(
            supported_extensions={}, metadata_size=None, client_version=None
        )
