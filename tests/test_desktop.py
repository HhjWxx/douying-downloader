"""桌面实际业务场景：会话隔离、分页恢复、安装包接口与历史迁移。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from config import ConfigLoader
from core.downloader_base import DownloadResult
from server.app import build_app
from server.desktop_bridge import BridgeError, ContentCache, DesktopAPIClient, PageBridge
from server.desktop_models import JobRequest, Options, Target
from server.desktop_runtime import DesktopRuntime


def runtime_at(tmp_path):
    return DesktopRuntime(tmp_path / "data", tmp_path / "downloads")


@pytest.mark.asyncio
async def test_bridge_keeps_large_integers_and_form_request():
    bridge = PageBridge()

    async def send(event):
        assert event["method"] == "POST"
        assert event["content_type"] == "application/x-www-form-urlencoded"
        bridge.answer({"id": event["id"], "status": 200,
                       "text": '{"collects_id": 7697097927726009663}'})

    bridge.send = send
    result = await bridge.fetch("/aweme/v1/web/aweme/listcollection/", {}, method="POST",
                                data={"cursor": "0"},
                                request_headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert str(result.body["collects_id"]) == "7697097927726009663"
    assert not bridge.pending
    with pytest.raises(BridgeError):
        await bridge.fetch("https://example.com/", {})


@pytest.mark.asyncio
async def test_disconnect_releases_pending_bridge():
    bridge = PageBridge()
    bridge.send = AsyncMock()
    task = asyncio.create_task(bridge.fetch("/aweme/v1/web/aweme/detail/", {}))
    await asyncio.sleep(0)
    bridge.disconnect()
    with pytest.raises(BridgeError, match="连接中断"):
        await task
    assert not bridge.pending


def test_cache_nested_response_dedup_and_capacity():
    cache = ContentCache(1)
    raw = {"data": [{"aweme_info": {"aweme_id": 7697097927726009663,
                                    "desc": "测试作品", "video": {"cover": {}},
                                    "author": {"sec_uid": "MS4abc"}}}]}
    view = cache.ingest(raw)
    assert view[0]["id"] == "7697097927726009663"
    cache.ingest({"aweme_id": "123456", "video": {"duration": 1}})
    assert list(cache.items) == ["123456"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status,text", [(403, '{}'), (429, '{}'), (200, '<html>验证</html>')])
async def test_desktop_client_blocks_without_silent_empty_success(status, text):
    bridge = SimpleNamespace(fetch=AsyncMock(return_value=SimpleNamespace(
        http_status=status, text=text, body={} if text == '{}' else None)))
    client = DesktopAPIClient({}, bridge, ContentCache())
    with pytest.raises(BridgeError):
        await client._request_json_gated("/aweme/v1/web/aweme/post/", {})


@pytest.mark.asyncio
async def test_restart_restores_unfinished_without_automatic_download(tmp_path):
    runtime = runtime_at(tmp_path)
    await runtime.open()
    entered = asyncio.Event()

    async def blocked(job):
        entered.set()
        await asyncio.Event().wait()

    runtime.expand = blocked
    job = await runtime.submit(JobRequest(target=Target(kind="items", ids=["123456"])))
    await entered.wait()
    await runtime.close()
    restored = runtime_at(tmp_path)
    await restored.open()
    assert restored.jobs[job["job_id"]]["status"] == "interrupted"
    assert not restored.tasks
    await restored.close()


@pytest.mark.asyncio
async def test_cancel_before_coroutine_first_runs(tmp_path):
    runtime = runtime_at(tmp_path)
    await runtime.open()
    job = await runtime.submit(JobRequest(target=Target(kind="items", ids=["123456"])))
    await runtime.cancel(job["job_id"])
    assert job["status"] == "cancelled"
    assert runtime.tasks[job["job_id"]].done()
    await runtime.close()


@pytest.mark.asyncio
async def test_retry_skips_successful_items_and_preserves_options(tmp_path):
    runtime = runtime_at(tmp_path)
    await runtime.open()
    calls = []

    async def download(job, key):
        calls.append((key, job["options"]["quality"]))
        job["results"][key] = {"status": "failed" if key == "234567" and job["attempt"] == 1 else "success"}

    runtime.download_one = download
    job = await runtime.submit(JobRequest(target=Target(kind="items", ids=["123456", "234567"]),
                                         options=Options(quality="720p")))
    await runtime.tasks[job["job_id"]]
    assert job["status"] == "partial"
    await runtime.resume(job["job_id"])
    await runtime.tasks[job["job_id"]]
    assert job["status"] == "success"
    assert calls == [("123456", "720p"), ("234567", "720p"), ("234567", "720p")]
    await runtime.close()


@pytest.mark.asyncio
async def test_account_change_interrupts_old_work_without_cookie_files(tmp_path):
    runtime = runtime_at(tmp_path)
    await runtime.open()
    await runtime.session({"sessionid": "first", "uid_tt": "account-a"})
    runtime.expand = AsyncMock(side_effect=lambda job: asyncio.sleep(20))
    # 用真实 await 阻塞模拟页面请求。
    async def wait(job):
        await asyncio.Event().wait()
    runtime.expand = wait
    job = await runtime.submit(JobRequest(target=Target(kind="items", ids=["123456"])))
    await asyncio.sleep(0)
    await runtime.session({"sessionid": "second", "uid_tt": "account-b"})
    assert job["status"] == "interrupted"
    with pytest.raises(ValueError, match="会话已变化"):
        await runtime.resume(job["job_id"])
    assert not list((tmp_path / "data").glob("*cookies*"))
    await runtime.close()


@pytest.mark.asyncio
async def test_failed_pagination_keeps_cursor_and_ids_then_resumes(tmp_path):
    runtime = runtime_at(tmp_path)
    await runtime.open()
    runtime.page = AsyncMock(side_effect=[
        {"items": [{"id": "123456", "created": 1, "media_type": "video"}], "cursor": "20", "has_more": True},
        BridgeError("INCOMPLETE", "需要验证"),
    ])
    job = await runtime.submit(JobRequest(target=Target(kind="author", id="MS4abcdef")))
    await runtime.tasks[job["job_id"]]
    assert job["status"] == "waiting"
    assert job["items"] == ["123456"] and job["cursor"] == "20"
    runtime.page = AsyncMock(return_value={"items": [], "cursor": "40", "has_more": False})

    async def done(job, key):
        job["results"][key] = {"status": "success"}

    runtime.download_one = done
    await runtime.resume(job["job_id"])
    await runtime.tasks[job["job_id"]]
    assert runtime.page.call_args.args[1] == "20"
    assert job["status"] == "success"
    await runtime.close()


@pytest.mark.asyncio
async def test_download_wires_history_and_cleanup_without_cookie_persistence(tmp_path, monkeypatch):
    runtime = runtime_at(tmp_path)
    await runtime.open()
    runtime.cookies = {"sessionid": "sensitive"}

    async def download(downloader, target):
        assert downloader.database is runtime.database
        assert downloader.cookie_manager.get_cookies() == runtime.cookies
        assert downloader.job_id
        result = DownloadResult()
        result.success = 1
        result.cleanup = [{"status": "partial", "reason": "字幕风险"}]
        return result

    monkeypatch.setattr("server.desktop_runtime.VideoDownloader.download", download)
    job = await runtime.submit(JobRequest(target=Target(kind="items", ids=["123456"])))
    await runtime.tasks[job["job_id"]]
    assert job["status"] == "partial"
    assert not list((tmp_path / "data").glob("*cookies*"))
    await runtime.close()


def test_api_auth_websocket_and_legacy_route(tmp_path):
    runtime = runtime_at(tmp_path)
    app = build_app(ConfigLoader(None), desktop=runtime, token="a" * 64)
    with TestClient(app) as client:
        assert client.get("/api/v1/health").status_code == 401
        client.headers["authorization"] = "Bearer " + "a" * 64
        assert client.get("/api/v1/health").status_code == 200
        with client.websocket_connect("/api/v1/desktop/session", subprotocols=["a" * 64]) as ws:
            assert ws.receive_json()["type"] == "connected"
        assert client.post("/api/v1/jobs", json={"target": {"kind": "url", "url": "http://localhost/"}}).status_code == 422
        assert client.get("/api/v1/jobs/missing").status_code == 404
        assert client.put("/api/v1/settings", json={"path": str(tmp_path), "proxy": "socks5://localhost:1"}).status_code == 422


@pytest.mark.asyncio
async def test_library_lists_actual_files_and_reports(tmp_path):
    runtime = runtime_at(tmp_path)
    await runtime.open()
    media_dir = tmp_path / "作品"
    media_dir.mkdir()
    (media_dir / "test.mp4").write_bytes(b"source")
    (media_dir / "test.clean.mp4").write_bytes(b"clean")
    (media_dir / "test.cleanup.json").write_text('{}')
    await runtime.database.add_aweme({"aweme_id": "123456", "aweme_type": "video", "file_path": str(media_dir)})
    page = await runtime.library()
    assert len(page["items"][0]["assets"]) == 1
    assert page["items"][0]["clean_path"].endswith("test.clean.mp4")
    await runtime.close()


@pytest.mark.asyncio
async def test_import_keeps_original_config_and_cookies_out(tmp_path):
    runtime = runtime_at(tmp_path)
    await runtime.open()
    original = tmp_path / "old.yml"
    text = 'path: ./old-downloads\ncookie: do-not-import\nthread: 2\n'
    original.write_text(text)
    result = await runtime.import_data(str(original))
    assert result["imported"] == 0
    assert original.read_text() == text
    saved = (tmp_path / "data" / "settings.yml").read_text()
    assert "do-not-import" not in saved
    assert runtime.config.get("path") == str(tmp_path / "old-downloads")
    await runtime.close()


@pytest.mark.parametrize("target", [
    {"kind": "items", "ids": ["1;rm -rf"]},
    {"kind": "url", "url": "https://www.douyin.com.evil.test/video/1"},
    {"kind": "url", "url": "https://user@www.douyin.com/video/1"},
])
def test_target_boundary(target):
    with pytest.raises(ValueError):
        Target(**target)


@pytest.mark.asyncio
async def test_real_local_download_history_cleanup_and_duplicate(tmp_path):
    import av
    import numpy as np
    from aiohttp import web

    source = tmp_path / "fixture.mp4"
    with av.open(str(source), "w") as container:
        container.metadata["comment"] = "vid:v0300fg10000db8p8evog65kopi713q0"
        stream = container.add_stream("libx264", rate=12)
        stream.width, stream.height, stream.pix_fmt = 160, 90, "yuv420p"
        for _ in range(12):
            for packet in stream.encode(av.VideoFrame.from_ndarray(np.zeros((90, 160, 3), dtype=np.uint8), format="rgb24")):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    payload = source.read_bytes()
    requests = []

    async def media(request):
        requests.append(request.path)
        return web.Response(body=payload, content_type="video/mp4")

    site_app = web.Application()
    site_app.router.add_get("/video.mp4", media)
    runner = web.AppRunner(site_app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    runtime = runtime_at(tmp_path)
    await runtime.open()
    runtime.config.update(watermark={"visible": False})
    raw = {"aweme_id": "7697097927726009663", "aweme_type": 0, "desc": "中文 空格 视频",
           "create_time": 1794200000, "author": {"uid": "123", "sec_uid": "MS4abcdef", "nickname": "测试作者"},
           "video": {"play_addr": {"url_list": ["http://127.0.0.1:%s/video.mp4" % port]}}}
    runtime.cache.ingest(raw)
    try:
        request = JobRequest(target=Target(kind="items", ids=[raw["aweme_id"]]))
        job = await runtime.submit(request)
        await asyncio.wait_for(runtime.tasks[job["job_id"]], 30)
        assert job["status"] == "success", job
        assert requests == ["/video.mp4"]
        result = await runtime.library()
        assert result["total"] == 1
        assert result["items"][0]["clean_path"]
        second = await runtime.submit(request)
        await asyncio.wait_for(runtime.tasks[second["job_id"]], 30)
        assert second["results"][raw["aweme_id"]]["status"] == "skipped"
        assert requests == ["/video.mp4"]
    finally:
        await runtime.close()
        await runner.cleanup()


@pytest.mark.asyncio
async def test_date_and_type_filters_survive_multiple_pages(tmp_path):
    runtime = runtime_at(tmp_path)
    await runtime.open()
    runtime.page = AsyncMock(side_effect=[
        {"items": [{"id": "111111", "created": 1600000000, "media_type": "video"},
                   {"id": "222222", "created": 1794200000, "media_type": "gallery"}],
         "cursor": "20", "has_more": True},
        {"items": [{"id": "333333", "created": 1794200000, "media_type": "video"}],
         "cursor": "40", "has_more": True},
    ])
    async def done(job, key):
        job["results"][key] = {"status": "success"}
    runtime.download_one = done
    job = await runtime.submit(JobRequest(target=Target(kind="author", id="MS4abcdef"),
                                         options=Options(limit=1, media_type="video", date_from="2026-01-01")))
    await runtime.tasks[job["job_id"]]
    assert job["items"] == ["333333"]
    assert job["listing_complete"]
    await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [
    {"status_code": 0},
    {"status_code": 0, "aweme_list": None, "has_more": 0},
    {"status_code": 0, "aweme_list": []},
])
async def test_missing_page_fields_never_become_empty_success(tmp_path, raw):
    runtime = runtime_at(tmp_path)
    runtime.bridge.fetch = AsyncMock(return_value=SimpleNamespace(http_status=200, body=raw, text=""))
    with pytest.raises(BridgeError):
        await runtime.page(Target(kind="author", id="MS4abcdef"))


@pytest.mark.asyncio
async def test_collection_folder_integer_id_reaches_ui_as_exact_string(tmp_path):
    runtime = runtime_at(tmp_path)
    raw = {"status_code": 0, "collects_list": [{"collects_info": {
        "collects_id": 7697097927726009663, "collects_name": "我的收藏夹",
    }}], "has_more": 0, "cursor": 0}
    runtime.bridge.fetch = AsyncMock(return_value=SimpleNamespace(http_status=200, body=raw, text=""))
    page = await runtime.page(Target(kind="folders"))
    assert page["items"] == [{"id": "7697097927726009663", "title": "我的收藏夹"}]
    assert not page["has_more"]
