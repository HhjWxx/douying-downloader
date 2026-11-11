"""下载和清理分别推进，阶段切换与进程心跳不伪造完成度。"""

from types import SimpleNamespace

from core.watermark import progress
from server.desktop_runtime import Reporter


def test_download_snapshot_survives_cleanup_and_stage_reset():
    job = {"progress": {}}
    reporter = Reporter(SimpleNamespace(notify=lambda event: None), job, "video")
    reporter.update_step("下载作品")
    reporter.on_item_progress(aweme_id="video", bytes_read=50, bytes_total=100)
    assert job["progress"]["video"]["download"]["percent"] == 50
    reporter.on_cleanup_progress("video.mp4", {"stage": "修复画面", "done": 40, "total": 100, "unit": "帧"})
    phases = job["progress"]["video"]
    assert phases["download"]["percent"] == 100
    assert phases["download"]["bytes_read"] == 50
    stopped_at = phases["download"]["ended_at"]
    reporter.on_cleanup_progress("video.mp4", {"stage": "修复画面", "done": 60, "total": 100, "unit": "帧"})
    assert phases["download"]["ended_at"] == stopped_at
    assert phases["cleanup"]["percent"] == 60
    reporter.update_step("自动去水印", "等待清理结束")
    assert phases["cleanup"]["percent"] == 60
    reporter.on_cleanup_progress("video.mp4", {"stage": "校验文件并保存结果"})
    assert phases["cleanup"]["percent"] is None
    assert "unit" not in phases["cleanup"]


def test_unknown_download_length_and_retry_never_exceed_total():
    job = {"progress": {}}
    reporter = Reporter(SimpleNamespace(notify=lambda event: None), job, "video")
    reporter.on_item_progress(aweme_id="video", bytes_read=50, bytes_total=0)
    assert job["progress"]["video"]["download"]["percent"] is None
    reporter.on_item_progress(aweme_id="video", bytes_read=10, bytes_total=100)
    assert job["progress"]["video"]["download"]["percent"] == 10
    reporter.on_item_progress(aweme_id="video", bytes_read=110, bytes_total=100)
    assert job["progress"]["video"]["download"]["percent"] == 99


def test_heartbeat_reports_liveness_without_inventing_work():
    events = []
    reporter = progress.Progress(events.append)
    reporter.stage("加载检测模型")
    waits = iter([False, True])
    reporter.stopped = SimpleNamespace(wait=lambda timeout: next(waits))
    reporter.heartbeat()
    assert len(events) == 2
    assert events[0]["done"] == events[1]["done"] == 0
    assert events[1]["total"] is None
    assert events[1]["updated_at"] >= events[0]["updated_at"]


def test_unknown_frame_count_uses_real_media_time():
    events = []
    reporter = progress.Progress(events.append)
    reporter.stage("检测")
    token = progress._current.set(reporter)
    try:
        stream = SimpleNamespace(start_time=100, time_base=0.01, frames=0, duration=1000)
        counter = progress.MediaProgress(stream)
        counter.advance(SimpleNamespace(pts=600, time_base=0.01))
        counter.finish()
    finally:
        progress._current.reset(token)
    assert events[-1]["unit"] == "秒"
    assert events[-1]["done"] == 5
    assert events[-1]["total"] == 10
