"""准备离线模型或对本地文件重试清理，不需要 Cookie。"""

import argparse
import asyncio
import json
from pathlib import Path

from config import ConfigLoader
from config.watermark import eligible
from core.watermark.models import prepare
from core.watermark.service import process_batch


def main():
    parser = argparse.ArgumentParser(description="本地视频自动去水印")
    parser.add_argument("command", choices=("setup", "clean"))
    parser.add_argument("paths", nargs="*")
    parser.add_argument("--config", default=None)
    parser.add_argument("--model-dir", default=None)
    parser.add_argument("--author", default="")
    parser.add_argument("--markers-only", action="store_true")
    args = parser.parse_args()
    settings = ConfigLoader(args.config).get("watermark")
    if args.model_dir is not None:
        settings["model_dir"] = args.model_dir
    if args.command == "setup":
        print(prepare(settings["model_dir"]))
        return
    if not args.paths:
        parser.error("clean 需要本地文件或目录路径")
    if args.markers_only:
        settings["visible"] = False
    requests = {}
    for value in args.paths:
        path = Path(value).expanduser().resolve()
        paths = path.rglob("*") if path.is_dir() else [path]
        for source in paths:
            if eligible(source) and source.is_file():
                requests[source] = args.author
    results = asyncio.run(process_batch(list(requests.items()), settings))
    for item in results:
        print(
            json.dumps(
                {key: item.get(key) for key in ("source", "status", "output", "report", "reason")},
                ensure_ascii=False,
            )
        )
    if not results or any(
        item["status"] in ("failed", "partial", "unsupported") for item in results
    ):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
