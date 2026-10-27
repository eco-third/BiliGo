"""把弹幕（ASS）烧进画面，生成「弹幕版」视频。

为什么必须重新编码：弹幕要画进像素里，没法像外挂字幕那样 `-c copy`。

两个关键细节：
1. ASS 先复制到临时目录、用纯 ASCII 文件名再交给 `ass=` 滤镜。
   用户标题里常有中文、空格、“”引号、逗号，这些在 ffmpeg 的滤镜串里
   需要层层转义，实测很难写对（带单引号/逗号时基本没法转义）——
   换成裸文件名 + cwd 就完全没有转义问题。
2. HDR（PQ / HLG）源先 tonemap 到 SDR 再烧。否则白字会发灰、画面颜色也会偏，
   因为 libass 是按 SDR 的数值去画字的。
"""

import os
import re
import shutil
import subprocess
import tempfile
import threading


class BurnError(RuntimeError):
    """烧录失败"""


class BurnCancelled(BurnError):
    """用户取消"""


_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_SIZE_RE = re.compile(r"Video:.*?,\s*(\d{2,5})x(\d{2,5})")
_FS_TAG_RE = re.compile(r"\\fs(\d+(?:\.\d+)?)")

# ── 字体渲染补偿 ──
# libass 的 DirectWrite 字体后端会按字体的（ascent+descender）归一化字号。
# 实测这一版 ffmpeg 里「微软雅黑 / Microsoft YaHei」只画出标称字号的 0.747 倍，
# 而 SimHei / SimSun 是 0.98~1.00。浏览器（B 站播放器）里 25px 就是 25px，
# 所以烧录前必须把雅黑的字号放大 1/0.747；否则按 B 站公式算出的 37.5 号
# 在成片里只有 28px —— 弹幕会明显偏小。
# 只补偿字号，不动描边宽度和坐标：描边本来就该按最终字号的比例走。
YAHEI_RENDER_SCALE = 1.3387


def _font_scale_for(ass_text: str) -> float:
    """按 ASS 里用的字体决定要不要补偿字号"""
    for line in ass_text.splitlines():
        if not line.startswith("Style:"):
            continue
        fields = line.split(":", 1)[1].split(",")
        if len(fields) < 3:
            continue
        name = fields[1].strip().strip('"').lower()
        if name in ("microsoft yahei", "微软雅黑", "microsoft yahei ui"):
            return YAHEI_RENDER_SCALE
        return 1.0
    return 1.0


def scale_ass_font(ass_text: str, factor: float) -> str:
    """把 ASS 的字号统一放大（Style 的 Fontsize 和每条的 \\fs），其余原样保留"""
    if factor == 1.0:
        return ass_text
    out = []
    for line in ass_text.splitlines():
        if line.startswith("Style:"):
            head, _, rest = line.partition(":")
            fields = rest.split(",")
            try:
                fields[2] = f"{float(fields[2]) * factor:.1f}"
            except (ValueError, IndexError):
                pass
            line = head + ":" + ",".join(fields)
        line = _FS_TAG_RE.sub(
            lambda m: "\\fs%.1f" % (float(m.group(1)) * factor), line)
        out.append(line)
    return "\n".join(out)


def probe_video(video_path: str, ffmpeg_path: str) -> dict:
    """读取视频的时长 / 分辨率 / 是否 HDR

    只用 ffmpeg.exe（应用只装了它，没有 ffprobe）。
    """
    info = {"duration": 0.0, "width": 0, "height": 0, "hdr": False}
    try:
        proc = subprocess.run(
            [ffmpeg_path, "-hide_banner", "-i", video_path],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60)
        text = (proc.stderr or "") + (proc.stdout or "")
    except Exception:
        return info

    m = _DURATION_RE.search(text)
    if m:
        info["duration"] = (int(m.group(1)) * 3600 + int(m.group(2)) * 60
                            + float(m.group(3)))

    m = _SIZE_RE.search(text)
    if m:
        info["width"], info["height"] = int(m.group(1)), int(m.group(2))

    low = text.lower()
    # PQ(smpte2084) / HLG(arib-std-b67) 才是 HDR；bt2020 单独出现不算
    info["hdr"] = ("smpte2084" in low) or ("arib-std-b67" in low)
    return info


def burn_danmaku(video_path: str, ass_path: str, out_path: str, ffmpeg_path: str,
                 on_progress=None, is_cancelled=None) -> dict:
    """把 ASS 烧进画面，输出到 out_path。

    Args:
        on_progress: 回调 (百分比 0~100)，会从 ffmpeg 的 -progress 里读
        is_cancelled: 返回 True 时中止（会杀掉 ffmpeg 进程）
    Returns:
        {"out_path":..., "duration":..., "hdr":...}
    """
    if not os.path.isfile(ffmpeg_path):
        raise BurnError("找不到 ffmpeg.exe，请先在设置里安装 FFmpeg")
    if not os.path.isfile(ass_path):
        raise BurnError(f"弹幕文件不存在：{ass_path}")

    info = probe_video(video_path, ffmpeg_path)
    duration = info["duration"]

    workdir = tempfile.mkdtemp(prefix="biligo-burn-")
    try:
        # ① 纯 ASCII 文件名，彻底绕开滤镜转义；顺便做字号补偿
        local_ass = os.path.join(workdir, "danmaku.ass")
        raw = open(ass_path, encoding="utf-8", errors="replace").read()
        factor = _font_scale_for(raw)
        with open(local_ass, "w", encoding="utf-8", newline="\n") as f:
            f.write(scale_ass_font(raw, factor))

        filters = []
        if info["hdr"]:
            # HDR → SDR，再烧字幕；否则白字会发灰、颜色也会偏
            filters.append(
                "zscale=t=linear:npl=100,format=gbrpf32le,"
                "zscale=p=bt709,tonemap=hable:desat=0,"
                "zscale=t=bt709:m=bt709:r=tv,format=yuv420p")
        filters.append("ass=danmaku.ass")

        cmd = [
            ffmpeg_path, "-hide_banner", "-nostdin", "-y",
            "-i", video_path,
            "-vf", ",".join(filters),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-pix_fmt", "yuv420p",
            "-c:a", "copy",
            "-progress", "pipe:1", "-nostats",
            out_path,
        ]

        proc = subprocess.Popen(
            cmd, cwd=workdir,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
        err_chunks = []

        def drain_stderr():
            try:
                for line in proc.stderr:
                    err_chunks.append(line)
                    if len(err_chunks) > 400:      # 只留尾部，别把内存吃满
                        del err_chunks[:200]
            except Exception:
                pass

        t_err = threading.Thread(target=drain_stderr, daemon=True)
        t_err.start()

        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                if is_cancelled and is_cancelled():
                    _kill(proc)
                    raise BurnCancelled("已取消")
                if line.startswith(("out_time_us=", "out_time_ms=")):
                    try:
                        us = int(line.split("=", 1)[1])
                    except ValueError:
                        continue
                    if duration > 0 and on_progress:
                        pct = min(99.0, us / 1_000_000.0 / duration * 100.0)
                        on_progress(pct)
                elif line == "progress=end" and on_progress:
                    on_progress(100.0)
        finally:
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                _kill(proc)
                proc.wait(timeout=10)
            t_err.join(timeout=3)

        if proc.returncode != 0:
            tail = "".join(err_chunks[-12:]).strip()
            if not os.path.exists(out_path):
                raise BurnError(f"ffmpeg 退出码 {proc.returncode}：{tail[:600]}")
        if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
            raise BurnError("ffmpeg 没有产出文件：" + "".join(err_chunks[-6:])[:400])

        return {"out_path": out_path, "duration": duration, "hdr": info["hdr"],
                "width": info["width"], "height": info["height"],
                "font_scale": factor}
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _kill(proc):
    try:
        proc.kill()
    except Exception:
        pass
