import struct

import pytest

from tinytorrent.daemon.peer_wire import messages as m


class TestEncodeDecodeRoundTrip:
    @pytest.mark.parametrize(
        "message",
        [
            m.KeepAlive(),
            m.Choke(),
            m.Unchoke(),
            m.Interested(),
            m.NotInterested(),
            m.Have(piece_index=7),
            m.Bitfield(bitfield=b"\xff\x00\xa3"),
            m.Request(index=1, begin=0, length=16384),
            m.Piece(index=1, begin=0, block=b"some block data"),
            m.Cancel(index=1, begin=0, length=16384),
            m.PortMessage(listen_port=6881),
            m.Extended(extended_message_id=1, payload=b"d1:ai1ee"),
        ],
    )
    def test_round_trip(self, message):
        frame = m.encode(message)
        # Frame is length-prefixed; strip the prefix before decoding, same
        # as the connection layer would after reading it off the wire.
        (length,) = struct.unpack("!I", frame[:4])
        assert length == len(frame) - 4
        decoded = m.decode(frame[4:])
        assert decoded == message


class TestEncodeFraming:
    def test_keep_alive_is_zero_length(self):
        assert m.encode(m.KeepAlive()) == b"\x00\x00\x00\x00"

    def test_choke_frame_bytes(self):
        assert m.encode(m.Choke()) == b"\x00\x00\x00\x01\x00"

    def test_have_frame_bytes(self):
        assert m.encode(m.Have(piece_index=1)) == b"\x00\x00\x00\x05\x04\x00\x00\x00\x01"

    def test_extended_frame_bytes(self):
        frame = m.encode(m.Extended(extended_message_id=3, payload=b"xyz"))
        assert frame == b"\x00\x00\x00\x05\x14\x03xyz"


class TestDecodeErrors:
    def test_choke_wrong_length(self):
        with pytest.raises(m.PeerProtocolError):
            m.decode(bytes([m.MessageId.CHOKE, 1, 2]))

    def test_have_wrong_length(self):
        with pytest.raises(m.PeerProtocolError):
            m.decode(bytes([m.MessageId.HAVE, 1, 2]))

    def test_request_wrong_length(self):
        with pytest.raises(m.PeerProtocolError):
            m.decode(bytes([m.MessageId.REQUEST]) + b"\x00" * 11)

    def test_piece_too_short(self):
        with pytest.raises(m.PeerProtocolError):
            m.decode(bytes([m.MessageId.PIECE]) + b"\x00" * 3)

    def test_extended_missing_id(self):
        with pytest.raises(m.PeerProtocolError):
            m.decode(bytes([m.MessageId.EXTENDED]))

    def test_unknown_message_id(self):
        with pytest.raises(m.PeerProtocolError):
            m.decode(bytes([99]))


class TestBitfieldAndPieceAllowAnyLength:
    def test_empty_bitfield(self):
        assert m.decode(bytes([m.MessageId.BITFIELD])) == m.Bitfield(b"")

    def test_zero_length_block(self):
        frame = bytes([m.MessageId.PIECE]) + struct.pack("!II", 0, 0)
        assert m.decode(frame) == m.Piece(index=0, begin=0, block=b"")
