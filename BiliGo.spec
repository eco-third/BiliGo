# -*- mode: python ; coding: utf-8 -*-
"""BiliGo 1.2.0 PyInstaller 打包配置（PyInstaller 6.x）

单文件、无控制台窗口；依赖的资源（yt-dlp 数据、customtkinter 主题、qrcode 子模块）
全部打进 exe，exe 旁边只放 ffmpeg / aria2 / config 目录即可。
"""

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

yt_dlp_datas = collect_data_files('yt_dlp')
yt_dlp_hidden = collect_submodules('yt_dlp')

# customtkinter 的主题 json、字体等
ctk_datas = collect_data_files('customtkinter')

# qrcode 的 image.pil 等是条件导入，必须显式收集
qrcode_hidden = collect_submodules('qrcode')

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=yt_dlp_datas + ctk_datas,
    hiddenimports=yt_dlp_hidden + qrcode_hidden + [
        'biligo',
        'biligo.app',
        'biligo.auth',
        'biligo.burn',
        'biligo.config',
        'biligo.danmaku',
        'biligo.downloader',
        'biligo.subtitle',
        'customtkinter',
        'PIL',
        'PIL.ImageTk',
        'requests',
        'urllib3',
        'certifi',
        'charset_normalizer',
        # 从浏览器读 Cookie 需要
        'Crypto',
        'Crypto.Cipher',
        'Crypto.Cipher.AES',
        'Crypto.Protocol',
        'Crypto.Protocol.KDF',
        'Crypto.Util',
        'Crypto.Util.Padding',
        # qrcode 依赖的 pypng，import 名是 png
        'png',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'matplotlib', 'numpy', 'scipy', 'pandas',
        'tkinter.test', 'unittest', 'test',
        'pytest', 'IPython', 'notebook',
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='BiliGo',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # GUI 程序，不要黑框
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,              # 有 .ico 就填路径
)
