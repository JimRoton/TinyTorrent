import pytest

from tinytorrent.common.ipc_protocol import IPCError, Request, Response


class TestRequest:
    def test_round_trip(self):
        req = Request(cmd="add", args={"magnet": "magnet:?xt=urn:btih:aa"})
        assert Request.from_line(req.to_line()) == req

    def test_round_trip_no_args(self):
        req = Request(cmd="list")
        parsed = Request.from_line(req.to_line())
        assert parsed.cmd == "list"
        assert parsed.args == {}

    def test_line_ends_with_newline(self):
        assert Request(cmd="list").to_line().endswith(b"\n")

    def test_malformed_json(self):
        with pytest.raises(IPCError):
            Request.from_line(b"not json\n")

    def test_missing_cmd(self):
        with pytest.raises(IPCError):
            Request.from_line(b'{"args": {}}\n')

    def test_cmd_not_a_string(self):
        with pytest.raises(IPCError):
            Request.from_line(b'{"cmd": 5}\n')

    def test_args_not_a_dict(self):
        with pytest.raises(IPCError):
            Request.from_line(b'{"cmd": "list", "args": [1,2]}\n')

    def test_not_a_json_object(self):
        with pytest.raises(IPCError):
            Request.from_line(b"[1, 2, 3]\n")


class TestResponse:
    def test_success_round_trip(self):
        resp = Response.success({"id": "abcd"})
        parsed = Response.from_line(resp.to_line())
        assert parsed.ok is True
        assert parsed.data == {"id": "abcd"}

    def test_success_default_empty_data(self):
        resp = Response.success()
        parsed = Response.from_line(resp.to_line())
        assert parsed.data == {}

    def test_failure_round_trip(self):
        resp = Response.failure("bad things happened")
        parsed = Response.from_line(resp.to_line())
        assert parsed.ok is False
        assert parsed.error == "bad things happened"

    def test_failure_line_omits_data_key(self):
        line = Response.failure("oops").to_line()
        assert b'"data"' not in line

    def test_success_line_omits_error_key(self):
        line = Response.success({"x": 1}).to_line()
        assert b'"error"' not in line

    def test_malformed_json(self):
        with pytest.raises(IPCError):
            Response.from_line(b"garbage\n")

    def test_missing_ok_field(self):
        with pytest.raises(IPCError):
            Response.from_line(b'{"data": {}}\n')
