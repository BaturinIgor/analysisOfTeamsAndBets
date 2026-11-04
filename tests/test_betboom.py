import json
import struct

from bkanalyzer.bookmakers.betboom import Recorder
from bkanalyzer.bookmakers.protowire import decode, dump, strings
from bkanalyzer.cli import inspect_run


def varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def field_len(num, payload):
    return varint(num << 3 | 2) + varint(len(payload)) + payload


def field_varint(num, value):
    return varint(num << 3) + varint(value)


def field_double(num, value):
    return varint(num << 3 | 1) + struct.pack("<d", value)


def sample_event():
    outcome = field_len(1, "П1".encode()) + field_double(2, 1.85)
    event = field_varint(1, 123456) + field_len(2, "Team Spirit".encode()) + field_len(3, "Tundra".encode()) + field_len(4, outcome)
    return field_len(5, event)


def test_decode_nested_message():
    tree = decode(sample_event())
    assert tree[0][0] == 5
    event = tree[0][1]
    assert (1, 123456) in event and (2, "Team Spirit") in event
    outcome = dict(event)[4]
    assert outcome == [(1, "П1"), (2, 1.85)]
    assert strings(tree) == ["Team Spirit", "Tundra", "П1"]
    assert "Team Spirit" in dump(tree)


def test_recorder_keeps_binary_and_filters_other_hosts(tmp_path):
    rec = Recorder(tmp_path, clock=lambda: 0.0)
    assert rec.save("ws", "wss://ru-ws.sporthub.bet/api/tree/ws/v1", b"\x08\x01", "out")
    assert rec.save("http", "https://analytics.example/collect", '{"page": "x"}') is None
    assert rec.save("http", "https://cdn.example/api/odds", '{"odds": 1}')
    rec.write_index()
    index = json.loads((tmp_path / "index.json").read_text())
    assert [i["dir"] for i in index] == ["out", "in"]
    assert (tmp_path / index[0]["file"]).read_bytes() == b"\x08\x01"
    assert index[0]["file"].endswith(".bin")


def test_inspect_run(tmp_path):
    rec = Recorder(tmp_path, clock=lambda: 0.0)
    rec.save("ws", "wss://ru-ws.sporthub.bet/api/line/ws/v1?x=1", sample_event(), "in")
    rec.save("ws", "wss://ru-ws.sporthub.bet/api/line/ws/v1?x=1", field_varint(1, 7), "out")
    rec.write_index()
    report = inspect_run(tmp_path)
    assert "Записей: 2" in report
    assert "Team Spirit" in report and "Tundra" in report
