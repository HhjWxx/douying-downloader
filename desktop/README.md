# 抖音桌面下载工作台

当前仓库内独立实现的桌面端，复用 Python 下载和自动处理能力。参考 Douzy 公开交互及当前仓库的桥接接口，未引入原作者未公开的桌面源码。

## 使用

安装对应系统的安装包，打开“浏览抖音”，在内嵌页面登录。打开具体视频或作者主页后使用上方下载按钮，也可以从右侧“页面中的作品”勾选下载。“我的内容”提供收藏、收藏夹和喜欢列表；“下载任务”支持取消、继续和查看处理报告。

- 应用有独立登录会话，不会自动继承 Chrome 的登录。需要验证码时打开“登录 / 验证窗口”，由用户完成后继续任务。
- 推荐和搜索页收集已加载作品；作者、合集和我的内容会分页读取。无法确定当前作品时从列表选择，不自动猜测。
- 默认作者最近 20 条、最高可用画质、下载并发 2、处理并发 1。“全部作品”需要明确选择。
- 默认保存至系统下载目录的 `DouyinDownloader`，可在设置中修改。应用数据位于 Electron 的系统用户数据目录，升级和卸载不删除下载文件。
- 自动处理保留原片；`partial` 表示处理不完整或存在字幕误伤风险。未知暗水印无法保证清除。图文下载不执行视频去水印。
- 中断后按作品恢复，未完成的单个文件重新下载。重新登录为另一个账号后，旧账号任务不能直接继续，应重新选择内容。
- 任务分别显示下载与去水印进度。下载显示大小、速度和百分比；去水印显示当前阶段的实际帧数或媒体时间进度，以及已耗时。阶段切换会重新计数；总量未知时显示活动条，不估算整项任务的百分比。
- 继续任务保留原来的下载目录和筛选选项，网络代理使用当前设置。
- 网络提供直连和 HTTP/HTTPS 代理。使用 Clash 时可以配置其 HTTP/混合端口；浏览器和下载服务使用同一设置。诊断 CDN 前先浏览一个视频。
- “导入 CLI 配置与历史”复制非敏感配置及历史记录，原文件不修改、视频不移动，Cookie 不导入。

## 开发

在仓库根目录安装 Python 依赖，桌面构建使用 Python 3.13、Node 24：

```sh
python -m venv .venv
# macOS
.venv/bin/python -m pip install -e '.[dev]' -r desktop/requirements-build.txt -c desktop/constraints.txt
.venv/bin/python -m tools.watermark setup
# Windows 将上面的解释器路径替换为 .venv\Scripts\python.exe
npm --prefix desktop ci
cd desktop
npx install-electron --no
npm run dev
```

默认使用仓库 `.venv`，可通过 `DOUYIN_PYTHON` 指定开发用解释器。开发时可设置 `DOUYIN_DESKTOP_DATA_DIR` 使用独立测试目录；正式安装包忽略这些开发覆盖项。独立 CLI/REST 服务保持原来的启动方式。

## 验证和打包

在目标系统原生构建，不能将 Mac 的 sidecar 装进 Windows 包。

```sh
python -m pytest tests/ -q
python -m ruff check .
python -m compileall -q core config cli server tools
npm --prefix desktop test
npm --prefix desktop run test:e2e
python -m tools.build_desktop
python -m tools.check_desktop dist/douyin-sidecar/douyin-sidecar --model-dir dist/ocr-v4
npm --prefix desktop run package -- --arm64
```

Windows 自检使用 `dist/douyin-sidecar/douyin-sidecar.exe`；Windows 和 Intel Mac 打包使用 `--x64`。安装包输出至 `desktop/release/`。本机已下载 Electron 时，可给打包命令追加 `--config.electronDist=node_modules/electron/dist` 避免再次下载运行时。

PyInstaller 包含 Python、媒体库、OCR 引擎、CA 证书，OCR 模型及许可声明随应用分发。macOS 使用 ad-hoc 签名，未做 Developer ID 公证；Windows 未配置商业签名。当前为手动升级，不依赖更新服务。

推送代码后 CI 默认构建 Windows x64；手动运行 Desktop build and acceptance 并选择 `platform: all` 可同时构建 macOS arm64/x64。每个平台运行后端测试、界面测试、冻结服务和离线媒体自检后生成安装包。Media cleanup 多版本矩阵按需手动运行，避免与桌面构建重复占用 runner。自动化测试不访问真实抖音，真实账号、分页和媒体下载需单独验收；构建成功不代表真实平台接口已经通过验收。

## 结构与接口

- Electron 主进程管理可见页面、同会话验证窗口、受限 IPC 和 Python 子进程。本地界面使用 React，远程页面禁用 Node 集成。
- sidecar 在 loopback 随机端口启动，每次启动使用独立令牌；WebSocket 负责页面请求与任务事件。抖音响应原文交由 Python 解析，跨语言 ID 保持字符串。
- 桌面使用 `POST /api/v1/jobs` 提交目标及独立选项；旧 `POST /api/v1/download` 继续接收 URL。任务表复用现有 `job` 表，新增 `desktop_payload` 保存恢复信息；作品仍使用 `aweme` 表。
- `server.desktop_main` 同时提供冻结 Worker 子命令，避免打包后错误地使用 `python -m`。去水印算法及原片保护逻辑沿用当前实现。
- 当前机器没有原作者的 sibling 桌面仓库。此次桌面实现全部放在当前仓库，未执行不存在的 sibling 同步脚本。
