"""BiliGo - Bilibili 视频下载器"""

import os
import sys

# 兼容 PyInstaller 打包后的路径
if getattr(sys, 'frozen', False):
    # PyInstaller 打包后，_MEIPASS 是临时解压目录
    BASE_DIR = sys._MEIPASS
    APP_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    APP_DIR = BASE_DIR

sys.path.insert(0, BASE_DIR)

# PyInstaller 的 --windowed 打包后 sys.stdout / sys.stderr 是 None，
# 任何库（yt-dlp、ffmpeg 包装层、我们自己的日志）往里写都会 AttributeError，
# 表现就是「exe 双击能开、一下载就报错」。这里兜底接到 devnull。
if getattr(sys, "frozen", False):
    for _name in ("stdout", "stderr"):
        if getattr(sys, _name, None) is None:
            try:
                setattr(sys, _name, open(os.devnull, "w", encoding="utf-8"))
            except OSError:
                pass



def _selftest() -> int:
    """自检：构造一次主界面再退出，用来验证打包出来的 exe 是完整的。

    exe 是 --windowed（无控制台），所以结果同时写进 exe 同目录的 selftest.log，
    并作为进程退出码返回，方便自动化判断。
    """
    import traceback

    code = 0
    try:
        import customtkinter as ctk

        from biligo import config
        from biligo.app import BiliGoApp

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("dark-blue")
        app = BiliGoApp()
        app.withdraw()
        app.update_idletasks()
        app.update()
        app.destroy()
        lines = [
            f"BiliGo {config.Config.VERSION} selftest OK",
            f"python {sys.version.split()[0]}",
            f"APP_DIR {APP_DIR}",
            f"frozen {getattr(sys, 'frozen', False)}",
        ]
    except Exception:
        code = 1
        lines = ["BiliGo selftest FAILED", traceback.format_exc()]

    text = "\n".join(lines)
    try:
        with open(os.path.join(APP_DIR, "selftest.log"), "w", encoding="utf-8") as f:
            f.write(text)
    except OSError:
        pass
    try:
        print(text)
    except Exception:
        pass
    return code


def _selftest_download(url: str) -> int:
    """自检（下载链路）：下载 → 弹幕 ASS → 烧录，验证打包后子进程路径也正常。

    只写临时目录，不动用户的配置和下载目录。
    """
    import shutil
    import tempfile
    import traceback

    lines = []
    code = 0
    work = os.path.join(tempfile.gettempdir(), "biligo-exe-selftest")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work, exist_ok=True)
    # --windowed 打包后没有控制台，把 stderr 接到文件，才能看到 yt-dlp/aria2c 的原始报错
    try:
        _diag = open(os.path.join(APP_DIR, "selftest.stderr.log"), "w",
                     encoding="utf-8", errors="replace")
        sys.stdout = _diag
        sys.stderr = _diag
    except OSError:
        pass
    try:
        from biligo.auth import BiliAuth
        from biligo.burn import burn_danmaku, probe_video
        from biligo.config import Config
        from biligo.danmaku import DanmakuProcessor
        from biligo.downloader import BiliDownloader

        cfg = Config()
        cfg.data["download_path"] = work
        cfg.save = lambda: None            # 绝不写用户配置
        ffmpeg = cfg.get_ffmpeg_path()

        d = BiliDownloader(cfg)
        lines.append(f"aria2c: {d._find_aria2c() or '(未找到)'}")
        lines.append(f"ffmpeg: {ffmpeg or '(未找到)'}")

        p = d.download(url, "360P")
        lines.append(f"下载: {p.status} {p.actual_quality} -> {p.output_path}")
        if p.status != "done" or not p.output_path:
            raise RuntimeError(p.error_msg or "下载失败")

        spec = probe_video(p.output_path, ffmpeg)
        lines.append(f"探测: {spec['width']}x{spec['height']} {spec['duration']:.0f}s "
                     f"HDR={spec['hdr']}")

        proc = DanmakuProcessor()
        ids = BiliAuth.resolve_video_ids(url)
        items = proc.download_danmaku(int(ids.get("cid") or 0))
        ass = proc.save_ass(items, p.output_path, spec["width"], spec["height"])
        lines.append(f"弹幕: {len(items)} 条 -> {os.path.basename(ass)}")

        dst = os.path.join(work, "弹幕版.mp4")
        res = burn_danmaku(p.output_path, ass, dst, ffmpeg,
                           on_progress=lambda pct: None)
        lines.append(f"烧录: {os.path.getsize(dst) / 1048576:.1f}MB  "
                     f"字号补偿={res['font_scale']:.4f}")
    except Exception:
        code = 1
        lines.append("下载自检 FAILED")
        lines.append(traceback.format_exc())

    text = "\n".join(lines)
    try:
        with open(os.path.join(APP_DIR, "selftest.log"), "w", encoding="utf-8") as f:
            f.write(text)
    except OSError:
        pass
    try:
        print(text)
    except Exception:
        pass
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return code


def main():
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    if "--selftest-download" in sys.argv:
        i = sys.argv.index("--selftest-download")
        url = sys.argv[i + 1] if len(sys.argv) > i + 1 else \
            "https://www.bilibili.com/bangumi/play/ep3854821"
        sys.exit(_selftest_download(url))

    from biligo.app import run_app
    run_app()


if __name__ == "__main__":
    main()
