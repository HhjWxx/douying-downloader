"""安装包自检：独立服务启动和离线媒体 Worker，不访问抖音。"""

import argparse
import hashlib
import json
import os
import secrets
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path


def check(executable, model_dir):
    import av
    import numpy as np

    with tempfile.TemporaryDirectory(prefix="douyin-package-") as temporary:
        root = Path(temporary) / "中文 目录"
        root.mkdir()
        source = root / "原片.mp4"
        with av.open(str(source), "w") as output:
            output.metadata["comment"] = "vid:v0300fg10000db8p8evog65kopi713q0"
            stream = output.add_stream("libx264", rate=12)
            stream.width, stream.height, stream.pix_fmt = 320, 180, "yuv420p"
            for _ in range(12):
                frame = av.VideoFrame.from_ndarray(np.zeros((180, 320, 3), dtype=np.uint8), format="rgb24")
                for packet in stream.encode(frame):
                    output.mux(packet)
            for packet in stream.encode():
                output.mux(packet)
        before = hashlib.sha256(source.read_bytes()).hexdigest()
        workspace = root / "worker"
        workspace.mkdir()
        response = workspace / "response.json"
        request = workspace / "request.json"
        request.write_text(json.dumps({"source": str(source), "workspace": str(workspace),
                                       "response": str(response), "config": {"model_dir": str(model_dir)}}),
                           encoding="utf-8")
        worker_log = workspace / "worker.log"
        # 按桌面实际运行方式保持父管道打开，验证 OCR 导入与正常退出不会互相阻塞。
        with worker_log.open("w", encoding="utf-8") as log:
            worker = subprocess.Popen(
                [str(executable), "--watermark-worker", str(request)],
                stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT,
                env={**os.environ, "DOUYIN_WATERMARK_PROGRESS": "1", "DOUYIN_WORKER_PARENT_PIPE": "1"},
            )
            try:
                worker.wait(timeout=120)
            finally:
                if worker.poll() is None:
                    worker.kill()
                    worker.wait()
                worker.stdin.close()
        output = worker_log.read_text(encoding="utf-8")
        if worker.returncode:
            raise RuntimeError(output)
        events = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
        assert any(event.get("unit") == "帧" and event.get("done") == 12 for event in events), "冻结 Worker 未报告帧进度"
        report = json.loads(response.read_text(encoding="utf-8"))
        assert report["status"] == "success", report.get("reason")
        assert Path(report["output"]).is_file()
        assert report["markers_detected"] and not report["markers_remaining"]
        assert hashlib.sha256(source.read_bytes()).hexdigest() == before
        orphan = subprocess.Popen(
            [str(executable), "--watermark-worker", str(request)], stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env={**os.environ, "DOUYIN_WORKER_PARENT_PIPE": "1"},
        )
        orphan.stdin.close()
        assert orphan.wait(timeout=15) == 75, "父进程退出后 Worker 未停止"
        token = secrets.token_hex(32)
        process = subprocess.Popen(
            [str(executable), "--data-dir", str(root / "app"), "--download-dir", str(root / "downloads"),
             "--model-dir", str(model_dir)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", env={**os.environ, "PYTHONPATH": ""},
        )
        try:
            process.stdin.write(token + "\n")
            process.stdin.flush()
            handshake = json.loads(process.stdout.readline())
            request_http = urllib.request.Request(
                "http://127.0.0.1:%s/api/v1/health" % handshake["port"],
                headers={"Authorization": "Bearer " + token},
            )
            for attempt in range(100):
                try:
                    with urllib.request.urlopen(request_http, timeout=2) as reply:
                        assert json.load(reply)["status"] == "ok"
                    break
                except OSError:
                    if attempt == 99:
                        raise
                    time.sleep(0.1)
        finally:
            process.stdin.close()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                raise
            if process.returncode:
                raise RuntimeError(process.stderr.read())
        print("安装包自检通过：独立服务、中文路径、离线 OCR、标记清理、原片保留、正常退出")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("executable", type=Path)
    parser.add_argument("--model-dir", type=Path, required=True)
    args = parser.parse_args()
    check(args.executable.resolve(), args.model_dir.resolve())


if __name__ == "__main__":
    main()
