"""桌面 sidecar 入口；冻结后同时承担独立媒体 Worker 的启动。"""

import argparse
import asyncio
import json
import os
import socket
import sys


def main():
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    if len(sys.argv) > 1 and sys.argv[1] == "--watermark-worker":
        from core.watermark.worker import main as worker_main

        sys.argv = [sys.argv[0], sys.argv[2]]
        worker_main()
        return
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--download-dir", required=True)
    parser.add_argument("--model-dir", default="")
    args = parser.parse_args()
    # 令牌经匿名管道传入，不写入命令行或日志。
    token = sys.stdin.readline().strip()
    if len(token) < 32:
        raise SystemExit("缺少桌面宿主凭据")
    import certifi

    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    from server.app import build_app
    from server.desktop_runtime import DesktopRuntime

    runtime = DesktopRuntime(args.data_dir, args.download_dir, args.model_dir)
    app = build_app(runtime.config, desktop=runtime, token=token)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    print(json.dumps({"port": port}), flush=True)
    # 第一次握手后 stdout 保留给协议，第三方库输出进入日志流。
    sys.stdout = sys.stderr

    async def serve():
        import uvicorn

        server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False))

        async def watch_parent():
            await asyncio.to_thread(sys.stdin.read)
            server.should_exit = True

        watcher = asyncio.create_task(watch_parent())
        try:
            await server.serve(sockets=[listener])
        finally:
            watcher.cancel()

    asyncio.run(serve())


if __name__ == "__main__":
    main()
