"""BiliGo B站二维码登录模块

实现B站Web端二维码扫码登录：
1. 请求B站API生成二维码
2. 显示二维码供用户用B站APP扫描
3. 轮询扫码状态
4. 扫码成功后提取Cookie（SESSDATA、bili_jct、DedeUserID）
"""

import io
import re
import json
import time
import qrcode
import requests
from urllib.parse import unquote


# B站二维码登录API
QRCODE_GENERATE_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
QRCODE_POLL_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
NAV_API_URL = "https://api.bilibili.com/x/web-interface/nav"

# 取 cid / aid / bvid 的接口
VIEW_API_URL = "https://api.bilibili.com/x/web-interface/view"      # 普通投稿：bvid 或 aid
SEASON_API_URL = "https://api.bilibili.com/pgc/view/web/season"     # 番剧/影视：ep_id

# B站扫码状态码（data.code）
QRCODE_NOT_SCANNED = 86101
QRCODE_SCANNED_WAITING = 86102
QRCODE_CONFIRMED = 0       # 扫码确认成功
QRCODE_EXPIRED = 86038

# B站API通用请求头
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Referer": "https://www.bilibili.com/",
    "Origin": "https://www.bilibili.com",
}


class BiliAuth:
    """B站二维码登录"""

    def __init__(self):
        self._qrcode_key = None
        self._qrcode_url = None

    def generate_qrcode(self):
        """请求B站API生成二维码

        Returns:
            dict: {"url": 二维码内容URL, "key": qrcode_key, "image": PIL Image对象}
        Raises:
            RuntimeError: 请求失败
        """
        resp = requests.get(QRCODE_GENERATE_URL, headers=HEADERS, timeout=10)
        data = resp.json()

        if data.get("code") != 0:
            raise RuntimeError(f"获取二维码失败: {data.get('message', '未知错误')}")

        result = data.get("data", {})
        self._qrcode_url = result.get("url", "")
        self._qrcode_key = result.get("qrcode_key", "")

        # 生成高清二维码图片（box_size=10 提高清晰度）
        qr = qrcode.QRCode(
            version=None,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=10,
            border=2,
        )
        qr.add_data(self._qrcode_url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        if img.mode != "RGB":
            img = img.convert("RGB")

        return {
            "url": self._qrcode_url,
            "key": self._qrcode_key,
            "image": img,
        }

    def poll_qrcode(self):
        """轮询扫码状态

        Returns:
            dict: {
                "status": str ("not_scanned"/"waiting"/"confirmed"/"expired"/"error"),
                "message": str,
                "cookies": dict or None  (仅 confirmed 时有值)
            }
        """
        if not self._qrcode_key:
            return {"status": "expired", "message": "未生成二维码", "cookies": None}

        try:
            session = requests.Session()
            resp = session.get(
                QRCODE_POLL_URL,
                params={"qrcode_key": self._qrcode_key},
                headers=HEADERS,
                timeout=10,
            )
            data = resp.json()
            top_code = data.get("code", -1)
            inner = data.get("data", {})
            inner_code = inner.get("code", -1)

            # 二维码过期
            if inner_code == QRCODE_EXPIRED or top_code == QRCODE_EXPIRED:
                return {"status": "expired", "message": "二维码已失效", "cookies": None}

            # 未扫码
            if inner_code == QRCODE_NOT_SCANNED:
                return {"status": "not_scanned", "message": "未扫码", "cookies": None}

            # 已扫码待确认
            if inner_code == QRCODE_SCANNED_WAITING:
                return {"status": "waiting", "message": "已扫描，待确认", "cookies": None}

            # 扫码确认成功：data.code == 0 且 top_code == 0
            if inner_code == QRCODE_CONFIRMED and top_code == 0:
                cookies = {}

                # 方法1: 从 session cookies 中提取
                for cookie in session.cookies:
                    if cookie.name in ("SESSDATA", "bili_jct", "DedeUserID"):
                        cookies[cookie.name] = unquote(cookie.value)

                # 方法2: 从 resp.cookies 提取
                if "SESSDATA" not in cookies:
                    for cookie in resp.cookies:
                        if cookie.name in ("SESSDATA", "bili_jct", "DedeUserID"):
                            cookies[cookie.name] = unquote(cookie.value)

                # 方法3: 从 Set-Cookie 原始头提取
                if "SESSDATA" not in cookies:
                    for header_val in resp.headers.get_list("Set-Cookie") if hasattr(resp.headers, 'get_list') else ([resp.headers.get("Set-Cookie", "")]):
                        for key in ("SESSDATA", "bili_jct", "DedeUserID"):
                            m = re.search(rf"{key}=([^;,]+)", header_val)
                            if m:
                                cookies[key] = unquote(m.group(1).strip())

                # 方法4: 从 data.url 中提取（B站标准方式）
                if "SESSDATA" not in cookies:
                    redirect_url = inner.get("url", "")
                    if redirect_url:
                        for key in ("SESSDATA", "bili_jct", "DedeUserID"):
                            m = re.search(rf"[?&]{key}=([^&\s#]+)", redirect_url)
                            if m:
                                cookies[key] = unquote(m.group(1))

                return {
                    "status": "confirmed",
                    "message": "登录成功",
                    "cookies": cookies if cookies.get("SESSDATA") else None,
                    "raw_data": inner,  # 传回原始数据便于调试
                }

            # 其他未知状态
            msg = inner.get("message", "") or data.get("message", f"未知状态({inner_code})")
            return {"status": "error", "message": msg, "cookies": None}

        except Exception as e:
            return {"status": "error", "message": str(e), "cookies": None}

    @staticmethod
    def verify_login(sessdata: str) -> dict:
        """验证SESSDATA是否有效，获取大会员状态"""
        if not sessdata:
            return {"is_login": False, "is_vip": False, "uname": "", "face": ""}

        try:
            resp = requests.get(
                NAV_API_URL,
                cookies={"SESSDATA": sessdata},
                headers=HEADERS,
                timeout=10,
            )
            resp_data = resp.json()

            if resp_data.get("code") != 0:
                return {"is_login": False, "is_vip": False, "uname": "", "face": ""}

            data = resp_data.get("data", {})
            is_login = data.get("isLogin", False)
            vip_type = data.get("vipType", 0)
            is_vip = vip_type >= 1 or data.get("vipStatus", 0) == 1
            uname = data.get("uname", "")
            face = data.get("face", "")

            return {
                "is_login": is_login,
                "is_vip": is_vip,
                "uname": uname,
                "face": face,
            }
        except Exception:
            return {"is_login": False, "is_vip": False, "uname": "", "face": ""}

    @staticmethod
    def fetch_video_detail(bvid: str) -> dict:
        """通过B站API获取视频详情（封面、UP主、播放量等）+ 分P信息

        Returns:
            dict: {
                "thumbnail": str, "uploader": str, "duration": int,
                "view_count": int, "pages": list[dict]
            }
            失败返回空 dict
        """
        if not bvid:
            return {}

        try:
            resp = requests.get(
                "https://api.bilibili.com/x/web-interface/view",
                params={"bvid": bvid},
                headers=HEADERS,
                timeout=10,
            )
            data = resp.json().get("data", {})
            if not data:
                return {}

            stat = data.get("stat", {})
            owner = data.get("owner", {})

            pages = data.get("pages", [])
            page_list = []
            for i, page in enumerate(pages):
                page_list.append({
                    "index": i + 1,
                    "title": page.get("part", f"第{i+1}P"),
                    "duration": page.get("duration", 0),
                    "thumbnail": page.get("first_frame", "") or data.get("pic", ""),
                })

            return {
                "thumbnail": data.get("pic", ""),
                "uploader": owner.get("name", ""),
                "duration": data.get("duration", 0),
                "view_count": stat.get("view", 0),
                "pages": page_list,
            }
        except Exception:
            return {}

    @staticmethod
    def fetch_video_pages(bvid: str) -> list:
        """通过B站API获取多P视频的分P信息（含缩略图），兼容旧调用"""
        detail = BiliAuth.fetch_video_detail(bvid)
        return detail.get("pages", [])

    @staticmethod
    def resolve_video_ids(url: str, sessdata: str = "") -> dict:
        """把任意 B 站链接解析成 cid / bvid / aid / ep_id。

        yt-dlp 不会返回 cid：番剧只给 episode_id / season_id，普通投稿只给 bvid。
        而弹幕要 oid=cid、字幕要 bvid+cid（player/v2 不接受 ep_id），所以这里自己查：

          · 番剧/影视  → pgc/view/web/season?ep_id=NNN   （返回剧集列表，含 cid/bvid/aid）
          · 普通投稿  → x/web-interface/view?bvid=BV...  （返回 cid/aid）

        Args:
            url: 视频链接（支持 /video/BVxxx、/video/av123、/bangumi/play/ep123）
            sessdata: 可选，带上登录态的 SESSDATA 能查到更完整的信息
        Returns:
            dict: {"cid": int, "bvid": str, "aid": int, "ep_id": int, "season_id": int}
            查不到时返回 {}（调用方据此提示"无法获取 CID"）
        """
        if not url:
            return {}

        ep_match = re.search(r"ep(\d+)", url, re.I)
        bv_match = re.search(r"(BV[0-9A-Za-z]{10})", url)
        av_match = re.search(r"/av(\d+)", url, re.I)

        cookies = {"SESSDATA": sessdata} if sessdata else None
        try:
            # ── 番剧 / 影视 ──
            if ep_match:
                ep_id = int(ep_match.group(1))
                resp = requests.get(
                    SEASON_API_URL,
                    params={"ep_id": ep_id},
                    headers=HEADERS,
                    cookies=cookies,
                    timeout=15,
                )
                data = resp.json()
                if data.get("code") != 0:
                    return {}
                result = data.get("result") or {}
                episodes = result.get("episodes") or []
                hit = next((e for e in episodes if e.get("id") == ep_id), None)
                if hit is None and episodes:
                    hit = episodes[0]
                if not hit:
                    return {}
                return {
                    "cid": int(hit.get("cid") or 0),
                    "bvid": hit.get("bvid") or "",
                    "aid": int(hit.get("aid") or 0),
                    "ep_id": ep_id,
                    "season_id": int(result.get("season_id") or 0),
                }

            # ── 普通投稿 ──
            params = {}
            if bv_match:
                params["bvid"] = bv_match.group(1)
            elif av_match:
                params["aid"] = int(av_match.group(1))
            else:
                return {}

            resp = requests.get(
                VIEW_API_URL, params=params, headers=HEADERS, cookies=cookies, timeout=15
            )
            data = resp.json()
            if data.get("code") != 0:
                return {}
            info = data.get("data") or {}
            return {
                "cid": int(info.get("cid") or 0),
                "bvid": info.get("bvid") or (bv_match.group(1) if bv_match else ""),
                "aid": int(info.get("aid") or 0),
                "ep_id": 0,
                "season_id": 0,
            }
        except Exception:
            return {}
