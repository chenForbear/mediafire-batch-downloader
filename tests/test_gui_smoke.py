#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""界面冒烟测试 —— 不显示窗口、不联网、不读写用户目录。

纪律：全程 ``root.withdraw()``，只用 ``update()`` 驱动事件，绝不 ``mainloop()``。
这样在无人值守的构建机上跑，也不会往屏幕上弹任何东西。

跑法：
    python tests/test_gui_smoke.py
"""

import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _pump(root, rounds=25, delay=0.01):
    """把事件队列跑干净，但只跑有限轮，避免 after 定时器无限续命。"""
    for _ in range(rounds):
        try:
            root.update_idletasks()
            root.update()
        except Exception:      # 窗口已销毁时 TclError 属正常
            return
        time.sleep(delay)


class TestTheme(unittest.TestCase):
    def test_palettes_have_all_tokens(self):
        from mfd.ui import theme
        need = ("bg", "surface", "surface_alt", "border", "text", "text_dim",
                "accent", "accent_hover", "success", "warning", "danger", "info")
        for mode in (theme.MODE_DARK, theme.MODE_LIGHT):
            palette = theme.palette_for(mode)
            for token in need:
                self.assertIn(token, palette, "色板 %s 缺 %s" % (mode, token))
                self.assertTrue(str(palette[token]).startswith("#"),
                                "色值应是 #RRGGBB：%s=%r" % (token, palette[token]))

    def test_toggle_is_reversible(self):
        from mfd.ui import theme
        first = theme.mode()
        theme.toggle_mode()
        self.assertNotEqual(theme.mode(), first)
        theme.toggle_mode()
        self.assertEqual(theme.mode(), first)

    def test_initial_geometry_respects_minimum(self):
        import tkinter
        from mfd.ui import theme
        root = tkinter.Tk()
        root.withdraw()
        try:
            width, height = theme.initial_geometry(root)
            self.assertGreaterEqual(width, 980)
            self.assertGreaterEqual(height, 680)
        finally:
            root.destroy()


class TestAppSmoke(unittest.TestCase):
    def test_build_and_destroy(self):
        import tkinter
        from mfd.ui import theme
        from mfd.ui.app import App

        theme.set_dpi_awareness()
        root = tkinter.Tk()
        root.withdraw()
        app = None
        try:
            started = time.time()
            app = App(root)
            self.assertLess(time.time() - started, 3.0, "App 构建应快于 3 秒")
            _pump(root, rounds=15)
            # 四个页签都在
            notebook = getattr(app, "notebook", None)
            if notebook is not None:
                titles = [notebook.tab(tab, "text") for tab in notebook.tabs()]
                self.assertEqual(len(titles), 4, "应有四个页签，实际：%r" % (titles,))
            # 关键部件存在
            for attr in ("queue_tree", "fail_tree", "log_text"):
                self.assertTrue(hasattr(app, attr), "缺少部件 %s" % attr)
        finally:
            try:
                if app is not None and hasattr(app, "_shutdown"):
                    app._shutdown()
            except Exception:
                pass
            try:
                root.destroy()
            except Exception:
                pass

    def test_switch_theme_does_not_crash(self):
        import tkinter
        from mfd.ui import theme
        from mfd.ui.app import App

        theme.set_dpi_awareness()
        root = tkinter.Tk()
        root.withdraw()
        app = None
        try:
            app = App(root)
            _pump(root, rounds=5)
            app._toggle_theme()
            _pump(root, rounds=5)
            app._toggle_theme()
            _pump(root, rounds=5)
        finally:
            try:
                if app is not None and hasattr(app, "_shutdown"):
                    app._shutdown()
            except Exception:
                pass
            try:
                root.destroy()
            except Exception:
                pass


if __name__ == "__main__":
    unittest.main(verbosity=2)
