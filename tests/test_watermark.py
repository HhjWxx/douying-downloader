"""离线合成媒体验收：真实编码、SEI 消息边界、取消和下载集成。"""

import asyncio
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import av
import cv2
import numpy as np
import pytest

from config import ConfigLoader
from config.watermark import artifact_paths, normalize
from core.ffmpeg import resolve_ffmpeg_path
from core.watermark import media
from core.watermark.detection import Detector, MaskTracker, highpass
from core.watermark.markers import (
    ENCODER_UUID,
    clean_extradata,
    clean_nal,
    clean_packet,
    escape,
    unescape,
)
from core.watermark.models import MODELS, create_ocr, model_root, sha256
from core.watermark.service import process_file
from core.watermark.worker import run


@pytest.fixture(autouse=True)
def short_worker_timeout(monkeypatch):
    from config.watermark import DEFAULTS

    # 合成媒体只有两秒，测试不能沿用真实长视频的四小时超时。
    monkeypatch.setitem(DEFAULTS, "timeout_seconds", 30)


def make_video(path, codec="libx264", audio=False, fragmented=False, hdr=False):
    command = [
        resolve_ffmpeg_path(),
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=320x180:rate=10:duration=2",
    ]
    if audio:
        command.extend(
            ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=2", "-c:a", "aac"]
        )
    command.extend(
        ["-c:v", codec, "-threads", "2", "-pix_fmt", "yuv420p10le" if hdr else "yuv420p"]
    )
    if codec == "libx265":
        command.extend(["-x265-params", "pools=none:frame-threads=1:log-level=error"])
    if hdr:
        command.extend(["-color_trc", "smpte2084"])
    if fragmented:
        command.extend(["-movflags", "frag_keyframe+empty_moov"])
    command.extend(["-metadata", "comment=vid:fixture", str(path)])
    subprocess.run(command, check=True, capture_output=True, timeout=30)
    return path


async def test_worker_streams_real_progress_and_preserves_verification(tmp_path):
    source = make_video(tmp_path / "progress.mp4", audio=True)
    events = []
    result = await process_file(source, {"visible": False}, progress=lambda path, event: events.append(event))
    assert result["status"] == "success", (result, events[-3:])
    assert any(e.get("unit") == "帧" and e.get("done") == 20 for e in events)
    assert any(e.get("stage") == "校验音频与视频完整性" for e in events)
    report = json.loads(Path(result["report"]).read_text())
    assert report["verification"]["audio_packets_identical"]


def test_decode_and_encode_report_completed_frames(tmp_path):
    from core.watermark import progress

    source = make_video(tmp_path / "source.mp4")
    events = []
    reporter = progress.Progress(events.append)
    token = progress._current.set(reporter)
    try:
        reporter.stage("检测")
        detector = SimpleNamespace(needs_sample=lambda *args: False, select=lambda *args: [])
        media.detect(source, detector)
        assert events[-1]["done"] == events[-1]["total"] == 20
        reporter.stage("编码")
        media.encode(source, tmp_path / "encoded.mp4", MaskTracker([]))
        assert events[-1]["done"] == events[-1]["total"] == 20
        assert events[-1]["stage_done"]
    finally:
        progress._current.reset(token)


def sei_message(kind, payload):
    def number(value):
        return b"\xff" * (value // 255) + bytes([value % 255])

    return number(kind) + number(len(payload)) + payload


@pytest.mark.parametrize("codec,header", [("h264", b"\x06"), ("hevc", b"\x4e\x01")])
def test_remove_only_matched_sei_message(codec, header):
    known = sei_message(5, ENCODER_UUID + b"bvc0ot v2.2.1.3\x00")
    unknown = sei_message(5, bytes(range(16)) + b"unknown\x00\x00\x01\x02")
    hdr = sei_message(137, bytes(range(24)))
    nal = header + escape(unknown + known + hdr + b"\x80")
    cleaned, removed = clean_nal(nal, codec)
    assert unescape(cleaned[len(header) :]) == unknown + hdr + b"\x80"
    assert len(removed) == 1
    assert clean_nal(cleaned, codec) == (cleaned, [])
    for length in (1, 2, 4):
        packet = len(nal).to_bytes(length, "big") + nal
        result, _ = clean_packet(packet, codec, length)
        assert result == len(cleaned).to_bytes(length, "big") + cleaned
    result, _ = clean_packet(b"\x00\x00\x00\x01" + nal, codec, None)
    assert result == b"\x00\x00\x00\x01" + cleaned


def test_unknown_uuid_and_malformed_sei_untouched():
    nal = b"\x06" + sei_message(5, ENCODER_UUID + b"unknown encoder") + b"\x80"
    assert clean_nal(nal, "h264") == (nal, [])
    malformed = nal[:-1]
    assert clean_nal(malformed, "h264") == (malformed, [])
    with pytest.raises(ValueError):
        clean_packet(b"\x00\x00\x00\xffshort", "h264", 4)


@pytest.mark.parametrize("data", [b"\0\0\1", b"\0\0\0\0\3", bytes(range(256))])
def test_rbsp_round_trip(data):
    assert unescape(escape(data)) == data


def test_hvcc_sei_preserves_other_arrays():
    sps = b"\x42\x01unchanged"
    sei = b"\x4e\x01" + sei_message(5, ENCODER_UUID + b"bvc0ot v2.2") + b"\x80"
    prefix = b"\x01" + bytes(21)

    def array(kind, nal):
        return bytes([kind]) + b"\x00\x01" + len(nal).to_bytes(2, "big") + nal

    data = prefix + b"\x02" + array(33, sps) + array(39, sei)
    result, matches = clean_extradata("hevc", data)
    assert result == prefix + b"\x01" + array(33, sps)
    assert len(matches) == 1


@pytest.mark.parametrize(
    "codec,fragmented", [("libx264", False), ("libx264", True), ("libx265", False)]
)
def test_real_lossless_remux_preserves_decode_audio_and_timestamps(tmp_path, codec, fragmented):
    source = make_video(tmp_path / "中文 source.mp4", codec, audio=True, fragmented=fragmented)
    before = sha256(source)
    target = tmp_path / "clean.mp4"
    assert media.scan_markers(source)
    media.remux(source, target)
    assert not media.scan_markers(target)
    result = media.verify(media.fingerprint(source), target, False)
    assert result["frames"] == 20
    assert result["pixels_identical"] and result["audio_packets_identical"]
    assert sha256(source) == before


def test_visual_encode_keeps_audio_and_timing(tmp_path):
    source = make_video(tmp_path / "source.mp4", audio=True)
    target = tmp_path / "encode.mp4"
    media.encode(source, target, MaskTracker([]))
    assert media.verify(media.fingerprint(source), target, True)["audio_packets_identical"]


async def test_worker_idempotence_missing_output_and_atomic_failure(tmp_path):
    source = make_video(tmp_path / "source.mp4")
    before = sha256(source)
    result = await process_file(source, {"visible": False})
    assert result["status"] == "success", result
    output, report = artifact_paths(source)
    assert output.exists() and report.exists()
    first_hash = sha256(output)
    second = await process_file(source, {"visible": False})
    assert second["cached"] is True
    assert sha256(output) == first_hash
    output.unlink()
    retry = await process_file(source, {"visible": False})
    assert retry["status"] == "success" and not retry["cached"]
    assert sha256(source) == before
    source.write_bytes(b"invalid media")
    failed = await process_file(source, {"visible": False})
    assert failed["status"] == "failed"
    assert sha256(output) == first_hash
    assert not list(tmp_path.glob(".wm-*/*"))


async def test_changed_clean_copy_is_not_overwritten(tmp_path):
    source = make_video(tmp_path / "source.mp4")
    await process_file(source, {"visible": False})
    output, _ = artifact_paths(source)
    output.write_bytes(b"user edited this copy")
    result = await process_file(source, {"visible": False})
    assert result["status"] == "failed"
    assert output.read_bytes() == b"user edited this copy"


async def test_toggle_processing_preserves_output_ownership(tmp_path):
    source = make_video(tmp_path / "source.mp4")
    first = await process_file(source, {"visible": False})
    assert first["status"] == "success"
    disabled_targets = await process_file(source, {"markers": False, "visible": False})
    assert disabled_targets["status"] == "not_matched"
    restored = await process_file(source, {"visible": False})
    assert restored["status"] == "success"


async def test_parallel_jobs_same_file_are_serialized(tmp_path):
    source = make_video(tmp_path / "source.mp4")
    first, second = await asyncio.gather(
        process_file(source, {"visible": False, "concurrency": 2}),
        process_file(source, {"visible": False, "concurrency": 2}),
    )
    assert first["status"] == second["status"] == "success"
    assert sum(bool(item["cached"]) for item in (first, second)) == 1


async def test_missing_models_is_partial_and_still_cleans_markers(tmp_path):
    source = make_video(tmp_path / "source.mp4")
    result = await process_file(source, {"model_dir": str(tmp_path / "missing")})
    assert result["status"] == "partial"
    assert "模型" in result["reason"]
    assert not media.scan_markers(Path(result["output"]))


async def test_hdr_preserved_without_sdr_conversion(tmp_path):
    source = make_video(tmp_path / "source.mp4", codec="libx265", hdr=True)
    result = await process_file(source, {})
    assert result["status"] == "partial"
    assert "HDR" in result["reason"]
    assert media.probe(result["output"])["hdr_or_high_bit_depth"]


async def test_disabled_does_not_start_worker(tmp_path, monkeypatch):
    spawn = AsyncMock(side_effect=AssertionError("不应启动 Worker"))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    result = await process_file(tmp_path / "source.mp4", {"enabled": False})
    assert result["status"] == "disabled"
    spawn.assert_not_called()


async def test_cancel_reaps_worker_and_removes_workspace(tmp_path, monkeypatch):
    import core.watermark.service as service

    started = asyncio.Event()
    killed = asyncio.Event()

    class Process:
        returncode = None
        pid = 987654

        async def wait(self):
            started.set()
            await killed.wait()

    proc = Process()

    async def terminate(value):
        assert value is proc
        killed.set()
        await value.wait()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=proc))
    monkeypatch.setattr(service, "_terminate", terminate)
    task = asyncio.create_task(process_file(tmp_path / "source.mp4", {}))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert killed.is_set()
    assert not list(tmp_path.glob(".wm-*"))


async def test_timeout_has_report_and_reaps_worker(tmp_path, monkeypatch):
    import core.watermark.service as service

    killed = asyncio.Event()

    class Process:
        returncode = None
        pid = 987654

        async def wait(self):
            await killed.wait()

    proc = Process()

    async def terminate(value):
        killed.set()
        await value.wait()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=proc))
    monkeypatch.setattr(service, "_terminate", terminate)
    result = await process_file(tmp_path / "source.mp4", {"timeout_seconds": 1})
    assert result["status"] == "failed" and "超时" in result["reason"]
    assert json.loads(Path(result["report"]).read_text())["status"] == "failed"
    assert killed.is_set()


async def test_cleanup_starts_while_download_continues(tmp_path, monkeypatch):
    import core.watermark.service as service
    from core.downloader_base import DownloadResult

    started, release = asyncio.Event(), asyncio.Event()

    async def process(path, config, author):
        started.set()
        await release.wait()
        return {"source": str(path), "status": "success"}

    monkeypatch.setattr(service, "process_file", process)

    class Downloader:
        config = ConfigLoader()
        _cleanup_requests = {}

        @service.cleanup_download
        async def download(self):
            self._cleanup_batch.submit(tmp_path / "source.mp4", "author")
            await asyncio.wait_for(started.wait(), 1)
            release.set()
            return DownloadResult()

        async def _finish_cleanup(self, result):
            result.cleanup.extend(await self._cleanup_batch.finish())

    result = await Downloader().download()
    assert result.cleanup_summary["counts"] == {"success": 1}


def test_write_failure_preserves_source(tmp_path, monkeypatch):
    from core.watermark import worker

    source = make_video(tmp_path / "source.mp4")
    before = sha256(source)
    work = tmp_path / "work"
    work.mkdir()

    def no_replace(*args):
        raise PermissionError("目标被占用")

    monkeypatch.setattr(worker.os, "replace", no_replace)
    result = run({"source": str(source), "config": {"visible": False}, "workspace": str(work)})
    assert result["status"] == "failed"
    assert sha256(source) == before


def test_tracking_moving_intermittent_mark_and_outside_pixels():
    rng = np.random.default_rng(42)
    source = rng.integers(0, 40, (180, 320, 3), dtype=np.uint8)
    cv2.putText(source, "MARK", (15, 35), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (200, 200, 200), 2)
    box = (10, 12, 85, 40)
    template = highpass(cv2.cvtColor(source[12:40, 10:85], cv2.COLOR_BGR2GRAY))
    tracker = MaskTracker([{"time": 1, "box": box, "template": template}])
    shifted = np.roll(source, 8, axis=1)
    result = tracker.repair(shifted, 1)
    assert tracker.applied
    changed = np.any(result != shifted, axis=2)
    assert changed.sum() > 20
    assert not changed[50:].any()
    assert np.array_equal(tracker.repair(source, 4), source)


def test_aggressive_tracking_repairs_uncertain_identity_and_ignores_old_position():
    rng = np.random.default_rng(123)
    image = rng.integers(0, 200, (180, 320, 3), dtype=np.uint8)
    candidates = [
        {
            "time": 0,
            "box": (10, 10, 70, 30),
            "template": rng.integers(0, 200, (20, 60), dtype=np.uint8),
            "kind": "identity",
            "track_key": "author",
        },
        {
            "time": 2,
            "box": (220, 120, 280, 140),
            "subtitle_overlap": True,
            "template": rng.integers(0, 200, (20, 60), dtype=np.uint8),
            "kind": "identity",
            "track_key": "author",
        },
    ]
    tracker = MaskTracker(candidates)
    result = tracker.repair(image, 2)
    assert tracker.applied[0]["low_confidence_fallbacks"]
    assert tracker.applied[0]["low_confidence_subtitle_regions"]
    assert np.array_equal(result[:60], image[:60])
    assert not np.array_equal(result[115:145, 215:285], image[115:145, 215:285])


def test_report_commit_failure_recovers_without_reencoding(tmp_path, monkeypatch):
    from core.watermark import worker

    source = make_video(tmp_path / "source.mp4")
    work = tmp_path / "work"
    work.mkdir()
    request = {"source": str(source), "config": {"visible": False}, "workspace": str(work)}
    atomic = worker.atomic_json

    def interrupted(path, data, directory=None):
        if data.get("status") == "success":
            raise OSError("报告提交中断")
        return atomic(path, data, directory)

    monkeypatch.setattr(worker, "atomic_json", interrupted)
    first = worker.run(request)
    assert first["status"] == "partial" and Path(first["output"]).exists()
    assert json.loads(artifact_paths(source)[1].read_text())["status"] == "committing"
    monkeypatch.setattr(worker, "atomic_json", atomic)
    monkeypatch.setattr(media, "remux", lambda *a: pytest.fail("应恢复已提交产物"))
    second = worker.run(request)
    assert second["status"] == "success" and second["cached"]


def test_subtitle_exclusion_and_author_overlap():
    detector = Detector(None, "作者字幕")
    for time in range(6):
        detector.candidates.extend(
            [
                {"time": time, "box": (250, 800, 750, 850), "text": "变化台词" + str(time)},
                {"time": time, "box": (250, 900, 750, 950), "text": "作者字幕"},
                {"time": time, "box": (10, 30, 110, 55), "text": "角落签名"},
            ]
        )
    selected = detector.select(1000, 1000)
    assert len(selected) == 12
    assert all(not item["text"].startswith("变化") for item in selected)
    assert any(item["subtitle_overlap"] for item in selected)


def test_residual_single_character_near_original_mark_is_detected():
    detector = Detector(
        None, "作者签名", reference=[{"time": 1, "box": (10, 20, 110, 50), "text": "作者签名"}]
    )
    detector.candidates = [{"time": 1.2, "box": (85, 22, 110, 48), "text": "名"}]
    assert detector.select(640, 360)[0]["kind"] == "identity"


def test_cropped_large_subtitle_is_not_promoted_to_watermark():
    detector = Detector(None, "胖子追影")
    detector.candidates = [
        {"time": time, "box": (300, 954, 725, 1049), "text": "没有北美伙伴的"} for time in range(6)
    ]
    assert detector.select(1920, 1080) == []
    # 即使旧规则留下了字幕候选，复检也不能将残字升级为作者身份匹配。
    detector.reference = [{**detector.candidates[0], "kind": "persistent_text"}]
    detector.candidates = [{"time": 0, "box": (436, 954, 725, 1049), "text": "北美伙"}]
    assert detector.select(1920, 1080) == []


def test_variable_timing_positive_start_preserves_sample_duration(tmp_path):
    from fractions import Fraction

    source = tmp_path / "offset-vfr.mp4"
    with av.open(str(source), "w", options={"movie_timescale": "1000000"}) as container:
        stream = container.add_stream("libx264", rate=30)
        stream.width, stream.height, stream.pix_fmt = 320, 180, "yuv420p"
        stream.time_base = stream.codec_context.time_base = Fraction(1, 15360)
        stream.options = {"bf": "0"}
        for pts in (353, 865, 1889, 2913, 3425):
            frame = av.VideoFrame.from_ndarray(np.zeros((180, 320, 3), np.uint8), format="bgr24")
            frame.pts, frame.time_base = pts, stream.time_base
            for packet in stream.encode(frame):
                packet.duration = 512
                container.mux(packet)
        for packet in stream.encode():
            packet.duration = 512
            container.mux(packet)
    encoded, cleaned = tmp_path / "encoded.mp4", tmp_path / "clean.mp4"
    media.encode(source, encoded, MaskTracker([]))
    media.remux(encoded, cleaned)
    assert media.verify(media.fingerprint(source), cleaned, True)["coded_tail_duration_preserved"]


async def test_flv_remux_and_long_unicode_paths(tmp_path):
    parent = tmp_path / ("long path " * 8) / ("中文目录" * 8)
    parent.mkdir(parents=True)
    source = make_video(parent / ("中文" * 35 + ".mp4"))
    flv = source.with_suffix(".flv")
    subprocess.run(
        [resolve_ffmpeg_path(), "-v", "error", "-i", str(source), "-c", "copy", str(flv)],
        check=True,
        capture_output=True,
        timeout=30,
    )
    result = await process_file(flv, {"visible": False})
    assert result["status"] == "success", result
    assert artifact_paths(source) != artifact_paths(flv)


def test_configuration_persistence_validation(tmp_path):
    path = tmp_path / "settings.yml"
    config = ConfigLoader(str(path))
    config.update(watermark={**normalize({}), "visible": False})
    assert config.save()
    assert not ConfigLoader(str(path)).get("watermark")["visible"]
    for settings in ({"enabled": "false"}, {"concurrency": 0}, {"keep_original": False}):
        with pytest.raises(ValueError):
            normalize(settings)


async def test_existing_video_cleanup_without_network(tmp_path, monkeypatch):
    from auth import CookieManager
    from core.video_downloader import VideoDownloader
    from storage import FileManager

    source = make_video(tmp_path / "作品_7412345678901234567.mp4")
    config = ConfigLoader()
    config.update(path=str(tmp_path), watermark={"visible": False})
    api = SimpleNamespace(get_video_detail=AsyncMock(side_effect=AssertionError("不应访问 API")))
    downloader = VideoDownloader(config, api, FileManager(str(tmp_path)), CookieManager())
    result = await downloader.download({"aweme_id": "7412345678901234567"})
    assert result.skipped == 1
    assert result.cleanup_summary["counts"] == {"success": 1}
    assert artifact_paths(source)[0].exists()
    api.get_video_detail.assert_not_called()


async def test_rest_partial_is_terminal_and_prunable():
    from server.jobs import JobManager, JobStatus

    async def executor(url):
        return {
            "total": 1,
            "success": 1,
            "cleanup": {"counts": {"partial": 1}, "items": [], "incomplete": True},
        }

    manager = JobManager(executor, max_jobs=1)
    job = await manager.submit("local")
    await job._task
    assert job.status == "partial" and job.status in JobStatus.TERMINAL
    assert job.to_dict()["cleanup"]["incomplete"]
    second = await manager.submit("second")
    await second._task
    assert await manager.get(job.job_id) is None


def test_offline_ocr_recognizes_synthetic_author_and_no_mark_video(tmp_path, monkeypatch):
    if not all((model_root() / name).exists() for name, _ in MODELS.values()):
        pytest.skip("运行 python -m tools.watermark setup 后执行真实 OCR 验收")
    import requests

    monkeypatch.setattr(requests, "get", lambda *a, **k: pytest.fail("处理期间不得联网"))
    ocr = create_ocr()
    detector = Detector(ocr, "WATERMARK")
    for time in (0, 1):
        image = np.full((360, 640, 3), 40, dtype=np.uint8)
        cv2.putText(image, "WATERMARK", (15, 45), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (190, 190, 190), 2)
        detector.observe(image, time)
    assert any(item["kind"] == "identity" for item in detector.select(640, 360))
    source = tmp_path / "plain.mp4"
    with av.open(str(source), "w") as container:
        stream = container.add_stream("libx264", rate=10)
        stream.width, stream.height, stream.pix_fmt = 320, 180, "yuv420p"
        for _ in range(20):
            frame = av.VideoFrame.from_ndarray(np.zeros((180, 320, 3), np.uint8), format="bgr24")
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    work = tmp_path / "work"
    work.mkdir()
    result = run({"source": str(source), "config": {"markers": False}, "workspace": str(work)})
    assert result["status"] == "not_matched", result
    assert not artifact_paths(source)[0].exists()
    assert json.loads(artifact_paths(source)[1].read_text())["unassessed"]
