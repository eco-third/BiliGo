# BiliGo

B 站视频下载器 —— Python + CustomTkinter 暗色 GUI，下载引擎基于 [yt-dlp](https://github.com/yt-dlp/yt-dlp)。

支持解析视频信息、多 P 批量下载、随视频变化的清晰度档位、扫码登录、弹幕转 ASS **并把弹幕烧进画面生成第二个视频**、CC 字幕转 SRT，以及 FFmpeg / aria2c 加速。

> 仅供个人学习与本地备份使用。请遵守哔哩哔哩用户协议与相关版权法规，不要用于二次分发。

---

## 下载

不想装 Python 的话，直接拿打包好的 Windows x64 版本（解压即用，已内置 FFmpeg 与 aria2c）：

| 版本 | 说明 |
| --- | --- |
| [v1.2.0](https://github.com/eco-third/BiliGo/releases/tag/v1.2.0) | 当前版本。新增「弹幕版视频」输出、弹幕外观对齐 B 站播放器、进度条重做、清晰度档位随视频变化 |
| [v1.1.0](https://github.com/eco-third/BiliGo/releases/tag/v1.1.0) | 加入 aria2c 多连接加速、浏览器 Cookie 导入、友好错误提示 |
| [v1.0.0](https://github.com/eco-third/BiliGo/releases/tag/v1.0.0) | 首个公开版本。无 aria2c 加速，仅支持扫码登录 / 手动 SESSDATA |

每个 Release 都附 `*-win64.zip`（程序 + FFmpeg + aria2c）与 `*-source.zip`（源码备份）。
Release 资产里**不含任何 Cookie**：`config/config.json` 与 `config/cookies.txt` 在首次运行时生成。

> 仓库 `main` 分支对应 v1.2.0 源码，且额外加了本 README 与 `.gitignore`；`*-source.zip` 是原始源码快照。

---

## 功能

**解析与下载**

- 链接识别：`bilibili.com/video/BV...`、`av` 号、`b23.tv` 短链、`bangumi/play` 番剧
- **清晰度档位随视频变化**：解析后按 B 站自己的 `qn` 编号生成下拉框，和播放器里看到的一致
  （`8K 超高清 / 杜比视界 / HDR 真彩 / 4K 超清 / 1080P60 / 1080P 高码率 / 1080P / 720P60 / 720P / 480P / 360P`，视频没有的档位不会出现）
  - 解析时用 `ydl.build_format_selector()` + 视频自带档位表决定 `format`，选哪档就真下哪档
  - 有 FFmpeg 时走 `bestvideo+bestaudio` 合并流，无 FFmpeg 时降级为单文件流
- 竖屏视频：画质按「宽高较小者」判断档位（480×852 是 480P，不是 720P），竖屏 4K 也能正确落到 2160×3840
- 多 P 视频：3 列紧凑分P列表（缩略图 + 时长 + 全选/取消全选 + 限高滚动），只下载勾选的 P
- 解析结果缓存：单 P 视频把 yt-dlp 的 `info` dict 缓存下来，点下载时跳过第二次 `extract_info`

**弹幕版视频（v1.2 新增）**

勾选「下载弹幕（含弹幕版）」后，下载目录里会有三个文件：

```
标题.mp4            无弹幕原片（内容不改）
标题.ass            弹幕字幕，可给 PotPlayer / VLC 挂载
标题.弹幕版.mp4     弹幕烧进画面，发给谁都能看
```

- 烧录用 FFmpeg `libx264 -preset veryfast -crf 20`，音频直接 copy；界面有进度百分比，可随时取消
- **HDR 源先 tonemap 到 SDR 再烧**（`zscale → tonemap=hable → bt709`），否则白字会发灰
- 标题里的中文、空格、`“”`、逗号不会破坏滤镜参数：ASS 先复制成临时 ASCII 文件名再交给 `ass=` 滤镜

**弹幕外观（对齐 B 站播放器）**

| 参数 | 取值 | 依据 |
| --- | --- | --- |
| 字号 | `25 × 视频高 / 720` | 弹幕 XML 的 `fontsize` 基准值是 25，播放器按高度等比放大（1080p→37.5，4K→75） |
| 字体 | 微软雅黑 | B 站播放器默认 |
| 描边 | `max(字号/25, 1)`，无阴影 | 720p 上等于 1px |
| 行高 | `字号 × 1.3`，最多 18 行 | 留 1/3 字号空隙 |
| 滚动 | 恒定像素速度，横穿约 8 秒 | 长弹幕停留更久，和 B 站一致 |
| 模式 | 1/2/3 滚动、4 底部、5 顶部、6 逆向、7/8 跳过 | B 站弹幕 XML 的模式定义 |

- 颜色按 B 站 `0xRRGGBB` → ASS `&HBBGGRR&` 转换
- 滚动弹幕从右边缘外侧进场，**完全移出左边缘才结束**（长弹幕不会在屏幕中间消失）
- 轨道按「上一条完全进场」释放，3600 条弹幕实测能保留约 75%（B 站满屏时也会丢，属正常）
- 拉取 `x/v1/dm/list.so` 时按 (时间, 模式, 文本) 去重 —— B 站会忽略 `segment_index`，不去重会存成好几倍

**下载加速与稳定性**

- 优先用外置 aria2c（`aria2/aria2c.exe` → 根目录 → PATH），16 连接 / 16 分片 / 1M 分片 / 重试 5 次
- **进度条直接读 aria2c 自己的输出**：把 aria2c 的 stdout 接成管道逐行解析
  `[#gid 9.1MiB/22MiB(40%) CN:4 DL:1.6MiB]`，百分比是 aria2 的真值。
  yt-dlp 用外置下载器时只在每个流结束时回调一次，靠它拿不到下载中的进度
- 无 aria2c 时回落到内置下载器：并发分片、`throttledratelimit`（低于 100KB/s 持续 30s 视为限速，自动换源）、重试 10 次
- 遇到 Windows 句柄类故障（`WinError 6` 等）会自动关掉 aria2c 用内置下载器重下一次，不会直接失败

**登录**

- 扫码登录（`passport-login/web/qrcode/generate` + `poll`），成功后落地 `SESSDATA / bili_jct / DedeUserID`
- 备选：手动粘贴 `SESSDATA`，或用 yt-dlp 的 `cookiesfrombrowser` 直接读 Edge / Chrome
- 大会员检测：`x/web-interface/nav` 的 `vipType / vipStatus`

**字幕**

- CC 字幕：`x/player/v2` 取列表（只认 `bvid+cid` 或 `aid+cid`，番剧要先解析出 bvid）→ 转 SRT
- 番剧与普通投稿都能拿到；弹幕/字幕文件与视频**同目录同名**保存

**其它**

- FFmpeg 一键安装：下载 gyan.dev essentials 包，只解压 `ffmpeg.exe` 到 `ffmpeg/`
- 便携化：配置、FFmpeg、默认下载目录都跟着 exe 所在目录走（`sys.frozen`），不写注册表
- 全中文暗色 UI，B 站粉 `#FB7299` → 紫 `#9B7BFF` 渐变

---

## 目录结构

```
main.py                 入口，处理 PyInstaller 打包后的路径（含 --selftest 自检）
BiliGo.spec             PyInstaller 打包配置
requirements.txt        依赖
biligo/
├── app.py              CustomTkinter 主界面（窗口、多P列表、进度、设置对话框、二维码内嵌）
├── config.py           配置持久化、清晰度档位表（qn）、FFmpeg 查找与安装、Cookie 文件生成
├── downloader.py       yt-dlp 封装：URL 识别、格式选择、aria2c 接入、进度管道、画质回读
├── burn.py             ASS 烧进画面（HDR tonemap、字号补偿、进度上报、可取消）
├── auth.py             二维码登录、SESSDATA 校验、视频详情/分P API
├── danmaku.py          弹幕 XML 解析 → ASS（B 站字号公式、轨道排布、颜色、模式）
└── subtitle.py         CC 字幕列表 → JSON → SRT
```

---

## 运行

需要 Python 3.8+（Windows 为主要目标平台）。

```bash
pip install -r requirements.txt
python main.py
```

首次启动会在程序目录下自动创建 `config/config.json`、`config/cookies.txt`、`ffmpeg/`、`Downloads/`。

外部依赖（可选，缺了会自动降级）：

- **FFmpeg** —— 缺失时只能下已合并的流，画质受限，且无法生成弹幕版。可在设置页一键安装
- **aria2c** —— 缺失时用内置下载器，速度通常慢一些，进度改用 yt-dlp 自己的回调（同样准确）

---

## 打包

```bash
pip install -r requirements.txt pyinstaller
pyinstaller --noconfirm --clean BiliGo.spec
```

`BiliGo.spec` 里已经处理好几件事：`yt_dlp` / `customtkinter` 的数据文件用 `collect_data_files` 收进来、
`qrcode` 的 `image.pil` 这类条件导入用 `collect_submodules` 显式带上、`Crypto`（浏览器 Cookie 解密）列进 hiddenimports。
打包完是单文件、无控制台窗口。

> 无控制台的 exe 里 `sys.stdout/stderr` 是 `None`，`main.py` 会把它们接到 `os.devnull` ——
> 否则 yt-dlp 一往里写就 `AttributeError`，表现为「双击能开、一下载就报错」。

打包后可以自检：

```bash
BiliGo.exe --selftest            # 构造一次主界面并退出，结果写入 selftest.log
BiliGo.exe --selftest-download   # 走一遍 下载 → 弹幕 ASS → 烧录，验证子进程路径
```

---

## 已知限制

- 多 P 视频（合集）下载时仍按「分辨率映射」选流，因为 flat 模式拿不到每个分P的完整格式表；单 P 不受影响
- 无 FFmpeg 时退回分辨率映射，且不能生成弹幕版
- HDR 源生成的弹幕版是 SDR（不 tonemap 的话弹幕文字会发灰）
- 未登录访问大会员内容时 B 站只给试看片段（如 180 秒），这不是程序的问题
- 大会员画质的可用性取决于 B 站 API 在当前 Cookie 下是否返回 4K/HDR 格式，程序只做提示不做绕过
- 仅针对 B 站，其它站点虽然 yt-dlp 支持，但 URL 校验会先拦下来
- 仓库内没有单元测试；弹幕渲染是按像素逐项校对过的（字号 / 颜色 / 行距 / 速度 / 完整离屏），校对脚本不随仓库发布
