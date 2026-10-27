"""BiliGo - Bilibili 视频下载器"""

import sys
import os

# 兼容 PyInstaller 打包后的路径
if getattr(sys, 'frozen', False):
    # PyInstaller 打包后，_MEIPASS 是临时解压目录
    BASE_DIR = sys._MEIPASS
    APP_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    APP_DIR = BASE_DIR

sys.path.insert(0, BASE_DIR)


def main():
    from biligo.app import run_app
    run_app()


if __name__ == "__main__":
    main()
