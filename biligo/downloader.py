"""BiliGo 下载引擎 - 基于 yt-dlp 封装"""

import copy
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import yt_dlp

from .config import quality_label


def build_quality_options(formats: list) -> list:
    """把 yt-dlp 的 formats 汇总成 B 站播放器那样的清晰度列表。

    B 站每个清晰度档位（qn）下面通常有好几条编码（avc / hevc / av1），
    播放器只显示一个档位名字，所以这里按「B 站显示名」合并，
    同一档位取码率最高的那条流。

    返回（按清晰度从高到低）：
        [{"label": "4K 超清", "qn": 122, "format_id": "100198",
          "width": 3840, "height": 2160, "fps": 25.0,
          "dynamic_range": "SDR", "tbr": 16594.9, "filesize": 2644477871}, ...]
    """
    best = {}
    for fmt in formats or []:
        if (fmt.get("vcodec") or "none") == "none":
            continue
        qn = int(fmt.get("quality") or 0)
        width = int(fmt.get("width") or 0)
        height = int(fmt.get("height") or 0)
        fps = float(fmt.get("fps") or 0)
        dyn = (fmt.get("dynamic_range") or "SDR")
        label = quality_label(qn, width, height, fps, dyn)
        tbr = float(fmt.get("tbr") or 0)
        size = int(fmt.get("filesize") or fmt.get("filesize_approx") or 0)
        # 同一档位若同时有多种帧率（例如 4K 25fps / 4K 50fps），不能合并，否则会丢档；
        # 先按 (名字, 帧率) 归并，最后再决定要不要在名字后面标帧率。
        key = (label, round(fps))
        cur = best.get(key)
        if cur is None or tbr > cur["tbr"]:
            best[key] = {
                "label": label,
                "qn": qn,
                "format_id": fmt.get("format_id", ""),
                "width": width,
                "height": height,
                "fps": fps,
                "dynamic_range": dyn,
                "tbr": tbr,
                "filesize": size,
            }

    # 同名但帧率不同 → 在名字后补帧率，避免出现两个一模一样的选项
    fps_by_label = {}
    for (label, fps), _ in best.items():
        fps_by_label.setdefault(label, set()).add(fps)
    for item in best.values():
        if len(fps_by_label.get(item["label"], set())) > 1 and item["fps"]:
            item["label"] = f"{item['label']} {round(item['fps'])}fps"

    return sorted(best.values(), key=lambda o: (o["qn"], o["tbr"]), reverse=True)


# ── aria2c 进度采集 ──
# yt-dlp 用外置下载器（aria2c）时，progress_hooks 每个流只在结束时回调一次
# finished，全程不发 downloading，所以进度条拿不到数据。
#
# 唯一准确又平滑的信号是 aria2c 自己打印的进度行：
#     [#41d514 9.1MiB/22MiB(40%) CN:4 DL:1.6MiB ETA:8s]
# 它的百分比和字节数都是真值 —— 比读 .part 文件可靠得多（多连接乱序写会把
# 文件长度顶到真实进度前面，表现就是「一下冲到 90% 再慢慢爬」）。
#
# 取法：把 yt-dlp 里 aria2c 的进程调用换成「stdout 走管道」，自己读管道解析。
# 千万不要再去改本进程的 fd 1（以前那么干过）：那会把 Python 的标准句柄换成
# 临时日志文件，子进程继承、事后再还原，在 Windows 上会偶发
# `[WinError 6] 句柄无效`，整个下载直接失败。

_ARIA2_PROGRESS_RE = re.compile(
    r"([\d.]+[KMGT]?i?B)/([\d.]+[KMGT]?i?B)\((\d+)%\)"
    r"(?:[^\]]*?DL:([\d.]+[KMGT]?i?B))?"
)

_SIZE_UNITS = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3,
               "T": 1024 ** 4, "P": 1024 ** 5}


def parse_aria2_size(text: str) -> int:
    """把 aria2 的 '9.1MiB' / '784KiB' / '1.2GiB' 转成字节数"""
    m = re.match(r"([\d.]+)\s*([KMGT]?)i?B", text or "", re.I)
    if not m:
        return 0
    try:
        value = float(m.group(1))
    except ValueError:
        return 0
    return int(value * _SIZE_UNITS[(m.group(2) or "").upper()])


class Aria2Capture:
    """线程安全的进度快照：读管道的线程写，界面轮询读。"""

    def __init__(self):
        self._lock = threading.Lock()
        self.bytes = 0        # 当前这条流已下载字节
        self.percent = 0      # 当前这条流的百分比（aria2 原值）
        self.speed = 0.0
        self.lines = 0        # 收到过多少条进度行
        self.updated = 0.0    # 最后一条进度行的时间

    def feed_line(self, line: str):
        found = None
        for found in _ARIA2_PROGRESS_RE.finditer(line):
            pass
        if found is None:
            return
        with self._lock:
            self.bytes = parse_aria2_size(found.group(1))
            try:
                self.percent = int(found.group(3))
            except ValueError:
                pass
            speed = parse_aria2_size(found.group(4) or "")
            if speed:
                self.speed = float(speed)
            self.lines += 1
            self.updated = time.time()

    def reset(self):
        """换流时清零：aria2c 是一条流一个进程，新流的读数要从 0 重新算"""
        with self._lock:
            self.bytes = 0
            self.percent = 0
            self.speed = 0.0

    def snapshot(self):
        with self._lock:
            return self.bytes, self.percent, self.speed, self.updated


_ARIA2_SINK = None          # 当前下载的 Aria2Capture；None 表示不采集
_CAPTURE_INSTALLED = False


def install_aria2_capture() -> bool:
    """把 yt-dlp 里 aria2c 的进程调用换成「读 stdout 管道」，返回是否可用。"""
    global _CAPTURE_INSTALLED
    if _CAPTURE_INSTALLED:
        return True
    try:
        from yt_dlp.downloader.external import Aria2cFD, ExternalFD
    except Exception:
        return False

    orig_call_process = ExternalFD._call_process

    def _call_process(self, cmd, info_dict):
        sink = _ARIA2_SINK
        if sink is None:
            return orig_call_process(self, cmd, info_dict)
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        except OSError:
            # 开不了管道就退回原实现：没有细进度，但下载照常
            return orig_call_process(self, cmd, info_dict)

        out_chunks, err_chunks = [], []

        def pump_stdout():
            buf = ""
            while True:
                try:
                    data = proc.stdout.read(1024)
                except Exception:
                    break
                if not data:
                    break
                out_chunks.append(data)
                text = buf + data.decode("utf-8", "replace")
                # aria2 的进度回显用 \r 覆盖同一行，不能按 \n 切
                parts = re.split(r"[\r\n]+", text)
                buf = parts.pop()
                for line in parts:
                    try:
                        sink.feed_line(line)
                    except Exception:
                        pass

        def pump_stderr():
            while True:
                try:
                    data = proc.stderr.read(1024)
                except Exception:
                    break
                if not data:
                    break
                err_chunks.append(data)

        t_out = threading.Thread(target=pump_stdout, daemon=True)
        t_err = threading.Thread(target=pump_stderr, daemon=True)
        t_out.start()
        t_err.start()
        try:
            returncode = proc.wait()
        finally:
            t_out.join(timeout=5)
            t_err.join(timeout=2)
        return (b"".join(out_chunks).decode("utf-8", "replace"),
                b"".join(err_chunks).decode("utf-8", "replace"),
                returncode)

    Aria2cFD._call_process = _call_process
    _CAPTURE_INSTALLED = True
    return True


class DownloadProgress:
    """下载进度信息"""

    def __init__(self):
        self.status = "idle"         # idle / extracting / downloading / merging / done / error
        self.percent = 0.0
        self.speed = 0.0             # bytes/s
        self.speed_str = ""
        self.downloaded = 0          # bytes
        self.total = 0               # bytes (可能为0未知)
        self.eta = 0                 # 秒
        self.current_file = ""
        self.error_msg = ""
        self.actual_quality = ""     # 实际下载清晰度（含分辨率/帧率/HDR）
        self.output_path = ""        # 实际下载落地的文件（弹幕/字幕要放在它旁边）
        self.output_paths = []       # 多P时为多个文件
        self.available_qualities = []  # 可用画质列表
        self.vip_required = False      # 是否需要大会员
        self.vip_hint = ""             # VIP 提示


class BiliDownloader:
    """Bilibili 视频下载器，基于 yt-dlp"""

    # Bilibili URL 匹配
    BILI_URL_PATTERN = re.compile(
        r"(https?://)?(www\.)?bilibili\.com/video/(BV[\w]+|av\d+)"
        r"|b23\.tv/\w+"
        r"|biligame\.com"
        r"|www\.bilibili\.com/bangumi/play/\w+",
        re.IGNORECASE,
    )

    def __init__(self, config):
        self.config = config
        self.progress = DownloadProgress()
        self._cancel_flag = False
        self._ydl = None
        self._last_info = None
        self._cached_raw_info = None   # 缓存 yt-dlp 原始 info dict
        self._cached_url = None         # 缓存对应的 URL

        # ── 外置下载器（aria2c）的进度监控 ──
        # yt-dlp 用外置下载器时只在结束时回调一次 progress_hooks，见下方 _watch_progress 注释
        self._using_aria2c = False
        self._watch_stop = threading.Event()
        self._watch_thread = None
        self._aria2_files_done = 0     # 已完成的流数量（视频流/音频流各算一个）
        self._aria2_files_expect = 0   # 预期流数量（多P 时用来估算百分比）
        self._finished_bytes = 0       # 已完成的流累计字节（来自 finished 回调）
        self._closed_bytes = 0         # 兜底方案：已下完的 .part 累计字节
        # 主方案：读 aria2c 的 stdout 管道
        self._capture = None
        # 句柄类故障后关掉 aria2c 重试（只重试一次）
        self._aria2_disabled = False
        self._aria2_retry_done = False

    @staticmethod
    def is_bilibili_url(url: str) -> bool:
        """判断是否为有效的 Bilibili 视频链接"""
        return bool(BiliDownloader.BILI_URL_PATTERN.search(url.strip()))

    def _find_aria2c(self) -> str:
        """查找 aria2c 可执行文件路径

        搜索顺序：
        1. exe 同目录下的 aria2 子目录（便携版）
        2. exe 同目录下直接放（便携版）
        3. 系统 PATH

        Returns:
            aria2c.exe 完整路径，未找到返回空字符串
        """
        # 开发模式/打包模式取应用根目录
        if getattr(os.sys, 'frozen', False):
            app_dir = os.path.dirname(os.sys.executable)
        else:
            app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

        # 1. aria2 子目录
        candidate = os.path.join(app_dir, "aria2", "aria2c.exe")
        if os.path.isfile(candidate):
            return candidate

        # 2. 根目录
        candidate = os.path.join(app_dir, "aria2c.exe")
        if os.path.isfile(candidate):
            return candidate

        # 3. 系统 PATH
        found = shutil.which("aria2c")
        if found:
            return found

        return ""

    @staticmethod
    def normalize_url(url: str) -> str:
        """标准化 URL（处理短链接等）"""
        url = url.strip()
        if url.startswith("b23.tv/") or url.startswith("https://b23.tv/"):
            if not url.startswith("http"):
                url = "https://" + url
        return url

    def _build_opts(self, for_info=False, playlist_items=None):
        """构建 yt-dlp 选项"""
        # 每次构建都重置，避免上一次的状态残留
        self._using_aria2c = False
        opts = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": False,
        }

        # 解析信息时使用 extract_flat 加速多P视频列表获取
        if for_info:
            opts["extract_flat"] = "in_playlist"

        # Cookie
        cookie_opt = self.config.get_ytdlp_cookie_option()
        opts.update(cookie_opt)

        # FFmpeg 位置
        ffmpeg_path = self.config.get_ffmpeg_path()
        if ffmpeg_path:
            opts["ffmpeg_location"] = os.path.dirname(ffmpeg_path)

        if for_info:
            return opts

        # 画质格式：根据 FFmpeg 可用性自动选择
        quality = self.config.get("quality", "4K 超清")
        fmt = self.config.get_format_for_quality(quality)
        opts["format"] = fmt

        # 多P选择
        if playlist_items:
            opts["playlist_items"] = ",".join(str(i) for i in playlist_items)
        elif playlist_items is None:
            # 未指定时默认下载全部
            pass
        else:
            # playlist_items=[] 表示空列表
            opts["playlist_items"] = "0"  # 不下载任何P

        # ── 下载加速策略 ──
        # 优先使用 aria2c 外部下载器（多连接并行，速度远超内置下载器）
        # 出过一次「句柄无效」就不再走外部进程，改用内置下载器
        aria2c_path = "" if self._aria2_disabled else self._find_aria2c()
        self._using_aria2c = bool(aria2c_path)
        if aria2c_path:
            opts["external_downloader"] = aria2c_path
            opts["external_downloader_args"] = {
                "aria2c": [
                    "-x", "16",        # 单服务器最大16连接
                    "-s", "16",        # 每文件最大16分片
                    "-k", "1M",        # 最小分片1MB
                    "--max-tries", "5",
                    "--retry-wait", "3",
                    "--connect-timeout", "10",
                    "--timeout", "60",
                    "--allow-overwrite=true",
                    "--auto-file-renaming=false",
                    # quiet=True 时 yt-dlp 会传 --show-console-readout=false，
                    # 那样就没有进度行可读了；这里必须显式打开（后出现的选项优先）
                    "--show-console-readout=true",
                    "--summary-interval=0",
                    "--console-log-level=warn",
                ],
            }
        else:
            # 无 aria2c 时使用内置下载器优化参数
            # 并发片段下载（B站CDN甜区约8）
            opts["concurrent_fragment_downloads"] = max(1, min(self.config.get("concurrent_fragments", 8), 16))

            # 绕过限速：速度低于 100KB/s 持续30秒时认为被限速，自动重连换源
            opts["throttledratelimit"] = 102400

            # 重试配置
            opts["retries"] = 10
            opts["fragment_retries"] = 10

            # HTTP 分块大小
            opts["http_chunk_size"] = 10485760

        # 输出路径模板
        download_path = self.config.get("download_path", os.path.expanduser("~/Downloads/BiliGo"))
        os.makedirs(download_path, exist_ok=True)
        opts["outtmpl"] = {
            "default": os.path.join(download_path, "%(title)s.%(ext)s"),
        }

        # 仅在有 FFmpeg 时设置合并格式
        if ffmpeg_path:
            opts["merge_output_format"] = "mp4"

        # 进度回调
        opts["progress_hooks"] = [self._on_progress]

        return opts

    def _on_progress(self, d):
        """yt-dlp 进度回调"""
        if self._cancel_flag:
            raise yt_dlp.utils.DownloadError("下载已取消")

        if d["status"] == "downloading":
            self.progress.status = "downloading"
            total_bytes = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes", 0)
            speed = d.get("speed") or 0

            self.progress.total = total_bytes
            self.progress.downloaded = downloaded
            self.progress.speed = speed
            self.progress.speed_str = d.get("_speed_str", "")
            self.progress.eta = d.get("eta", 0) or 0
            self.progress.current_file = d.get("filename", "")

            if total_bytes > 0:
                self.progress.percent = (downloaded / total_bytes) * 100
            else:
                pct_str = d.get("_percent_str", "0%").strip().replace("%", "")
                try:
                    self.progress.percent = float(pct_str)
                except ValueError:
                    pass

        elif d["status"] == "finished":
            # 外置下载器：每个流（视频/音频）结束都会回调一次，这里只计数，
            # 进度状态交给 _watch_progress 线程判断（否则会在音频还没下时误报"合并中"）
            if self._using_aria2c:
                self._aria2_files_done += 1
                cap = self._capture
                aria_bytes = 0
                if cap is not None and cap.lines:
                    aria_bytes = cap.snapshot()[0]
                hook_bytes = int(d.get("total_bytes")
                                 or d.get("downloaded_bytes") or 0)
                self._finished_bytes += max(aria_bytes, hook_bytes)
                # 关键：必须清掉快照。否则下一条流开始时，进度还是上一条流的最后读数，
                # 会跟刚累加的 _finished_bytes 重复计算 —— 表现为百分比先冲到 100%、
                # 等新流的读数进来又掉回一半（实测 100% → 51.5%）。
                if cap is not None:
                    cap.reset()
            else:
                self.progress.status = "merging"

        elif d["status"] == "error":
            self.progress.status = "error"
            self.progress.error_msg = "下载过程中出现错误"

    def fetch_info(self, url: str) -> dict:
        """获取视频信息

        单P视频：做完整 extract_info（含格式信息）
        多P视频：先 extract_flat 获取列表（快），再 noplaylist=True 提取第一P格式信息
        """
        url = self.normalize_url(url)

        # 清除旧缓存
        self._cached_raw_info = None
        self._cached_url = None

        # ── 第1步：快速获取分P列表（判断单P/多P） ──
        opts_flat = self._build_opts(for_info=True)
        with yt_dlp.YoutubeDL(opts_flat) as ydl:
            flat_info = ydl.extract_info(url, download=False)

        is_multi_p = bool(flat_info.get("entries")) and len(flat_info.get("entries", [])) > 1

        if not is_multi_p:
            # ── 单P视频：flat_info 本身就足够（extract_flat 对单P仍返回 formats） ──
            # 但 extract_flat 模式不返回 formats，需做完整提取
            opts_full = {
                "quiet": True,
                "no_warnings": True,
                # 这里不能限制 format：要把视频的全部清晰度档位都拿回来，
                # 供界面动态生成「和 B 站一致」的清晰度选项。
                # 真正选哪一档由 download() 里的 _format_for_quality() 决定。
            }
            cookie_opt = self.config.get_ytdlp_cookie_option()
            opts_full.update(cookie_opt)
            ffmpeg_path = self.config.get_ffmpeg_path()
            if ffmpeg_path:
                opts_full["ffmpeg_location"] = os.path.dirname(ffmpeg_path)
            opts_full["noplaylist"] = False

            with yt_dlp.YoutubeDL(opts_full) as ydl:
                full_info = ydl.extract_info(url, download=False)

            result = self._parse_info(full_info)
            # 单P视频缓存完整 info dict，供 download 复用跳过 extract_info
            self._cached_raw_info = full_info
            self._cached_url = url
            self._last_info = result
            return result

        # ── 多P视频：flat_info 已有列表，额外提取第一P格式信息 ──
        opts_full = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,  # 只取第一P
        }
        cookie_opt = self.config.get_ytdlp_cookie_option()
        opts_full.update(cookie_opt)
        ffmpeg_path = self.config.get_ffmpeg_path()
        if ffmpeg_path:
            opts_full["ffmpeg_location"] = os.path.dirname(ffmpeg_path)

        with yt_dlp.YoutubeDL(opts_full) as ydl:
            first_p_info = ydl.extract_info(url, download=False)

        # 合并：flat_info 的 entries 列表 + first_p_info 的格式信息
        result = self._parse_info(flat_info)
        # 回填格式信息（按第一P的真实档位生成清晰度选项）
        result["quality_options"] = build_quality_options(first_p_info.get("formats") or [])
        result["available_qualities"] = [o["label"] for o in result["quality_options"]]

        # 多P视频不缓存（下载时需按选中P重新提取）
        self._cached_raw_info = None
        self._cached_url = None

        self._last_info = result
        return result

    def _parse_info(self, info: dict) -> dict:
        """解析 yt-dlp 返回的视频信息"""
        result = {
            "title": info.get("title", "未知标题"),
            "description": info.get("description", ""),
            "thumbnail": info.get("thumbnail", ""),
            "duration": info.get("duration", 0),
            "uploader": info.get("uploader", ""),
            "uploader_id": info.get("uploader_id", ""),
            "view_count": info.get("view_count", 0),
            "like_count": info.get("like_count", 0),
            "id": info.get("id", ""),
            "webpage_url": info.get("webpage_url", ""),
            "extractor": info.get("extractor", ""),
            "cid": None,
            "bvid": None,
            "entries": None,
            "available_qualities": [],
            "has_danmaku": True,
            "has_subtitle": False,
            "subtitles": {},
        }

        webpage_url = result["webpage_url"]
        bv_match = re.search(r"BV[\w]+", webpage_url)
        if bv_match:
            result["bvid"] = bv_match.group()

        entries = info.get("entries")
        if entries:
            result["entries"] = []
            for i, entry in enumerate(entries):
                entry_info = {
                    "index": i + 1,
                    "title": entry.get("title", f"第{i+1}P"),
                    "duration": entry.get("duration", 0),
                    "url": entry.get("url", ""),
                    "webpage_url": entry.get("webpage_url", ""),
                    "thumbnail": entry.get("thumbnail", ""),
                }
                result["entries"].append(entry_info)

        # 解析可用清晰度：按 B 站自己的档位（qn）汇总，和播放器里的选项一致
        result["quality_options"] = build_quality_options(info.get("formats") or [])
        result["available_qualities"] = [o["label"] for o in result["quality_options"]]

        # 字幕
        subs = info.get("subtitles", {})
        if subs:
            result["has_subtitle"] = True
            result["subtitles"] = {k: True for k in subs.keys()}

        return result

    def _format_for_quality(self, info: dict, quality: str, ydl=None) -> str:
        """把界面上的清晰度名字翻译成 yt-dlp 的 format 字符串。

        优先按这个视频自己的档位取精确那一条视频流（+ 最好的音频），
        名字对不上（比如换了视频、旧配置里的档位已不存在）就退回按分辨率映射。
        """
        if not quality:
            return ""
        if not self.config.get_ffmpeg_path():
            # 没有 FFmpeg 无法合并音视频，只能退回到「已合并单流」的映射
            return self.config.get_format_for_quality(quality)

        options = info.get("quality_options") or build_quality_options(info.get("formats") or [])
        hit = next((o for o in options if o["label"] == quality), None)
        if hit and hit.get("format_id"):
            fid = hit["format_id"]
            return f"{fid}+ba/{fid}"
        return self.config.get_format_for_quality(quality)

    def download(self, url: str, quality: str = None, playlist_items: list = None) -> DownloadProgress:
        """下载视频

        如果 fetch_info 已缓存原始 info dict（单P视频 + URL 匹配），
        直接复用跳过 extract_info，大幅减少等待时间。
        缓存未命中时，在同一实例中 extract_info + process_info。

        Args:
            url: 视频URL
            quality: 画质名称
            playlist_items: 要下载的分P索引列表(1-based)，None表示全部
        """
        url = self.normalize_url(url)
        self._cancel_flag = False
        self.progress = DownloadProgress()

        if quality:
            self.config.set("quality", quality)

        effective_quality = quality or self.config.get("quality", "")

        # 缓存里存的是「全部清晰度的原始 info」，没有绑定某一档，
        # 所以只要 URL 一致就能复用；选哪一档在下面用 process_video_result 重新选。
        use_cache = (
            self._cached_raw_info is not None
            and self._cached_url == url
            and playlist_items is None  # 单P或全部下载才缓存
        )

        opts = self._build_opts(playlist_items=playlist_items)
        download_dir = os.path.dirname(opts["outtmpl"]["default"])
        started_at = time.time()

        try:
            # ① 先拿到「全部清晰度」的原始信息（有缓存就不联网）
            if use_cache:
                raw_info = self._cached_raw_info
                self._cached_raw_info = None
                self._cached_url = None
            else:
                self.progress.status = "extracting"
                with yt_dlp.YoutubeDL(opts) as probe:
                    raw_info = probe.extract_info(url, download=False)

            # ② 按选中的清晰度决定 format（单P 用视频自己的档位，多P 退回按分辨率映射）
            fmt_str = ""
            if raw_info and not raw_info.get("entries"):
                fmt_str = self._format_for_quality(raw_info, effective_quality)

            # ③ 真正下载用「新建时就把 format 带上」的实例。
            #    踩过的坑：同一个 YoutubeDL 实例中途改 ydl.params['format'] 不生效，
            #    process_video_result 仍按建实例时的默认 best 选，会把 1080P60 下成 4K。
            opts_final = dict(opts)
            if fmt_str:
                opts_final["format"] = fmt_str

            with yt_dlp.YoutubeDL(opts_final) as ydl:
                self._ydl = ydl
                info = raw_info
                if fmt_str:
                    try:
                        info = ydl.process_video_result(
                            copy.deepcopy(raw_info), download=False)
                    except Exception:
                        info = raw_info
                        fmt_str = ""
                if not fmt_str:
                    fmt_str = ydl.params.get("format", "") or ""

                self.progress.status = "downloading"

                # 提取画质信息
                if info:
                    self._extract_quality_info(info)

                # 外置下载器不上报下载中进度，起一个线程接住 aria2c 自己的进度
                if self._using_aria2c and info:
                    self._start_progress_watch(download_dir, info, ydl, fmt_str)

                # 触发实际下载
                retry_without_aria2c = False
                try:
                    ydl.process_info(info)
                except Exception as exc:
                    if not self._should_retry_without_aria2c(exc):
                        raise
                    retry_without_aria2c = True
                finally:
                    self._stop_progress_watch()

                if retry_without_aria2c:
                    # 典型情况：外部进程（aria2c）启动时 Windows 报了
                    # `[WinError 6] 句柄无效`。换个不依赖子进程的下载器重来一次，
                    # 别让用户整个下载白等（已下好的 .part 会被续传）。
                    self._aria2_disabled = True
                    self._aria2_retry_done = True
                    return self.download(url, quality, playlist_items)

                if not self._last_info:
                    self._parse_info(info)

                # 回填可用画质
                if self._last_info and not self._last_info.get("available_qualities"):
                    parsed = self._parse_info(info)
                    self._last_info["available_qualities"] = parsed.get("available_qualities", [])

                # 下载完成后用真实结果覆盖（下载前那次只是按格式表推算的预估值）
                self._extract_actual_resolution(info)

                # 记录真实落地的文件，弹幕/字幕按这个路径保存
                outputs = self._collect_output_paths(info)
                self.progress.output_paths = outputs
                self.progress.output_path = outputs[0] if outputs else ""

            self.progress.status = "done"
            self.progress.percent = 100

        except yt_dlp.utils.DownloadError as e:
            if self._cancel_flag:
                self.progress.status = "cancelled"
            else:
                self.progress.status = "error"
                self.progress.error_msg = self._friendly_error(str(e)) + \
                    self._leftover_hint(download_dir, started_at)
        except Exception as e:
            self.progress.status = "error"
            self.progress.error_msg = self._friendly_error(str(e)) + \
                self._leftover_hint(download_dir, started_at)
        finally:
            self._ydl = None

        return self.progress

    # ── 外置下载器（aria2c）的进度监控 ──
    # 背景：只要启用外置下载器，yt-dlp 的 ExternalFD.real_download() 全程只在文件下完时
    #      回调一次 progress_hooks(status="finished")，从不发 "downloading"，
    #      所以进度条拿不到任何中间值（表现为一直是 0%，然后直接跳到 100%）。
    #
    # 进度来源（按优先级）：
    #   ①【主】读 aria2c 自己的 stdout（管道），直接拿它的百分比和字节数。
    #        实测 1 秒一条，序列 6→19→20→35→50→70→82→91%，既准又不跳。
    #   ②【兜底】5 秒还等不到进度行（管道未生效）才退回盯 *.part 的写入量。
    #
    # 已排除的方案（都实测过，别再试）：
    #   · 改本进程 stdout(fd 1) 重定向到日志：能抓到进度，但在 Windows 上会让
    #     标准句柄被子进程继承/还原，偶发 `[WinError 6] 句柄无效` → 下载直接失败。
    #   · `--enable-rpc` + aria2.tellActive：数字最准，但开了 RPC 的 aria2c 下完不退出，
    #     yt-dlp 永远等不到进程结束 → 整个下载卡死。
    #   · `.part.aria2` 控制文件的 bitfield：`--auto-save-interval=1` 实测根本不落盘
    #     （逐秒监控 12 秒，文件从未生成），只有 aria2 被强杀时才写一次。
    #   · `--log=file --log-level=debug`：4MB 日志里一条进度都没有。
    #   · 读 `.part` 文件长度：多连接乱序写会让文件长度跑到真实进度前面
    #     （实测 aria2 报 63% 时文件已有 91%），这正是「跳到 90% 再慢爬」的原因。
    #     磁盘实际分配字节（GetCompressedFileSize）与文件长度完全一致，同样没用。

    ARIA2_PROGRESS_RE = _ARIA2_PROGRESS_RE

    @staticmethod
    def _parse_size(text: str) -> int:
        """把 aria2 的 '9.1MiB' / '784KiB' / '1.2GiB' 转成字节数"""
        return parse_aria2_size(text)

    def _estimate_total_bytes(self, info: dict, ydl, fmt_str: str = "") -> int:
        """估算本次要下载的总字节数（视频流 + 音频流），估不出来返回 0"""
        try:
            formats = info.get("formats") or []
            if not formats:
                return 0
            fmt_str = fmt_str or self.config.get_format_for_quality(
                self.config.get("quality", ""))
            # 借 yt-dlp 自己的格式选择器，保证和实际下载选中的流一致
            selector = ydl.build_format_selector(fmt_str)
            selected = selector({"formats": formats, "incomplete_formats": False}) or []
            duration = info.get("duration") or 0
            total = 0
            for fmt in selected:
                size = fmt.get("filesize") or fmt.get("filesize_approx") or 0
                if not size and duration:
                    # 没有文件大小就按码率 × 时长估
                    tbr = fmt.get("tbr") or fmt.get("vbr") or fmt.get("abr") or 0
                    size = tbr * 1000 / 8 * duration
                total += int(size)
            return total
        except Exception:
            return 0

    def _start_progress_watch(self, download_dir: str, info: dict, ydl, fmt_str: str = ""):
        """启动进度监控：进度由 aria2c 的 stdout 管道提供"""
        global _ARIA2_SINK

        # 启动前已存在的 .part 属于别人的残留，兜底方案不能算进来
        try:
            pre = {os.path.join(download_dir, n)
                   for n in os.listdir(download_dir) if n.endswith(".part")}
        except OSError:
            pre = set()

        total = self._estimate_total_bytes(info, ydl, fmt_str)
        entries = info.get("entries") or []
        streams_per_entry = 2 if self.config.get_ffmpeg_path() else 1
        self._aria2_files_done = 0
        self._finished_bytes = 0
        self._aria2_files_expect = streams_per_entry * max(1, len(entries))
        self.progress.total = total

        capture = Aria2Capture()
        install_aria2_capture()
        self._capture = capture
        _ARIA2_SINK = capture

        self._watch_stop.clear()
        self._watch_thread = threading.Thread(
            target=self._watch_progress,
            args=(download_dir, pre, total, capture),
            daemon=True,
        )
        self._watch_thread.start()

    # ── 兜底方案：盯 .part 文件的写入量 ──

    @staticmethod
    def _written_size(path: str) -> int:
        """读某个文件当前写入到多少字节。

        Windows 上目录项里的 size 对「仍被打开的文件」会滞后（NTFS 懒更新），
        os.path.getsize / DirEntry.stat 可能长时间读到旧值，
        所以优先打开句柄后用 fstat 取——那条路径拿到的是实时的文件长度。
        """
        try:
            with open(path, "rb") as f:
                return os.fstat(f.fileno()).st_size
        except OSError:
            try:
                return os.path.getsize(path)
            except OSError:
                return 0

    def _scan_written(self, download_dir: str, pre: set, seen_parts: dict):
        """扫一次下载目录，返回 (累计已写入字节, 当前正在写的最大 .part 路径)

        消失的 .part 说明那个流已经下完（yt-dlp 把它改名成正式文件了），
        把它的字节数并进 closed，保证「视频流结束 → 音频流开始」时百分比不回退。
        """
        current = {}
        try:
            with os.scandir(download_dir) as it:
                for entry in it:
                    if entry.name.endswith(".part") and entry.path not in pre:
                        size = self._written_size(entry.path)
                        if size:
                            current[entry.path] = size
        except OSError:
            pass

        for path, size in list(seen_parts.items()):
            if path not in current:
                self._closed_bytes += size
                seen_parts.pop(path, None)
        seen_parts.update(current)

        biggest = max(current, key=current.get) if current else ""
        return self._closed_bytes + sum(current.values()), biggest

    def _watch_progress(self, download_dir: str, pre: set, total_est: int,
                        capture: "Aria2Capture"):
        """每 0.3 秒更新一次整体进度。

        数据源优先用 aria2c 自报的「当前流已下载字节」，再叠加已完成的流
        （视频流下完、音频流开始时百分比不会回退）。
        5 秒还没收到任何进度行（管道不可用）才退回看 .part 写入量。
        """
        seen_parts = {}
        self._closed_bytes = 0
        known_total = total_est
        history = []             # [(时间, 累计字节)]，用 3 秒窗口算速度
        started = time.time()

        while not self._watch_stop.is_set():
            time.sleep(0.3)

            cur_bytes, cur_pct, aria_speed, _ = capture.snapshot()
            use_capture = capture.lines > 0

            if use_capture:
                downloaded = self._finished_bytes + cur_bytes
                speed = aria_speed
            elif time.time() - started > 5.0:
                # 兜底：管道没生效，退回盯 .part 的写入量
                try:
                    stream_bytes, biggest = self._scan_written(
                        download_dir, pre, seen_parts)
                except Exception:      # 监控线程不能因为一次扫描失败就退出
                    continue
                if biggest:
                    self.progress.current_file = biggest
                downloaded = self._finished_bytes + stream_bytes
                speed = 0.0
            else:
                # 刚开始，还没等到第一条进度行，别乱报
                self.progress.status = "downloading"
                continue

            now = time.time()
            history.append((now, downloaded))
            while len(history) > 2 and now - history[0][0] > 3.0:
                history.pop(0)
            if not speed:
                span = now - history[0][0]
                speed = (downloaded - history[0][1]) / span if span >= 1.0 else 0.0

            self.progress.speed = max(0.0, float(speed))
            self.progress.downloaded = downloaded
            # 有估算总大小就以它为准；只在估不出来时才跟着 downloaded 往上抬，
            # 免得偶发的虚高读数把总大小永久改大（那会让百分比凭空掉一半）
            if total_est <= 0:
                known_total = max(known_total, downloaded)

            if known_total > 0:
                self.progress.total = known_total
                if downloaded >= known_total * 0.995:
                    self.progress.status = "merging"
                    self.progress.percent = 100.0
                else:
                    self.progress.status = "downloading"
                    # 管道拿到的是 aria2 的真值，不需要封顶；
                    # 兜底方案的文件长度会虚高，仍然封 99%
                    cap = 99.9 if use_capture else 99.0
                    self.progress.percent = min(
                        downloaded * 100.0 / known_total, cap)
            else:
                # 估不出总大小（如多P）：按「已完成流数 + 当前流进度」估算
                expect = max(1, self._aria2_files_expect)
                frac = (cur_pct / 100.0) if use_capture else 0.0
                done = min(self._aria2_files_done, expect)
                self.progress.status = "downloading"
                self.progress.percent = min((done + frac) * 100.0 / expect, 99.0)

    def _stop_progress_watch(self):
        """停止进度监控线程，并撤掉进度采集器"""
        global _ARIA2_SINK
        self._watch_stop.set()
        if self._watch_thread is not None:
            self._watch_thread.join(timeout=1.5)
            self._watch_thread = None
        _ARIA2_SINK = None
        self._capture = None

    def _extract_quality_info(self, info: dict):
        """从 extract_info 结果中提取画质信息，填充到 progress"""
        formats = info.get("formats", [])
        seen_heights = set()
        quality_list = []  # (height, label)

        for fmt in formats:
            h = fmt.get("height")
            vcodec = fmt.get("vcodec", "none")
            if h and vcodec != "none" and h not in seen_heights:
                seen_heights.add(h)
                label = self._height_to_label(h, fmt.get("width", 0))
                quality_list.append((h, label))

        quality_list.sort(key=lambda x: x[0], reverse=True)
        self.progress.available_qualities = [q[1] for q in quality_list]

        # VIP 画质检测
        max_available = quality_list[0][0] if quality_list else 0
        has_sessdata = bool(self.config.get("sessdata", ""))
        has_browser = bool(self.config.get("cookies_from_browser", ""))

        if max_available < 2160 and not has_sessdata and not has_browser:
            self.progress.vip_required = True
            self.progress.vip_hint = "未登录，4K/1080P高码率不可用，请在设置中登录大会员"
        elif max_available < 2160:
            self.progress.vip_hint = "该内容不支持4K画质"

        # 模拟选择：推算实际下载的分辨率
        effective_quality = self.config.get("quality", "4K 超清")
        fmt_str = self.config.get_format_for_quality(effective_quality)
        actual_height = self._simulate_format_selection(formats, fmt_str)
        if actual_height:
            for h, label in quality_list:
                if h == actual_height:
                    self.progress.actual_quality = label
                    break
            else:
                self.progress.actual_quality = self._height_to_label(actual_height, 0)

        # 回填到 _last_info
        if self._last_info and not self._last_info.get("available_qualities"):
            self._last_info["available_qualities"] = self.progress.available_qualities

    def _should_retry_without_aria2c(self, exc) -> bool:
        """判断这个异常是不是「外部下载器/句柄失效」这类，值得关掉 aria2c 重试。

        只认 Windows 句柄故障的几种典型文本，别把网络错误也当它（那样会白重下）。
        """
        if getattr(self, "_aria2_retry_done", False) or not self._using_aria2c:
            return False
        text = f"{type(exc).__name__}: {exc}".lower()
        keys = ("winerror 6", "句柄无效", "invalid handle",
                "bad file descriptor", "[errno 9]")
        return any(k in text for k in keys)

    def _friendly_error(self, raw_msg: str) -> str:
        """将 yt-dlp 原始错误转为用户友好的提示"""
        msg_lower = raw_msg.lower()

        # 大会员专属内容认证失败
        if any(kw in msg_lower for kw in ("premium", "vip", "member", "login", "sign in")):
            if "premium" in msg_lower or "vip" in msg_lower or "member" in msg_lower:
                return "该内容为大会员专属，请先在设置中登录大会员账号"
            return "认证失败，请检查登录状态（设置 → 扫码登录或输入SESSDATA）"

        # 付费内容
        if any(kw in msg_lower for kw in ("pay", "purchase", "buy")):
            return "该内容为付费内容，需要单独购买后才能下载"

        # 地区限制
        if any(kw in msg_lower for kw in ("region", "geo", "country")):
            return "该内容受地区限制，当前网络无法访问"

        # Windows 句柄故障（外部下载器启动/继承句柄失败）
        if any(k in msg_lower for k in ("winerror 6", "invalid handle", "bad file descriptor")) \
                or "句柄无效" in raw_msg:
            return f"下载器进程启动失败（{raw_msg}）。已自动改用内置下载器重试；若反复出现请重开程序。"

        # aria2c 相关
        if "aria2c" in msg_lower:
            return f"下载器错误: {raw_msg}"

        # extractor 错误（如 KeyError）
        if "extractor error" in msg_lower or "keyerror" in msg_lower:
            return f"解析失败（B站API可能已更新），请尝试更新BiliGo或使用BV链接代替ep链接"

        return raw_msg

    def _extract_actual_resolution(self, info: dict):
        """从下载结果里提取「实际下到的规格」：分辨率 + 帧率 + HDR

        合并后的结果对象常常不带 fps / dynamic_range，所以用 format_id 回查原始
        formats 表拿完整信息，避免只显示出分辨率。
        """
        def spec_of(node):
            if not isinstance(node, dict):
                return ""
            formats = {f.get("format_id"): f for f in (node.get("formats") or [])}
            ids = [rf.get("format_id") for rf in (node.get("requested_formats") or [])]
            if not ids and node.get("format_id"):
                ids = str(node["format_id"]).split("+")
            for fid in ids:
                fmt = formats.get(fid) or {}
                if (fmt.get("vcodec") or "none") == "none":
                    continue
                if fmt.get("height"):
                    return self._height_to_label(
                        fmt["height"], fmt.get("width", 0),
                        fps=fmt.get("fps") or 0,
                        dynamic_range=fmt.get("dynamic_range") or "")
            # 回退：直接用结果里带的分辨率
            for src in (list(node.get("requested_downloads") or [])
                        + list(node.get("requested_formats") or [])):
                if src.get("height") and (src.get("vcodec") or "none") != "none":
                    return self._height_to_label(
                        src["height"], src.get("width", 0),
                        fps=src.get("fps") or 0,
                        dynamic_range=src.get("dynamic_range") or "")
            return ""

        try:
            text = spec_of(info)
            if not text:
                for e in info.get("entries") or []:
                    if not e:
                        continue
                    text = spec_of(e.get("entries")[0] if e.get("entries") else e)
                    if text:
                        break
            self.progress.actual_quality = text or "未知（无法从下载结果提取）"
        except Exception as e:
            self.progress.actual_quality = f"未知（{e}）"

    @staticmethod
    def _leftover_hint(download_dir: str, started_at: float) -> str:
        """下载失败时提示磁盘上留下了什么，避免用户对着 .f30125.mp4 这种中间文件发懵。

        yt-dlp 合并前每个流是独立的 ``标题.f<format_id>.mp4``，中途失败就会留下它，
        这种文件通常没有音轨、也不是最终文件名。
        """
        leftovers = []
        try:
            for name in os.listdir(download_dir):
                path = os.path.join(download_dir, name)
                try:
                    if os.path.getmtime(path) < started_at - 1:
                        continue
                    size = os.path.getsize(path)
                except OSError:
                    continue
                if size > 1 << 20:      # 只看大于 1MB 的
                    leftovers.append(f"{name}（{size / 1048576:.0f}MB）")
        except OSError:
            return ""
        if not leftovers:
            return ""
        return ("\n磁盘上留下了未完成的文件：" + "、".join(leftovers)
                + "\n这些是合并前的中间文件（可能没有音轨），重新下载会覆盖它们。")

    def _collect_output_paths(self, info: dict) -> list:
        """收集本次真正落地的文件路径，弹幕/字幕要放到它们旁边。

        不能自己拿标题拼路径：yt-dlp 对非法字符的替换规则和本地 re.sub 不一样
        （它会把 / 换成 ⁄、| 换成 ｜，我们换成 _），拼出来的名字和实际文件对不上，
        就会出现「视频下好了但字幕找不到」。
        """
        paths = []

        def walk(node):
            if not isinstance(node, dict):
                return
            for rd in node.get("requested_downloads") or []:
                fp = rd.get("filepath") or rd.get("_filename")
                if fp and fp not in paths:
                    paths.append(fp)
            for key in ("filepath", "_filename"):
                fp = node.get(key)
                if fp and fp not in paths:
                    paths.append(fp)
            for e in node.get("entries") or []:
                walk(e)

        walk(info)
        return [p for p in paths if os.path.exists(p)]

    def _height_to_label(self, height, width=0, with_size=True, fps=0, dynamic_range=""):
        """分辨率转画质标签。

        档位按「宽高里较小的那条边」判断：B 站的档位就是这么标的
        （480P 横屏是 852x480，竖屏是 480x852，短边都是 480）。
        只看 height 会把竖屏 480P 错标成 720P。
        """
        short = min(height, width) if width else height
        if short >= 2160:
            label = f"4K ({short}p)"
        elif short >= 1080:
            label = f"1080P ({short}p)"
        elif short >= 720:
            label = f"720P ({short}p)"
        elif short >= 480:
            label = f"480P ({short}p)"
        else:
            label = f"{short}P"
        if width and with_size:
            label += f" {width}x{height}"
        if fps:
            label += f" {round(float(fps))}fps"
        if (dynamic_range or "SDR").upper() not in ("SDR", ""):
            label += f" {dynamic_range.upper()}"
        return label

    def pre_analyze(self, url: str, quality: str = None) -> dict:
        """预解析画质信息（下载前获取可选画质和实际将下载的画质）

        Returns:
            {
                "available": ["4K (2160p)", "1080P (1080p)", ...],
                "actual": "4K (2160p) 3840x2160",
                "vip_required": True/False,  # 是否需要大会员才能获取更高画质
                "vip_hint": str,             # VIP 相关提示
            }
        """
        url = self.normalize_url(url)
        result = {"available": [], "actual": "", "vip_required": False, "vip_hint": ""}

        try:
            opts = {
                "quiet": True,
                "no_warnings": True,
            }

            # Cookie
            cookie_opt = self.config.get_ytdlp_cookie_option()
            opts.update(cookie_opt)

            # FFmpeg
            ffmpeg_path = self.config.get_ffmpeg_path()
            if ffmpeg_path:
                opts["ffmpeg_location"] = os.path.dirname(ffmpeg_path)

            # 多P视频：只提取第一P的格式信息（全量提取极慢）
            is_multi_p = self._last_info and self._last_info.get("entries")
            if is_multi_p:
                opts["noplaylist"] = True  # 只取第一P，不遍历整个列表
            else:
                opts["noplaylist"] = False

            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)

            # 解析可用画质
            formats = info.get("formats", [])
            seen_heights = set()
            quality_list = []  # (height, label)

            for fmt in formats:
                h = fmt.get("height")
                vcodec = fmt.get("vcodec", "none")
                if h and vcodec != "none" and h not in seen_heights:
                    seen_heights.add(h)
                    label = self._height_to_label(h, fmt.get("width", 0))
                    quality_list.append((h, label))

            # 按分辨率从高到低排序
            quality_list.sort(key=lambda x: x[0], reverse=True)
            result["available"] = [q[1] for q in quality_list]

            # VIP 画质检测：如果最高可用画质 < 用户选择的画质，说明需要大会员
            max_available = quality_list[0][0] if quality_list else 0
            has_sessdata = bool(self.config.get("sessdata", ""))
            has_browser = bool(self.config.get("cookies_from_browser", ""))

            # 检测是否缺少4K（未登录时B站不返回4K格式）
            if max_available < 2160 and not has_sessdata and not has_browser:
                result["vip_required"] = True
                result["vip_hint"] = "未登录，4K/1080P高码率不可用，请在设置中登录大会员"
            elif max_available < 2160:
                # 已登录但无4K，可能是内容本身不支持4K
                result["vip_hint"] = "该内容不支持4K画质"

            # 模拟选择：根据用户选择的画质，推算实际下载的分辨率
            # 注意：不要调用 self.config.set()，预解析不应有副作用
            effective_quality = quality or self.config.get("quality", "4K 超清")
            fmt_str = self.config.get_format_for_quality(effective_quality)

            # 用 yt-dlp 的 format selector 模拟，找出实际会选中的 format
            actual_height = self._simulate_format_selection(formats, fmt_str)
            if actual_height:
                # 找到对应 label（含分辨率）
                for h, label in quality_list:
                    if h == actual_height:
                        result["actual"] = label
                        break
                else:
                    result["actual"] = self._height_to_label(actual_height, 0)

            # 回填到 _last_info
            if self._last_info and not self._last_info.get("available_qualities"):
                self._last_info["available_qualities"] = result["available"]

        except Exception as e:
            result["actual"] = f"预解析失败: {e}"

        return result

    def _simulate_format_selection(self, formats: list, fmt_str: str) -> int:
        """模拟 yt-dlp 的 format 选择逻辑，返回实际将下载的视频高度

        简化策略：根据 format 字符串中的高度限制，从 formats 中找匹配的视频流
        """
        import re

        # 处理 worst 模式：返回最低可用高度
        if "worst" in fmt_str and "height" not in fmt_str:
            min_h = 9999
            for fmt in formats:
                h = fmt.get("height", 0) or 0
                vcodec = fmt.get("vcodec", "") or ""
                tbr = fmt.get("tbr", 0) or 0
                if vcodec != "none" and h > 0 and tbr > 0 and h < min_h:
                    min_h = h
            return min_h if min_h < 9999 else 0

        # 处理 height= 精确匹配模式
        exact_match = re.search(r"height\s*=\s*(\d+)", fmt_str)
        if exact_match:
            target_h = int(exact_match.group(1))
            # 在 formats 中找精确匹配的最高 tbr 流
            for fmt in formats:
                h = fmt.get("height", 0) or 0
                vcodec = fmt.get("vcodec", "") or ""
                if vcodec != "none" and h == target_h:
                    return h
            # 精确匹配不到时降级到 <= 模式
            max_height = target_h
        else:
            # 处理 height<= 模式
            max_height = 9999
            height_match = re.search(r"height\s*<=\s*(\d+)", fmt_str)
            if height_match:
                max_height = int(height_match.group(1))

        # 找最高可用视频流（受高度限制）
        best_h = 0
        for fmt in formats:
            h = fmt.get("height", 0) or 0
            vcodec = fmt.get("vcodec", "") or ""
            tbr = fmt.get("tbr", 0) or 0
            if vcodec != "none" and h > 0 and h <= max_height and tbr > 0:
                if h > best_h:
                    best_h = h

        return best_h if best_h > 0 else 0

    def cancel(self):
        """取消当前下载"""
        self._cancel_flag = True

    def get_download_path_for(self, info: dict) -> str:
        """根据视频信息获取下载文件路径"""
        download_path = self.config.get("download_path", os.path.expanduser("~/Downloads/BiliGo"))
        title = info.get("title", "video")
        safe_title = re.sub(r'[\\/:*?"<>|]', "_", title)
        return os.path.join(download_path, safe_title + ".mp4")
