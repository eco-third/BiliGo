"""BiliGo 弹幕处理器 - 下载 Bilibili 弹幕并转换为 ASS 字幕"""

import math
import os
import random
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

    # ASS 配置
    PLAY_RES_X = 1920
    PLAY_RES_Y = 1080
    FONT_NAME = "Microsoft YaHei"
    BASE_FONT_SIZE = 36
    SCROLL_DURATION = 12.0    # 滚动弹幕持续时间（秒）
    FIX_DURATION = 5.0        # 固定弹幕持续时间（秒）
    DANMAKU_MARGIN = 4        # 弹幕行间距
    MAX_ROWS = 18             # 最大弹幕行数

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "Referer": "https://www.bilibili.com",
        })

    def download_danmaku(self, cid: int, cookies: dict = None, segment_index: int = 1) -> List[DanmakuItem]:
        """
        下载弹幕
        Args:
            cid: 视频 CID
            cookies: {"sessdata": "..."}
            segment_index: 弹幕分段索引 (1-6 对应不同分钟数)
        Returns:
            DanmakuItem 列表
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
                items.append(DanmakuItem(time, mode, font_size, color, timestamp, pool, uid, text))

        return items

    def to_ass(self, danmakus: List[DanmakuItem], width: int = 1920, height: int = 1080) -> str:
        """
        将弹幕列表转换为 ASS 字幕格式
        Args:
            danmakus: 弹幕列表
            width: 视频宽度
            height: 视频高度
        Returns:
            ASS 字幕文件内容
        """
        self.PLAY_RES_X = width
        self.PLAY_RES_Y = height

        lines = self._ass_header()

        # 按时间排序
        sorted_danmaku = sorted(danmakus, key=lambda d: d.time)

        # 分配轨道位置并生成 Dialogue 行
        scroll_tracks = [0.0] * self.MAX_ROWS    # 滚动弹幕轨道最后占用时间
        top_tracks = [0.0] * self.MAX_ROWS        # 顶部弹幕轨道
        bottom_tracks = [0.0] * self.MAX_ROWS      # 底部弹幕轨道

        for dm in sorted_danmaku:
            text = dm.text.replace("\n", "\\N").replace("{", "\\{").replace("}", "\\}")
            color_hex = f"#{dm.color:06x}" if dm.color != 16777215 else "#FFFFFF"

            # 字体大小缩放
            scale = dm.font_size / 25.0
            font_size = int(self.BASE_FONT_SIZE * scale)

            if dm.mode in (1, 2, 3):
                # 滚动弹幕 (右→左)
                row = self._find_track(scroll_tracks, dm.time)
                if row is None:
                    continue

                y = self._get_y(row)
                start_x = self.PLAY_RES_X + 50
                end_x = -500

                start = self._format_time(dm.time)
                end = self._format_time(dm.time + self.SCROLL_DURATION)

                effect = f"{{\\c&{self._to_ass_color(dm.color)}\\fs{font_size}}}"
                override = f"{{\\move({start_x},{y},{end_x},{y})}}"
                lines.append(
                    f"Dialogue: 0,{start},{end},Danmaku,,0,0,0,,{effect}{override}{text}"
                )

                scroll_tracks[row] = dm.time + self.SCROLL_DURATION

            elif dm.mode == 5:
                # 顶部弹幕
                row = self._find_track(top_tracks, dm.time)
                if row is None:
                    continue

                y = self._get_y(row) + 20
                start = self._format_time(dm.time)
                end = self._format_time(dm.time + self.FIX_DURATION)

                effect = f"{{\\c&{self._to_ass_color(dm.color)}\\fs{font_size}}}"
                override = f"{{\\an8\\pos({self.PLAY_RES_X // 2},{y})}}"
                lines.append(
                    f"Dialogue: 0,{start},{end},Danmaku,,0,0,0,,{effect}{override}{text}"
                )

                top_tracks[row] = dm.time + self.FIX_DURATION

            elif dm.mode == 6:
                # 底部弹幕
                row = self._find_track(bottom_tracks, dm.time)
                if row is None:
                    continue

                y = self.PLAY_RES_Y - self._get_y(row) - 20
                start = self._format_time(dm.time)
                end = self._format_time(dm.time + self.FIX_DURATION)

                effect = f"{{\\c&{self._to_ass_color(dm.color)}\\fs{font_size}}}"
                override = f"{{\\an2\\pos({self.PLAY_RES_X // 2},{y})}}"
                lines.append(
                    f"Dialogue: 0,{start},{end},Danmaku,,0,0,0,,{effect}{override}{text}"
                )

                bottom_tracks[row] = dm.time + self.FIX_DURATION

        return "\n".join(lines)

    def save_ass(self, danmakus: List[DanmakuItem], output_path: str,
                 width: int = 1920, height: int = 1080) -> str:
        """
        下载弹幕并保存为 ASS 文件
        Args:
            danmakus: 弹幕列表
            output_path: 视频文件路径，ASS 文件将保存为同名 .ass
            width: 视频宽度
            height: 视频高度
        Returns:
            ASS 文件路径
        """
        ass_content = self.to_ass(danmakus, width, height)

        # 生成 ASS 文件路径
        base, _ = os.path.splitext(output_path)
        ass_path = base + ".ass"

        with open(ass_path, "w", encoding="utf-8") as f:
            f.write(ass_content)

        return ass_path

    # ── 内部方法 ──

    def _ass_header(self) -> list:
        """生成 ASS 文件头部"""
        return [
            "[Script Info]",
            "Title: BiliGo Danmaku",
            "ScriptType: v4.00+",
            f"PlayResX: {self.PLAY_RES_X}",
            f"PlayResY: {self.PLAY_RES_Y}",
            "Timer: 10.0000",
            "",
            "[V4+ Styles]",
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
            "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
            "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
            "Alignment, MarginL, MarginR, MarginV, Encoding",
            f"Style: Danmaku,{self.FONT_NAME},{self.BASE_FONT_SIZE},"
            "&H00FFFFFF,&H00FFFFFF,&H00000000,&H50000000,"
            "0,0,0,0,100,100,0,0,1,2,0,2,0,0,0,0",
            "",
            "[Events]",
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        ]

    def _find_track(self, tracks: list, current_time: float) -> Optional[int]:
        """寻找可用的弹幕轨道"""
        best_row = None
        best_time = float("inf")
        for i, t in enumerate(tracks):
            if t <= current_time:
                if t < best_time:
                    best_time = t
                    best_row = i
        return best_row

    def _get_y(self, row: int) -> int:
        """根据行号获取 Y 坐标"""
        line_height = self.BASE_FONT_SIZE + self.DANMAKU_MARGIN
        return row * line_height + 40

    @staticmethod
    def _to_ass_color(bili_color: int) -> str:
        """B站颜色值转 ASS 颜色 (BGR)"""
        r = (bili_color >> 16) & 0xFF
        g = (bili_color >> 8) & 0xFF
        b = bili_color & 0xFF
        # ASS 颜色格式: &HBBGGRR&
        return f"{b:02X}{g:02X}{r:02X}"

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
