"""PyAV 流式媒体处理：复制音频、保留时间戳、封装后完整解码校验。"""

import hashlib

import av

from core.watermark.markers import clean_extradata, clean_metadata, clean_packet, nal_length_size
from core.watermark.progress import MediaProgress


def raw_timing_options(container):
    # edit list 是 MOV/MP4 解封装选项；传给 FLV 会触发部分 Windows
    # FFmpeg 构建的原生崩溃。按实际容器选择，不依据文件扩展名猜测。
    if "mov" in container.format.name.split(","):
        return {"ignore_editlist": "1"}
    return {}


def probe(path):
    with av.open(str(path)) as container:
        videos = list(container.streams.video)
        if len(videos) != 1:
            raise ValueError("仅支持单视频轨道文件")
        stream = videos[0]
        context = stream.codec_context
        format_name = context.format.name if context.format else "unknown"
        first_frame = next(container.decode(stream), None)
        rotation = first_frame.rotation if first_frame is not None else 0
        hdr = context.color_trc in (16, 18) or format_name not in ("yuv420p", "yuvj420p")
        return {
            "codec": context.name,
            "width": context.width,
            "height": context.height,
            "pixel_format": format_name,
            "hdr_or_high_bit_depth": hdr,
            "rotation": rotation,
            "time_base": str(stream.time_base),
            "rate": str(stream.average_rate),
            "audio_tracks": len(container.streams.audio),
            "other_tracks": [s.type for s in container.streams if s.type not in ("audio", "video")],
            "metadata": dict(container.metadata),
        }


def scan_markers(path):
    matches = []
    with av.open(str(path)) as container:
        progress = MediaProgress(container.streams.video[0], packets=True)
        _, removed = clean_metadata(container.metadata)
        matches.extend(removed)
        for stream in container.streams:
            _, removed = clean_metadata(stream.metadata)
            matches.extend(removed)
            if stream.type == "video":
                _, removed = clean_extradata(
                    stream.codec_context.name, stream.codec_context.extradata or b""
                )
                matches.extend(removed)
        for packet in container.demux(video=0):
            if not packet.size:
                continue
            context = packet.stream.codec_context
            _, removed = clean_packet(
                bytes(packet), context.name, nal_length_size(context.name, context.extradata or b"")
            )
            matches.extend(removed)
            progress.advance(packet)
        progress.finish()
    return matches


def restore_sample_duration(packet, raw_video):
    original = next(raw_video, None)
    # edit-list demux 会截短正起始 PTS 的尾部时长；仅匹配同一压缩包时恢复。
    if original is not None and bytes(original) == bytes(packet):
        packet.duration = round(original.duration * original.time_base / packet.time_base)


def remux(source, target, markers=True):
    with av.open(str(source)) as src, av.open(
        str(source), options=raw_timing_options(src)
    ) as raw, av.open(
        str(target),
        "w",
        format="mp4",
        options={
            "fflags": "+bitexact",
            "avoid_negative_ts": "disabled",
            "movie_timescale": "1000000",
        },
    ) as dst:
        progress = MediaProgress(src.streams.video[0], packets=True)
        raw_video = (p for p in raw.demux(video=0) if p.size)
        dst.metadata.update(clean_metadata(src.metadata)[0] if markers else src.metadata)
        mapping = {}
        for stream in src.streams:
            out = dst.add_stream_from_template(stream)
            out.metadata.clear()
            out.metadata.update(clean_metadata(stream.metadata)[0] if markers else stream.metadata)
            if markers and stream.type == "video":
                cleaned, removed = clean_extradata(
                    stream.codec_context.name, stream.codec_context.extradata or b""
                )
                if removed:
                    out.codec_context.extradata = cleaned
            mapping[stream.index] = out
        for packet in src.demux():
            if packet.dts is None:
                continue
            index = packet.stream.index
            if packet.stream.type == "video":
                restore_sample_duration(packet, raw_video)
                progress.advance(packet)
            if markers and packet.stream.type == "video":
                context = packet.stream.codec_context
                cleaned, removed = clean_packet(
                    bytes(packet),
                    context.name,
                    nal_length_size(context.name, context.extradata or b""),
                )
                if removed:
                    if not cleaned:
                        continue
                    replacement = av.Packet(cleaned)
                    replacement.pts, replacement.dts = packet.pts, packet.dts
                    replacement.duration, replacement.time_base = packet.duration, packet.time_base
                    replacement.is_keyframe = packet.is_keyframe
                    packet = replacement
            packet.stream = mapping[index]
            dst.mux(packet)
        progress.finish()


def detect(source, detector):
    with av.open(str(source)) as container:
        stream = container.streams.video[0]
        stream.codec_context.thread_count = 2
        progress = MediaProgress(stream)
        for frame in container.decode(stream):
            if frame.pts is None:
                raise ValueError("视频帧缺少时间戳")
            time = float(frame.pts * frame.time_base)
            # 小图场景检测不构造每帧完整 RGB 副本。
            small = frame.reformat(width=96, height=54, format="bgr24").to_ndarray()
            if detector.needs_sample(small, time):
                detector.observe(frame.to_ndarray(format="bgr24"), time)
            progress.advance(frame)
        result = detector.select(stream.width, stream.height)
        progress.finish()
        return result


def encode(source, target, tracker):
    with av.open(str(source)) as src, av.open(
        str(source), options=raw_timing_options(src)
    ) as raw, av.open(
        str(target),
        "w",
        format="mp4",
        options={
            "fflags": "+bitexact",
            "avoid_negative_ts": "disabled",
            "movie_timescale": "1000000",
        },
    ) as dst:
        raw_video = (p for p in raw.demux(video=0) if p.size)
        video = src.streams.video[0]
        progress = MediaProgress(video)
        video.codec_context.thread_count = 2
        context = video.codec_context
        out = dst.add_stream("libx264", rate=video.average_rate or 30)
        out.width, out.height, out.pix_fmt = context.width, context.height, "yuv420p"
        out.time_base = video.time_base
        out.codec_context.time_base = video.time_base
        out.codec_context.thread_count = 2
        out.options = {"crf": "18", "preset": "veryfast", "bf": "0"}
        for name in (
            "color_range",
            "color_primaries",
            "color_trc",
            "colorspace",
            "sample_aspect_ratio",
        ):
            value = getattr(context, name)
            if value is not None:
                setattr(out.codec_context, name, value)
        out.metadata.update(video.metadata)
        dst.metadata.update(src.metadata)
        audio = {s.index: dst.add_stream_from_template(s) for s in src.streams.audio}
        durations = {}

        def mux_encoded(packet):
            timestamp = packet.pts * packet.time_base
            duration = durations.pop(timestamp, None)
            if duration:
                packet.duration = round(duration / packet.time_base)
            dst.mux(packet)

        def emit(frame):
            time = float(frame.pts * frame.time_base)
            image = frame.to_ndarray(format="bgr24")
            fixed = av.VideoFrame.from_ndarray(tracker.repair(image, time), format="bgr24")
            fixed.pts, fixed.time_base = frame.pts, frame.time_base
            fixed.duration = frame.duration
            durations[frame.pts * frame.time_base] = frame.duration * frame.time_base
            for encoded in out.encode(fixed):
                mux_encoded(encoded)
            progress.advance(frame)

        for packet in src.demux():
            if packet.stream.type == "video":
                if packet.size:
                    restore_sample_duration(packet, raw_video)
                for frame in packet.decode():
                    emit(frame)
            elif packet.stream.index in audio and packet.dts is not None:
                packet.stream = audio[packet.stream.index]
                dst.mux(packet)
        for packet in out.encode():
            mux_encoded(packet)
        progress.finish()


def fingerprint(path):
    """完整解码视频，同时检查音频包内容和音视频时间线。"""
    pixels = hashlib.sha256()
    times, audio = [], {}
    last_duration = 0.0
    with av.open(str(path)) as container:
        raw_options = raw_timing_options(container)
        stream = container.streams.video[0]
        progress = MediaProgress(stream)
        stream.codec_context.thread_count = 2
        audio_indices = {s.index: i for i, s in enumerate(container.streams.audio)}
        descriptors = [
            (
                s.codec_context.name,
                s.codec_context.sample_rate,
                s.codec_context.layout.name,
                (s.codec_context.extradata or b"").hex(),
            )
            for s in container.streams.audio
        ]
        context = stream.codec_context
        video_spec = (
            context.width,
            context.height,
            str(stream.sample_aspect_ratio),
            context.color_range,
            context.color_primaries,
            context.color_trc,
            context.colorspace,
        )
        for packet in container.demux():
            if packet.stream.type == "video":
                for frame in packet.decode():
                    if frame.pts is None:
                        raise ValueError("校验帧缺少时间戳")
                    times.append(float(frame.pts * frame.time_base))
                    last_duration = float(frame.duration * frame.time_base)
                    pixels.update(frame.format.name.encode())
                    for index, plane in enumerate(frame.planes):
                        components = [c for c in frame.format.components if c.plane == index]
                        stride = sum((c.bits + 7) // 8 for c in components)
                        data = memoryview(plane)
                        for row in range(plane.height):
                            start = row * plane.line_size
                            pixels.update(data[start : start + plane.width * stride])
                    progress.advance(frame)
            elif packet.stream.type == "audio" and packet.size:
                item = audio.setdefault(
                    audio_indices[packet.stream.index], {"hash": hashlib.sha256(), "times": []}
                )
                item["hash"].update(bytes(packet))
                item["times"].append(float(packet.pts * packet.time_base))
                # 校验压缩包可以解码（含最后 flush packet）。
                packet.decode()
        if not times:
            raise ValueError("视频没有可解码帧")
        if any(b <= a for a, b in zip(times, times[1:])):
            raise ValueError("视频时间戳不递增")
    coded_tail_duration = 0.0
    with av.open(str(path), options=raw_options) as raw:
        latest = None
        for packet in raw.demux(video=0):
            if packet.size and packet.pts is not None:
                timestamp = packet.pts * packet.time_base
                if latest is None or timestamp > latest:
                    latest = timestamp
                    coded_tail_duration = float(packet.duration * packet.time_base)
    progress.finish()
    return {
        "pixels": pixels.hexdigest(),
        "times": times,
        "duration": last_duration,
        "coded_tail_duration": coded_tail_duration,
        "video_spec": video_spec,
        "audio_spec": descriptors,
        "audio": {
            key: {"hash": item["hash"].hexdigest(), "times": item["times"]}
            for key, item in audio.items()
        },
    }


def verify(source_fingerprint, target, visual):
    actual = fingerprint(target)
    expected = source_fingerprint

    def same_times(left, right):
        return len(left) == len(right) and all(abs(a - b) <= 0.001 for a, b in zip(left, right))

    if not same_times(expected["times"], actual["times"]):
        raise ValueError("清理前后视频帧数或时间线不一致")
    if (
        expected["video_spec"] != actual["video_spec"]
        or expected["audio_spec"] != actual["audio_spec"]
    ):
        raise ValueError("清理前后画面/音频关键属性不一致")
    if abs(expected["coded_tail_duration"] - actual["coded_tail_duration"]) > 0.001:
        raise ValueError("清理前后末帧编码持续时间不一致")
    if expected["audio"].keys() != actual["audio"].keys():
        raise ValueError("清理前后音轨数量不一致")
    for key, item in expected["audio"].items():
        other = actual["audio"][key]
        if item["hash"] != other["hash"] or not same_times(item["times"], other["times"]):
            raise ValueError("清理前后音频包或时间线不一致")
    if not visual and expected["pixels"] != actual["pixels"]:
        raise ValueError("无损清理改变了解码后画面")
    return {
        "full_decode": True,
        "frames": len(actual["times"]),
        "timestamps_preserved": True,
        "audio_packets_identical": True,
        "coded_tail_duration_preserved": True,
        "demux_tail_duration_delta": actual["duration"] - expected["duration"],
        "pixels_identical": expected["pixels"] == actual["pixels"],
    }
