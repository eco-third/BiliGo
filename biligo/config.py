"""BiliGo 配置管理模块"""

import json
import os
import sys
import platform
import zipfile
import shutil


def _merge_format(height: int, exact: bool = False) -> str:
    """生成「音视频分离流合并下载」的 yt-dlp format 字符串。

    两个坑：
      1. 竖屏视频（例如 B 站的 480P 竖屏是 480x852）width/height 是反的，
         只按 height<=480 过滤会一条都匹配不到；
      2. 纯 DASH 视频没有合并流，兜底的 /best（= 单个带音视频的文件）也会失败。
    所以顺序是：按 height 选 → 按 width 选 → 最后兜底「任意视频 + 任意音频」。
    """
    op = "=" if exact else "<="
    return (f"bv*[height{op}{height}]+ba"
            f"/bv*[width{op}{height}]+ba"
            f"/bv*+ba/b")


def _single_format(height: int, exact: bool = False) -> str:
    """无 FFmpeg 时用的：只下已合并的单文件流"""
    op = "=" if exact else "<="
    return f"b[height{op}{height}]/b[width{op}{height}]/b"


# ── B 站自己的清晰度编号（qn）对应的名字 ──
# 和网页播放器右下角的选择框一致。实际能选哪些，要看具体视频给了哪些格式。
BILI_QN_LABELS = {
    127: "8K 超高清",
    126: "杜比视界",
    125: "HDR 真彩",
    120: "4K 超清",
    122: "4K 超清",     # 4K 的高码率变体，播放器里同样显示成「4K 超清」
    116: "1080P60",
    112: "1080P 高码率",
    80: "1080P",
    74: "720P60",
    64: "720P",
    32: "480P",
    16: "360P",
}


def quality_label(qn: int, width: int = 0, height: int = 0,
                  fps: float = 0, dynamic_range: str = "SDR") -> str:
    """把 yt-dlp 报的 qn 翻译成 B 站播放器里的清晰度名字。

    qn 不在表里时，用「分辨率 + 60帧 + HDR」拼一个，保证不会出现空名字。
    """
    label = BILI_QN_LABELS.get(int(qn or 0))
    if label:
        return label
    short = min(height, width) if (width and height) else height
    label = f"{short}P" if short else "未知清晰度"
    if (fps or 0) >= 50:
        label += "60"
    if (dynamic_range or "SDR").upper() not in ("SDR", ""):
        label += " HDR"
    return label


class Config:
    """应用配置管理，自动持久化到本地JSON文件"""

    APP_NAME = "BiliGo"
    VERSION = "1.2.0"

    # 默认配色
    COLORS = {
        "bg_dark": "#1a1a2e",
        "bg_card": "#16213e",
        "bg_input": "#0f3460",
        "accent": "#FB7299",       # B站粉
        "accent_hover": "#e8556e",
        "text_primary": "#FFFFFF",
        "text_secondary": "#a0a0b0",
        "success": "#00d26a",
        "warning": "#ffb347",
        "error": "#ff4757",
    }

    # 画质映射 (显示名 -> yt-dlp format)
    # 需要 FFmpeg 的格式（音视频分离流）
    QUALITY_MAP_MERGE = {
        "4K 超清": _merge_format(2160),
        "1080P 高码率": _merge_format(1080),
        "1080P": _merge_format(1080, exact=True),
        "720P": _merge_format(720),
        "480P": _merge_format(480),
        "360P": "wv*[height<=360]+wa/wv*[width<=360]+wa/wv*+wa/w",
    }

    # 无 FFmpeg 时使用的格式（下载已合并流，无需合并）
    QUALITY_MAP_NO_MERGE = {
        "4K 超清": _single_format(2160),
        "1080P 高码率": _single_format(1080),
        "1080P": _single_format(1080, exact=True),
        "720P": _single_format(720),
        "480P": _single_format(480),
        "360P": "wv*[height<=360]/wv*[width<=360]/w",
    }

    QUALITY_LIST = list(QUALITY_MAP_MERGE.keys())

    # FFmpeg 自动下载地址 (Windows essentials build，约 30MB)
    FFMPEG_DOWNLOAD_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"

    @staticmethod
    def _get_app_dir():
        """获取应用根目录（exe所在目录或脚本目录，便携化）"""
        if getattr(sys, 'frozen', False):
            # PyInstaller 打包后
            return os.path.dirname(sys.executable)
        else:
            # 开发模式：用项目根目录
            return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def __init__(self):
        app_dir = self._get_app_dir()
        self.config_dir = os.path.join(app_dir, "config")
        self.ffmpeg_dir = os.path.join(app_dir, "ffmpeg")
        self.cookie_file = os.path.join(self.config_dir, "cookies.txt")

        self.config_file = os.path.join(self.config_dir, "config.json")

        self._ensure_dirs()
        self._load()

    def _ensure_dirs(self):
        os.makedirs(self.config_dir, exist_ok=True)
        os.makedirs(self.ffmpeg_dir, exist_ok=True)

    def _defaults(self):
        app_dir = self._get_app_dir()
        download_path = os.path.join(app_dir, "Downloads")

        return {
            "download_path": download_path,
            "quality": "1080P",    # 默认 1080P，和 B 站网页一致；实际可选档位由视频决定
            "download_danmaku": True,
            "download_subtitle": True,
            "burn_danmaku": False,
            "concurrent_fragments": 16,
            "cookies_from_browser": "",
            "sessdata": "",
            "bili_jct": "",
            "DedeUserID": "",
            "ffmpeg_path": "",
            "auto_check_clipboard": True,
        }

    def _load(self):
        defaults = self._defaults()
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                defaults.update(saved)
            except (json.JSONDecodeError, IOError):
                pass
        self.data = defaults

    def save(self):
        try:
            with open(self.config_file, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2, ensure_ascii=False)
        except IOError:
            pass

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value
        self.save()

    # ── 画质 ──

    def get_format_for_quality(self, quality: str) -> str:
        """根据画质偏好和 FFmpeg 可用性返回 yt-dlp format 字符串"""
        has_ffmpeg = self.get_ffmpeg_path() is not None
        if has_ffmpeg:
            return self.QUALITY_MAP_MERGE.get(quality, "bestvideo+bestaudio/best")
        else:
            return self.QUALITY_MAP_NO_MERGE.get(quality, "best")

    # ── FFmpeg 相关 ──

    def find_ffmpeg(self):
        """查找系统中的 FFmpeg"""
        # 1. 用户自定义路径
        custom = self.data.get("ffmpeg_path", "")
        if custom and os.path.isfile(custom):
            return custom

        # 2. 内置目录
        if sys.platform == "win32":
            builtin = os.path.join(self.ffmpeg_dir, "ffmpeg.exe")
        else:
            builtin = os.path.join(self.ffmpeg_dir, "ffmpeg")
        if os.path.isfile(builtin):
            return builtin

        # 也搜索内置目录的子目录 (解压后可能有一层目录)
        if os.path.isdir(self.ffmpeg_dir):
            for root, dirs, files in os.walk(self.ffmpeg_dir):
                for f in files:
                    if f.lower() == "ffmpeg.exe" or f.lower() == "ffmpeg":
                        return os.path.join(root, f)

        # 3. 系统 PATH
        found = shutil.which("ffmpeg")
        if found:
            return found

        # 4. Windows 常见路径
        if sys.platform == "win32":
            common_paths = [
                r"C:\ffmpeg\bin\ffmpeg.exe",
                r"C:\ProgramData\chocolatey\bin\ffmpeg.exe",
                os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "ffmpeg", "bin", "ffmpeg.exe"),
            ]
            for p in common_paths:
                if os.path.isfile(p):
                    return p

        return None

    def get_ffmpeg_path(self):
        """获取 FFmpeg 路径，未找到返回 None"""
        return self.find_ffmpeg()

    def download_ffmpeg(self, progress_callback=None) -> str:
        """
        自动下载并解压 FFmpeg essentials 到内置目录
        Args:
            progress_callback: 回调函数 (message: str)
        Returns:
            ffmpeg.exe 路径，失败抛异常
        """
        import requests as req
        import tempfile

        if progress_callback:
            progress_callback("正在下载 FFmpeg...")

        zip_path = os.path.join(self.ffmpeg_dir, "ffmpeg-essentials.zip")

        try:
            # 下载
            resp = req.get(self.FFMPEG_DOWNLOAD_URL, stream=True, timeout=60)
            resp.raise_for_status()
            total = int(resp.headers.get("content-length", 0))
            downloaded = 0

            with open(zip_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total and progress_callback:
                        pct = downloaded / total * 100
                        progress_callback(f"下载 FFmpeg: {pct:.0f}%")

            if progress_callback:
                progress_callback("正在解压 FFmpeg...")

            # 解压
            with zipfile.ZipFile(zip_path, "r") as zf:
                # 找到 ffmpeg.exe 所在路径
                ffmpeg_members = [m for m in zf.namelist() if m.endswith("ffmpeg.exe")]
                if not ffmpeg_members:
                    raise RuntimeError("压缩包中未找到 ffmpeg.exe")

                # 只解压 ffmpeg.exe
                for member in ffmpeg_members:
                    # 提取文件名
                    filename = os.path.basename(member)
                    source = zf.open(member)
                    target = os.path.join(self.ffmpeg_dir, filename)
                    with open(target, "wb") as f:
                        shutil.copyfileobj(source, f)
                    break  # 只取第一个

            # 清理 zip
            os.remove(zip_path)

            # 也解压 ffprobe.exe（yt-dlp 也可能用到）
            with zipfile.ZipFile(zip_path if os.path.exists(zip_path) else "", "r") as zf:
                pass  # zip 已删除，忽略

            result = self.get_ffmpeg_path()
            if not result:
                raise RuntimeError("FFmpeg 安装失败")

            if progress_callback:
                progress_callback(f"FFmpeg 安装完成: {result}")

            return result

        except Exception as e:
            # 清理可能残留的 zip
            if os.path.exists(zip_path):
                os.remove(zip_path)
            raise

    # ── Cookie 相关 ──

    def write_cookie_file(self, sessdata: str, bili_jct: str = "", dede_user_id: str = ""):
        """将Cookie写入 Netscape 格式文件供 yt-dlp 使用"""
        lines = ["# Netscape HTTP Cookie File"]
        if sessdata:
            lines.append(f".bilibili.com	TRUE	/	FALSE	0	SESSDATA	{sessdata}")
        if bili_jct:
            lines.append(f".bilibili.com	TRUE	/	FALSE	0	bili_jct	{bili_jct}")
        if dede_user_id:
            lines.append(f".bilibili.com	TRUE	/	FALSE	0	DedeUserID	{dede_user_id}")
        with open(self.cookie_file, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        return self.cookie_file

    def get_ytdlp_cookie_option(self):
        """返回 yt-dlp 需要的 cookie 配置

        优先级：
        1. cookies_from_browser（如果配置了浏览器名）
        2. cookiefile（SESSDATA 写入 Netscape 格式文件）
        """
        # 优先使用浏览器 Cookie 导入（包含最完整的 Cookie 集合）
        browser = self.data.get("cookies_from_browser", "")
        if browser:
            return {"cookiesfrombrowser": (browser,)}

        # 使用 SESSDATA cookiefile
        sessdata = self.data.get("sessdata", "")
        if sessdata:
            bili_jct = self.data.get("bili_jct", "")
            dede_user_id = self.data.get("DedeUserID", "")
            cookiefile = self.write_cookie_file(sessdata, bili_jct, dede_user_id)
            return {"cookiefile": cookiefile}

        return {}
