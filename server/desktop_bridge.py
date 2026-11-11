"""桌面主进程和 Python 之间的受限页面请求通道。"""

import asyncio
import json
import time
import uuid
from collections import OrderedDict
from types import SimpleNamespace

from core.api_client import DouyinAPIClient

PATHS = frozenset("/aweme/v1/web/" + value + "/" for value in (
    "aweme/detail", "aweme/post", "aweme/favorite", "aweme/listcollection",
    "collects/list", "collects/video/list", "mix/listcollection", "mix/detail",
    "mix/aweme", "mix/list", "series/list", "user/profile/self", "user/profile/other",
))


class BridgeError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.page_bridge_code = code


class PageBridge:
    def __init__(self):
        self.socket = None
        self.pending = {}
        self.send_lock = asyncio.Lock()

    async def send(self, event):
        if self.socket is None:
            raise BridgeError("UNAVAILABLE", "桌面连接已断开")
        async with self.send_lock:
            if self.socket is None:
                raise BridgeError("UNAVAILABLE", "桌面连接已断开")
            await self.socket.send_json(event)

    def disconnect(self):
        self.socket = None
        for future in list(self.pending.values()):
            if not future.done():
                future.set_exception(BridgeError("UNAVAILABLE", "页面连接中断，请重新连接"))

    def answer(self, message):
        future = self.pending.get(message.get("id"))
        if future is not None and not future.done():
            if message.get("error"):
                future.set_exception(BridgeError(message.get("code", "PAGE_ERROR"), message["error"]))
            else:
                future.set_result(message)

    async def fetch(self, path, params, *, method="GET", data=None, request_headers=None):
        if path not in PATHS or method not in {"GET", "POST"}:
            raise BridgeError("FORBIDDEN", "此页面请求不在支持范围内")
        request_id = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await self.send({"type": "fetch", "id": request_id, "path": path,
                             "params": params, "method": method, "data": data,
                             "content_type": (request_headers or {}).get("Content-Type", "")})
            raw = await asyncio.wait_for(future, timeout=45)
        except asyncio.TimeoutError as exc:
            raise BridgeError("TIMEOUT", "抖音页面响应超时，可在浏览页面检查连接") from exc
        finally:
            self.pending.pop(request_id, None)
        text = raw.get("text", "")
        try:
            body = json.loads(text)
        except (ValueError, TypeError):
            body = None
        return SimpleNamespace(http_status=int(raw.get("status", 0)), body=body, text=text)


class ContentCache:
    def __init__(self, capacity=2000):
        self.capacity = capacity
        self.items = OrderedDict()
        self.timestamps = {}

    def ingest(self, value):
        found = []

        def walk(node, depth=0):
            if depth > 12:
                return
            if isinstance(node, dict):
                if node.get("aweme_id") and (node.get("video") or node.get("images")):
                    key = str(node["aweme_id"])
                    self.items[key] = node
                    self.timestamps[key] = time.monotonic()
                    self.items.move_to_end(key)
                    found.append(item_view(node))
                else:
                    for child in node.values():
                        walk(child, depth + 1)
            elif isinstance(node, list):
                for child in node[:500]:
                    walk(child, depth + 1)

        walk(value)
        while len(self.items) > self.capacity:
            key, _ = self.items.popitem(last=False)
            self.timestamps.pop(key, None)
        return list({x["id"]: x for x in found}.values())


def item_view(raw):
    author = raw.get("author") or {}
    video = raw.get("video") or {}
    covers = (video.get("cover") or {}).get("url_list") or []
    if not covers and raw.get("images"):
        covers = raw["images"][0].get("url_list") or []
    return {"id": str(raw["aweme_id"]), "title": raw.get("desc") or "未命名作品",
            "author": author.get("nickname") or "未知作者", "sec_uid": author.get("sec_uid") or "",
            "cover": covers[0] if covers else "", "created": raw.get("create_time", 0),
            "media_type": "gallery" if raw.get("images") else "video"}


class DesktopAPIClient(DouyinAPIClient):
    """桌面请求统一由登录页发出，CLI 的既有网络路径保持不变。"""

    def __init__(self, cookies, bridge, cache, proxy=""):
        super().__init__(cookies, proxy=proxy, page_bridge=bridge)
        self.cache = cache

    async def _ensure_ms_token(self):
        # Electron 会话和页面 SDK 负责令牌生命周期，避免后台另建浏览器身份。
        return self.cookies.get("msToken", "")

    async def _request_json_gated(self, path, params, *, method="GET", data=None,
                                  request_headers=None, suppress_error=False):
        response = await self.page_bridge.fetch(path, params, method=method, data=data,
                                                request_headers=request_headers)
        if response.http_status in (401, 403, 429):
            raise BridgeError("VERIFY_REQUIRED", "抖音要求登录、验证或限流等待，请在页面检查后继续")
        if response.http_status != 200:
            raise BridgeError("HTTP_ERROR", "抖音请求失败，HTTP %s" % response.http_status)
        if not isinstance(response.body, dict):
            raise BridgeError("VERIFY_REQUIRED", "抖音未返回数据，请在页面完成验证后继续")
        if response.body.get("status_code", 0) != 0:
            raise BridgeError("API_ERROR", "抖音接口未成功返回内容，请检查登录状态")
        self.cache.ingest(response.body)
        return response.body

    async def _request_json(self, path, params, **kwargs):
        if path in PATHS:
            return await self._request_json_gated(path, params, **kwargs)
        return await super()._request_json(path, params, **kwargs)

    async def get_video_detail(self, aweme_id, *, suppress_error=False):
        cached = self.cache.items.get(str(aweme_id))
        if cached and time.monotonic() - self.cache.timestamps.get(str(aweme_id), 0) < 300:
            return cached
        return await super().get_video_detail(aweme_id, suppress_error=suppress_error)
