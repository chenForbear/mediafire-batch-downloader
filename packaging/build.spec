# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置 —— MediaFire 批量下载器 2.0

为什么单独写 spec 而不用命令行参数：

  1. **必须显式带上 tcl/tk 数据目录**，否则打出来的 exe 一运行就报
     "Can't find a usable init.tcl"。这是 PyInstaller 打包 Tkinter 最常见、
     也最难查的坑：便携版 Python 的 tcl 装在 <prefix>/tcl 下，官方 hook
     常常扫不到。本 spec 直接遍历该目录，按运行时约定的相对路径塞进去。

  2. 要写入版本资源，让接收者右键属性能看到名称与版本号。

  3. 排除项只排"确定用不到且排错也无害"的第三方大件，**不动标准库**——
     误排一个标准库（例如 urllib 连带用到的 email）会让 exe 一启动就崩。

  4. 要剔除打包机自带的 UCRT / Win10+ 转发 DLL，否则在 Win7 上会以
     "The procedure entry point GetDaylightFlag could not be located"
     的方式崩溃（详见 _trim_packaged_runtime 的注释）。

构建命令（必须用 Python 3.8 打包，Win7 才跑得起来）：

    python -m PyInstaller --clean --noconfirm build.spec

或直接双击同目录下的 build.bat（它会自动找 build-env 里的 Python 3.8）。
"""

import os
import sys

# 本 spec 放在 v2/packaging/ 下；PROJECT_DIR = v2 根（含 run.pyw 与 mfd/）
PROJECT_DIR = os.path.dirname(os.path.abspath(SPECPATH))
ENTRY = os.path.join(PROJECT_DIR, "run.pyw")
ICON = os.path.join(PROJECT_DIR, "assets", "app.ico")
VERSION_FILE = os.path.join(PROJECT_DIR, "assets", "version_info.txt")
APP_EXE_NAME = "MediaFire批量下载器2.0"

# 只排除这些：体积大、确定不被本程序导入、排错也不会让启动失败。
EXCLUDES = [
    "PIL", "Pillow",
    "numpy", "scipy", "pandas", "matplotlib",
    "requests", "urllib3", "chardet", "bs4", "lxml",
    "pytest", "_pytest",
    "PyQt5", "PyQt6", "PySide2", "PySide6",
    "IPython", "jupyter", "notebook",
    "pydoc_data", "lib2to3", "ensurepip", "idlelib",
]

# 显式保留：本程序真正依赖、但静态分析可能看不全的模块。
# 说明：mfd.* 是本项目自己的包（不在 site-packages），靠 pathex 找；
#       仍然显式列出，避免冻结后因动态 import 丢模块。
HIDDEN = [
    # 本项目
    "mfd", "mfd.mediafire", "mfd.downloader", "mfd.failures",
    "mfd.diagnostics", "mfd.storage", "mfd.util",
    "mfd.ui", "mfd.ui.theme", "mfd.ui.widgets", "mfd.ui.app",
    # tkinter 全家桶
    "tkinter", "tkinter.ttk", "tkinter.messagebox", "tkinter.filedialog",
    "tkinter.font", "tkinter.scrolledtext", "tkinter.constants",
    "tkinter.dialog", "tkinter.simpledialog", "tkinter.commondialog",
    # 网络与解析
    "email", "email.parser", "email.message", "html", "html.parser",
    "queue", "http.client", "urllib.request", "urllib.parse", "urllib.error",
    # ssl / 证书是 https 的硬依赖，打包时最容易漏
    "ssl", "ssl_", "_ssl", "socket", "hashlib", "hmac", "secrets",
    # 编码：中文文件名与系统代码页
    "encodings.utf_8", "encodings.gbk", "encodings.gb2312",
    "encodings.cp936", "encodings.idna",
    # 诊断模块会读注册表 / 系统信息
    "winreg", "platform", "locale", "getpass", "shutil", "tempfile",
    "json", "csv", "datetime", "threading", "collections", "dataclasses",
]

EXCLUDES = [name for name in EXCLUDES if name not in HIDDEN]

# tcl/tk 的数据目录必须随 exe 一起带上，目标相对路径要和 Tkinter 运行时查找的一致。
TCL_ROOT = os.path.join(sys.base_prefix, "tcl")
tcl_datas = []
if os.path.isdir(TCL_ROOT):
    for name in sorted(os.listdir(TCL_ROOT)):
        path = os.path.join(TCL_ROOT, name)
        if not os.path.isdir(path):
            continue
        if name in ("tcl8.6", "tk8.6"):
            tcl_datas.append((path, "tcl/" + name))
        elif name == "tcl8":
            tcl_datas.append((path, "tcl/tcl8"))

# 文档随 exe 一起发（开源项目：用户点开就能看到用法与许可）
extra_datas = []
for name in ("README.md", "README.zh-CN.md", "LICENSE", "CHANGELOG.md"):
    path = os.path.join(PROJECT_DIR, name)
    if os.path.isfile(path):
        extra_datas.append((path, "."))

a = Analysis(
    [ENTRY],
    pathex=[PROJECT_DIR],
    binaries=[],
    datas=tcl_datas + extra_datas,
    hiddenimports=HIDDEN,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=None,
    noarchive=False,
)


def _trim_packaged_runtime(toc):
    """剔除「打包机自带的 UCRT 与 Win10+ 专有转发 DLL」。

    为什么必须做这一步（实测取证，2026-10-12）：
      · PyInstaller 是在**打包机上现场扫依赖表**收集 DLL 的，它不关心目标系统版本。
        打包机是 Win11 级系统，于是它会把 System32\\downlevel 下的 ucrtbase.dll
        （版本 10.0.26100.x）和几十个 api-ms-win-* 转发 DLL 一起塞进 exe。
      · 微软官方文档写明：Vista 到 8.1 能装的 UCRT 最高只到 10.0.14393。
        把 Win11 那一代 ucrtbase.dll 带到 Win7 上，就是 PyInstaller #6621 那种
        "The procedure entry point GetDaylightFlag could not be located" 崩溃。
      · api-ms-win-core-*.dll 是 kernel32 的转发壳（Win10+ 的 API 合同名），
        对 Win7 零收益、纯风险。

    处置方式：
      · 丢掉 ucrtbase.dll —— 让它用**目标机自带的**那一份。
      · 丢掉 api-ms-win-core-*。
      · **保留 api-ms-win-crt-***（UCRT 的转发壳，每个才 20 多 KB）：它是
        python38.dll 导入表里的硬依赖，留着才能在装了 UCRT 的机器上解析成功。
    """
    keep, dropped = [], []
    for entry in toc:
        base = entry[0].replace("\\", "/").rsplit("/", 1)[-1].lower()
        if base == "ucrtbase.dll" or base.startswith("api-ms-win-core-"):
            dropped.append(entry[0])
        else:
            keep.append(entry)
    print(">>> 已剔除打包机自带的 UCRT / Win10+ 转发 DLL：%d 个" % len(dropped))
    for name in dropped:
        print(">>>   drop %s" % name)
    return keep


a.binaries = _trim_packaged_runtime(a.binaries)

pyz = PYZ(a.pure, a.zipped_data, cipher=None)

# ── 两种打包形态 ──────────────────────────────────────────────────────────
# 单文件（onefile，默认）：只有一个 exe，双击即用，但每次启动都要把自身
#   解压到 %TEMP%\_MEIxxxx。代价有三个，都会真实咬人：
#     1. 启动慢（9.8 MB 解压，机械盘上 3~6 秒）；
#     2. %TEMP% 不可写 / 被组策略限制 / 被安全软件拦住时，双击直接报错——
#        而"以管理员身份运行"恰好因为换了完整性级别而能过，于是用户会误以为
#        这程序需要管理员权限；
#     3. 每启动一次就在 %TEMP% 留一份副本，磁盘上多出几十 MB 垃圾。
# 目录版（onedir，MFD_ONEDIR=1）：一个文件夹，启动不解压，0.3~0.8 秒起来，
#   不碰 %TEMP%，也没有"管理员才跑得动"的怪现象。分发时压成 zip 即可。
ONEDIR = os.environ.get("MFD_ONEDIR", "") == "1"

_COMMON = dict(
    name=APP_EXE_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,              # 不用 UPX：压过的 exe 更容易被杀软误报，Win7 上还常报错
    upx_exclude=[],
    console=False,          # 纯界面程序，双击不弹黑框
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=ICON if os.path.isfile(ICON) else None,
    version=VERSION_FILE if os.path.isfile(VERSION_FILE) else None,
)

if ONEDIR:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        runtime_tmpdir=None,
        **_COMMON
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.zipfiles,
        a.datas,
        strip=False,
        upx=False,
        upx_exclude=[],
        name=APP_EXE_NAME,
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.zipfiles,
        a.datas,
        [],
        runtime_tmpdir=None,
        **_COMMON
    )
