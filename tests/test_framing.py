import pytest

from victron_trends.framing import (KEEPALIVE, Incomplete, Message, ReplyStream, decode_item, encode_get, encode_set,
                                    parse_messages)

BLOCK_REPLY = bytes.fromhex("0803" "19ec5b" "4c") + bytes(12)  # VALUE 0xEC5B with a 12-byte value


def test_decode_items():
    assert decode_item(bytes.fromhex("17"), 0) == (23, 1)
    assert decode_item(bytes.fromhex("19ec5b"), 0) == (0xEC5B, 3)
    assert decode_item(bytes.fromhex("24"), 0) == (-5, 1)
    assert decode_item(bytes.fromhex("43010203"), 0) == (b"\x01\x02\x03", 4)
    assert decode_item(bytes.fromhex("8219ec5b4101"), 0) == ([0xEC5B, b"\x01"], 6)


def test_truncated_item():
    with pytest.raises(Incomplete):
        decode_item(bytes.fromhex("19ec"), 0)
    with pytest.raises(Incomplete):
        decode_item(bytes.fromhex("4301"), 0)


def test_requests_match_the_app():
    assert encode_get(0xEC5A) == bytes.fromhex("05038119ec5a")
    assert encode_set(0xEC63, bytes.fromhex("10203040")) == bytes.fromhex("06038219ec634410203040")
    assert KEEPALIVE == bytes.fromhex("0600821893421027")


def test_parse_messages_skips_noise():
    messages, skipped = parse_messages(b"\xf9\x01" + BLOCK_REPLY + encode_get(0xEC5D))
    assert messages == [Message("VALUE", 3, 0xEC5B, bytes(12)), Message("GET", 3, [0xEC5D], None)]
    assert skipped == 2


def test_reply_stream_across_notifications():
    stream = ReplyStream()
    assert stream.feed(BLOCK_REPLY[:7]) == []
    assert stream.feed(BLOCK_REPLY[7:] + bytes.fromhex("0903" "19ec5b" "03")) == [
        Message("VALUE", 3, 0xEC5B, bytes(12)),
        Message("STATUS", 3, 0xEC5B, 3),
    ]
    assert stream.buf == bytearray()
