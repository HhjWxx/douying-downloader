"""固定 PP-OCRv4 mobile 模型；下载只由显式 setup 命令触发。"""

import hashlib
import os
import tempfile
from pathlib import Path

MODELS = {
    "det": (
        "ch_PP-OCRv4_det_mobile.onnx",
        "d2a7720d45a54257208b1e13e36a8479894cb74155a5efe29462512d42f49da9",
    ),
    "rec": (
        "ch_PP-OCRv4_rec_mobile.onnx",
        "48fc40f24f6d2a207a2b1091d3437eb3cc3eb6b676dc3ef9c37384005483683b",
    ),
}
MODEL_BASE = "https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.10.0/onnx/PP-OCRv4"


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def model_root(directory=""):
    return (
        Path(directory).expanduser().resolve()
        if directory
        else (Path.home() / ".cache" / "douyin-downloader" / "ocr-v4")
    )


def prepare(directory=""):
    import requests

    root = model_root(directory)
    root.mkdir(parents=True, exist_ok=True)
    for kind, (name, checksum) in MODELS.items():
        target = root / name
        if target.is_file() and sha256(target) == checksum:
            continue
        descriptor, temporary = tempfile.mkstemp(prefix=".model-", dir=str(root))
        try:
            with os.fdopen(descriptor, "wb") as handle:
                with requests.get(
                    MODEL_BASE + "/" + kind + "/" + name, stream=True, timeout=(30, 120)
                ) as response:
                    response.raise_for_status()
                    for chunk in response.iter_content(1024 * 1024):
                        handle.write(chunk)
            if sha256(temporary) != checksum:
                raise ValueError("OCR 模型校验失败：" + name)
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)
    return root


def create_ocr(directory=""):
    root = model_root(directory)
    # 模型缺失时直接降级，不先加载 OCR 引擎及其原生依赖。
    for name, checksum in MODELS.values():
        path = root / name
        if not path.is_file() or sha256(path) != checksum:
            raise RuntimeError("OCR 模型缺失或校验失败，请运行 python -m tools.watermark setup")

    from rapidocr import ModelType, OCRVersion, RapidOCR

    params = {
        "Global.use_cls": False,
        "Global.log_level": "error",
        "Global.text_score": 0.35,
        "Det.limit_side_len": 960,
        "Det.limit_type": "max",
        "Det.box_thresh": 0.3,
        "Det.thresh": 0.2,
        "Det.std": [0.229, 0.224, 0.225],
        "Det.mean": [0.485, 0.456, 0.406],
        "EngineConfig.onnxruntime.intra_op_num_threads": 2,
        "EngineConfig.onnxruntime.inter_op_num_threads": 1,
    }
    for kind, (name, _) in MODELS.items():
        path = root / name
        section = kind.capitalize()
        params[section + ".model_path"] = str(path)
        params[section + ".ocr_version"] = OCRVersion.PPOCRV4
        params[section + ".model_type"] = ModelType.MOBILE
    # 模型包含字典。先验证，杜绝底层库在缺字典时隐式访问网络。
    import onnxruntime as ort

    options = ort.SessionOptions()
    options.intra_op_num_threads = 1
    session = ort.InferenceSession(
        str(root / MODELS["rec"][0]), options, providers=["CPUExecutionProvider"]
    )
    if "character" not in session.get_modelmeta().custom_metadata_map:
        raise RuntimeError("OCR 模型未包含字符字典")
    del session
    return RapidOCR(params=params)
