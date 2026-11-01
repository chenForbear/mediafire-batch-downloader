#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""界面包 —— 对外只导出 ``run()``。

``run.pyw`` 双击进来的第一站就是这里：开 DPI 感知、建根窗口、装配 ``App``、
进主循环。除此之外不暴露任何东西，界面内部的类都关在子模块里。
"""

__all__ = ["run"]


def run(argv=None):
    """启动图形界面并进入主循环（阻塞直到窗口关闭）。"""
    import tkinter as tk

    from . import theme

    # 必须在建 Tk 之前设置 DPI 感知，否则高分屏首帧会被系统拉伸
    theme.set_dpi_awareness()

    root = tk.Tk()
    root.withdraw()          # 先藏起来，等布局算完再显示，避免开局闪一下白框

    from .app import App     # 延迟导入：此时 tkinter 已就绪

    app = App(root)          # noqa: F841  持有引用，防止被回收
    theme.apply_dpi_scaling(root)

    try:
        root.deiconify()
        root.lift()
    except tk.TclError:
        pass

    root.mainloop()
    return 0
