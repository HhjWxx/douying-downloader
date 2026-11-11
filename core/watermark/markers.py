"""仅按已知 UUID 和内容清理 SEI；不重写 MP4 sample table。"""

import re

ENCODER_UUID = bytes.fromhex("dc45e9bde6d948b7962cd820d923eeef")


def unescape(data):
    result = bytearray()
    zeros = 0
    for i, value in enumerate(data):
        if zeros >= 2 and value == 3 and i + 1 < len(data) and data[i + 1] <= 3:
            zeros = 0
            continue
        result.append(value)
        zeros = zeros + 1 if value == 0 else 0
    return bytes(result)


def escape(data):
    result = bytearray()
    zeros = 0
    for value in data:
        if zeros >= 2 and value <= 3:
            result.append(3)
            zeros = 0
        result.append(value)
        zeros = zeros + 1 if value == 0 else 0
    return bytes(result)


def _number(value):
    return b"\xff" * (value // 255) + bytes([value % 255])


def known_payload(codec, payload):
    if codec not in ("h264", "hevc") or payload[:16] != ENCODER_UUID:
        return False
    text = payload[16:]
    return bool(re.match(rb"(?:bvc0ot v\d+\.\d+|x264 - core \d+)", text))


def clean_nal(nal, codec):
    header = 1 if codec == "h264" else 2
    kind = (nal[0] & 31) if codec == "h264" else ((nal[0] >> 1) & 63)
    if kind not in ((6,) if codec == "h264" else (39, 40)):
        return nal, []
    rbsp = unescape(nal[header:])
    pos, messages, removed = 0, [], []
    try:
        while pos < len(rbsp):
            if rbsp[pos:] == b"\x80":
                break
            values = []
            for _ in range(2):
                number = 0
                while rbsp[pos] == 255:
                    number += 255
                    pos += 1
                number += rbsp[pos]
                pos += 1
                values.append(number)
            kind, size = values
            if pos + size > len(rbsp):
                raise ValueError("SEI 长度越界")
            payload = rbsp[pos : pos + size]
            pos += size
            if kind == 5 and known_payload(codec, payload):
                removed.append(
                    {
                        "codec": codec,
                        "uuid": payload[:16].hex(),
                        "payload": payload[16:].decode("utf-8", "replace"),
                    }
                )
            else:
                messages.append(_number(kind) + _number(size) + payload)
        if pos >= len(rbsp) or rbsp[pos:] != b"\x80":
            raise ValueError("SEI 缺少 trailing bits")
    except (IndexError, ValueError):
        # 无法完整解释的消息原样保留，绝不部分擦除。
        return nal, []
    if not removed:
        return nal, []
    if not messages:
        return b"", removed
    return nal[:header] + escape(b"".join(messages) + b"\x80"), removed


def nal_length_size(codec, extradata):
    if codec == "h264" and len(extradata) >= 5 and extradata[0] == 1:
        return (extradata[4] & 3) + 1
    if codec == "hevc" and len(extradata) >= 22 and extradata[0] == 1:
        return (extradata[21] & 3) + 1
    return None


def clean_extradata(codec, data):
    """HEVC 配置记录也允许携带 prefix/suffix SEI 数组。"""
    if codec != "hevc" or len(data) < 23 or data[0] != 1:
        return data, []
    arrays, removed, pos = [], [], 23
    try:
        for _ in range(data[22]):
            header = data[pos : pos + 1]
            count = int.from_bytes(data[pos + 1 : pos + 3], "big")
            pos += 3
            entries = []
            for _ in range(count):
                size = int.from_bytes(data[pos : pos + 2], "big")
                pos += 2
                if not size or pos + size > len(data):
                    raise ValueError("hvcC 长度越界")
                cleaned, matches = clean_nal(data[pos : pos + size], codec)
                removed.extend(matches)
                if cleaned:
                    entries.append(len(cleaned).to_bytes(2, "big") + cleaned)
                pos += size
            if entries:
                arrays.append(header + len(entries).to_bytes(2, "big") + b"".join(entries))
        if pos != len(data):
            raise ValueError("hvcC 尾部无法解释")
    except (IndexError, ValueError):
        return data, []
    if not removed:
        return data, []
    return data[:22] + bytes([len(arrays)]) + b"".join(arrays), removed


def clean_packet(data, codec, length_size):
    if codec not in ("h264", "hevc"):
        return data, []
    parts = []
    if length_size:
        pos = 0
        while pos < len(data):
            size = int.from_bytes(data[pos : pos + length_size], "big")
            pos += length_size
            if size < 1 or pos + size > len(data):
                raise ValueError("NAL 长度越界")
            parts.append((data[pos : pos + size], None))
            pos += size
    else:
        starts = list(re.finditer(b"\x00\x00(?:\x00)?\x01", data))
        if not starts or starts[0].start() != 0:
            raise ValueError("不支持的 NAL framing")
        for i, start in enumerate(starts):
            end = starts[i + 1].start() if i + 1 < len(starts) else len(data)
            parts.append((data[start.end() : end], start.group()))
    output, removed = bytearray(), []
    for nal, prefix in parts:
        if not nal:
            raise ValueError("空 NAL")
        clean, matches = clean_nal(nal, codec)
        removed.extend(matches)
        if clean:
            output.extend(len(clean).to_bytes(length_size, "big") if length_size else prefix)
            output.extend(clean)
    return bytes(output), removed


def clean_metadata(metadata):
    cleaned, removed = {}, []
    for key, value in metadata.items():
        if (key.lower() == "comment" and re.fullmatch(r"vid:[\w-]+", value.strip())) or (
            key.lower() == "encoder" and re.match(r"(?:Lavf|Lavc|x264|bvc0ot)", value)
        ):
            removed.append({"key": key, "value": value})
        else:
            cleaned[key] = value
    return cleaned, removed
