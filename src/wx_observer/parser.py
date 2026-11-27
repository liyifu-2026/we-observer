"""Clean-room WeChat 4.x message payload decoder and structured normalizer."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any
from xml.etree import ElementTree

from wx_observer.models import QuoteInfo

ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
MAX_XML_CHARS = 1_048_576
MAX_EXCERPT_CHARS = 240
MAX_TEXT_CHARS = 16_000

_GROUP_XML_PREFIX_RE = re.compile(r"^[A-Za-z0-9_.-]{2,128}:\r?\n(?=\s*<)")
_GROUP_WXID_PREFIX_RE = re.compile(r"^(?:wxid_|gh_)[A-Za-z0-9_.-]{2,120}:\r?\n")
_RAW_XML_RE = re.compile(
    r"^\s*<(?:msg|appmsg|sysmsg|emoji|img|voip|location|videomsg|voicemsg)\b",
    re.I,
)
_CTRL_KEEP_NEWLINES_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]+")
_MD5_ATTR_RE = re.compile(r'md5="([0-9a-fA-F]{32})"')
_MD5_HEX_RE = re.compile(r"^[0-9a-fA-F]{32}$")

_ZSTD_MODULE: Any = None


@dataclass(slots=True)
class ParsedMessage:
    msg_type: str
    raw_type: int
    text: str
    quote: QuoteInfo | None
    metadata: dict[str, Any]


def _get_zstd():
    global _ZSTD_MODULE
    if _ZSTD_MODULE is not None:
        return _ZSTD_MODULE or None
    for name in ("zstandard", "zstd"):
        try:
            _ZSTD_MODULE = __import__(name)
            return _ZSTD_MODULE
        except ImportError:
            continue
    _ZSTD_MODULE = False
    return None


def _looks_readable(text: str) -> bool:
    if not text:
        return False
    printable = 0
    bad = 0
    for ch in text:
        if ch in "\n\r\t":
            printable += 1
            continue
        if unicodedata.category(ch) in ("Cc", "Co", "Cs", "Cn"):
            bad += 1
        else:
            printable += 1
    total = len(text)
    return (printable / total) >= 0.7 and (bad / total) <= 0.1


def _extract_text_from_blob(blob: bytes) -> str:
    if not blob:
        return ""
    offsets = [10] + [off for off in range(min(16, len(blob))) if off != 10]
    for off in offsets:
        if off >= len(blob):
            continue
        segment = blob[off:]
        if b"\x01\x00" in segment:
            segment = segment.split(b"\x01\x00", 1)[0]
        try:
            text = segment.decode("utf-8")
        except UnicodeDecodeError:
            continue
        text = _CTRL_KEEP_NEWLINES_RE.sub("", text).strip()
        if text and _looks_readable(text):
            return text
    return ""


def decode_payload(raw_value: str | bytes | None) -> str:
    """Decode raw message_content or compress_content (plain UTF-8, zstd, or binary wrapper)."""
    if raw_value is None:
        return ""
    if isinstance(raw_value, str):
        if "\x01" in raw_value:
            raw_value = raw_value.split("\x01", 1)[0]
        return _CTRL_KEEP_NEWLINES_RE.sub("", raw_value).strip()

    if not raw_value:
        return ""

    if raw_value[:4] == ZSTD_MAGIC:
        zstd = _get_zstd()
        if zstd is not None:
            try:
                decompressed = zstd.ZstdDecompressor().decompress(
                    raw_value, max_output_size=262_144
                )
                text = decompressed.decode("utf-8", errors="ignore").strip()
                if text:
                    return _CTRL_KEEP_NEWLINES_RE.sub("", text).strip()
            except Exception:
                pass
        extracted = _extract_text_from_blob(raw_value)
        if extracted:
            return extracted
        return ""

    try:
        text = raw_value.decode("utf-8")
        if b"\x01\x00" in raw_value:
            text = text.split("\x01", 1)[0]
        text = _CTRL_KEEP_NEWLINES_RE.sub("", text).strip()
        return text
    except UnicodeDecodeError:
        return _extract_text_from_blob(raw_value)


def strip_group_sender_prefix(text: str, sender_id: str | None = None) -> str:
    """Strip WeChat's `<wxid>:\\n` prefix on group messages without touching normal colons."""
    cleaned = str(text or "").strip()
    if not cleaned:
        return ""
    if sender_id:
        for sep in ("\n", "\r\n"):
            prefix = f"{sender_id}:{sep}"
            if cleaned.startswith(prefix):
                return cleaned[len(prefix) :].strip()
    for pat in (_GROUP_XML_PREFIX_RE, _GROUP_WXID_PREFIX_RE):
        match = pat.match(cleaned)
        if match:
            rest = cleaned[match.end() :].strip()
            if rest:
                return rest
    return cleaned


def _clean_single_line(text: str, limit: int = MAX_EXCERPT_CHARS) -> str:
    compact = re.sub(r"\s+", " ", str(text or "")).strip()
    if not compact or _RAW_XML_RE.match(compact):
        return ""
    return compact[:limit]


def _safe_parse_xml(body: str) -> ElementTree.Element | None:
    if not body or len(body) > MAX_XML_CHARS:
        return None
    stripped = body.lstrip()
    if not stripped.startswith("<"):
        return None
    try:
        return ElementTree.fromstring(stripped)
    except (ElementTree.ParseError, ValueError):
        return None


def _parse_refermsg(refer: ElementTree.Element | None, depth: int) -> QuoteInfo | None:
    if refer is None:
        return None
    ref_type = (refer.findtext("type") or "").strip()
    svrid = (refer.findtext("svrid") or "").strip()[:80] or None
    display_name = _clean_single_line(refer.findtext("displayname") or "", 80) or None
    from_user = (refer.findtext("chatusr") or refer.findtext("fromusr") or "").strip()[:128] or None
    raw_content = (refer.findtext("content") or "").strip()
    if not raw_content:
        content_el = refer.find("content")
        if content_el is not None and len(content_el) > 0:
            raw_content = ElementTree.tostring(content_el[0], encoding="unicode").strip()

    quoted_type = "other"
    excerpt = "[消息]"
    if ref_type == "1":
        quoted_type = "text"
        body = strip_group_sender_prefix(raw_content, from_user)
        excerpt = _clean_single_line(body) or "[文字消息]"
    elif ref_type == "3":
        quoted_type = "image"
        excerpt = "[图片]"
    elif ref_type == "34":
        quoted_type = "voice"
        excerpt = "[语音]"
    elif ref_type == "43":
        quoted_type = "video"
        excerpt = "[视频]"
    elif ref_type in ("47", "11000"):
        quoted_type = "emoji"
        excerpt = "[动画表情]"
    elif ref_type == "48":
        quoted_type = "location"
        excerpt = "[位置]"
    elif ref_type == "49":
        nested = _parse_appmsg(raw_content, sender_id=from_user, depth=depth + 1) if depth < 1 else None
        if nested is not None:
            if nested.msg_type == "file":
                quoted_type = "file"
                fname = nested.metadata.get("filename", "")
                excerpt = f"[文件: {fname}]" if fname else "[文件]"
            elif nested.quote is not None or nested.msg_type == "text":
                quoted_type = "text"
                excerpt = _clean_single_line(nested.text) or "[引用消息]"
            else:
                quoted_type = nested.msg_type
                excerpt = _clean_single_line(nested.text) or "[卡片/链接]"
        else:
            quoted_type = "card"
            excerpt = "[卡片/链接]"

    return QuoteInfo(
        quoted_type=quoted_type,
        excerpt=excerpt,
        sender_id=from_user,
        sender_name=display_name,
        server_id=svrid,
    )


def _parse_appmsg(
    body: str,
    *,
    sender_id: str | None = None,
    depth: int = 0,
    raw_type: int = 49,
) -> ParsedMessage | None:
    stripped = strip_group_sender_prefix(body, sender_id)
    root = _safe_parse_xml(stripped)
    if root is None:
        return None
    appmsg = root if root.tag == "appmsg" else root.find("appmsg")
    if appmsg is None:
        return None

    app_type_str = (appmsg.findtext("type") or "").strip()
    try:
        app_type = int(app_type_str) if app_type_str else 0
    except ValueError:
        app_type = 0

    title = _clean_single_line(appmsg.findtext("title") or "", 240)
    desc = _clean_single_line(appmsg.findtext("des") or "", 400)
    url = (appmsg.findtext("url") or "").strip()[:1024]
    source_name = _clean_single_line(
        appmsg.findtext("sourcedisplayname") or root.findtext("appinfo/appname") or "",
        120,
    )

    if app_type == 57:
        reply_text = (appmsg.findtext("title") or "").strip()[:MAX_TEXT_CHARS]
        quote = _parse_refermsg(appmsg.find("refermsg"), depth)
        return ParsedMessage(
            msg_type="text",
            raw_type=raw_type,
            text=reply_text,
            quote=quote,
            metadata={"app_type": 57},
        )

    if app_type == 6:
        size_raw = (appmsg.findtext("appattach/totallen") or "").strip()
        ext = (appmsg.findtext("appattach/fileext") or "").strip()[:32]
        md5_raw = (
            appmsg.findtext("md5")
            or appmsg.findtext("appattach/md5")
            or ""
        ).strip()
        meta: dict[str, Any] = {"app_type": 6}
        if title:
            meta["filename"] = title
        if size_raw.isdigit():
            meta["size_bytes"] = int(size_raw)
        if ext:
            meta["file_ext"] = ext
        if _MD5_HEX_RE.match(md5_raw):
            meta["md5"] = md5_raw.lower()
        return ParsedMessage(
            msg_type="file",
            raw_type=raw_type,
            text=f"[文件: {title}]" if title else "[文件]",
            quote=None,
            metadata=meta,
        )

    if app_type == 5:
        meta = {"app_type": 5}
        if title:
            meta["title"] = title
        if desc:
            meta["description"] = desc
        if url:
            meta["url"] = url
        if source_name:
            meta["source"] = source_name
        return ParsedMessage(
            msg_type="link",
            raw_type=raw_type,
            text=f"[链接: {title}]" if title else "[链接]",
            quote=None,
            metadata=meta,
        )

    if app_type in (33, 36):
        meta = {"app_type": app_type}
        if title:
            meta["title"] = title
        if source_name:
            meta["source"] = source_name
        label = source_name or title
        return ParsedMessage(
            msg_type="miniapp",
            raw_type=raw_type,
            text=f"[小程序: {label}]" if label else "[小程序]",
            quote=None,
            metadata=meta,
        )

    if app_type == 19:
        meta = {"app_type": 19}
        if title:
            meta["title"] = title
        embedded_files = _extract_recorditem_files(appmsg)
        if embedded_files:
            meta["embedded_files"] = embedded_files
        return ParsedMessage(
            msg_type="chat_history",
            raw_type=raw_type,
            text=f"[聊天记录: {title}]" if title else "[聊天记录]",
            quote=None,
            metadata=meta,
        )

    if app_type == 2000:
        fee = _clean_single_line(appmsg.findtext("wcpayinfo/feedesc") or "", 64)
        meta = {"app_type": 2000}
        if fee:
            meta["amount"] = fee
        return ParsedMessage(
            msg_type="transfer",
            raw_type=raw_type,
            text=f"[转账: {fee}]" if fee else "[转账]",
            quote=None,
            metadata=meta,
        )

    if app_type == 2001:
        greeting = _clean_single_line(appmsg.findtext("wcpayinfo/sendertitle") or title, 120)
        meta = {"app_type": 2001}
        if greeting:
            meta["greeting"] = greeting
        return ParsedMessage(
            msg_type="red_packet",
            raw_type=raw_type,
            text=f"[红包: {greeting}]" if greeting else "[红包]",
            quote=None,
            metadata=meta,
        )

    meta = {"app_type": app_type} if app_type else {}
    if title:
        meta["title"] = title
    if url:
        meta["url"] = url
    return ParsedMessage(
        msg_type="card",
        raw_type=raw_type,
        text=f"[卡片: {title}]" if title else "[卡片/应用消息]",
        quote=None,
        metadata=meta,
    )


def _extract_recorditem_files(appmsg: ElementTree.Element) -> list[dict[str, Any]]:
    rec_el = appmsg.find("recorditem")
    if rec_el is None:
        return []
    rec_raw = (rec_el.text or "").strip()
    if not rec_raw and len(rec_el) > 0:
        rec_raw = ElementTree.tostring(rec_el[0], encoding="unicode").strip()
    rec_root = _safe_parse_xml(rec_raw)
    if rec_root is None:
        return []
    files: list[dict[str, Any]] = []
    for item in rec_root.findall(".//dataitem"):
        if (item.attrib.get("datatype") or "").strip() != "8":
            continue
        fname = _clean_single_line(item.findtext("datatitle") or "", 240)
        if not fname:
            continue
        entry: dict[str, Any] = {"filename": fname}
        size_raw = (item.findtext("datasize") or "").strip()
        if size_raw.isdigit():
            entry["size_bytes"] = int(size_raw)
        md5_raw = (item.findtext("fullmd5") or "").strip()
        if _MD5_HEX_RE.match(md5_raw):
            entry["md5"] = md5_raw.lower()
        fmt = (item.findtext("datafmt") or "").strip()[:32]
        if fmt:
            entry["file_ext"] = fmt
        files.append(entry)
    return files


def _parse_system_text(body: str) -> str:
    root = _safe_parse_xml(body)
    if root is not None:
        replace = root.findtext(".//replacemsg")
        if replace:
            return _clean_single_line(replace, MAX_TEXT_CHARS) or "[系统消息]"
        txt = "".join(root.itertext())
        cleaned = _clean_single_line(txt, MAX_TEXT_CHARS)
        return cleaned or "[系统消息]"
    return _clean_single_line(body, MAX_TEXT_CHARS) or "[系统消息]"


def parse_message_row(
    *,
    local_type: int,
    message_content: str | bytes | None,
    compress_content: str | bytes | None = None,
    sender_id: str | None = None,
    is_group: bool = False,
) -> ParsedMessage:
    """Normalize a raw WeChat 4.x database message row into structured fields."""
    raw_type = int(local_type or 0)
    base_type = raw_type & 0xFFFFFFFF

    if raw_type == 8594229559345:
        return ParsedMessage(
            msg_type="red_packet",
            raw_type=raw_type,
            text="[红包]",
            quote=None,
            metadata={},
        )

    if base_type in (47, 11000):
        return ParsedMessage(
            msg_type="emoji",
            raw_type=raw_type,
            text="[动画表情]",
            quote=None,
            metadata={},
        )

    if base_type == 50:
        return ParsedMessage(
            msg_type="voip",
            raw_type=raw_type,
            text="[音视频通话]",
            quote=None,
            metadata={},
        )

    primary = decode_payload(message_content)
    secondary = decode_payload(compress_content) if not primary and compress_content else ""
    body = primary or secondary
    if is_group and body:
        body = strip_group_sender_prefix(body, sender_id)

    if base_type == 1:
        if not body and compress_content:
            body = decode_payload(compress_content)
            if is_group and body:
                body = strip_group_sender_prefix(body, sender_id)
        return ParsedMessage(
            msg_type="text",
            raw_type=raw_type,
            text=body[:MAX_TEXT_CHARS],
            quote=None,
            metadata={},
        )

    if base_type == 3:
        meta: dict[str, Any] = {}
        md5_match = _MD5_ATTR_RE.search(body)
        if md5_match:
            meta["md5"] = md5_match.group(1).lower()
        root = _safe_parse_xml(body)
        if root is not None:
            img = root if root.tag == "img" else root.find("img")
            if img is not None:
                if img.attrib.get("md5"):
                    meta["md5"] = img.attrib["md5"].lower()
                if img.attrib.get("length", "").isdigit():
                    meta["size_bytes"] = int(img.attrib["length"])
        return ParsedMessage(
            msg_type="image",
            raw_type=raw_type,
            text="[图片]",
            quote=None,
            metadata=meta,
        )

    if base_type == 34:
        meta = {}
        root = _safe_parse_xml(body)
        if root is not None:
            voicemsg = root if root.tag == "voicemsg" else root.find("voicemsg")
            if voicemsg is not None and voicemsg.attrib.get("voicelength", "").isdigit():
                meta["duration_ms"] = int(voicemsg.attrib["voicelength"])
        return ParsedMessage(
            msg_type="voice",
            raw_type=raw_type,
            text="[语音]",
            quote=None,
            metadata=meta,
        )

    if base_type == 43:
        meta = {}
        root = _safe_parse_xml(body)
        if root is not None:
            videomsg = root if root.tag == "videomsg" else root.find("videomsg")
            if videomsg is not None:
                if videomsg.attrib.get("playlength", "").isdigit():
                    meta["duration_sec"] = int(videomsg.attrib["playlength"])
                if videomsg.attrib.get("length", "").isdigit():
                    meta["size_bytes"] = int(videomsg.attrib["length"])
        return ParsedMessage(
            msg_type="video",
            raw_type=raw_type,
            text="[视频]",
            quote=None,
            metadata=meta,
        )

    if base_type == 48:
        meta = {}
        label = ""
        root = _safe_parse_xml(body)
        if root is not None:
            loc = root if root.tag == "location" else root.find("location")
            if loc is not None:
                label = _clean_single_line(
                    loc.attrib.get("poiname") or loc.attrib.get("label") or "", 160
                )
                if label:
                    meta["label"] = label
                if loc.attrib.get("x"):
                    meta["lat"] = loc.attrib["x"]
                if loc.attrib.get("y"):
                    meta["lng"] = loc.attrib["y"]
        return ParsedMessage(
            msg_type="location",
            raw_type=raw_type,
            text=f"[位置: {label}]" if label else "[位置]",
            quote=None,
            metadata=meta,
        )

    if base_type == 49:
        xml_source = body
        if not xml_source.lstrip().startswith("<") and compress_content:
            alt = decode_payload(compress_content)
            if is_group and alt:
                alt = strip_group_sender_prefix(alt, sender_id)
            if alt.lstrip().startswith("<"):
                xml_source = alt
        parsed_app = _parse_appmsg(xml_source, sender_id=sender_id, raw_type=raw_type)
        if parsed_app is not None:
            return parsed_app
        return ParsedMessage(
            msg_type="card",
            raw_type=raw_type,
            text="[卡片/应用消息]",
            quote=None,
            metadata={},
        )

    if base_type == 10000:
        return ParsedMessage(
            msg_type="system",
            raw_type=raw_type,
            text=_parse_system_text(body),
            quote=None,
            metadata={},
        )

    return ParsedMessage(
        msg_type="other",
        raw_type=raw_type,
        text=f"[消息 type={raw_type}]",
        quote=None,
        metadata={},
    )
