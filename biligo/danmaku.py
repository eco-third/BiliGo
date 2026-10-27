"""BiliGo 弹幕处理器 - 下载 Bilibili 弹幕并转换为 ASS 字幕

视觉参数尽量对齐 B 站 web 播放器：

· 字号：B 站弹幕 XML 里 fontsize 的基准值是 25，对应「720p 的画面高度」，
  播放器按 视频高度 / 720 等比放大 —— 1080p 上是 37.5，4K 上是 75。
· 字体：微软雅黑（B 站播放器默认）。
· 描边：纯黑，宽度 = max(字号 / 25, 1)，无阴影；配合 ScaledBorderAndShadow。
· 位置：滚动弹幕从右边缘外侧进入、完全移出左边缘才结束（长弹幕不会中途消失）；
  顶部弹幕自上而下、底部弹幕自下而上排。
· 速度：所有弹幕同一个像素速度，所以长弹幕在屏幕上停留更久（B 站就是这样）。
· 密度：轨道按「上一条弹幕已经完全进场」释放，而不是等它走完，
  否则 3000 条弹幕会被丢掉一大半。
"""

import os
import requests
import xml.etree.ElementTree as ET
from typing import List, Optional


class DanmakuItem:
    """单条弹幕"""
    __slots__ = ("time", "mode", "font_size", "color", "timestamp", "pool", "uid", "text")

    def __init__(self, time: float, mode: int, font_size: int, color: int,
                 timestamp: int, pool: int, uid: str, text: str):
        self.time = time
        self.mode = mode
        self.font_size = font_size
        self.color = color
        self.timestamp = timestamp
        self.pool = pool
        self.uid = uid
        self.text = text


class DanmakuProcessor:
    """弹幕下载与 ASS 格式转换"""

    DANMAKU_API = "https://api.bilibili.com/x/v1/dm/list.so"
    DANMAKU_HISTORY_API = "https://api.bilibili.com/x/v2/dm/history?type=1&oid={oid}"

    # ── 视觉参数（对齐 B 站播放器）──
    FONT_NAME = "Microsoft YaHei"
    REF_HEIGHT = 720.0        # XML 里 fontsize=25 所对应的画面高度
    REF_FONT_SIZE = 25.0      # B 站弹幕 XML 的基准字号
    SCROLL_DURATION = 8.0     # 一条「零宽度」弹幕横穿整个画面所需秒数
    FIX_DURATION = 5.0        # 顶部/底部固定弹幕停留时间
    MAX_ROWS = 18             # 最多排多少行
    LINE_HEIGHT_RATIO = 1.3   # 行高 = 字号 × 该系数（留出约 1/3 字号的空隙）
    SIDE_GAP_RATIO = 0.015    # 进出画面时额外留白（占画面宽度的比例）

    # 运行时由 to_ass() 按视频实际分辨率覆盖
    PLAY_RES_X = 1920
    PLAY_RES_Y = 1080

    # B 站弹幕模式：1/2/3 滚动（右→左），4 底部，5 顶部，6 逆向滚动，7/8 高级
    MODE_SCROLL = (1, 2, 3)
    MODE_BOTTOM = 4
    MODE_TOP = 5
    MODE_REVERSE = 6

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "Referer": "https://www.bilibili.com",
        })

    # ── 下载与解析 ──

    def download_danmaku(self, cid: int, cookies: dict = None,
                         segment_index: int = 1) -> List[DanmakuItem]:
        """下载一段弹幕

        Args:
            cid: 视频 CID
            cookies: {"sessdata": "..."}
            segment_index: 弹幕分段索引（B 站有时会忽略它，每段返回同一份数据，
                           去重由调用方负责）
        """
        params = {"oid": cid, "segment_index": segment_index}
        if cookies and cookies.get("sessdata"):
            self.session.headers["Cookie"] = f"SESSDATA={cookies['sessdata']}"

        try:
            resp = self.session.get(self.DANMAKU_API, params=params, timeout=15)
            resp.raise_for_status()
        except requests.RequestException:
            return []

        return self._parse_xml(resp.content)

    def _parse_xml(self, xml_content: bytes) -> List[DanmakuItem]:
        """解析弹幕 XML"""
        items = []
        try:
            root = ET.fromstring(xml_content)
        except ET.ParseError:
            return items

        for elem in root.findall(".//d"):
            p_attr = elem.get("p", "")
            parts = p_attr.split(",")
            if len(parts) < 3:
                continue

            try:
                time = float(parts[0])
                mode = int(parts[1])
                font_size = int(parts[2])
                color = int(parts[3]) if len(parts) > 3 else 16777215
                timestamp = int(float(parts[4])) if len(parts) > 4 else 0
                pool = int(parts[5]) if len(parts) > 5 else 0
                uid = parts[6] if len(parts) > 6 else ""
                text = (elem.text or "").strip()
            except (ValueError, IndexError):
                continue

            if text:
                items.append(DanmakuItem(time, mode, font_size, color,
                                         timestamp, pool, uid, text))

        return items

    # ── 转 ASS ──

    def to_ass(self, danmakus: List[DanmakuItem],
               width: int = 1920, height: int = 1080) -> str:
        """把弹幕列表转成 ASS 字幕内容

        Args:
            danmakus: 弹幕列表
            width / height: 视频实际分辨率（ASS 画布尺寸，决定字号与坐标）
        """
        self.PLAY_RES_X = int(width or 1920)
        self.PLAY_RES_Y = int(height or 1080)

        base_size = self._base_font_size()
        line_h = base_size * self.LINE_HEIGHT_RATIO
        rows = self._row_count(line_h)
        speed = self._scroll_speed()          # px/s，所有弹幕一致
        gap = self.PLAY_RES_X * self.SIDE_GAP_RATIO
        entry_x = self.PLAY_RES_X + gap       # 弹幕进场的 x（左边缘在图外）

        lines = self._ass_header(base_size)

        scroll_free = [0.0] * rows    # 该轨道「下一条弹幕最早能进场」的时间
        top_free = [0.0] * rows
        bottom_free = [0.0] * rows

        for dm in sorted(danmakus, key=lambda d: d.time):
            if dm.time < 0:
                continue
            body_text = self._escape_text(dm.text)
            if not body_text:
                continue

            # 字号：XML 里通常是 25，个别是 18（小字弹幕）
            size = base_size * (dm.font_size or self.REF_FONT_SIZE) / self.REF_FONT_SIZE
            text_w = self._text_width(dm.text, size)
            color = self._to_ass_color(dm.color)
            tag = f"{{\\c{color}\\fs{size:.1f}}}"

            if dm.mode in self.MODE_SCROLL or dm.mode == self.MODE_REVERSE:
                row = self._pick_track(scroll_free, dm.time)
                if row is None:          # 满屏了，和 B 站一样直接不显示
                    continue
                y = row * line_h
                if dm.mode == self.MODE_REVERSE:
                    x1, x2 = -text_w - gap, entry_x
                else:
                    x1, x2 = entry_x, -text_w - gap
                duration = abs(x2 - x1) / speed
                move = f"{{\\an7\\move({x1:.0f},{y:.0f},{x2:.0f},{y:.0f})}}"
                lines.append(self._dialogue(dm.time, duration, tag + move + body_text))
                # 轨道何时释放：等这条弹幕「完全进场」（尾部越过右边缘）即可
                scroll_free[row] = dm.time + (text_w + gap) / speed

            elif dm.mode == self.MODE_TOP:
                row = self._pick_track(top_free, dm.time)
                if row is None:
                    continue
                y = row * line_h
                pos = f"{{\\an8\\pos({self.PLAY_RES_X // 2},{y:.0f})}}"
                lines.append(self._dialogue(dm.time, self.FIX_DURATION, tag + pos + body_text))
                top_free[row] = dm.time + self.FIX_DURATION

            elif dm.mode == self.MODE_BOTTOM:
                row = self._pick_track(bottom_free, dm.time)
                if row is None:
                    continue
                y = self.PLAY_RES_Y - row * line_h
                pos = f"{{\\an2\\pos({self.PLAY_RES_X // 2},{y:.0f})}}"
                lines.append(self._dialogue(dm.time, self.FIX_DURATION, tag + pos + body_text))
                bottom_free[row] = dm.time + self.FIX_DURATION

            # mode 7 / 8（高级弹幕、代码弹幕）不处理

        return "\n".join(lines)

    def save_ass(self, danmakus: List[DanmakuItem], output_path: str,
                 width: int = 1920, height: int = 1080) -> str:
        """保存为与视频同名的 ASS 文件

        Args:
            danmakus: 弹幕列表
            output_path: 视频文件路径，ASS 存成同名 .ass
            width / height: 视频实际分辨率
        Returns:
            ASS 文件路径
        """
        ass_content = self.to_ass(danmakus, width, height)

        base, _ = os.path.splitext(output_path)
        ass_path = base + ".ass"
        with open(ass_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(ass_content)
        return ass_path

    # ── 内部工具 ──

    def _base_font_size(self) -> float:
        """基准字号：25 × (画面高度 / 720)，和 B 站播放器的缩放方式一致"""
        scale = self.PLAY_RES_Y / self.REF_HEIGHT
        return max(8.0, self.REF_FONT_SIZE * scale)

    def _row_count(self, line_h: float) -> int:
        """按画面高度算能排多少行（再受 MAX_ROWS 限制）"""
        return max(4, min(self.MAX_ROWS, int(self.PLAY_RES_Y // max(1.0, line_h))))

    def _scroll_speed(self) -> float:
        """滚动速度（像素/秒）：按画面宽度定，保证任何分辨率下横穿时间一致"""
        gap = self.PLAY_RES_X * self.SIDE_GAP_RATIO
        return (self.PLAY_RES_X + 2 * gap) / self.SCROLL_DURATION

    def _ass_header(self, base_size: float) -> list:
        """ASS 文件头部"""
        outline = max(base_size / self.REF_FONT_SIZE, 1.0)
        return [
            "[Script Info]",
            "Title: BiliGo Danmaku",
            "ScriptType: v4.00+",
            f"PlayResX: {self.PLAY_RES_X}",
            f"PlayResY: {self.PLAY_RES_Y}",
            "WrapStyle: 2",
            "ScaledBorderAndShadow: yes",
            "YCbCr Matrix: TV.709",
            "Timer: 10.0000",
            "",
            "[V4+ Styles]",
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
            "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
            "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
            "Alignment, MarginL, MarginR, MarginV, Encoding",
            f"Style: Danmaku,{self.FONT_NAME},{base_size:.1f},"
            "&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,"
            f"0,0,0,0,100,100,0,0,1,{outline:.2f},0,7,0,0,0,0",
            "",
            "[Events]",
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        ]

    @staticmethod
    def _pick_track(tracks: list, current_time: float) -> Optional[int]:
        """找一条已经空出来的轨道（从上往下找）"""
        for i, free_at in enumerate(tracks):
            if free_at <= current_time:
                return i
        return None

    @staticmethod
    def _dialogue(start: float, duration: float, body: str) -> str:
        return (f"Dialogue: 0,{DanmakuProcessor._format_time(start)},"
                f"{DanmakuProcessor._format_time(start + duration)},"
                f"Danmaku,,0,0,0,,{body}")

    @staticmethod
    def _text_width(text: str, font_size: float) -> float:
        """粗估一行文字的宽度（像素）。

        全角字符按 1em、半角 0.55em 算。宁可略大一点 —— 估小了弹幕会互相压住。
        """
        widest = 0
        for line in text.split("\n"):
            w = 0.0
            for ch in line:
                code = ord(ch)
                if code >= 0x2E80 or ch in "，。！？；：、“”《》（）【】…—":
                    w += 1.0
                elif ch == " ":
                    w += 0.3
                else:
                    w += 0.55
            widest = max(widest, w)
        return widest * font_size

    @staticmethod
    def _escape_text(text: str) -> str:
        """转义 ASS 里有特殊含义的字符，换行转成 \\N"""
        parts = []
        for line in text.split("\n"):
            line = line.replace("\\", "\\\\")
            line = line.replace("{", "\\{").replace("}", "\\}")
            parts.append(line)
        return "\\N".join(parts).strip()

    @staticmethod
    def _to_ass_color(bili_color: int) -> str:
        """B 站颜色值（0xRRGGBB）转 ASS 颜色（&HBBGGRR&）"""
        r = (bili_color >> 16) & 0xFF
        g = (bili_color >> 8) & 0xFF
        b = bili_color & 0xFF
        return f"&H{b:02X}{g:02X}{r:02X}&"

    @staticmethod
    def _format_time(seconds: float) -> str:
        """秒数转 ASS 时间格式 H:MM:SS.CC"""
        if seconds < 0:
            seconds = 0
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = int(seconds % 60)
        cs = int((seconds % 1) * 100)
        return f"{h}:{m:02d}:{s:02d}.{cs:02d}"
