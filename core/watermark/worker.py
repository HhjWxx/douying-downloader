"""独立清理进程入口；源文件不以写模式打开。"""

import hashlib
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path

from config.watermark import RULE_VERSION, UNASSESSED, artifact_paths, normalize
from core.watermark.models import sha256
from core.watermark.progress import reporting
from core.watermark.progress import stage as progress


def atomic_json(path, data, directory=None):
    import tempfile

    descriptor, temporary = tempfile.mkstemp(prefix=".wm-json-", dir=str(directory or path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextmanager
def file_lock(source):
    # 内核锁随进程退出释放；锁文件保持 inode 不变，避免删除/重建造成双持有者。
    name = hashlib.sha256(os.path.normcase(str(source)).encode()).hexdigest()[:24]
    lock_path = source.parent / (".wm-lock-" + name)
    with open(lock_path, "a+b") as handle:
        if os.name == "nt":
            import msvcrt

            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            # LK_LOCK 只重试 10 次，故非阻塞轮询由父进程总超时约束。
            import time

            while True:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.2)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@reporting
def run(request):
    progress("校验原片")
    source = Path(request["source"]).resolve()
    config = normalize(request["config"])
    output, report_path = artifact_paths(source)
    work = Path(request["workspace"])
    with file_lock(source):
        report = {
            "source": str(source),
            "output": None,
            "report": str(report_path),
            "status": "failed",
            "rule_version": RULE_VERSION,
            "config": config,
            "unassessed": UNASSESSED,
            "reason": None,
        }
        old = {}
        committed = False
        try:
            if report_path.is_file():
                try:
                    old = json.loads(report_path.read_text(encoding="utf-8"))
                    if not isinstance(old, dict) or old.get("source") != str(source):
                        old = {}
                except (OSError, ValueError):
                    old = {}
            author = request.get("author") or old.get("author", "")
            report["author"] = author
            report["previous_output_sha256"] = old.get("output_sha256") or old.get(
                "previous_output_sha256"
            )
            source_hash = sha256(source)
            identity = {
                "source": source_hash,
                "rule": RULE_VERSION,
                "config": config,
                "author": author,
            }
            key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
            report.update(input_sha256=source_hash, cache_key=key)
            if report_path.is_file():
                if (
                    old.get("status") == "committing"
                    and old.get("cache_key") == key
                    and output.is_file()
                    and sha256(output) == old.get("output_sha256")
                ):
                    old["status"] = old.pop("final_status")
                    atomic_json(report_path, old, work)
                if old.get("cache_key") == key and old.get("status") in ("success", "not_matched"):
                    if old["status"] == "not_matched" or (
                        output.is_file() and sha256(output) == old.get("output_sha256")
                    ):
                        return {**old, "cached": True}
            from core.watermark import media

            info = media.probe(source)
            report["media"] = info
            if info["codec"] not in ("h264", "hevc") or info["other_tracks"] or info["rotation"]:
                report.update(
                    status="unsupported", reason="仅支持无旋转矩阵的 H.264/HEVC 单视频轨与音轨"
                )
                atomic_json(report_path, report, work)
                return report
            progress("扫描已知标记")
            markers = media.scan_markers(source) if config["markers"] else []
            report["markers_detected"] = markers
            candidates, visible_error = [], None
            if config["visible"]:
                if info["hdr_or_high_bit_depth"]:
                    visible_error = "HDR、高位深或非 YUV420 画面暂不进行可见水印修复"
                else:
                    try:
                        import cv2

                        from core.watermark.detection import Detector, describe
                        from core.watermark.models import create_ocr

                        cv2.setNumThreads(2)
                        progress("加载水印检测模型")
                        ocr = create_ocr(config["model_dir"])
                        progress("扫描可见水印")
                        candidates = media.detect(source, Detector(ocr, author))
                        report["planned_masks"] = describe(candidates)
                    except Exception as exc:
                        visible_error = "可见水印检测未完成：" + str(exc)
            report["sampling"] = (
                "约 1 fps 加场景切换；候选附近逐帧模板跟踪，可能漏检短暂或低对比度水印"
            )
            if not markers and not candidates:
                progress("确认原片与保存报告")
                if sha256(source) != source_hash:
                    raise ValueError("检测期间源文件发生变化")
                report.update(
                    status="partial" if visible_error else "not_matched", reason=visible_error
                )
                atomic_json(report_path, report, work)
                return report
            progress("读取音视频指纹")
            original = media.fingerprint(source)
            intermediate = work / "encoded.mp4"
            final = work / "final.mp4"
            if candidates:
                from core.watermark.detection import Detector, MaskTracker, describe

                tracker = MaskTracker(candidates)
                progress("修复画面")
                media.encode(source, intermediate, tracker)
                progress("复检处理结果")
                residual = media.detect(intermediate, Detector(ocr, author, reference=candidates))
                report["residual_first_pass"] = describe(residual)
                if residual:
                    # 第二次仍以原始帧编码，避免再次压缩第一代有损结果。
                    tracker = MaskTracker(candidates + residual, expanded=True)
                    progress("重新修复剩余候选")
                    media.encode(source, intermediate, tracker)
                    progress("复检二次修复结果")
                    residual = media.detect(
                        intermediate, Detector(ocr, author, reference=candidates)
                    )
                report["applied_masks"] = tracker.applied
                report["residual_final"] = describe(residual)
                report["repair"] = "Telea; local glyph mask; rectangle fallback once"
                if residual:
                    visible_error = "复检仍发现可见水印候选，已达到一次重试上限"
                if not tracker.applied:
                    visible_error = "候选未通过逐帧跟踪，未应用修复"
                overlap_frames = sum(
                    bool(frame.get("low_confidence_subtitle_regions")) for frame in tracker.applied
                )
                report["subtitle_fallback_frames"] = overlap_frames
                if overlap_frames:
                    warning = "低置信度修复涉及字幕区域，可能误伤字幕；原片已保留"
                    visible_error = (visible_error + "；" + warning) if visible_error else warning
                progress("清理标记并封装副本")
                media.remux(intermediate, final, config["markers"])
            else:
                progress("清理标记并封装副本")
                media.remux(source, final, config["markers"])
            progress("校验音频与视频完整性")
            report["verification"] = media.verify(original, final, bool(candidates))
            progress("复检已知标记")
            after = media.scan_markers(final) if config["markers"] else []
            if after:
                raise ValueError("输出仍含已知目标标记")
            progress("校验文件并保存结果")
            if sha256(source) != source_hash:
                raise ValueError("处理期间源文件发生变化，放弃提交")
            report["output_sha256"] = sha256(final)
            report.update(
                output=str(output),
                status="partial" if visible_error else "success",
                reason=visible_error,
                markers_remaining=after,
            )
            # 没有有效旧报告时不覆盖用户自行创建的同名文件。
            if output.exists():
                owned_hashes = {old.get("output_sha256"), old.get("previous_output_sha256")}
                if sha256(output) not in owned_hashes:
                    raise FileExistsError(
                        "同名副本未归属本任务或已被修改，保留现有文件：" + str(output)
                    )
            journal = {
                **report,
                "status": "committing",
                "final_status": report["status"],
                "previous_output_sha256": old.get("output_sha256")
                or old.get("previous_output_sha256"),
            }
            atomic_json(report_path, journal, work)
            os.replace(final, output)
            committed = True
            atomic_json(report_path, report, work)
        except Exception as exc:
            if committed:
                # 已验证的副本已提交；保留 journal，下一次可通过哈希恢复报告。
                report.update(status="partial", reason="副本已提交，报告待恢复：" + str(exc))
                return report
            report.update(status="failed", output=None, reason=type(exc).__name__ + ": " + str(exc))
            report["previous_output_sha256"] = old.get("output_sha256") or old.get(
                "previous_output_sha256"
            )
            try:
                atomic_json(report_path, report, work)
            except OSError:
                report["report"] = None
        return report


def main():
    if os.environ.get("DOUYIN_WORKER_PARENT_PIPE") == "1":
        import threading

        def watch_parent():
            # 父进程持有管道写端；异常退出后 EOF 自动终止 Worker，兼容 Windows。
            # 使用 os.read 避免 daemon 线程持有 Python 缓冲流锁影响正常退出。
            try:
                while os.read(0, 1):
                    pass
            finally:
                os._exit(75)

        threading.Thread(target=watch_parent, daemon=True).start()
    request = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    result = run(request)
    atomic_json(Path(request["response"]), result)


if __name__ == "__main__":
    main()
