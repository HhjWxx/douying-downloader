"""桌面任务、内容分页与下载编排；网络和媒体能力复用现有后端。"""

import asyncio
import hashlib
import sqlite3
import time
import uuid
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
import yaml

from auth import CookieManager
from config import ConfigLoader
from config.watermark import artifact_paths
from control import RateLimiter
from core import URLParser
from core.video_downloader import VideoDownloader
from server.desktop_bridge import BridgeError, ContentCache, DesktopAPIClient, PageBridge, item_view
from server.desktop_models import Options, Settings, Target
from server.desktop_store import DesktopStore
from storage import Database, FileManager
from utils.validators import is_short_url, normalize_short_url

ACTIVE = {"pending", "running"}
FINISHED_ITEMS = {"success", "skipped"}


class Reporter:
    def __init__(self, runtime, job, item_id):
        self.runtime, self.job, self.item_id = runtime, job, item_id
        self.last_emit = 0
        self.last_bytes = 0
        self.last_sample = time.monotonic()

    def phase(self, name):
        item = self.job["progress"].setdefault(self.item_id, {})
        return item.setdefault(name, {"state": "pending"})

    def complete_download(self):
        phase = self.phase("download")
        phase.update(state="done", percent=100, updated_at=time.time())
        phase.setdefault("ended_at", time.time())

    def update_step(self, step, detail=""):
        self.job["progress"].setdefault(self.item_id, {}).update(stage=step, detail=detail)
        if not self.phase("cleanup").get("started_at"):
            phase = self.phase("download")
            phase.update(stage=step, state="active", updated_at=time.time())
            phase.setdefault("started_at", time.time())
        self.emit(force=True)

    def set_item_total(self, total, detail=""):
        pass

    def advance_item(self, status, detail="", reason=""):
        self.job["progress"].setdefault(self.item_id, {}).update(reason=reason)
        if status in ("success", "skipped"):
            self.complete_download()
        elif status == "failed":
            self.phase("download").update(state="failed", ended_at=time.time())
        self.emit(force=True)

    def on_item_progress(self, *, aweme_id, bytes_read, bytes_total):
        now = time.monotonic()
        phase = self.phase("download")
        if bytes_read < self.last_bytes:
            self.last_bytes, self.last_sample = 0, now
            phase["started_at"] = time.time()
        seconds = now - self.last_sample
        speed = max(0, bytes_read - self.last_bytes) / seconds if seconds > 0 else 0
        self.last_bytes, self.last_sample = bytes_read, now
        phase.update(state="active", bytes_read=bytes_read, bytes_total=bytes_total,
                     speed=speed, percent=min(99, bytes_read / bytes_total * 100) if bytes_total else None,
                     updated_at=time.time())
        phase.setdefault("started_at", time.time())
        self.emit()

    def on_output_dir(self, *, path):
        if path not in self.job["output_dirs"]:
            self.job["output_dirs"].append(path)

    def on_author(self, nickname=None, sec_uid=None):
        if nickname:
            authors = self.job.setdefault("authors", [])
            if nickname not in authors:
                authors.append(nickname)

    def on_cleanup_progress(self, source, event):
        self.complete_download()
        phase = self.phase("cleanup")
        # 兼容旧 Worker 的纯阶段事件，且不再覆盖下载进度。
        if isinstance(event, str):
            event = {"stage": event}
        changed = phase.get("stage") != event["stage"]
        if changed:
            for key in ("done", "total", "unit", "stage_done"):
                phase.pop(key, None)
        phase.update(event)
        phase.update(state=event.get("state", "active"), source=source,
                     updated_at=event.get("updated_at", time.time()))
        phase.setdefault("started_at", time.time())
        phase["percent"] = (
            100 if event.get("stage_done") else min(99, event.get("done", 0) / event["total"] * 100)
        ) if event.get("total") else None
        self.emit(force=changed)

    def finish(self, result):
        if result.failed:
            self.phase("download").update(state="failed", ended_at=time.time())
        else:
            self.complete_download()
        cleanup = self.phase("cleanup")
        statuses = {item.get("status") for item in result.cleanup}
        state = "failed" if "failed" in statuses else "partial" if statuses & {"partial", "unsupported"} else "done" if statuses - {"disabled"} else "skipped"
        if result.failed and not statuses:
            state = "pending"
        cleanup.update(state=state, ended_at=time.time(), updated_at=time.time())
        if state in {"done", "partial"}:
            cleanup["percent"] = 100
        self.emit(force=True)

    def emit(self, force=False):
        if force or time.monotonic() - self.last_emit > 0.3:
            self.last_emit = time.monotonic()
            self.runtime.notify({"type": "job", "job": self.job})


class MemoryCookies(CookieManager):
    def set_cookies(self, cookies):
        self.cookies = dict(cookies)

    def get_cookies(self):
        return self.cookies


class DesktopRuntime:
    def __init__(self, data_dir, download_dir, model_dir=""):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.config = ConfigLoader(str(self.data_dir / "settings.yml"))
        # 桌面会话由 Electron 管理，不读取工作目录或环境中的旧 Cookie。
        self.config.update(cookie="", auto_cookie=False, transcript={"enabled": False},
                           comments={"enabled": False}, homepage_screenshot=False)
        if not self.config.config_path or not Path(self.config.config_path).exists():
            self.config.update(path=str(download_dir), thread=2)
        if model_dir:
            self.config.update(watermark={"model_dir": model_dir})
        self.database = Database(str(self.data_dir / "library.db"))
        self.store = DesktopStore(self.database.db_path)
        self.bridge = PageBridge()
        self.cache = ContentCache()
        self.cookies = {}
        self.account_key = "guest"
        self.jobs = {}
        self.tasks = {}
        self.events = set()
        self.gate = asyncio.Semaphore(1)
        self.rate = RateLimiter(max_per_second=2)
        self.item_locks = {}
        self.closing = False

    async def open(self):
        await self.database.initialize()
        await self.store.open()
        for job in await self.store.load():
            if job["status"] in ACTIVE:
                job["status"] = "interrupted"
                job["error"] = "应用已退出，点击继续处理未完成作品"
                await self.store.save(job)
            self.jobs[job["job_id"]] = job

    def notify(self, event):
        if self.bridge.socket is None or self.closing:
            return

        async def send():
            try:
                await self.bridge.send(deepcopy(event))
            except Exception:
                pass

        task = asyncio.create_task(send())
        self.events.add(task)
        task.add_done_callback(self.events.discard)

    async def checkpoint(self, job):
        await self.store.save(job)
        self.notify({"type": "job", "job": job})

    def client(self, proxy=None):
        return DesktopAPIClient(self.cookies, self.bridge, self.cache,
                                self.config.get("proxy") if proxy is None else proxy)

    async def session(self, cookies):
        # 会话标识只保存摘要；真实 Cookie 仅驻留内存。
        session_id = cookies.get("sessionid") or cookies.get("sessionid_ss") or ""
        identity = cookies.get("uid_tt") or cookies.get("uid_tt_ss") or session_id
        key = hashlib.sha256(identity.encode()).hexdigest() if session_id else "guest"
        if key != self.account_key:
            for job in list(self.jobs.values()):
                if job["status"] in ACTIVE:
                    await self.cancel(job["job_id"], "interrupted")
            self.cache.items.clear()
        self.cookies = dict(cookies)
        self.account_key = key
        return {"logged_in": key != "guest"}

    async def me(self):
        async with self.client() as client:
            data = await client.get_self_info()
        if not data or not data.get("sec_uid"):
            raise BridgeError("NOT_LOGGED_IN", "请在应用内登录抖音")
        return {"nickname": data.get("nickname", ""), "sec_uid": data["sec_uid"]}

    async def resolve(self, target):
        if target.kind != "url":
            return target
        url = target.url
        async with self.client() as client:
            if is_short_url(url):
                url = await client.resolve_short_url(normalize_short_url(url))
        parsed = URLParser.parse(url or "")
        if not parsed:
            raise ValueError("无法识别此链接，请打开具体作品、作者或合集")
        kind = parsed["type"]
        if kind in {"video", "gallery"}:
            return Target(kind="items", ids=[parsed["aweme_id"]])
        if kind == "user":
            return Target(kind="author", id=parsed["sec_uid"])
        if kind == "collection":
            return Target(kind="mix", id=parsed["mix_id"])
        raise ValueError("桌面端支持视频、图文、作者及合集链接")

    async def page(self, target, cursor="0"):
        target = await self.resolve(target)
        await self.rate.acquire()
        async with self.client() as client:
            if target.kind == "author":
                page = await client.get_user_post(target.id, int(cursor), 20)
            elif target.kind == "mix":
                page = await client.get_mix_aweme(target.id, int(cursor), 20)
            elif target.kind == "likes":
                me = await self.me()
                page = await client.get_user_like(me["sec_uid"], int(cursor), 20)
            elif target.kind == "folders":
                page = await client.get_user_collects("self", int(cursor), 20)
            elif target.kind == "collection":
                if target.id:
                    page = await client.get_collect_aweme(target.id, int(cursor), 20)
                else:
                    page = await client.get_user_collection("self", int(cursor), 20)
            elif target.kind == "items":
                raw = [await client.get_video_detail(key) for key in target.ids]
                if any(x is None for x in raw):
                    raise ValueError("部分作品未返回详情，请重新打开作品后再试")
                return {"items": [item_view(x) for x in raw], "cursor": "0", "has_more": False}
            else:
                raise ValueError("不支持的列表类型")
        if (not page.get("raw") or page.get("items_missing") or page.get("pagination_missing")
                or page.get("status_code", 0)):
            raise BridgeError("INCOMPLETE", "抖音未返回完整列表，已获取的数据会保留")
        list_key = "collects_list" if target.kind == "folders" else "aweme_list"
        if not isinstance(page["raw"].get(list_key), list) or "has_more" not in page["raw"]:
            raise BridgeError("INCOMPLETE", "列表或分页信息缺失，不能判定已经读取完毕")
        if target.kind == "folders":
            items = []
            for item in page.get("items", []):
                raw = item.get("collects_info") or item
                key = raw.get("collects_id_str") or raw.get("collects_id") or raw.get("id")
                if key:
                    items.append({"id": str(key), "title": raw.get("collects_name") or raw.get("name") or "收藏夹"})
        else:
            raw_items = []
            for value in page.get("items", []):
                item = value.get("aweme_info") or value
                if item.get("aweme_id"):
                    raw_items.append(item)
            self.cache.ingest(raw_items)
            items = [item_view(x) for x in raw_items]
        next_cursor = str(page.get("max_cursor", page.get("cursor", 0)))
        if page.get("has_more") and next_cursor == str(cursor):
            raise BridgeError("INCOMPLETE", "抖音分页游标没有前进，请稍后继续")
        return {"items": items, "cursor": next_cursor, "has_more": bool(page.get("has_more"))}

    async def submit(self, request):
        if self.closing:
            raise ValueError("应用正在退出")
        target = await self.resolve(request.target)
        if target.kind == "folders":
            raise ValueError("请选择具体收藏夹")
        first = self.cache.items.get(target.ids[0], {}) if target.ids else {}
        job = {"job_id": uuid.uuid4().hex, "target": target.model_dump(),
               "title": first.get("desc", "") if len(target.ids) == 1 else "", "authors": [],
               "options": request.options.model_dump(mode="json"),
               "settings": self.settings(), "account": self.account_key,
               "created_at": datetime.now().astimezone().isoformat(), "status": "pending",
               "items": [], "results": {}, "progress": {}, "output_dirs": [],
               "cursor": "0", "listing_complete": False, "error": "", "attempt": 1,
               "page_cursors": []}
        self.jobs[job["job_id"]] = job
        await self.checkpoint(job)
        self.tasks[job["job_id"]] = asyncio.create_task(self.run(job))
        return job

    async def expand(self, job):
        if job["listing_complete"]:
            return
        target = Target(**job["target"])
        options = Options(**job["options"])
        if target.kind == "items":
            job["items"] = target.ids
            job["listing_complete"] = True
            await self.checkpoint(job)
            return
        seen = set(job["items"])
        while True:
            if job["cursor"] in job.setdefault("page_cursors", []):
                raise BridgeError("INCOMPLETE", "分页游标发生循环，已获取的作品会保留")
            page = await self.page(target, job["cursor"])
            for item in page["items"]:
                if item["id"] in seen:
                    continue
                if options.media_type != "all" and options.media_type != item["media_type"]:
                    continue
                created = datetime.fromtimestamp(item["created"]).date()
                if options.date_from and created < options.date_from:
                    continue
                if options.date_to and created > options.date_to:
                    continue
                seen.add(item["id"])
                job["items"].append(item["id"])
                if options.limit and len(job["items"]) >= options.limit:
                    break
            job["page_cursors"].append(job["cursor"])
            job["cursor"] = page["cursor"]
            job["listing_complete"] = not page["has_more"] or bool(
                options.limit and len(job["items"]) >= options.limit
            )
            await self.checkpoint(job)
            if job["listing_complete"]:
                return

    async def run(self, job):
        try:
            async with self.gate:
                job["status"] = "running"
                await self.checkpoint(job)
                await self.expand(job)
                remaining = iter([key for key in job["items"]
                                  if job["results"].get(key, {}).get("status") not in FINISHED_ITEMS])

                async def consume():
                    for key in remaining:
                        await self.download_one(job, key)

                workers = [asyncio.create_task(consume()) for _ in range(job["settings"]["thread"])]
                try:
                    await asyncio.gather(*workers)
                finally:
                    for worker in workers:
                        if not worker.done():
                            worker.cancel()
                    await asyncio.gather(*workers, return_exceptions=True)
                statuses = [x["status"] for x in job["results"].values()]
                job["status"] = "partial" if any(x not in FINISHED_ITEMS for x in statuses) else "success"
                if statuses and all(x == "failed" for x in statuses):
                    job["status"] = "failed"
        except asyncio.CancelledError:
            if job["status"] in ACTIVE:
                job["status"] = "interrupted"
        except BridgeError as exc:
            job["status"] = "waiting"
            job["error"] = str(exc)
        except Exception as exc:
            job["status"] = "failed"
            job["error"] = str(exc)
        finally:
            for item in job["progress"].values():
                for name in ("download", "cleanup"):
                    phase = item.get(name, {})
                    if phase.get("state") == "active":
                        phase.update(state="paused", ended_at=time.time())
            await self.checkpoint(job)

    async def download_one(self, job, key):
        lock_key = (str(Path(job["settings"]["path"]).resolve()), key)
        lock = self.item_locks.setdefault(lock_key, asyncio.Lock())
        async with lock:
            config = ConfigLoader(None)
            config.config = deepcopy(self.config.config)
            config.update(path=job["settings"]["path"], proxy=job["settings"]["proxy"],
                          video_quality=job["options"]["quality"],
                          watermark={"enabled": job["options"]["cleanup"]},
                          increase={"post": True}, thread=1)
            cookie_manager = MemoryCookies(cookie_file=str(self.data_dir / "unused-cookies.json"))
            cookie_manager.set_cookies(self.cookies)
            reporter = Reporter(self, job, key)
            async with self.client(job["settings"]["proxy"]) as client:
                downloader = VideoDownloader(config, client, FileManager(config.get("path")),
                                             cookie_manager, self.database, self.rate,
                                             progress_reporter=reporter, job_id=job["job_id"])
                result = await downloader.download({"type": "video", "aweme_id": key})
            reporter.finish(result)
            outcome = "failed" if result.failed else "skipped" if result.skipped else "success"
            if result.cleanup_summary["incomplete"] and outcome != "failed":
                outcome = "partial"
            job["results"][key] = {"status": outcome, "cleanup": result.cleanup,
                                   "title": self.cache.items.get(key, {}).get("desc", key),
                                   "reason": job["progress"].get(key, {}).get("reason", "")}
            await self.checkpoint(job)

    async def cancel(self, key, status="cancelled"):
        job = self.jobs[key]
        task = self.tasks.get(key)
        if task and not task.done():
            job["status"] = status
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                # 协程尚未首次运行时，不会进入 run 的 finally。
                pass
            await self.checkpoint(job)
        return job

    async def resume(self, key):
        job = self.jobs[key]
        if job["status"] in ACTIVE:
            return job
        if job["status"] == "success":
            return job
        if job["account"] != self.account_key:
            raise ValueError("登录会话已变化，请重新选择内容创建任务")
        job.update(status="pending", error="", attempt=job["attempt"] + 1)
        # 页面会话全局共享代理；用户手动继续时同步当前网络配置。
        job["settings"]["proxy"] = self.config.get("proxy") or ""
        if job["cursor"] in job.get("page_cursors", []):
            job["page_cursors"] = []
        job["results"] = {key: value for key, value in job["results"].items()
                          if value.get("status") in FINISHED_ITEMS}
        job["progress"] = {}
        await self.checkpoint(job)
        self.tasks[key] = asyncio.create_task(self.run(job))
        return job

    def settings(self):
        return {"path": self.config.get("path"), "proxy": self.config.get("proxy") or "",
                "thread": int(self.config.get("thread", 2)),
                "cleanup": self.config.get("watermark")["enabled"]}

    async def save_settings(self, settings):
        if any(x["status"] in ACTIVE for x in self.jobs.values()):
            raise ValueError("请先取消或等待当前任务结束，再修改设置")
        path = Path(settings.path).expanduser().resolve()
        await asyncio.to_thread(path.mkdir, parents=True, exist_ok=True)
        old = deepcopy(self.config.config)
        self.config.update(path=str(path), proxy=settings.proxy, thread=settings.thread,
                           watermark={"enabled": settings.cleanup})
        if not await asyncio.to_thread(self.config.save):
            self.config.config = old
            raise ValueError("设置保存失败，请检查目录权限")
        return self.settings()

    async def library(self, **kwargs):
        result = await self.database.get_aweme_history(**kwargs)
        for item in result["items"]:
            source = Path(item["file_path"])
            item.update(clean_path="", report_path="", assets=[])

            def assets():
                paths = list(source.iterdir()) if source.is_dir() else [source]
                return [p for p in paths if p.is_file() and p.suffix.lower() in {
                    ".mp4", ".mov", ".mkv", ".jpg", ".jpeg", ".png", ".webp",
                } and not p.stem.endswith((".clean", ".audio", ".video"))]

            for asset in await asyncio.to_thread(assets):
                image = asset.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
                item["assets"].append({"file_path": str(asset), "kind": "image" if image else "video"})
                if not image:
                    clean, report = artifact_paths(asset)
                    item["clean_path"] = str(clean) if clean.is_file() else ""
                    item["report_path"] = str(report) if report.is_file() else ""
        return result

    async def import_data(self, config_path):
        if any(x["status"] in ACTIVE for x in self.jobs.values()):
            raise ValueError("请先等待或取消运行中的任务")
        source = Path(config_path).resolve()
        raw = yaml.safe_load(await asyncio.to_thread(source.read_text, encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ValueError("配置格式无效")
        root = Path(raw.get("path") or "Downloaded")
        if not root.is_absolute():
            root = source.parent / root
        settings = Settings(path=str(root.resolve()), proxy=raw.get("proxy") or "",
                            thread=min(4, max(1, int(raw.get("thread", 2)))),
                            cleanup=bool((raw.get("watermark") or {}).get("enabled", True)))
        history = Path(raw.get("database_path") or "dy_downloader.db")
        if not history.is_absolute():
            history = source.parent / history
        imported = 0
        if history.is_file() and history.resolve() != Path(self.database.db_path).resolve():
            snapshot = self.data_dir / ("import-" + uuid.uuid4().hex + ".db")

            def backup():
                with sqlite3.connect(history.resolve().as_uri() + "?mode=ro", uri=True) as src:
                    with sqlite3.connect(snapshot) as dst:
                        src.backup(dst)

            await asyncio.to_thread(backup)
            original = Database(str(snapshot))
            try:
                await original.initialize()
                src = await original._get_conn()
                cursor = await src.execute("SELECT aweme_id,aweme_type,title,author_id,author_name,"
                                           "create_time,download_time,file_path,metadata,author_sec_uid,cover_urls "
                                           "FROM aweme WHERE file_path IS NOT NULL AND file_path != ''")
                dst = await self.database._get_conn()
                while True:
                    rows = await cursor.fetchmany(200)
                    if not rows:
                        break
                    for row in rows:
                        values = list(row)
                        location = Path(values[7])
                        if not location.is_absolute():
                            values[7] = str((source.parent / location).resolve())
                        result = await dst.execute(
                            "INSERT OR IGNORE INTO aweme(aweme_id,aweme_type,title,author_id,author_name,"
                            "create_time,download_time,file_path,metadata,author_sec_uid,cover_urls) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?,?)", values,
                        )
                        imported += max(0, result.rowcount)
                await dst.commit()
            finally:
                await original.close()
                await asyncio.to_thread(snapshot.unlink, missing_ok=True)
        await self.save_settings(settings)
        return {"imported": imported}

    async def diagnostics(self):
        result = []
        try:
            await asyncio.wait_for(self.me(), timeout=12)
            result.append({"name": "登录 API", "ok": True, "detail": "页面请求正常"})
        except Exception:
            result.append({"name": "登录 API", "ok": False, "detail": "未登录、需要验证或接口不可用"})
        candidate = None
        for item in reversed(list(self.cache.items.values())):
            video = item.get("video") or {}
            urls = (video.get("play_addr") or {}).get("url_list") or []
            for url in urls:
                parsed = urlparse(url)
                host = parsed.hostname or ""
                if parsed.scheme == "https" and host.endswith((".douyinvod.com", ".bytecdn.cn", ".bytecdn.com")):
                    candidate = url
                    break
            if candidate:
                break
        if not candidate:
            result.append({"name": "视频 CDN", "ok": False, "detail": "请先在应用中打开一个视频，再运行检测"})
            return result
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12)) as client:
                async with client.get(candidate, proxy=self.config.get("proxy") or None,
                                      headers={"Range": "bytes=0-0", "Referer": "https://www.douyin.com/"}) as reply:
                    await reply.content.read(1)
                    result.append({"name": "视频 CDN", "ok": reply.status in (200, 206),
                                   "detail": "%s · HTTP %s" % (urlparse(candidate).hostname, reply.status)})
        except Exception:
            result.append({"name": "视频 CDN", "ok": False, "detail": "连接失败，请检查代理与 CDN 路由"})
        return result

    async def close(self):
        self.closing = True
        for key in list(self.tasks):
            await self.cancel(key, "interrupted")
        for task in list(self.events):
            task.cancel()
        await asyncio.gather(*self.events, return_exceptions=True)
        self.bridge.disconnect()
        await self.store.close()
        await self.database.close()
