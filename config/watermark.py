"""清理参数、产物命名与稳定的规则版本。"""

import hashlib
from pathlib import Path

RULE_VERSION = "1.0.4"
DEFAULTS = {
    "enabled": True,
    "markers": True,
    "visible": True,
    "strategy": "aggressive",
    "keep_original": True,
    "concurrency": 1,
    "timeout_seconds": 14400,
    "model_dir": "",
}
UNASSESSED = ["未知像素域或频域盲水印", "音频水印", "未匹配规则的私有 SEI"]


def normalize(value):
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError("watermark 必须是配置对象")
    result = {**DEFAULTS, **value}
    for key in ("enabled", "markers", "visible", "keep_original"):
        if not isinstance(result[key], bool):
            raise ValueError("watermark.%s 必须是布尔值" % key)
    if result["strategy"] != "aggressive" or not result["keep_original"]:
        raise ValueError("仅支持 aggressive 策略，且必须保留原片")
    for key in ("concurrency", "timeout_seconds"):
        if isinstance(result[key], bool) or not isinstance(result[key], int) or result[key] < 1:
            raise ValueError("watermark.%s 必须是正整数" % key)
    if result["concurrency"] > 4:
        raise ValueError("watermark.concurrency 不能超过 4")
    if not isinstance(result["model_dir"], str):
        raise ValueError("watermark.model_dir 必须是路径字符串")
    return result


def artifact_paths(source):
    source = Path(source)
    stem = source.stem if source.suffix.lower() == ".mp4" else source.name
    # 给文件系统的单组件长度限制预留后缀空间；长中文名用摘要消除截断碰撞。
    if len(stem.encode("utf-8")) > 210:
        digest = hashlib.sha256(stem.encode("utf-8")).hexdigest()[:12]
        stem = stem.encode("utf-8")[:190].decode("utf-8", "ignore") + "_" + digest
    return source.with_name(stem + ".clean.mp4"), source.with_name(stem + ".cleanup.json")


def eligible(path):
    path = Path(path)
    return path.suffix.lower() in {".mp4", ".mov", ".flv", ".mkv"} and not (
        path.stem.endswith((".clean", ".audio", ".video")) or path.name.startswith(".wm-")
    )
