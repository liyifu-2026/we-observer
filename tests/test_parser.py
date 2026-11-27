"""Tests for WeChat 4.x payload decoding, group prefix stripping, and XML parsing."""

from __future__ import annotations

import zstandard as zstd

from tests.fixtures import make_file_xml, make_link_xml, make_quote_xml
from wx_observer.parser import parse_message_row, strip_group_sender_prefix


def test_strip_group_prefix_preserves_normal_colons() -> None:
    assert strip_group_sender_prefix("wxid_abc123:\nhello world", "wxid_abc123") == "hello world"
    assert strip_group_sender_prefix("wxid_abc123:\r\n你好", "wxid_abc123") == "你好"
    # Normal text with colons and newlines must NOT be truncated
    assert strip_group_sender_prefix("Note:\nline two", None) == "Note:\nline two"
    assert strip_group_sender_prefix("https://example.com\nnext", None) == "https://example.com\nnext"


def test_zstd_and_container_blob_decoding() -> None:
    compressed = zstd.ZstdCompressor().compress("Long text with emoji 🚀 and 中文".encode("utf-8"))
    res = parse_message_row(local_type=1, message_content=compressed)
    assert res.msg_type == "text"
    assert res.text == "Long text with emoji 🚀 and 中文"

    # Binary header + UTF-8 + \x01\x00 tail padding fallback
    padded = (b"\x00" * 10) + "Fallback UTF-8 text".encode("utf-8") + b"\x01\x00\xff\xfe"
    res2 = parse_message_row(local_type=1, message_content=padded)
    assert res2.msg_type == "text"
    assert res2.text == "Fallback UTF-8 text"


def test_appmsg_quote_nested_file_link_and_malformed_xml() -> None:
    nested_inner = make_quote_xml(
        "Earlier reply",
        ref_type=1,
        ref_content="Original root",
        display_name="RootUser",
        from_usr="wxid_root",
    )
    outer = make_quote_xml(
        "Replying to reply",
        ref_type=49,
        ref_content=nested_inner,
        display_name="MidUser",
        from_usr="wxid_mid",
        svrid="7777",
    )
    parsed = parse_message_row(
        local_type=49,
        message_content=f"wxid_sender:\n{outer}",
        sender_id="wxid_sender",
        is_group=True,
    )
    assert parsed.msg_type == "text"
    assert parsed.text == "Replying to reply"
    assert parsed.quote is not None
    assert parsed.quote.quoted_type == "text"
    assert parsed.quote.excerpt == "Earlier reply"
    assert parsed.quote.server_id == "7777"

    file_msg = parse_message_row(
        local_type=49,
        message_content=make_file_xml("report.docx", 98765, "docx"),
    )
    assert file_msg.msg_type == "file"
    assert file_msg.metadata["filename"] == "report.docx"
    assert file_msg.metadata["size_bytes"] == 98765

    link_msg = parse_message_row(
        local_type=49,
        message_content=make_link_xml("Release Notes", "Details", "https://example.com/r"),
    )
    assert link_msg.msg_type == "link"
    assert link_msg.metadata["url"] == "https://example.com/r"

    # Malformed XML must never raise
    bad_xml = parse_message_row(local_type=49, message_content="<msg><appmsg><unclosed>")
    assert bad_xml.msg_type == "card"
    assert bad_xml.text == "[卡片/应用消息]"


def test_non_text_metadata_and_64bit_red_packet() -> None:
    img = parse_message_row(
        local_type=3,
        message_content='<msg><img md5="aabbccddeeff00112233445566778899" length="12345"/></msg>',
    )
    assert img.msg_type == "image"
    assert img.metadata["md5"] == "aabbccddeeff00112233445566778899"

    voice = parse_message_row(
        local_type=34,
        message_content='<msg><voicemsg voicelength="4500"/></msg>',
    )
    assert voice.msg_type == "voice"
    assert voice.metadata["duration_ms"] == 4500

    loc = parse_message_row(
        local_type=48,
        message_content='<msg><location x="31.23" y="121.47" poiname="Shanghai Tower"/></msg>',
    )
    assert loc.msg_type == "location"
    assert "Shanghai Tower" in loc.text

    # 0x7D100000031 low 32 bits == 49, must still classify as red_packet rather than card
    rp = parse_message_row(local_type=8594229559345, message_content="")
    assert rp.msg_type == "red_packet"
