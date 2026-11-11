"""在目标平台构建独立 sidecar 与校验后的 OCR 模型。"""

import subprocess
import sys
from importlib import metadata
from pathlib import Path

from core.watermark.models import prepare


def resources(root):
    from PIL import Image, ImageDraw

    # 图标由基础几何绘制，构建时同时生成 Windows 与 macOS 所需 PNG。
    directory = root / "desktop" / "assets"
    directory.mkdir(exist_ok=True)
    icon = Image.new("RGBA", (1024, 1024), (0, 0, 0, 0))
    draw = ImageDraw.Draw(icon)
    draw.rounded_rectangle((48, 48, 976, 976), radius=228, fill="#237759")
    draw.line([(512, 250), (512, 700)], fill="white", width=72)
    draw.line([(310, 510), (512, 712), (714, 510)], fill="white", width=72, joint="curve")
    draw.rounded_rectangle((284, 782, 740, 842), radius=30, fill="white")
    icon.save(directory / "icon.png")
    notices = ["Douyin Downloader\n\n" + (root / "LICENSE").read_text(),
               "OCR 模型：RapidAI/RapidOCR PP-OCRv4 mobile\n"
               "https://www.modelscope.cn/models/RapidAI/RapidOCR\n"
               "以下为构建环境第三方依赖声明；Electron 自带 LICENSE/LICENSES.chromium.html。\n"]
    for distribution in sorted(metadata.distributions(), key=lambda d: d.metadata["Name"].lower()):
        notices.append("\n\n" + distribution.metadata["Name"] + " " + distribution.version)
        notices.append(distribution.metadata.get("License-Expression") or distribution.metadata.get("License") or "")
        for filename in distribution.files or []:
            if filename.name.lower().startswith(("license", "copying", "notice")):
                try:
                    notices.append(distribution.locate_file(filename).read_text(encoding="utf-8"))
                except (OSError, UnicodeError):
                    pass
    (root / "dist" / "THIRD_PARTY_NOTICES.txt").write_text("\n".join(notices), encoding="utf-8")


def main():
    root = Path(__file__).resolve().parents[1]
    prepare(str(root / "dist" / "ocr-v4"))
    resources(root)
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
                    str(root / "desktop" / "sidecar.spec")], cwd=root, check=True)


if __name__ == "__main__":
    main()
