# Douyin Downloader

本仓库新增了独立的抖音桌面工作台，支持内嵌登录、浏览选取、批量下载、任务恢复和自动去水印。开发、打包及验证说明见 [desktop/README.md](desktop/README.md)。这是当前仓库的实现，与下文原作者发布的 Douzy 安装包分别维护。

<p align="center">
  <img src="https://camo.githubusercontent.com/327409f4eab82516b28e9c67bd4831261917ee9a29ffd865a60996a9c53709cb/68747470733a2f2f736f6369616c6966792e6769742e63692f6a696a693236322f646f7579696e2d646f776e6c6f616465722f696d6167653f637573746f6d5f6465736372697074696f6e3d446f7579696e2b62617463682b646f776e6c6f61642b746f6f6c2532432b72656d6f76652b77617465726d61726b732532432b737570706f72742b62617463682b646f776e6c6f61642b6f662b766964656f732532432b67616c6c6572792532432b616e642b617574686f722b686f6d6570616765732e266465736372697074696f6e3d3126666f6e743d536f757263652b436f64652b50726f26666f726b733d31266f776e65723d31267061747465726e3d436972637569742b426f617264267374617267617a6572733d31267468656d653d4c69676874" alt="douyin-downloader" width="820" />
</p>

[简体中文](README.zh-CN.md) · **English**

Download Douyin videos and photos without watermarks. Use **Douzy** for a desktop interface, or the **Python CLI** for scripts.

## Douzy Desktop

A desktop app for **Douyin, TikTok, YouTube, Telegram, and X**. Paste links, track downloads, and browse your local archive.

**[Download for Windows / macOS](https://github.com/jiji262/douyin-downloader/releases)**

Platform features depend on enabled plugins or local components. Some batch and advanced features require activation.

| Platform | Content |
|:---|:---|
| Douyin | Videos, photos, profiles, collections; following, subscriptions, favorites, and likes |
| TikTok | Public videos, photos, and profiles |
| YouTube | Videos, Shorts, channels, and playlists; audio and subtitles |
| Telegram | Media from accessible chats, groups, and channels; requires setup and sign-in |
| X | Posts, profile media, your bookmarks and likes; requires account cookies |

| **Douyin** | **TikTok** | **YouTube** |
|:---:|:---:|:---:|
| [![Douzy Douyin download options](img/desktop/001.png)](img/desktop/001.png) | [![Douzy TikTok workspace](img/desktop/002.png)](img/desktop/002.png) | [![Douzy YouTube workspace](img/desktop/003.png)](img/desktop/003.png) |
| **Telegram setup** | **X** | **Platform switcher** |
| [![Douzy Telegram setup](img/desktop/004.png)](img/desktop/004.png) | [![Douzy X workspace](img/desktop/005.png)](img/desktop/005.png) | [![Douzy platform switcher](img/desktop/006.png)](img/desktop/006.png) |

_Screenshots: Douzy 0.11.6 on macOS. Telegram is shown before setup. Click an image to enlarge._

## CLI Status

The CLI supports **Douyin only**. It includes batch downloads, date filters, retries, download history, and optional comments, transcription, and notifications.

> **Download limitations:** Douyin's request verification currently blocks CLI downloads of individual videos/photos, collections, music, likes, and favorites. Profile posts can try the Playwright browser fallback, but success is not guaranteed. Use **Douzy** for everyday downloads.

Refreshing cookies or retrying does not fix this verification block. Live recording is experimental; HLS sources save a playlist, not a playable video.

## Quick Start

Requires **Python 3.9+** on Windows, macOS, or Linux. Read the CLI limitations above first.

### 1. Install

```bash
git clone https://github.com/jiji262/douyin-downloader.git
cd douyin-downloader
python -m pip install -r requirements.txt
python -m pip install playwright
python -m playwright install chromium
```

### 2. Configure and sign in

Copy [config.example.yml](config.example.yml) to `config.yml`:

```bash
cp config.example.yml config.yml
python -m tools.cookie_fetcher --config config.yml
```

On Windows PowerShell, use `Copy-Item config.example.yml config.yml` to copy the file. Sign in to Douyin in the browser, then return to the terminal and press Enter to save cookies.

Replace the sample `link` in `config.yml` with your creator's profile URL. Keep the saved cookies and adjust these fields:

```yaml
link:
  - https://www.douyin.com/user/YOUR_SEC_UID
path: ./Downloaded/
mode: [post]
number:
  post: 10                 # 0 = unlimited
increase:
  post: true              # skip downloaded items
redownload_missing_files: true
```

### 3. Run

```bash
python run.py -c config.yml
```

Use `python run.py --help` for all options.

| Option | Purpose |
|:---|:---|
| `-c, --config` | Config file |
| `-u, --url` | Append a URL to the links in the config; repeatable |
| `-p, --path` | Download folder |
| `-t, --thread` | Concurrent downloads |
| `-v, --verbose` | Detailed logs |

## Configuration

Full settings and examples: **[config.example.yml](config.example.yml)**.

| Setting | Purpose |
|:---|:---|
| `number.post` | Item limit; `0` means unlimited |
| `start_time` / `end_time` | Date range (`YYYY-MM-DD`); includes the whole end date |
| `video_quality` | `highest` by default; `original` tries the original upload and falls back |
| `increase.post` | `true` skips downloaded items; `false` downloads again and overwrites files in the selected scope |
| `redownload_missing_files` | Default `true`: download missing files again. With `false`, valid database history can still skip them |

Incremental downloads check for non-empty primary media on disk. You do **not** need to clear the database to download again: set `increase.post: false`. The same switch is available for `like`, `mix`, and `music`.

<details>
<summary>More CLI commands</summary>

```bash
# Hot search board and keyword search → JSONL
python run.py --hot-board 30
python run.py --search "cats" --search-max 50

# Optional REST API server
python -m pip install fastapi uvicorn
python run.py --serve --serve-port 8000
```

Comments, transcription, notifications, and live recording are configured in [config.example.yml](config.example.yml).

</details>

## FAQ

**Cookies expired?** Run `python -m tools.cookie_fetcher --config config.yml` again.

**Only a few profile posts?** Keep `browser_fallback.enabled: true` and `headless: false`. Complete any verification yourself in the browser; the current verification block may still prevent downloads.

**Need logs?** Run `python run.py -c config.yml -v`.

## Development

```bash
python -m pip install -e ".[dev]"
python -m pytest tests/
ruff check .
```

## Community & License

[QQ group](https://qm.qq.com/q/8wrCzYyLHa) · [LINUX DO](https://linux.do/) · [MIT License](LICENSE)

<img src="img/qq-group.png" alt="Community QR code" width="200" />

For learning and personal data management. Respect copyright, privacy, and platform rules. You are responsible for how you use this tool; platform changes may affect availability.

## Star History

[View GitHub Star History](https://www.star-history.com/?repos=jiji262%2Fdouyin-downloader&type=date&legend=top-left)

## 下载后自动去水印

默认开启本地 CPU 处理，支持 macOS 13+（Apple Silicon / Intel）和 Windows 10/11 x64，
依赖版本按 Python 3.9–3.13 选择。首次安装依赖后准备离线 OCR 模型：

```bash
python -m pip install -e ".[server,dev]"
python -m tools.watermark setup
```

模型使用固定 SHA256 的 PP-OCRv4 mobile 中英文模型，仅 setup 访问模型源。
视频处理不会上传媒体，也不会自动下载模型。模型缺失时仍可完成已知标记清理，结果为 `partial`。
默认模型目录为 `~/.cache/douyin-downloader/ocr-v4`，可通过 `watermark.model_dir` 配置。

下载完成后自动处理普通视频、图集 Live Photo、已合并回放和正常结束的 FLV 录制。
原片保持不变，旁边生成 `<stem>.clean.mp4` 和 `<stem>.cleanup.json`；过长文件名会缩短并带摘要。
非 MP4 输入保留扩展名参与副本命名（如 `live.flv.clean.mp4`），避免同名不同格式相互覆盖。
未命中目标时只生成报告，不复制视频。图片、音频文件和 HLS playlist 不进入清理。
清理队列与下载并发分离，默认一次处理一个文件。相同输入、规则、配置及作者提示可复用已校验结果。
重新下载时遇到已有原片，会补做清理；也可完全离线对文件或目录重试：

```bash
python -m tools.watermark clean "本地视频.mp4" --author "作者昵称"
python -m tools.watermark clean "Downloaded" --config config.yml
python -m tools.watermark clean "本地视频.mp4" --markers-only
```

`config.example.yml` 中的 `watermark` 配置控制开关。`strategy` 固定为 `aggressive`，
`keep_original` 必须为 `true`。`enabled: false` 可关闭全部处理。

能力边界：

- 精确移除匹配规则的 `vid:` comment、已知编码器元信息和 H.264/HEVC 的已知 UUID + payload SEI。
  保留未识别 SEI；使用 PyAV remux，不擦除 `mdat` 空隙或手改 sample offsets。
- OCR、外围持续结构及作者提示生成时序候选，逐帧局部跟踪后用 Telea 修复。
  复检仍命中时，最多从原始帧再处理一次。字幕默认排除；匹配作者的字幕重叠区域按激进策略处理。
  作者签名的低置信度跟踪会在临近检测位置做局部矩形修复，并在报告中记录 fallback 区域。
  此操作涉及字幕区域时仍按激进策略执行，但会标记 `partial` 并报告字幕受损风险。
  可能误删画面文字、漏检短暂/低对比度标记，也可能留下模糊、闪烁或修复痕迹。
  残留 OCR 复检集中在已有候选周边，自动复检未命中仍不等于肉眼确认无残留。
- 可见修复支持 H.264/HEVC 的 8-bit YUV420 SDR，输出 H.264 CRF 18 / veryfast，复制音频。
  HDR / 高位深只处理可验证的已知标记，并报告可见处理未完成。
  带旋转矩阵或额外数据轨的媒体报告 `unsupported`，避免丢失显示属性。
- 无法保证消除未知像素域、频域、音频盲水印；报告中的 `unassessed` 明确记录未覆盖项。
  `not_matched` 只表示未检测到规则目标，不能证明视频不存在水印。

清理产物提交前完整解码，核对帧数、时间线、音频包以及无损路径的解码像素哈希。
源文件、输出文件哈希、候选、逐帧实际区域、残留候选、验证结果写入独立 JSON 报告。
正起始 PTS 的 MP4 会额外检查 sample table 的原始时长，报告单独记录 demux 尾帧时长差。
同名清理副本被用户修改后不会自动覆盖；超时等启动层失败另存 `.cleanup.failure.json`。
失败不会替换原片；取消会终止 Worker 并清理临时目录。`.wm-lock-*` 是保留的内核锁载体，
文件存在不表示任务仍在运行。

CLI 汇总和通知单独展示清理计数。REST 的下载请求仍只需要 `url`，任务结果新增 `cleanup`
（`counts`、`items`、`incomplete`）。下载成功但清理失败、不支持或残留时，任务终态为 `partial`；
下载失败仍为 `failed`。无数据库 schema 变更，manifest 增加清理报告引用及完成记录。

新增依赖：PyAV 负责封装与时间戳，OpenCV/NumPy 负责跟踪与局部修复，RapidOCR/ONNX Runtime
负责 CPU 文本识别。未引入 `video_scan_clean` 的受限授权源码。
