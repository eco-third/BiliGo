# BiliGo

B 站视频下载器 —— Python + CustomTkinter 暗色 GUI，下载引擎基于 [yt-dlp](https://github.com/yt-dlp/yt-dlp)。

支持解析视频信息、多 P 批量下载、4K 画质选择、扫码登录、弹幕转 ASS、CC 字幕转 SRT，以及 FFmpeg / aria2c 加速。

> 仅供个人学习与本地备份使用。请遵守哔哩哔哩用户协议与相关版权法规，不要用于二次分发。

---

## 功能

**解析与下载**

- 链接识别：`bilibili.com/video/BV...`、`av` 号、`b23.tv` 短链、`bangumi/play` 番剧
- 画质档位：4K 超清 / 1080P 高码率 / 1080P / 720P / 480P / 360P
  - 有 FFmpeg 时走 `bestvideo+bestaudio` 合并流，无 FFmpeg 时自动降级为单文件流（`Config.QUALITY_MAP_MERGE / _NO_MERGE`）
- 多 P 视频：3 列紧凑分P列表（缩略图 + 时长 + 全选/取消全选 + 限高滚动），只下载勾选的 P
- 解析结果缓存：单 P 视频把 yt-dlp 的 `info` dict 缓存下来，点下载时跳过第二次 `extract_info`，省一轮等待
- 实际画质回读：下载完成后从 `requested_downloads` / `requested_formats` / `entries` 依次尝试，把真实分辨率写进日志

**登录**

- B 站 Web 二维码扫码登录（`passport-login/web/qrcode/generate` + `poll`），成功后自动落地 `SESSDATA / bili_jct / DedeUserID`
- Cookie 提取有四重回退：轮询 session cookies → response cookies → `Set-Cookie` 原文 → 重定向 URL 里的 query
- 备选：手动粘贴 `SESSDATA`，或用 yt-dlp 的 `cookiesfrombrowser` 直接读 Edge / Chrome
- 大会员检测：`x/web-interface/nav` 的 `vipType / vipStatus`；未登录且最高画质 < 2160p 时给出「4K 需要大会员」提示

**弹幕与字幕**

- 弹幕：拉 `x/v1/dm/list.so` XML（按 `segment_index` 分段，1–3），解析 `p` 属性，按 18 条轨道做碰撞排布，输出 ASS
  - 滚动弹幕 mode 1/2/3 → `\move` + 12s；顶部 mode 5 / 底部 mode 6 → `\pos` + 5s
  - 颜色按 BGR 转 ASS，字号按 B 站原始字号 /25 缩放
- CC 字幕：`x/player/v2` 取字幕列表 → `aisubtitle.hdslb.com` 的 JSON → 转 SRT，多语言逐个保存

**下载加速**

- 优先用外置 aria2c（`aria2/aria2c.exe` → 根目录 → PATH），参数 16 连接 / 16 分片 / 1M 分片，失败重试 5 次
- 无 aria2c 时回落到内置下载器：并发分片、`throttledratelimit`（低于 100KB/s 持续 30s 视为被限速，自动换源）、重试 10 次、10MB HTTP 分块
- `_friendly_error` 把 yt-dlp 的原始报错翻译成「需要大会员 / 付费内容 / 地区限制 / 解析失败」这类可读提示

**其它**

- FFmpeg 一键安装：下载 gyan.dev essentials 包，只解压 `ffmpeg.exe` 到 `ffmpeg/` 目录
- 便携化：配置、FFmpeg、默认下载目录都跟着 exe 所在目录走（`sys.frozen` 判断），不写注册表
- 全中文暗色 UI，B 站粉 `#FB7299` → 紫 `#9B7BFF` 渐变作为品牌元素

---

## 目录结构

```
main.py                 入口，处理 PyInstaller 打包后的路径
requirements.txt        依赖
biligo/
├── app.py              CustomTkinter 主界面（窗口、多P列表、进度、设置对话框、二维码内嵌）
├── config.py           配置持久化（config/config.json）、画质映射、FFmpeg 查找与安装、Cookie 文件生成
├── downloader.py       yt-dlp 封装：URL 识别、格式选择、aria2c 接入、进度回调、画质回读
├── auth.py             二维码登录、SESSDATA 校验、视频详情/分P API
├── danmaku.py          弹幕 XML 解析 → ASS 生成（轨道排布、颜色、时间轴）
└── subtitle.py         CC 字幕列表 → JSON → SRT
```

---

## 运行

需要 Python 3.8+（Windows 为主要目标平台）。

```bash
pip install -r requirements.txt
python main.py
```

> **注意**：`biligo/auth.py` 直接 `import qrcode`，但当前 `requirements.txt` 未列出该包。
> 全新环境请额外执行 `pip install qrcode`（连同 `Pillow` 一起生成二维码图片），否则扫码登录会 `ModuleNotFoundError`。

首次启动会在程序目录下自动创建：

| 路径 | 用途 |
| --- | --- |
| `config/config.json` | 全部设置（画质、下载目录、并发数、Cookie 来源等） |
| `config/cookies.txt` | SESSDATA 写成的 Netscape 格式 Cookie 文件，供 yt-dlp 使用 |
| `ffmpeg/` | FFmpeg 一键安装的落地目录 |
| `Downloads/` | 默认下载目录 |

外部依赖（可选，缺了会自动降级）：

- **FFmpeg** —— 缺失时只能下已合并的流，画质受限。可在设置页一键安装，也可自行放到 `ffmpeg/` 或 PATH
- **aria2c** —— 缺失时用内置下载器，速度通常慢一些

---

## 打包

```bash
pyinstaller --noconsole --name BiliGo ^
  --collect-all yt_dlp ^
  --collect-all customtkinter ^
  --collect-submodules qrcode ^
  main.py
```

yt-dlp 与 customtkinter 都带数据文件（extractor 清单、主题 json），必须用 `--collect-all`；
qrcode 的 `image.pil` 是条件导入，需要用 `--collect-submodules` 显式带上。

---

## 已知限制

- 没有单元测试，也没有 CI；多 P 视频的格式探测依赖「先 flat 取列表、再单 P 取 formats」两次请求
- `app.py::_get_cid()` 在拿不到 `cid` 时返回 `0`，此时弹幕与字幕会被跳过（日志里会提示「无法获取 CID」）
- 大会员画质的可用性完全取决于 B 站 API 是否在该 Cookie 下返回 4K 格式，程序里只做提示不做绕过
- 仅针对 B 站，其它站点虽然 yt-dlp 支持，但 URL 校验会先拦下来
