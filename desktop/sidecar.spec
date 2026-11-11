# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

root = Path(SPECPATH).parent
datas, binaries, hiddenimports = [], [], []
for package in ("rapidocr", "onnxruntime", "cv2", "av", "imageio_ffmpeg", "certifi", "gmssl"):
    package_data, package_binaries, package_imports = collect_all(package)
    datas += package_data
    binaries += package_binaries
    hiddenimports += package_imports
hiddenimports += collect_submodules("core.user_modes")
hiddenimports += collect_submodules("uvicorn")
hiddenimports += ["websockets.legacy.server", "server.desktop_main", "core.watermark.worker"]
a = Analysis([str(root / "server" / "desktop_main.py")], pathex=[str(root)],
             binaries=binaries, datas=datas, hiddenimports=hiddenimports,
             excludes=["torch", "tensorflow", "matplotlib", "IPython", "pytest", "playwright"],
             noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="douyin-sidecar", debug=False,
          bootloader_ignore_signals=False, strip=False, upx=False, console=True)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="douyin-sidecar")
