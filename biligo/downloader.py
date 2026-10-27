"""BiliGo 下载引擎 - 基于 yt-dlp 封装"""

import os
import re
import shutil
import yt_dlp


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
        self.actual_quality = ""     # 实际下载清晰度
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
        aria2c_path = self._find_aria2c()
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
        # 回填格式信息（从第一P）
        formats = first_p_info.get("formats", [])
        seen_heights = set()
        for fmt in formats:
            h = fmt.get("height")
            vc = fmt.get("vcodec", "none")
            if h and vc != "none" and h not in seen_heights:
                seen_heights.add(h)
                if h >= 2160:
                    result["available_qualities"].append(f"4K ({h}p)")
                elif h >= 1080:
                    result["available_qualities"].append(f"1080P ({h}p)")
                elif h >= 720:
                    result["available_qualities"].append(f"720P ({h}p)")
                elif h >= 480:
                    result["available_qualities"].append(f"480P ({h}p)")
                else:
                    result["available_qualities"].append(f"{h}P")
        result["available_qualities"] = list(dict.fromkeys(result["available_qualities"]))

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

        # 解析可用画质
        formats = info.get("formats", [])
        seen_heights = set()
        for fmt in formats:
            h = fmt.get("height")
            vcodec = fmt.get("vcodec", "none")
            if h and vcodec != "none" and h not in seen_heights:
                seen_heights.add(h)
                if h >= 2160:
                    result["available_qualities"].append(f"4K ({h}p)")
                elif h >= 1080:
                    result["available_qualities"].append(f"1080P ({h}p)")
                elif h >= 720:
                    result["available_qualities"].append(f"720P ({h}p)")
                elif h >= 480:
                    result["available_qualities"].append(f"480P ({h}p)")
                else:
                    result["available_qualities"].append(f"{h}P")

        result["available_qualities"] = list(dict.fromkeys(result["available_qualities"]))

        # 字幕
        subs = info.get("subtitles", {})
        if subs:
            result["has_subtitle"] = True
            result["subtitles"] = {k: True for k in subs.keys()}

        return result

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

        # 检查是否可以复用缓存（单P视频 + URL 匹配 + 无特定分P选择）
        use_cache = (
            self._cached_raw_info is not None
            and self._cached_url == url
            and playlist_items is None  # 单P或全部下载才缓存
        )

        opts = self._build_opts(playlist_items=playlist_items)

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                self._ydl = ydl

                if use_cache:
                    # ── 复用缓存：跳过 extract_info，直接下载 ──
                    info = self._cached_raw_info
                    self._cached_raw_info = None
                    self._cached_url = None
                    self.progress.status = "downloading"
                else:
                    # ── 无缓存：在同一实例中提取+下载 ──
                    self.progress.status = "extracting"
                    info = ydl.extract_info(url, download=False)

                # 提取画质信息
                if info:
                    self._extract_quality_info(info)

                # 触发实际下载（同一 YoutubeDL 实例）
                ydl.process_info(info)

                if not self._last_info:
                    self._parse_info(info)

                # 回填可用画质
                if self._last_info and not self._last_info.get("available_qualities"):
                    parsed = self._parse_info(info)
                    self._last_info["available_qualities"] = parsed.get("available_qualities", [])

                # 提取实际下载的分辨率
                if not self.progress.actual_quality:
                    self._extract_actual_resolution(info)

            self.progress.status = "done"
            self.progress.percent = 100

        except yt_dlp.utils.DownloadError as e:
            if self._cancel_flag:
                self.progress.status = "cancelled"
            else:
                self.progress.status = "error"
                self.progress.error_msg = self._friendly_error(str(e))
        except Exception as e:
            self.progress.status = "error"
            self.progress.error_msg = self._friendly_error(str(e))
        finally:
            self._ydl = None

        return self.progress

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

        # aria2c 相关
        if "aria2c" in msg_lower:
            return f"下载器错误: {raw_msg}"

        # extractor 错误（如 KeyError）
        if "extractor error" in msg_lower or "keyerror" in msg_lower:
            return f"解析失败（B站API可能已更新），请尝试更新BiliGo或使用BV链接代替ep链接"

        return raw_msg

    def _extract_actual_resolution(self, info: dict):
        """从下载结果中提取实际视频分辨率"""
        try:
            # 方法1：从 requested_downloads 提取（最可靠）
            for rd in info.get("requested_downloads", []):
                h = rd.get("height")
                w = rd.get("width")
                if h:
                    self.progress.actual_quality = self._height_to_label(h, w)
                    return

            # 方法2：从 requested_formats 提取
            for rf in info.get("requested_formats", []):
                h = rf.get("height")
                w = rf.get("width")
                if h:
                    self.progress.actual_quality = self._height_to_label(h, w)
                    return

            # 方法3：多P视频，从 entries 中取
            entries = info.get("entries")
            if entries:
                for e in entries:
                    if not e:
                        continue
                    # 递归取子entry
                    sub_entries = e.get("entries")
                    target = sub_entries[0] if sub_entries else e
                    h = target.get("height")
                    w = target.get("width")
                    if h:
                        self.progress.actual_quality = self._height_to_label(h, w)
                        return
                    for rf in target.get("requested_formats", []):
                        h = rf.get("height")
                        w = rf.get("width")
                        if h:
                            self.progress.actual_quality = self._height_to_label(h, w)
                            return

            # 方法4：从 formats 里找最佳视频流
            if info.get("formats"):
                for fmt in reversed(info["formats"]):
                    h = fmt.get("height", 0)
                    vc = fmt.get("vcodec", "") or ""
                    if h and vc != "none" and fmt.get("tbr", 0):
                        self.progress.actual_quality = self._height_to_label(h, fmt.get("width", 0))
                        return

            self.progress.actual_quality = "未知（无法从下载结果提取）"
        except Exception as e:
            self.progress.actual_quality = f"未知（{e}）"

    def _height_to_label(self, height, width=0):
        """高度转画质标签"""
        if height >= 2160:
            label = f"4K ({height}p)"
        elif height >= 1080:
            label = f"1080P ({height}p)"
        elif height >= 720:
            label = f"720P ({height}p)"
        elif height >= 480:
            label = f"480P ({height}p)"
        else:
            label = f"{height}P"
        if width:
            label += f" {width}x{height}"
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
