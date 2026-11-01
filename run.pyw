#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MediaFire 批量下载器 2.0 —— 图形界面入口。

用 ``.pyw`` 后缀，双击运行时不会带出一个黑色控制台窗口。

崩溃处理原则：先把异常完整写进日志，再尽力弹一次提示——绝不让程序"点一下
什么都没发生"。

另有一条隐藏的自检通道（打包验收与 CI 用）：

    MediaFire批量下载器2.0.exe --selftest

它会把界面完整构建一遍但**绝不显示窗口**，结果写进
%LOCALAPPDATA%\\MediaFireDownloader2\\selftest.log，进程退出码 0 表示通过。
之所以要这条通道：打包出来的 exe 有 Tcl/Tk 资源、内核模块、SSL 证书三处
是"只有真正跑起来才知道有没有丢"的，而验收时又不该往用户屏幕上弹窗。
"""

import os
import sys
import traceback


def _bootstrap_path():
    """把自己所在的目录放进 sys.path，保证以源码方式双击也能 import 到 mfd。"""
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)


def _data_dir():
    """返回本程序的数据目录（与内核共用 mfd.paths 的算法）。"""
    _bootstrap_path()
    try:
        from mfd.paths import data_dir
        return data_dir()
    except Exception:
        pass
    # 兜底：mfd 都导入不了时也要能写出崩溃日志
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if not base:
        base = os.path.expanduser("~")
    directory = os.path.join(base, "MediaFireDownloader2")
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError:
        return ""
    return directory


def _log_path(name):
    directory = _data_dir()
    return os.path.join(directory, name) if directory else ""


def _write_log(name, text):
    path = _log_path(name)
    if not path:
        return ""
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(text)
        return path
    except OSError:
        return ""


def _report_crash(exc_text):
    """写崩溃日志；能弹提示就弹一次（仅限启动失败这种"用户什么都看不到"的情况）。"""
    stamp = "\n=== %s ===\n" % __import__("datetime").datetime.now().isoformat()
    path = _write_log("crash.log", stamp + exc_text)

    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "启动失败",
            "程序启动时出错。\n\n%s\n\n完整信息已写入：\n%s" % (
                exc_text.strip().splitlines()[-1] if exc_text.strip() else "未知错误",
                path or "（日志写入失败）",
            ),
        )
        root.destroy()
    except Exception:
        pass


def _selftest():
    """构建界面但绝不显示；把结论写进 selftest.log 并用退出码表达结果。"""
    lines = []
    ok = True
    try:
        import tkinter
        lines.append("python   : %s" % sys.version.split()[0])
        lines.append("frozen   : %s" % bool(getattr(sys, "frozen", False)))
        lines.append("tk       : %s" % tkinter.TkVersion)
        lines.append("tcl      : %s" % tkinter.TclVersion)

        from mfd import __version__
        lines.append("version  : %s" % __version__)

        from mfd.ui import theme
        theme.set_dpi_awareness()

        root = tkinter.Tk()
        root.withdraw()          # 全程不显示
        try:
            from mfd.ui.app import App
            app = App(root)
            for _ in range(10):
                root.update_idletasks()
                root.update()

            kernel = getattr(app, "kernel", None)
            missing = list(getattr(kernel, "missing", []) or [])
            if missing:
                ok = False
                lines.append("kernel   : 缺件 → %s" % "; ".join(missing))
            else:
                lines.append("kernel   : 全部就绪")

            # https 的硬依赖：证书库不在，联网解析必挂
            try:
                import ssl
                paths = ssl.get_default_verify_paths()
                lines.append("ssl      : %s" % (ssl.OPENSSL_VERSION,))
                lines.append("cafile   : %s" % (paths.cafile or paths.openssl_cafile or "（用系统库）"))
            except Exception as exc:
                lines.append("ssl      : 读取失败 %s" % exc)

            for attr in ("queue_tree", "fail_tree", "log_text", "notebook"):
                if not hasattr(app, attr):
                    ok = False
                    lines.append("widget   : 缺 %s" % attr)
            lines.append("ui       : 构建通过")

            try:
                if hasattr(app, "_shutdown"):
                    app._shutdown()
            except Exception:
                pass
        finally:
            try:
                root.destroy()
            except Exception:
                pass
    except Exception:
        ok = False
        lines.append(traceback.format_exc())

    lines.append("result   : %s" % ("PASS" if ok else "FAIL"))
    stamp = "\n=== %s ===\n" % __import__("datetime").datetime.now().isoformat()
    _write_log("selftest.log", stamp + "\n".join(lines) + "\n")
    return 0 if ok else 1


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    _bootstrap_path()

    if any(arg in ("--selftest", "-t") for arg in argv):
        return _selftest()

    try:
        from mfd.ui import run
        run(argv)
        return 0
    except Exception:
        _report_crash(traceback.format_exc())
        return 1


if __name__ == "__main__":
    sys.exit(main())
