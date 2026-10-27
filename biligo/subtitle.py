"""BiliGo 字幕处理器 - 下载 Bilibili CC 字幕"""

import json
import os
import re
import requests
from typing import List, Optional, Dict


class SubtitleProcessor:
    """Bilibili CC 字幕下载与转换"""

    PLAYER_API = "https://api.bilibili.com/x/player/v2"
    SUBTITLE_API = "https://aisubtitle.hdslb.com/bfs/ai_subtitle_prod/"

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "Referer": "https://www.bilibili.com",
        })

    def get_subtitle_info(self, bvid: str, cid: int, cookies: dict = None) -> List[dict]:
        """
        获取视频可用字幕列表
        Args:
            bvid: BV 号
            cid: CID
            cookies: {"sessdata": "..."}
        Returns:
            [{"id": ..., "lan": "zh-CN", "lan_doc": "中文", "subtitle_url": "..."}]
        """
        params = {"bvid": bvid, "cid": cid}
        if cookies and cookies.get("sessdata"):
            self.session.headers["Cookie"] = f"SESSDATA={cookies['sessdata']}"

        try:
            resp = self.session.get(self.PLAYER_API, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, json.JSONDecodeError):
            return []

        subtitles = data.get("data", {}).get("subtitle", {}).get("subtitles", [])
        result = []
        for sub in subtitles:
            url = sub.get("subtitle_url", "")
            if url and not url.startswith("http"):
                url = "https:" + url
            result.append({
                "id": sub.get("id", ""),
                "lan": sub.get("lan", ""),
                "lan_doc": sub.get("lan_doc", ""),
                "subtitle_url": url,
            })
        return result

    def download_subtitle(self, subtitle_url: str) -> List[dict]:
        """
        下载字幕 JSON 内容
        Returns:
            [{"from": 0.0, "to": 5.0, "content": "字幕文本"}, ...]
        """
        try:
            resp = self.session.get(subtitle_url, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, json.JSONDecodeError):
            return []

        body = data.get("body", [])
        return body

    def to_srt(self, subtitles: List[dict]) -> str:
        """将 Bilibili 字幕 JSON 转换为 SRT 格式"""
        lines = []
        for i, sub in enumerate(subtitles, 1):
            start = self._format_srt_time(sub.get("from", 0))
            end = self._format_srt_time(sub.get("to", 0))
            content = sub.get("content", "").strip()
            if content:
                lines.append(f"{i}")
                lines.append(f"{start} --> {end}")
                lines.append(content)
                lines.append("")
        return "\n".join(lines)

    def save_srt(self, subtitles: List[dict], output_path: str) -> str:
        """
        保存字幕为 SRT 文件
        Args:
            subtitles: 字幕数据
            output_path: 视频文件路径，SRT 保存为同名 .srt
        Returns:
            SRT 文件路径
        """
        srt_content = self.to_srt(subtitles)
        base, _ = os.path.splitext(output_path)
        srt_path = base + ".srt"

        with open(srt_path, "w", encoding="utf-8") as f:
            f.write(srt_content)

        return srt_path

    @staticmethod
    def _format_srt_time(seconds: float) -> str:
        """秒数转 SRT 时间格式 HH:MM:SS,mmm"""
        if seconds < 0:
            seconds = 0
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = int(seconds % 60)
        ms = int((seconds % 1) * 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
