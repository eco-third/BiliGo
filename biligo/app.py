"""BiliGo 主界面 - CustomTkinter 暗色主题"""

import os
import sys
import threading
import webbrowser
import tkinter as tk
from tkinter import filedialog, messagebox
from urllib.parse import urlparse

import customtkinter as ctk
from PIL import Image, ImageTk

from .config import Config
from .downloader import BiliDownloader
from .danmaku import DanmakuProcessor
from .subtitle import SubtitleProcessor
from .auth import BiliAuth


# ═══════════════════════════════════════════
#  颜色主题（BiliGo 品牌：B站粉 → 紫渐变）
# ═══════════════════════════════════════════
BG = "#14142B"            # 深底（带紫调）
BG_CARD = "#1E1E3A"       # 卡片层
BG_INPUT = "#2A2A52"      # 输入/浮起层
BORDER = "#3A3A6A"        # 细边框
ACCENT = "#FB7299"        # B站粉
ACCENT_HOVER = "#FF8FAE"  # 亮粉 hover
ACCENT_DEEP = "#E8556E"   # 深粉（渐变底部）
ACCENT_PURPLE = "#9B7BFF" # 辅助紫（渐变末端）
TEXT = "#F5F5FA"
TEXT_DIM = "#9A9AB5"
SUCCESS = "#2ED573"
WARNING = "#FFB347"
ERROR = "#FF6B6B"


class BiliGoApp(ctk.CTk):
    """BiliGo 主窗口"""

    def __init__(self):
        super().__init__()

        self.config = Config()
        self.downloader = BiliDownloader(self.config)
        self.danmaku_proc = DanmakuProcessor()
        self.subtitle_proc = SubtitleProcessor()

        self._is_downloading = False
        self._video_info = None
        self._stop_event = threading.Event()
        self._part_vars = []  # 多P复选框变量列表
        self._part_checkboxes = []  # 多P复选框控件列表

        self._setup_window()
        self._build_ui()
        self._check_ffmpeg()
        self._refresh_login_status()

    # ── 窗口设置 ──

    def _setup_window(self):
        self.title("BiliGo - Bilibili 视频下载器")
        self.configure(fg_color=BG)
        icon = self._get_icon()
        if icon:
            try:
                self.iconbitmap(icon)
            except Exception:
                pass

        # 自适应屏幕尺寸，居中显示
        self.update_idletasks()
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        w = min(850, sw - 100)
        h = min(780, sh - 80)  # 预留任务栏
        x = max(0, (sw - w) // 2)
        y = max(0, (sh - h) // 2 - 20)  # 稍微偏上避免贴底
        self.geometry(f"{w}x{h}+{x}+{y}")
        self.minsize(700, 600)

    def _get_icon(self):
        """生成简单的应用图标"""
        icon_path = os.path.join(os.path.dirname(__file__), "icon.ico")
        if os.path.exists(icon_path):
            return icon_path
        return None

    # ── UI 构建 ──

    def _build_ui(self):
        # 主容器
        self.main_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.main_frame.pack(fill="both", expand=True, padx=20, pady=15)

        self._build_header()
        self._build_gradient_bar()
        self._build_url_input()
        self._build_video_info()
        self._build_multi_p_list()
        self._build_download_options()
        self._build_progress()
        self._build_log()

    def _build_header(self):
        """顶部标题栏：Logo + 品牌字标"""
        frame = ctk.CTkFrame(self.main_frame, fg_color="transparent")
        frame.pack(fill="x", pady=(0, 10))

        # Logo + 标题
        title_frame = ctk.CTkFrame(frame, fg_color="transparent")
        title_frame.pack(side="left")

        # 播放三角 Logo 色块（粉色渐变圆角块）
        ctk.CTkLabel(
            title_frame, text="▶", width=38, height=38,
            fg_color=ACCENT, corner_radius=10,
            font=ctk.CTkFont(family="Segoe UI", size=15, weight="bold"),
            text_color="#FFFFFF",
        ).pack(side="left", padx=(0, 10))

        ctk.CTkLabel(
            title_frame, text="BiliGo",
            font=ctk.CTkFont(family="Segoe UI", size=25, weight="bold"),
            text_color=ACCENT,
        ).pack(side="left")

        ctk.CTkLabel(
            title_frame, text="  Bilibili 视频下载器",
            font=ctk.CTkFont(family="Segoe UI", size=14),
            text_color=TEXT_DIM,
        ).pack(side="left", padx=(4, 0))

        # 登录状态标签
        self._header_login_label = ctk.CTkLabel(
            frame, text="",
            font=ctk.CTkFont(size=12),
            text_color=TEXT_DIM, anchor="e",
        )
        self._header_login_label.pack(side="right", padx=(0, 12))

        # 设置按钮
        ctk.CTkButton(
            frame, text="设置", width=70, height=32,
            font=ctk.CTkFont(size=13),
            fg_color=BG_CARD, hover_color=ACCENT,
            border_width=1, border_color=BORDER,
            text_color=TEXT_DIM,
            command=self._open_settings,
        ).pack(side="right")

    def _make_gradient_image(self, width: int, height: int) -> ctk.CTkImage:
        """生成粉→紫水平渐变图（品牌签名元素）"""
        from PIL import ImageDraw
        img = Image.new("RGB", (width, height), color=BG_CARD)
        draw = ImageDraw.Draw(img)
        c1 = (0xFB, 0x72, 0x99)   # ACCENT
        c2 = (0x9B, 0x7B, 0xFF)   # ACCENT_PURPLE
        for x in range(width):
            t = x / max(1, width - 1)
            r = int(c1[0] + (c2[0] - c1[0]) * t)
            g = int(c1[1] + (c2[1] - c1[1]) * t)
            b = int(c1[2] + (c2[2] - c1[2]) * t)
            draw.line([(x, 0), (x, height)], fill=(r, g, b))
        return ctk.CTkImage(light_image=img, dark_image=img, size=(width, height))

    def _build_gradient_bar(self):
        """顶部品牌渐变分隔线（随窗口宽度自适应）"""
        self._gradient_img = self._make_gradient_image(1200, 3)
        self.gradient_label = ctk.CTkLabel(
            self.main_frame, text="", image=self._gradient_img, height=3,
        )
        self.gradient_label.pack(fill="x", pady=(0, 12))
        self.gradient_label.bind("<Configure>", self._on_gradient_resize)

    def _on_gradient_resize(self, event):
        """渐变分隔线宽度随窗口自适应"""
        try:
            w = max(100, event.width)
            self._gradient_img.configure(size=(w, 3))
            self.gradient_label.configure(image=self._gradient_img)
        except Exception:
            pass

    def _build_url_input(self):
        """URL 输入区域"""
        frame = ctk.CTkFrame(self.main_frame, fg_color=BG_CARD, corner_radius=10,
                            border_width=1, border_color=BORDER)
        frame.pack(fill="x", pady=(0, 10))

        inner = ctk.CTkFrame(frame, fg_color="transparent")
        inner.pack(fill="x", padx=14, pady=10)

        self.url_entry = ctk.CTkEntry(
            inner,
            placeholder_text="粘贴 Bilibili 视频链接，如 https://www.bilibili.com/video/BV...",
            font=ctk.CTkFont(size=13),
            height=40,
            fg_color=BG_INPUT,
            border_width=1, border_color=BORDER,
            text_color=TEXT,
            placeholder_text_color=TEXT_DIM,
        )
        self.url_entry.pack(side="left", fill="x", expand=True, padx=(0, 10))
        self.url_entry.bind("<Return>", lambda e: self._fetch_info())

        self.fetch_btn = ctk.CTkButton(
            inner, text="解析", width=80, height=40,
            font=ctk.CTkFont(size=14, weight="bold"),
            fg_color=ACCENT, hover_color=ACCENT_HOVER,
            text_color="#FFFFFF",
            command=self._fetch_info,
        )
        self.fetch_btn.pack(side="right")

    def _build_video_info(self):
        """视频信息展示区域"""
        self.info_frame = ctk.CTkFrame(self.main_frame, fg_color=BG_CARD, corner_radius=10,
                                       border_width=1, border_color=BORDER)
        self.info_frame.pack(fill="x", pady=(0, 10))

        # 默认隐藏，解析后显示
        self.info_inner = ctk.CTkFrame(self.info_frame, fg_color="transparent")
        self.info_inner.pack(fill="x", padx=14, pady=12)

        # 封面缩略图
        self.thumb_label = ctk.CTkLabel(
            self.info_inner, text="",
            width=160, height=90,
            fg_color=BG_INPUT, corner_radius=6,
        )
        self.thumb_label.pack(side="left", padx=(0, 14))

        # 文字信息
        text_frame = ctk.CTkFrame(self.info_inner, fg_color="transparent")
        text_frame.pack(side="left", fill="both", expand=True)

        self.title_label = ctk.CTkLabel(
            text_frame, text="等待解析视频...",
            font=ctk.CTkFont(size=15, weight="bold"),
            text_color=TEXT, wraplength=450, justify="left",
            anchor="w",
        )
        self.title_label.pack(fill="x", pady=(0, 6))

        self.meta_label = ctk.CTkLabel(
            text_frame, text="",
            font=ctk.CTkFont(size=12),
            text_color=TEXT_DIM, wraplength=450, justify="left",
            anchor="w",
        )
        self.meta_label.pack(fill="x")

        self.info_frame.pack_forget()  # 初始隐藏

    def _build_multi_p_list(self):
        """多P视频列表区域（默认隐藏，解析多P后显示）"""
        self.multi_p_frame = ctk.CTkFrame(self.main_frame, fg_color=BG_CARD, corner_radius=8,
                                          border_width=1, border_color=BORDER)
        # 不 pack，默认隐藏

        # 标题行
        header = ctk.CTkFrame(self.multi_p_frame, fg_color="transparent")
        header.pack(fill="x", padx=8, pady=(6, 4))

        self.multi_p_title = ctk.CTkLabel(
            header, text="分P列表",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=TEXT,
        )
        self.multi_p_title.pack(side="left")

        self.select_all_btn = ctk.CTkButton(
            header, text="全选", width=50, height=24,
            font=ctk.CTkFont(size=11),
            fg_color=BG_INPUT, hover_color=ACCENT,
            border_width=1, border_color=BORDER,
            text_color=TEXT_DIM,
            command=self._toggle_select_all,
        )
        self.select_all_btn.pack(side="right")

        # 用 Canvas + Frame 实现限高滚动（精确控制最大高度）
        self._part_canvas = tk.Canvas(
            self.multi_p_frame, bg=BG_CARD, highlightthickness=0, height=78,
            highlightbackground=BORDER,
        )
        self._part_canvas.pack(fill="x", padx=8, pady=(0, 6))
        self._part_canvas.pack_propagate(False)  # 关键：不让 canvas 被内容撑大

        self._part_inner = ctk.CTkFrame(self._part_canvas, fg_color="transparent")
        self._part_canvas_window = self._part_canvas.create_window((0, 0), window=self._part_inner, anchor="nw")

        # 鼠标滚轮绑定
        self._part_canvas.bind("<Enter>", lambda e: self._part_canvas.bind_all("<MouseWheel>", self._on_part_scroll))
        self._part_canvas.bind("<Leave>", lambda e: self._part_canvas.unbind_all("<MouseWheel>"))

        # 宽度自适应
        self._part_inner.bind("<Configure>", self._on_part_inner_configure)

    def _on_part_scroll(self, event):
        """分P列表鼠标滚轮滚动"""
        self._part_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _on_part_inner_configure(self, event):
        """分P列表内容变化时更新滚动区域"""
        self._part_canvas.configure(scrollregion=self._part_canvas.bbox("all"))
        self._part_canvas.itemconfig(self._part_canvas_window, width=self._part_canvas.winfo_width())

    def _toggle_select_all(self):
        """全选/取消全选"""
        if not self._part_vars:
            return
        # 检查当前是否全部选中
        all_checked = all(v.get() for v in self._part_vars)
        new_val = not all_checked
        for v in self._part_vars:
            v.set(new_val)
        self.select_all_btn.configure(text="取消全选" if new_val else "全选")

    def _update_multi_p_list(self, info: dict):
        """更新多P列表显示（紧凑3列布局，保留缩略图）"""
        entries = info.get("entries")
        if not entries or len(entries) <= 1:
            self.multi_p_frame.pack_forget()
            return

        # 清空旧内容
        for w in self._part_inner.winfo_children():
            w.destroy()
        self._part_vars.clear()
        self._part_checkboxes.clear()
        self._thumb_refs = []

        COLS = 3
        row = None

        for i, entry in enumerate(entries):
            var = ctk.BooleanVar(value=True)
            self._part_vars.append(var)

            title = entry.get("title", f"第{i+1}P")
            dur = entry.get("duration", 0)
            label = f"P{entry.get('index', i+1)}: {title}"
            if dur:
                m, s = divmod(int(dur), 60)
                label += f" [{m}:{s:02d}]"

            if i % COLS == 0:
                row = ctk.CTkFrame(self._part_inner, fg_color="transparent")
                row.pack(fill="x", pady=0)

            cell = ctk.CTkFrame(row, fg_color="transparent")
            cell.pack(side="left", fill="x", expand=True, padx=(0, 4))

            # 小缩略图
            thumb_label = ctk.CTkLabel(
                cell, text="", width=44, height=25,
                fg_color=BG_INPUT, corner_radius=2,
            )
            thumb_label.pack(side="left", padx=(0, 3))

            cb = ctk.CTkCheckBox(
                cell, text=label, variable=var,
                font=ctk.CTkFont(size=11),
                checkbox_width=15, checkbox_height=15,
                fg_color=ACCENT, hover_color=ACCENT_HOVER,
                text_color=TEXT_DIM,
            )
            cb.pack(side="left", fill="x", expand=True)
            self._part_checkboxes.append(cb)

            thumb_url = entry.get("thumbnail", "")
            if thumb_url:
                self._load_part_thumbnail(thumb_label, thumb_url, small=True)

        self.multi_p_title.configure(text=f"分P列表（共 {len(entries)} 个）")
        self.select_all_btn.configure(text="取消全选")

        self.multi_p_frame.pack(fill="x", pady=(0, 10),
                                 after=self.info_frame, before=self._download_opts_frame)

    def _load_part_thumbnail(self, label, url, small=False):
        """异步加载分P缩略图"""
        def load():
            try:
                import urllib.request
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=8) as resp:
                    img_data = resp.read()
                import io
                img = Image.open(io.BytesIO(img_data))
                if small:
                    img = img.resize((44, 25), Image.LANCZOS)
                    ctk_img = ctk.CTkImage(light_image=img, dark_image=img, size=(44, 25))
                else:
                    img = img.resize((64, 36), Image.LANCZOS)
                    ctk_img = ctk.CTkImage(light_image=img, dark_image=img, size=(64, 36))
                self._thumb_refs.append(ctk_img)
                self.after(0, lambda: label.configure(image=ctk_img, text=""))
            except Exception:
                pass

        threading.Thread(target=load, daemon=True).start()

    def _build_download_options(self):
        """下载选项区域"""
        self._download_opts_frame = ctk.CTkFrame(self.main_frame, fg_color=BG_CARD, corner_radius=10,
                                                 border_width=1, border_color=BORDER)
        self._download_opts_frame.pack(fill="x", pady=(0, 10))
        frame = self._download_opts_frame

        inner = ctk.CTkFrame(frame, fg_color="transparent")
        inner.pack(fill="x", padx=14, pady=12)

        # 第一行：画质 + 保存路径
        row1 = ctk.CTkFrame(inner, fg_color="transparent")
        row1.pack(fill="x", pady=(0, 10))

        ctk.CTkLabel(row1, text="画质:", font=ctk.CTkFont(size=13),
                     text_color=TEXT_DIM).pack(side="left")

        self.quality_var = ctk.StringVar(value=self.config.get("quality", "4K 超清"))
        self.quality_menu = ctk.CTkOptionMenu(
            row1,
            values=Config.QUALITY_LIST,
            variable=self.quality_var,
            width=160, height=32,
            font=ctk.CTkFont(size=12),
            fg_color=BG_INPUT, button_color=ACCENT, button_hover_color=ACCENT_HOVER,
            text_color=TEXT,
        )
        self.quality_menu.pack(side="left", padx=(6, 20))

        # VIP 画质提示标签
        self.vip_hint_label = ctk.CTkLabel(
            row1, text="",
            font=ctk.CTkFont(size=11),
            text_color=WARNING, anchor="w",
        )
        self.vip_hint_label.pack(side="left", padx=(0, 10))

        ctk.CTkLabel(row1, text="保存到:", font=ctk.CTkFont(size=13),
                     text_color=TEXT_DIM).pack(side="left")

        self.path_var = ctk.StringVar(value=self.config.get("download_path", ""))
        path_entry = ctk.CTkEntry(
            row1, textvariable=self.path_var,
            width=220, height=32,
            font=ctk.CTkFont(size=12),
            fg_color=BG_INPUT,
            border_width=1, border_color=BORDER,
            text_color=TEXT,
        )
        path_entry.pack(side="left", padx=(6, 6))

        ctk.CTkButton(
            row1, text="浏览", width=50, height=32,
            font=ctk.CTkFont(size=12),
            fg_color=BG_INPUT, hover_color=ACCENT,
            border_width=1, border_color=BORDER,
            text_color=TEXT_DIM,
            command=self._browse_path,
        ).pack(side="left")

        # 第二行：选项勾选
        row2 = ctk.CTkFrame(inner, fg_color="transparent")
        row2.pack(fill="x")

        self.danmaku_var = ctk.BooleanVar(value=self.config.get("download_danmaku", True))
        ctk.CTkCheckBox(
            row2, text="下载弹幕", variable=self.danmaku_var,
            font=ctk.CTkFont(size=13),
            checkbox_width=20, checkbox_height=20,
            fg_color=ACCENT, hover_color=ACCENT_HOVER,
            text_color=TEXT_DIM,
            command=self._on_option_change,
        ).pack(side="left", padx=(0, 16))

        self.subtitle_var = ctk.BooleanVar(value=self.config.get("download_subtitle", True))
        ctk.CTkCheckBox(
            row2, text="下载字幕", variable=self.subtitle_var,
            font=ctk.CTkFont(size=13),
            checkbox_width=20, checkbox_height=20,
            fg_color=ACCENT, hover_color=ACCENT_HOVER,
            text_color=TEXT_DIM,
            command=self._on_option_change,
        ).pack(side="left", padx=(0, 16))

    def _build_progress(self):
        """下载进度区域"""
        frame = ctk.CTkFrame(self.main_frame, fg_color="transparent")
        frame.pack(fill="x", pady=(0, 8))

        # 进度条（粉→紫渐变，品牌签名元素）
        self.progress_bar = ctk.CTkProgressBar(
            frame, height=8, corner_radius=4,
            fg_color=BG_INPUT,
            progress_color=ACCENT,
        )
        self.progress_bar.pack(fill="x", pady=(0, 6))
        self.progress_bar.set(0)

        # 状态行
        status_frame = ctk.CTkFrame(frame, fg_color="transparent")
        status_frame.pack(fill="x")

        self.status_label = ctk.CTkLabel(
            status_frame, text="就绪",
            font=ctk.CTkFont(size=12),
            text_color=TEXT_DIM, anchor="w",
        )
        self.status_label.pack(side="left")

        self.speed_label = ctk.CTkLabel(
            status_frame, text="",
            font=ctk.CTkFont(size=12),
            text_color=TEXT_DIM, anchor="e",
        )
        self.speed_label.pack(side="right")

        # 下载/取消按钮
        btn_frame = ctk.CTkFrame(self.main_frame, fg_color="transparent")
        btn_frame.pack(fill="x", pady=(4, 8))

        self.download_btn = ctk.CTkButton(
            btn_frame, text="开始下载", width=200, height=40,
            font=ctk.CTkFont(size=15, weight="bold"),
            fg_color=ACCENT, hover_color=ACCENT_HOVER,
            text_color="#FFFFFF",
            command=self._start_download,
            state="disabled",
        )
        self.download_btn.pack()

    def _build_log(self):
        """日志输出区域"""
        frame = ctk.CTkFrame(self.main_frame, fg_color=BG_CARD, corner_radius=10,
                             border_width=1, border_color=BORDER)
        frame.pack(fill="both", expand=True)

        ctk.CTkLabel(
            frame, text="  下载日志", font=ctk.CTkFont(size=12),
            text_color=TEXT_DIM, anchor="w",
        ).pack(fill="x", padx=10, pady=(8, 2))

        self.log_text = ctk.CTkTextbox(
            frame, height=120,
            font=ctk.CTkFont(family="Consolas", size=11),
            fg_color=BG_CARD, text_color=TEXT_DIM,
            border_width=1, border_color=BORDER,
            corner_radius=8, wrap="word",
        )
        self.log_text.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.log_text.configure(state="disabled")

    # ── 功能逻辑 ──

    def _log(self, msg: str, color: str = None):
        """向日志区追加消息"""
        self.log_text.configure(state="normal")
        prefix = ""
        if color == SUCCESS:
            prefix = "[完成] "
        elif color == ERROR:
            prefix = "[错误] "
        elif color == WARNING:
            prefix = "[警告] "
        else:
            prefix = "[信息] "

        self.log_text.insert("end", prefix + msg + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _fetch_info(self):
        """解析视频信息"""
        url = self.url_entry.get().strip()
        if not url:
            self._log("请输入 Bilibili 视频链接", WARNING)
            return

        if not BiliDownloader.is_bilibili_url(url):
            self._log("不是有效的 Bilibili 视频链接", ERROR)
            return

        self.fetch_btn.configure(text="解析中...", state="disabled")
        self._log("正在解析视频信息...")

        def do_fetch():
            try:
                info = self.downloader.fetch_info(url)
                self._video_info = info

                is_multi_p = bool(info.get("entries"))

                # 多P视频：用B站API补充顶层信息（extract_flat 模式不返回封面/UP主等）和每P缩略图
                if is_multi_p:
                    bvid = info.get("bvid", "")
                    if bvid:
                        detail = BiliAuth.fetch_video_detail(bvid)
                        if detail:
                            if not info.get("thumbnail") and detail.get("thumbnail"):
                                info["thumbnail"] = detail["thumbnail"]
                            if not info.get("uploader") and detail.get("uploader"):
                                info["uploader"] = detail["uploader"]
                            if not info.get("duration") and detail.get("duration"):
                                info["duration"] = detail["duration"]
                            if not info.get("view_count") and detail.get("view_count"):
                                info["view_count"] = detail["view_count"]
                            pages_info = detail.get("pages", [])
                            for entry in info["entries"]:
                                idx = entry.get("index", 0) - 1
                                if 0 <= idx < len(pages_info):
                                    # 用B站API的标题覆盖（extract_flat标题不准确）
                                    entry["title"] = pages_info[idx].get("title", entry.get("title", ""))
                                    if not entry.get("thumbnail"):
                                        entry["thumbnail"] = pages_info[idx].get("thumbnail", "")

                # 统一更新 UI（此时信息已补全）
                self.after(0, lambda: self._update_video_info(info))
                self.after(0, lambda: self._log(f"解析成功: {info['title']}"))

                if info.get("uploader"):
                    self.after(0, lambda: self._log(
                        f"UP主: {info['uploader']}  |  时长: {self._format_duration(info['duration'])}"))

                if is_multi_p:
                    self.after(0, lambda: self._log(f"多P视频，共 {len(info['entries'])} 个分P"))

                # 无论是否多P都更新分P列表（单P时自动隐藏）
                self.after(0, lambda: self._update_multi_p_list(info))

                if info.get("available_qualities"):
                    q_str = ", ".join(info["available_qualities"])
                    self.after(0, lambda: self._log(f"可用画质: {q_str}"))

                # VIP 画质提示：如果最高画质非4K且用户选了4K，提示需要大会员
                has_4k = any("4K" in q for q in info.get("available_qualities", []))
                is_bangumi = "bangumi" in (info.get("extractor", "") or "").lower()
                if not has_4k:
                    sessdata = self.config.get("sessdata", "")
                    browser = self.config.get("cookies_from_browser", "")
                    if not sessdata and not browser:
                        hint = "未登录，4K画质不可用"
                        self.after(0, lambda: self.vip_hint_label.configure(
                            text=hint, text_color=ERROR))
                    else:
                        # 已登录但无4K，可能是内容本身不支持
                        self.after(0, lambda: self.vip_hint_label.configure(
                            text="该内容不支持4K", text_color=TEXT_DIM))
                else:
                    self.after(0, lambda: self.vip_hint_label.configure(
                        text="", text_color=TEXT_DIM))

                # 番剧/大会员专属内容提示
                if is_bangumi:
                    self.after(0, lambda: self._log("检测到番剧/影视内容，大会员可下载更高画质", WARNING))

                self.after(0, lambda: self.download_btn.configure(state="normal"))

            except Exception as e:
                self.after(0, lambda: self._log(f"解析失败: {e}", ERROR))
            finally:
                self.after(0, lambda: self.fetch_btn.configure(text="解析", state="normal"))

        threading.Thread(target=do_fetch, daemon=True).start()

    def _update_video_info(self, info: dict):
        """更新视频信息显示"""
        # 确保信息区域可见
        # 确保 info_frame 在正确位置显示
        self.info_frame.pack(fill="x", pady=(0, 10), before=self._download_opts_frame)

        self.title_label.configure(text=info["title"])

        meta_parts = []
        if info.get("uploader"):
            meta_parts.append(f"UP: {info['uploader']}")
        if info.get("duration"):
            meta_parts.append(self._format_duration(info["duration"]))
        if info.get("view_count"):
            meta_parts.append(f"播放: {info['view_count']:,}")

        self.meta_label.configure(text="  |  ".join(meta_parts))

        # 尝试加载缩略图
        if info.get("thumbnail"):
            self._load_thumbnail(info["thumbnail"])

    def _load_thumbnail(self, url: str):
        """异步加载缩略图"""
        def load():
            try:
                import urllib.request
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    img_data = resp.read()

                import io
                img = Image.open(io.BytesIO(img_data))
                img = img.resize((160, 90), Image.LANCZOS)
                ctk_img = ctk.CTkImage(light_image=img, dark_image=img, size=(160, 90))

                self.after(0, lambda: self.thumb_label.configure(image=ctk_img, text=""))
                self._thumb_ref = ctk_img  # 防止被回收
            except Exception:
                self.after(0, lambda: self.thumb_label.configure(text="封面"))

        threading.Thread(target=load, daemon=True).start()

    def _start_download(self):
        """开始下载"""
        if not self._video_info:
            self._log("请先解析视频", WARNING)
            return

        if self._is_downloading:
            self._cancel_download()
            return

        # 保存选项到配置
        quality = self.quality_var.get()
        self.config.set("quality", quality)
        self.config.set("download_danmaku", self.danmaku_var.get())
        self.config.set("download_subtitle", self.subtitle_var.get())
        self.config.set("download_path", self.path_var.get())

        # 4K画质 + 大会员状态检测
        if quality == "4K 超清":
            sessdata = self.config.get("sessdata", "")
            browser = self.config.get("cookies_from_browser", "")
            if not sessdata and not browser:
                self._log("4K画质需要大会员认证，请先在设置中登录", WARNING)
                self.vip_hint_label.configure(text="未登录，4K不可用", text_color=ERROR)
                return
            # 已有 Cookie 但不确定是否为大会员，在下载中自然降级即可

        self._is_downloading = True
        self._stop_event.clear()
        self.download_btn.configure(text="取消下载", fg_color=ERROR, hover_color="#e8384f")
        self.progress_bar.set(0)
        self.status_label.configure(text="正在下载...", text_color=WARNING)

        url = self.url_entry.get().strip()

        # 多P选择：获取选中的分P索引
        # 单P视频传 None（表示下载全部），多P视频传选中的索引列表
        selected_parts = None
        if self._part_vars and self._video_info.get("entries"):
            selected_parts = []
            for i, var in enumerate(self._part_vars):
                if var.get():
                    selected_parts.append(i + 1)  # 1-based
            if not selected_parts:
                self._log("请至少选择一个分P", WARNING)
                self._is_downloading = False
                self.download_btn.configure(text="开始下载", fg_color=ACCENT, hover_color=ACCENT_HOVER)
                return
        self._selected_parts = selected_parts

        def do_download():
            try:
                # 启动进度更新循环
                self.after(0, self._update_progress)

                # 1. 下载视频（内部已合并画质预检测，只做一次 extract_info）
                self.after(0, lambda: self._log(f"开始下载: {self._video_info['title']}"))
                self.after(0, lambda: self._log(f"选择画质: {quality}"))
                if selected_parts:
                    self.after(0, lambda: self._log(f"选中分P: {','.join(map(str, selected_parts))}"))

                progress = self.downloader.download(url, quality, playlist_items=selected_parts)

                # 下载过程中已提取画质信息，显示到日志
                if progress.available_qualities:
                    self.after(0, lambda: self._log(f"可选画质: {', '.join(progress.available_qualities)}"))
                if progress.actual_quality:
                    self.after(0, lambda: self._log(f"实际下载: {progress.actual_quality}"))
                if progress.vip_required:
                    self.after(0, lambda: self._log(progress.vip_hint, WARNING))
                elif progress.vip_hint:
                    self.after(0, lambda: self._log(progress.vip_hint, WARNING))

                if progress.status == "done":
                    video_path = self.downloader.get_download_path_for(self._video_info)

                    # 2. 下载弹幕
                    if self.danmaku_var.get():
                        self._download_danmaku(video_path)

                    # 3. 下载字幕
                    if self.subtitle_var.get():
                        self._download_subtitle(video_path)

                    self.after(0, self._on_download_done)
                elif progress.status == "cancelled":
                    self.after(0, lambda: self._log("下载已取消", WARNING))
                    self.after(0, self._on_download_done)
                else:
                    err = progress.error_msg or "未知错误"
                    self.after(0, lambda: self._log(f"下载失败: {err}", ERROR))
                    self.after(0, self._on_download_done)

            except Exception as e:
                self.after(0, lambda: self._log(f"下载出错: {e}", ERROR))
                self.after(0, self._on_download_done)

        threading.Thread(target=do_download, daemon=True).start()

    def _update_progress(self):
        """定时更新进度显示"""
        if not self._is_downloading:
            return

        p = self.downloader.progress
        if p.status == "downloading":
            pct = min(p.percent, 100)
            self.progress_bar.set(pct / 100)
            speed_str = self._format_speed(p.speed)
            self.status_label.configure(text=f"下载中... {pct:.1f}%", text_color=TEXT)
            self.speed_label.configure(text=speed_str)
        elif p.status == "extracting":
            self.status_label.configure(text="正在提取视频信息...", text_color=WARNING)
            self.speed_label.configure(text="")
        elif p.status == "merging":
            self.status_label.configure(text="正在合并音视频...", text_color=WARNING)
            self.speed_label.configure(text="")
            self.progress_bar.set(1.0)

        if self._is_downloading:
            self.after(300, self._update_progress)

    def _cancel_download(self):
        """取消下载"""
        self._is_downloading = False
        self._stop_event.set()
        self.downloader.cancel()
        self._log("正在取消...", WARNING)

    def _on_download_done(self):
        """下载完成回调"""
        self._is_downloading = False
        self.download_btn.configure(text="开始下载", fg_color=ACCENT, hover_color=ACCENT_HOVER)
        self.status_label.configure(text="就绪", text_color=TEXT_DIM)
        self.speed_label.configure(text="")
        self.progress_bar.set(1.0 if self.downloader.progress.status == "done" else 0)

    def _download_danmaku(self, video_path: str):
        """下载弹幕"""
        self.after(0, lambda: self._log("正在下载弹幕..."))
        try:
            cid = self._get_cid()
            if cid:
                cookies = self._get_cookies()
                # 下载多个分段
                all_danmaku = []
                for seg in range(1, 4):
                    dm_list = self.danmaku_proc.download_danmaku(cid, cookies, seg)
                    if not dm_list:
                        break
                    all_danmaku.extend(dm_list)

                if all_danmaku:
                    ass_path = self.danmaku_proc.save_ass(all_danmaku, video_path)
                    self.after(0, lambda: self._log(f"弹幕已保存: {os.path.basename(ass_path)} ({len(all_danmaku)} 条)", SUCCESS))
                else:
                    self.after(0, lambda: self._log("该视频暂无弹幕", WARNING))
            else:
                self.after(0, lambda: self._log("无法获取 CID，跳过弹幕下载", WARNING))
        except Exception as e:
            self.after(0, lambda: self._log(f"弹幕下载失败: {e}", ERROR))

    def _download_subtitle(self, video_path: str):
        """下载字幕"""
        self.after(0, lambda: self._log("正在下载字幕..."))
        try:
            bvid = self._video_info.get("bvid", "")
            cid = self._get_cid()
            if bvid and cid:
                cookies = self._get_cookies()
                sub_info = self.subtitle_proc.get_subtitle_info(bvid, cid, cookies)
                if sub_info:
                    for sub in sub_info:
                        sub_data = self.subtitle_proc.download_subtitle(sub["subtitle_url"])
                        if sub_data:
                            srt_path = self.subtitle_proc.save_srt(sub_data, video_path)
                            lang = sub.get("lan_doc", "")
                            self.after(0, lambda l=lang, p=srt_path: self._log(
                                f"字幕已保存: {os.path.basename(p)} ({l})", SUCCESS))
                else:
                    self.after(0, lambda: self._log("该视频暂无CC字幕", WARNING))
            else:
                self.after(0, lambda: self._log("无法获取视频信息，跳过字幕下载", WARNING))
        except Exception as e:
            self.after(0, lambda: self._log(f"字幕下载失败: {e}", ERROR))

    # ── 设置对话框 ──

    def _open_settings(self):
        """打开设置对话框（二维码内嵌版）"""
        dialog = ctk.CTkToplevel(self)
        dialog.title("BiliGo 设置")
        dialog.geometry("520x720")
        dialog.configure(fg_color=BG)
        dialog.transient(self)
        dialog.grab_set()

        # 居中
        dialog.update_idletasks()
        x = self.winfo_x() + (self.winfo_width() - 520) // 2
        y = self.winfo_y() + (self.winfo_height() - 720) // 2
        dialog.geometry(f"+{x}+{y}")

        container = ctk.CTkFrame(dialog, fg_color="transparent")
        container.pack(fill="both", expand=True, padx=20, pady=20)

        # ── 账号设置 ──
        ctk.CTkLabel(container, text="账号与登录", font=ctk.CTkFont(size=16, weight="bold"),
                     text_color=ACCENT).pack(anchor="w", pady=(0, 8))

        login_frame = ctk.CTkFrame(container, fg_color=BG_INPUT, corner_radius=8,
                                   border_width=1, border_color=BORDER)
        login_frame.pack(fill="x", pady=(0, 10))
        login_inner = ctk.CTkFrame(login_frame, fg_color="transparent")
        login_inner.pack(fill="x", padx=12, pady=10)

        # 当前登录状态
        current_sessdata = self.config.get("sessdata", "")
        login_info = BiliAuth.verify_login(current_sessdata) if current_sessdata else None
        is_logged_in = login_info and login_info.get("is_login")
        is_vip = login_info and login_info.get("is_vip")

        if is_logged_in:
            status_text = f"已登录: {login_info.get('uname', '未知')}"
            status_color = SUCCESS
            if is_vip:
                status_text += "  [大会员]"
                status_color = ACCENT
        else:
            status_text = "未登录（无法下载大会员4K视频）"
            status_color = ERROR

        self._settings_login_label = ctk.CTkLabel(
            login_inner, text=status_text,
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=status_color, anchor="w",
        )
        self._settings_login_label.pack(fill="x", pady=(0, 8))

        # ── 内嵌二维码区域（简约水平布局，无多余边框） ──
        qr_row = ctk.CTkFrame(login_inner, fg_color="transparent")
        qr_row.pack(fill="x", pady=(0, 4))

        # 左侧：二维码图片（品牌色边框圆角框）
        self._settings_qr_label = ctk.CTkLabel(
            qr_row, text="点击生成",
            width=220, height=220,
            fg_color=BG_INPUT,
            corner_radius=12,
            border_width=1, border_color=BORDER,
            font=ctk.CTkFont(size=11), text_color=TEXT_DIM,
        )
        self._settings_qr_label.pack(side="left", padx=(0, 14))

        # 右侧：提示 + 状态 + 刷新按钮
        qr_right = ctk.CTkFrame(qr_row, fg_color="transparent")
        qr_right.pack(side="left", fill="y")

        ctk.CTkLabel(
            qr_right, text="扫码登录",
            font=ctk.CTkFont(size=14, weight="bold"),
            text_color=TEXT, anchor="w",
        ).pack(fill="x", pady=(0, 4))

        ctk.CTkLabel(
            qr_right, text="用B站APP扫描二维码\n即可登录，支持4K下载",
            font=ctk.CTkFont(size=11),
            text_color=TEXT_DIM, anchor="w", justify="left",
        ).pack(fill="x", pady=(0, 10))

        self._settings_qr_status = ctk.CTkLabel(
            qr_right, text="",
            font=ctk.CTkFont(size=12),
            text_color=TEXT_DIM, anchor="w",
        )
        self._settings_qr_status.pack(fill="x", pady=(0, 8))

        self._settings_qr_refresh_btn = ctk.CTkButton(
            qr_right, text="生成二维码", width=130, height=32,
            font=ctk.CTkFont(size=13, weight="bold"),
            fg_color=ACCENT, hover_color=ACCENT_HOVER,
            text_color="#FFFFFF",
            command=lambda: self._generate_and_show_qrcode_inline(dialog),
        )
        self._settings_qr_refresh_btn.pack(anchor="w")

        # 分隔线
        ctk.CTkLabel(container, text="", height=2,
                     fg_color=BG_INPUT).pack(fill="x", pady=(2, 8))

        # ── Cookie 来源 ──
        ctk.CTkLabel(container, text="Cookie 来源", font=ctk.CTkFont(size=16, weight="bold"),
                     text_color=ACCENT).pack(anchor="w", pady=(12, 8))

        cookie_mode_frame = ctk.CTkFrame(container, fg_color="transparent")
        cookie_mode_frame.pack(fill="x", pady=(0, 8))

        ctk.CTkLabel(cookie_mode_frame, text="浏览器Cookie导入：",
                     font=ctk.CTkFont(size=13), text_color=TEXT_DIM).pack(side="left")

        saved_browser = self.config.get("cookies_from_browser", "")
        self._settings_cookie_mode = ctk.StringVar(value=saved_browser if saved_browser else "sessdata")

        ctk.CTkOptionMenu(
            cookie_mode_frame,
            values=["sessdata", "edge", "chrome"],
            variable=self._settings_cookie_mode,
            width=120, height=32,
            font=ctk.CTkFont(size=12),
            fg_color=BG_INPUT, button_color=ACCENT, button_hover_color=ACCENT_HOVER,
            text_color=TEXT,
            command=self._on_cookie_mode_change,
        ).pack(side="left", padx=(8, 10))

        self._cookie_mode_hint = ctk.CTkLabel(
            cookie_mode_frame, text="",
            font=ctk.CTkFont(size=11),
            text_color=TEXT_DIM, anchor="w",
        )
        self._cookie_mode_hint.pack(side="left")
        # 初始化提示
        self._update_cookie_mode_hint()

        # SESSDATA 手动输入（仅 sessdata 模式下显示）
        self._sessdata_frame = ctk.CTkFrame(container, fg_color="transparent")
        self._sessdata_frame.pack(fill="x")

        ctk.CTkLabel(self._sessdata_frame, text="手动输入 SESSDATA（备选方式）：",
                     font=ctk.CTkFont(size=12), text_color=TEXT_DIM).pack(anchor="w", pady=(0, 4))

        self._settings_sessdata_entry = ctk.CTkEntry(
            self._sessdata_frame,
            placeholder_text="F12 → Application → Cookies → bilibili.com → SESSDATA",
            font=ctk.CTkFont(size=12),
            height=36,
            fg_color=BG_INPUT,
            border_width=1, border_color=BORDER,
            text_color=TEXT, placeholder_text_color=TEXT_DIM,
        )
        self._settings_sessdata_entry.pack(fill="x")
        if current_sessdata:
            self._settings_sessdata_entry.insert(0, current_sessdata)

        # ── 下载设置 ──
        ctk.CTkLabel(container, text="下载设置", font=ctk.CTkFont(size=16, weight="bold"),
                     text_color=ACCENT).pack(anchor="w", pady=(12, 8))

        concur_frame = ctk.CTkFrame(container, fg_color="transparent")
        concur_frame.pack(fill="x", pady=(0, 8))

        ctk.CTkLabel(concur_frame, text="并发下载线程数：",
                     font=ctk.CTkFont(size=13), text_color=TEXT_DIM).pack(side="left")

        concur_var = ctk.StringVar(value=str(self.config.get("concurrent_fragments", 4)))
        ctk.CTkOptionMenu(
            concur_frame,
            values=["1", "2", "4", "8"],
            variable=concur_var,
            width=80, height=32,
            font=ctk.CTkFont(size=12),
            fg_color=BG_INPUT, button_color=ACCENT, button_hover_color=ACCENT_HOVER,
            text_color=TEXT,
        ).pack(side="left", padx=(8, 0))

        # FFmpeg 状态
        ffmpeg_info = self.config.get_ffmpeg_path()
        ffmpeg_frame = ctk.CTkFrame(container, fg_color="transparent")
        ffmpeg_frame.pack(fill="x", pady=(0, 6))

        ctk.CTkLabel(ffmpeg_frame, text="FFmpeg：",
                     font=ctk.CTkFont(size=13), text_color=TEXT_DIM).pack(side="left")

        if ffmpeg_info:
            ctk.CTkLabel(ffmpeg_frame, text="已安装",
                         font=ctk.CTkFont(size=12), text_color=SUCCESS).pack(side="left", padx=(0, 10))
        else:
            ctk.CTkLabel(ffmpeg_frame, text="未安装（画质受限）",
                         font=ctk.CTkFont(size=12), text_color=WARNING).pack(side="left", padx=(0, 10))

            ctk.CTkButton(
                ffmpeg_frame, text="一键安装", width=80, height=28,
                font=ctk.CTkFont(size=12, weight="bold"),
                fg_color=ACCENT, hover_color=ACCENT_HOVER,
                text_color="#FFFFFF",
                command=self._install_ffmpeg,
            ).pack(side="left")

        # 保存按钮
        def save_settings():
            sd = self._settings_sessdata_entry.get().strip()
            self.config.set("sessdata", sd)

            # Cookie 来源
            cookie_mode = self._settings_cookie_mode.get()
            if cookie_mode == "sessdata":
                self.config.set("cookies_from_browser", "")
            else:
                self.config.set("cookies_from_browser", cookie_mode)

            try:
                self.config.set("concurrent_fragments", int(concur_var.get()))
            except ValueError:
                pass
            self.config.save()
            self._log("设置已保存")

            # 刷新登录状态
            self._refresh_login_status()

            dialog.destroy()

        ctk.CTkButton(
            container, text="保存", width=120, height=36,
            font=ctk.CTkFont(size=14, weight="bold"),
            fg_color=ACCENT, hover_color=ACCENT_HOVER,
            command=save_settings,
        ).pack(pady=(10, 0))

        # 设置页关闭时停止二维码轮询
        self._qr_poll_id = None

        def on_dialog_close():
            if self._qr_poll_id:
                try:
                    self.after_cancel(self._qr_poll_id)
                except Exception:
                    pass
                self._qr_poll_id = None
            dialog.destroy()

        dialog.protocol("WM_DELETE_WINDOW", on_dialog_close)

    def _generate_and_show_qrcode_inline(self, dialog):
        """内嵌模式：生成并显示二维码，启动轮询"""
        # 取消之前的轮询
        if self._qr_poll_id:
            try:
                self.after_cancel(self._qr_poll_id)
            except Exception:
                pass
            self._qr_poll_id = None

        self._settings_qr_label.configure(text="正在生成二维码...", image="")
        self._settings_qr_status.configure(text="", text_color=TEXT_DIM)
        self._settings_qr_refresh_btn.configure(text="生成中...")

        auth = BiliAuth()

        def do_generate():
            try:
                result = auth.generate_qrcode()
                qr_img = result["image"]
                qr_img = qr_img.resize((220, 220), Image.LANCZOS)
                ctk_img = ctk.CTkImage(light_image=qr_img, dark_image=qr_img, size=(220, 220))

                self.after(0, lambda: self._settings_qr_label.configure(image=ctk_img, text=""))
                self._qr_img_ref = ctk_img
                self.after(0, lambda: self._settings_qr_status.configure(text="请用B站APP扫描二维码"))
                self.after(0, lambda: self._settings_qr_refresh_btn.configure(text="刷新二维码"))

                # 开始轮询（按钮保持可用，随时可刷新）
                self._poll_qrcode_inline(auth, dialog)

            except Exception as e:
                self.after(0, lambda: self._settings_qr_label.configure(
                    text="生成失败", image=""))
                self.after(0, lambda: self._settings_qr_status.configure(
                    text=f"错误: {e}", text_color=ERROR))
                self.after(0, lambda: self._settings_qr_refresh_btn.configure(text="重试"))

        threading.Thread(target=do_generate, daemon=True).start()

    def _poll_qrcode_inline(self, auth, dialog):
        """内嵌模式：轮询二维码扫码状态"""
        def do_poll():
            # 如果设置页已关闭，停止轮询
            if not dialog.winfo_exists():
                return

            try:
                result = auth.poll_qrcode()
                status = result["status"]

                if status == "confirmed":
                    cookies = result.get("cookies", {})
                    self.after(0, lambda: self._log(
                        f"扫码确认，提取Cookie: {list(cookies.keys()) if cookies else '空'}"))

                    if cookies and cookies.get("SESSDATA"):
                        sessdata = cookies["SESSDATA"]
                        self.config.set("sessdata", sessdata)
                        if cookies.get("bili_jct"):
                            self.config.set("bili_jct", cookies["bili_jct"])
                        if cookies.get("DedeUserID"):
                            self.config.set("DedeUserID", cookies["DedeUserID"])
                        self.config.save()

                        self.after(0, lambda: self._log("Cookie已保存，正在验证大会员身份..."))

                        login_info = BiliAuth.verify_login(sessdata)
                        uname = login_info.get("uname", "未知")
                        is_vip = login_info.get("is_vip", False)
                        is_login = login_info.get("is_login", False)
                        vip_tag = " [大会员]" if is_vip else ""

                        if not is_login:
                            self.after(0, lambda: self._log("Cookie验证失败：SESSDATA无效或已过期", ERROR))
                            self.after(0, lambda: self._settings_qr_status.configure(
                                text="认证失败，请重试", text_color=ERROR))
                            self.after(0, lambda: self._settings_qr_refresh_btn.configure(text="重试"))
                            return

                        # 更新内嵌状态文字
                        self.after(0, lambda: self._settings_qr_status.configure(
                            text=f"登录成功: {uname}{vip_tag}", text_color=SUCCESS))
                        self.after(0, lambda: self._log(
                            f"大会员登录成功: {uname}{vip_tag}", SUCCESS))

                        # 自动填充 SESSDATA 到手动输入框
                        self.after(0, lambda: self._fill_sessdata_entry(sessdata))

                        # 刷新主界面和设置页的登录状态
                        self.after(0, lambda: self._apply_login_status(login_info))

                        # 刷新按钮恢复可用（方便下次重新登录）
                        self.after(0, lambda: self._settings_qr_refresh_btn.configure(text="刷新二维码"))
                        return
                    else:
                        self.after(0, lambda: self._log(
                            f"扫码成功但未提取到Cookie, 原始数据: {result.get('raw_data', '')}", WARNING))
                        self.after(0, lambda: self._settings_qr_status.configure(
                            text="登录成功但未获取到Cookie", text_color=WARNING))
                        self.after(0, lambda: self._settings_qr_refresh_btn.configure(text="刷新二维码"))

                elif status == "expired":
                    self.after(0, lambda: self._settings_qr_status.configure(
                        text="二维码已失效，请点击刷新", text_color=ERROR))
                    self.after(0, lambda: self._settings_qr_refresh_btn.configure(text="刷新二维码"))
                    return

                elif status == "waiting":
                    self.after(0, lambda: self._settings_qr_status.configure(
                        text="已扫描，请在手机上确认登录...",
                        text_color=WARNING))

                elif status == "not_scanned":
                    pass

                elif status == "error":
                    msg = result.get("message", "")
                    self.after(0, lambda: self._log(f"轮询出错: {msg}", ERROR))

                # 继续轮询
                self._qr_poll_id = self.after(2000, lambda: self._poll_qrcode_inline(auth, dialog))

            except Exception as e:
                self.after(0, lambda: self._log(f"轮询异常: {e}", ERROR))
                self._qr_poll_id = self.after(3000, lambda: self._poll_qrcode_inline(auth, dialog))

        threading.Thread(target=do_poll, daemon=True).start()

    def _on_cookie_mode_change(self, value: str):
        """Cookie来源切换回调"""
        self._update_cookie_mode_hint()
        # 切换浏览器模式时隐藏/显示SESSDATA输入框
        if value == "sessdata":
            self._sessdata_frame.pack(fill="x")
        else:
            self._sessdata_frame.pack_forget()

    def _update_cookie_mode_hint(self):
        """更新Cookie来源提示文字"""
        try:
            mode = self._settings_cookie_mode.get()
            hints = {
                "sessdata": "使用二维码登录或手动输入SESSDATA",
                "edge": "从Edge浏览器提取Cookie（需关闭Edge）",
                "chrome": "从Chrome浏览器提取Cookie（需关闭Chrome）",
            }
            self._cookie_mode_hint.configure(text=hints.get(mode, ""))
        except Exception:
            pass

    def _fill_sessdata_entry(self, sessdata: str):
        """扫码成功后自动填充SESSDATA输入框"""
        try:
            if hasattr(self, '_settings_sessdata_entry') and self._settings_sessdata_entry.winfo_exists():
                self._settings_sessdata_entry.delete(0, "end")
                self._settings_sessdata_entry.insert(0, sessdata)
        except Exception:
            pass

    # ── 辅助方法 ──

    def _refresh_login_status(self):
        """异步刷新登录状态（主界面 + 设置页）"""
        def do_refresh():
            sessdata = self.config.get("sessdata", "")
            if sessdata:
                info = BiliAuth.verify_login(sessdata)
                self.after(0, lambda: self._apply_login_status(info))
            else:
                self.after(0, lambda: self._apply_login_status(None))

        threading.Thread(target=do_refresh, daemon=True).start()

    def _apply_login_status(self, login_info):
        """更新主界面和设置页的登录状态显示"""
        if login_info and login_info.get("is_login"):
            uname = login_info.get("uname", "")
            is_vip = login_info.get("is_vip", False)
            if is_vip:
                self._header_login_label.configure(
                    text=f"{uname} [大会员]", text_color=ACCENT)
            else:
                self._header_login_label.configure(
                    text=f"{uname}", text_color=SUCCESS)
        else:
            self._header_login_label.configure(
                text="未登录", text_color=TEXT_DIM)

        # 如果设置页的登录状态标签还存在，也更新它
        try:
            if hasattr(self, '_settings_login_label') and self._settings_login_label.winfo_exists():
                if login_info and login_info.get("is_login"):
                    uname = login_info.get("uname", "未知")
                    is_vip = login_info.get("is_vip", False)
                    if is_vip:
                        self._settings_login_label.configure(
                            text=f"已登录: {uname}  [大会员]", text_color=ACCENT)
                    else:
                        self._settings_login_label.configure(
                            text=f"已登录: {uname}", text_color=SUCCESS)
                else:
                    self._settings_login_label.configure(
                        text="未登录（无法下载大会员4K视频）", text_color=ERROR)
        except Exception:
            pass

    def _check_ffmpeg(self):
        """检查 FFmpeg，未找到则提示一键安装"""
        ffmpeg = self.config.get_ffmpeg_path()
        if ffmpeg:
            self._log(f"FFmpeg 已就绪: {ffmpeg}")
        else:
            self._log("FFmpeg 未找到，已自动切换为兼容模式（画质可能受限）", WARNING)
            self._log("建议点击「设置」一键安装 FFmpeg 以获得最高画质", WARNING)

    def _install_ffmpeg(self):
        """一键安装 FFmpeg"""
        def do_install():
            try:
                def on_progress(msg):
                    self.after(0, lambda: self._log(msg))

                result = self.config.download_ffmpeg(progress_callback=on_progress)
                self.after(0, lambda: self._log(f"FFmpeg 安装成功！", SUCCESS))
                self.after(0, lambda: self._log(f"路径: {result}"))
                self.after(0, lambda: self._check_ffmpeg())
            except Exception as e:
                self.after(0, lambda: self._log(f"FFmpeg 安装失败: {e}", ERROR))
                self.after(0, lambda: self._log("请手动下载: https://ffmpeg.org/download.html", WARNING))

        threading.Thread(target=do_install, daemon=True).start()

    def _browse_path(self):
        """浏览下载路径"""
        path = filedialog.askdirectory(title="选择下载保存路径")
        if path:
            self.path_var.set(path)
            self.config.set("download_path", path)

    def _on_option_change(self):
        """选项变更时保存"""
        self.config.set("download_danmaku", self.danmaku_var.get())
        self.config.set("download_subtitle", self.subtitle_var.get())
        self.config.save()

    def _get_cid(self) -> int:
        """获取视频 CID（从视频信息或 API）"""
        if self._video_info and self._video_info.get("cid"):
            return self._video_info["cid"]

        # 尝试从 yt-dlp 的 last_info 中获取
        try:
            url = self.url_entry.get().strip()
            import yt_dlp
            opts = {"quiet": True, "no_warnings": True}
            cookie_opt = self.config.get_ytdlp_cookie_option()
            opts.update(cookie_opt)
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
                # yt-dlp 不直接暴露 cid，尝试从 entries 获取
                if info.get("entries"):
                    entry = info["entries"][0]
                    # 从 url 中提取
                    for f in entry.get("formats", []):
                        manifest = f.get("manifest_url", "")
                        if "cid=" in manifest:
                            import re
                            m = re.search(r"cid=(\d+)", manifest)
                            if m:
                                return int(m.group(1))
        except Exception:
            pass

        return 0

    def _get_cookies(self) -> dict:
        """获取 Cookie 配置"""
        return {"sessdata": self.config.get("sessdata", "")}

    @staticmethod
    def _format_duration(seconds) -> str:
        if not seconds:
            return "未知"
        try:
            seconds = int(seconds)
            m, s = divmod(seconds, 60)
            h, m = divmod(m, 60)
            if h > 0:
                return f"{h}:{m:02d}:{s:02d}"
            return f"{m}:{s:02d}"
        except (ValueError, TypeError):
            return "未知"

    @staticmethod
    def _format_speed(bps: float) -> str:
        if not bps or bps <= 0:
            return ""
        if bps >= 1024 * 1024:
            return f"{bps / 1024 / 1024:.1f} MB/s"
        elif bps >= 1024:
            return f"{bps / 1024:.1f} KB/s"
        return f"{bps:.0f} B/s"

    def _get_widget_index(self, widget):
        """获取 widget 在父容器中的索引"""
        parent = widget.master
        for i, child in enumerate(parent.winfo_children()):
            if child == widget:
                return i
        return 0


def run_app():
    """启动应用"""
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("dark-blue")
    app = BiliGoApp()
    app.mainloop()


if __name__ == "__main__":
    run_app()
