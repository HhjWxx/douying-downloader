"""异步调度器：CPU 工作在独立进程执行，网络下载槽不参与清理限流。"""

import asyncio
import functools
import json
import os
import signal
import sys
import tempfile
import weakref
from pathlib import Path

from config.watermark import artifact_paths, eligible, normalize

_GATES = weakref.WeakKeyDictionary()


def cleanup_download(method):
    """所有公开下载入口统一收尾，包括提前返回的本地跳过分支。"""

    @functools.wraps(method)
    async def wrapped(self, *args, **kwargs):
        progress = getattr(getattr(self, "progress_reporter", None), "on_cleanup_progress", None)
        batch = CleanupBatch(self.config.get("watermark"), progress=progress)
        self._cleanup_batch = batch
        try:
            for path, author in self._cleanup_requests.items():
                batch.submit(path, author)
            result = await method(self, *args, **kwargs)
            await self._finish_cleanup(result)
            return result
        finally:
            await batch.close()
            self._cleanup_batch = None

    return wrapped


def summary(items):
    counts = {}
    for item in items:
        status = item.get("status", "failed")
        counts[status] = counts.get(status, 0) + 1
    return {
        "counts": counts,
        "items": items,
        "incomplete": any(key in counts for key in ("failed", "partial", "unsupported")),
    }


async def _terminate(proc):
    if proc.returncode is None:
        try:
            if os.name == "nt":
                # Worker 仅使用进程内 PyAV/ORT，不启动子孙进程。
                proc.kill()
            else:
                os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    await proc.wait()


async def process_file(source, config, author="", progress=None):
    # 目录不可写、路径不存在等启动失败也属于清理失败，不改写下载计数。
    try:
        return await _process_file(source, config, author, progress=progress)
    except Exception as exc:
        return {
            "source": str(source),
            "output": None,
            "report": None,
            "status": "failed",
            "reason": str(exc),
        }


async def _process_file(source, config, author="", progress=None):
    config = normalize(config)
    source = Path(source).resolve()
    output, report_path = artifact_paths(source)
    base = {"source": str(source), "output": None, "report": str(report_path)}
    if not config["enabled"]:
        return {**base, "status": "disabled"}
    loop = asyncio.get_running_loop()
    gate = _GATES.get(loop)
    if gate is None:
        gate = _GATES[loop] = asyncio.Semaphore(config["concurrency"])
    if progress:
        progress(str(source), {"stage": "等待去水印队列", "state": "pending"})
    async with gate:
        # 控制文件使用短文件名，避免长中文原片名突破组件长度限制。
        workspace = await asyncio.to_thread(tempfile.mkdtemp, prefix=".wm-", dir=str(source.parent))
        request = Path(workspace) / "request.json"
        response = Path(workspace) / "response.json"
        payload = {
            "source": str(source),
            "config": config,
            "author": author,
            "workspace": workspace,
            "response": str(response),
        }
        proc = None
        try:
            await asyncio.to_thread(request.write_text, json.dumps(payload), encoding="utf-8")
            env = {**os.environ, "OMP_NUM_THREADS": "2", "OPENBLAS_NUM_THREADS": "2",
                   "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1",
                   "DOUYIN_WORKER_PARENT_PIPE": "1"}
            if progress:
                env["DOUYIN_WATERMARK_PROGRESS"] = "1"
            root = str(Path(__file__).resolve().parents[2])
            env["PYTHONPATH"] = root + os.pathsep + env.get("PYTHONPATH", "")
            options = {"start_new_session": True} if os.name != "nt" else {}
            command = (
                [sys.executable, "--watermark-worker", str(request)]
                if getattr(sys, "frozen", False)
                else [sys.executable, "-m", "core.watermark.worker", str(request)]
            )
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE if progress else asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                env=env,
                **options,
            )
            async def read_progress():
                if progress and proc.stdout:
                    async for line in proc.stdout:
                        try:
                            event = json.loads(line)
                            if isinstance(event, dict) and isinstance(event.get("stage"), str):
                                progress(str(source), event)
                        except (ValueError, KeyError, TypeError):
                            pass

            await asyncio.wait_for(
                asyncio.gather(proc.wait(), read_progress()), config["timeout_seconds"]
            )
            if proc.returncode or not response.exists():
                raise RuntimeError("清理 Worker 异常退出 (%s)" % proc.returncode)
            data = json.loads(await asyncio.to_thread(response.read_text, encoding="utf-8"))
            return {
                key: data.get(key)
                for key in ("source", "output", "report", "status", "reason", "cached")
            }
        except asyncio.CancelledError:
            if proc is not None:
                await _terminate(proc)
            raise
        except Exception as exc:
            if proc is not None:
                await _terminate(proc)
            reason = "清理超时" if isinstance(exc, asyncio.TimeoutError) else str(exc)
            # 独立尝试写失败报告，磁盘不可写时仍返回结构化失败给调用方。
            failure_path = report_path.with_suffix(".failure.json")
            failure = {**base, "status": "failed", "reason": reason, "report": str(failure_path)}
            try:
                from core.watermark.worker import atomic_json

                await asyncio.to_thread(atomic_json, failure_path, failure, Path(workspace))
            except OSError:
                failure["report"] = None
            return failure
        finally:
            import shutil

            await asyncio.to_thread(shutil.rmtree, workspace, True)


async def process_batch(requests, config):
    """固定数量的协程消费请求；大批量任务不创建等量 subprocess/task。"""
    iterator = iter(requests)
    results = []

    async def consume():
        for path, author in iterator:
            results.append(await process_file(path, config, author))

    tasks = [asyncio.create_task(consume()) for _ in range(normalize(config)["concurrency"])]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return results


class CleanupBatch:
    """下载完成即入队；仅建立固定数量的消费协程，不占用下载槽。"""

    def __init__(self, config, progress=None):
        self.config = normalize(config)
        self.progress = progress
        self.queue = asyncio.Queue()
        self.tasks = []
        self.seen = set()
        self.results = []

    def submit(self, path, author):
        if path in self.seen:
            return
        self.seen.add(path)
        self.queue.put_nowait((path, author))
        if not self.tasks:
            self.tasks = [
                asyncio.create_task(self._consume()) for _ in range(self.config["concurrency"])
            ]

    async def _consume(self):
        while True:
            path, author = await self.queue.get()
            try:
                if self.progress:
                    result = await process_file(path, self.config, author, progress=self.progress)
                else:
                    result = await process_file(path, self.config, author)
                self.results.append(result)
            finally:
                self.queue.task_done()

    async def finish(self):
        await self.queue.join()
        return self.results

    async def close(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)


def find_existing(base_path, aweme_id):
    return [
        path
        for path in Path(base_path).rglob("*%s*" % aweme_id)
        if eligible(path) and not any(p.startswith(".wm-") for p in path.parts) and path.is_file()
    ]
