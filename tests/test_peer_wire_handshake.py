import pytest

from tinytorrent.daemon.peer_wire import handshake as h
from tinytorrent.daemon.peer_wire.messages import PeerProtocolError

INFO_HASH = bytes(range(20))
PEER_ID = bytes(range(20, 40))


class TestBuildHandshake:
    def test_length(self):
        data = h.build_handshake(INFO_HASH, PEER_ID)
        assert len(data) == h.HANDSHAKE_LENGTH == 68

    def test_structure(self):
        data = h.build_handshake(INFO_HASH, PEER_ID)
        assert data[0] == 19
        assert data[1:20] == b"BitTorrent protocol"
        assert data[28:48] == INFO_HASH
        assert data[48:68] == PEER_ID

    def test_extension_bit_set_by_default(self):
        data = h.build_handshake(INFO_HASH, PEER_ID)
        reserved = data[20:28]
        assert reserved[5] & 0x10

    def test_extension_bit_can_be_disabled(self):
        data = h.build_handshake(INFO_HASH, PEER_ID, extension_protocol=False)
        reserved = data[20:28]
        assert reserved[5] & 0x10 == 0

    def test_rejects_bad_info_hash_length(self):
        with pytest.raises(PeerProtocolError):
            h.build_handshake(b"short", PEER_ID)

    def test_rejects_bad_peer_id_length(self):
        with pytest.raises(PeerProtocolError):
            h.build_handshake(INFO_HASH, b"short")


class TestParseHandshake:
    def test_round_trip(self):
        data = h.build_handshake(INFO_HASH, PEER_ID)
        parsed = h.parse_handshake(data)
        assert parsed.info_hash == INFO_HASH
        assert parsed.peer_id == PEER_ID
        assert parsed.supports_extension_protocol is True

    def test_round_trip_no_extension(self):
        data = h.build_handshake(INFO_HASH, PEER_ID, extension_protocol=False)
        parsed = h.parse_handshake(data)
        assert parsed.supports_extension_protocol is False

    def test_wrong_length(self):
        with pytest.raises(PeerProtocolError):
            h.parse_handshake(b"too short")

    def test_wrong_protocol_name_length_byte(self):
        data = bytearray(h.build_handshake(INFO_HASH, PEER_ID))
        data[0] = 5
        with pytest.raises(PeerProtocolError):
            h.parse_handshake(bytes(data))

    def test_wrong_protocol_name(self):
        data = bytearray(h.build_handshake(INFO_HASH, PEER_ID))
        data[1:20] = b"NotBitTorrent proto"
        with pytest.raises(PeerProtocolError):
            h.parse_handshake(bytes(data))
