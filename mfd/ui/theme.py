#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""主题层 —— 颜色令牌、字体探测、ttk 样式构建、高 DPI。

一条硬规矩：**界面里除了本文件，任何地方都不写颜色字面量**。
业务代码只认令牌名（``bg`` / ``surface`` / ``accent`` …），换主题时把新的
令牌字典递给各控件即可。这样深色/浅色两套皮肤只有一份真源，不会漂移。

本模块只依赖标准库与 tkinter，不碰任何网络或下载逻辑。
"""

import sys
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk


# ----------------------------------------------------------------------
# 一、颜色令牌（唯一真源，严格对应 UI-CONTRACT 第 2 节表格）
# ----------------------------------------------------------------------

DARK = {
    "bg": "#0E1116",          # 窗口底
    "surface": "#161B22",     # 卡片
    "surface_alt": "#1C2330",  # 次级面 / 表头
    "border": "#252D3A",      # 分隔线
    "text": "#E6EDF3",        # 主文字
    "text_dim": "#8B98A9",    # 次要文字
    "accent": "#3B82F6",      # 主按钮 / 进度
    "accent_hover": "#2F6FE0",  # hover
    "success": "#22C55E",     # 完成
    "warning": "#F59E0B",     # 跳过 / 警告
    "danger": "#EF4444",      # 失败
    "info": "#38BDF8",        # 解析 / 提示
}

LIGHT = {
    "bg": "#F4F6FA",
    "surface": "#FFFFFF",
    "surface_alt": "#EDF1F7",
    "border": "#DCE3EC",
    "text": "#17202B",
    "text_dim": "#5C6B7F",
    "accent": "#2563EB",
    "accent_hover": "#1D4ED8",
    "success": "#15803D",
    "warning": "#B45309",
    "danger": "#B91C1C",
    "info": "#0369A1",
}

MODE_DARK = "dark"
MODE_LIGHT = "light"
MODE_NAMES = (MODE_DARK, MODE_LIGHT)
MODE_TEXT = {MODE_DARK: "深色", MODE_LIGHT: "浅色"}

# 中性端点色，只用于在主题内部「调和」出派生令牌，不直接出现在界面代码里
_BLACK = "#000000"
_WHITE = "#FFFFFF"

# 下载状态 → 令牌名（表格行前景色、状态点、日志级别共用一张映射）
STATUS_TOKEN = {
    "waiting": "text_dim",
    "running": "info",
    "done": "success",
    "skipped": "warning",
    "failed": "danger",
    "canceled": "text_dim",
}

# 状态点 → 令牌名
DOT_TOKEN = {
    "idle": "text_dim",
    "online": "success",
    "busy": "info",
    "warn": "warning",
    "error": "danger",
}

# 中文字形优先级（用 families() 实测，不硬猜）
CJK_FONT_CANDIDATES = ("Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI")
MONO_FONT_CANDIDATES = ("Cascadia Mono", "Cascadia Code", "Consolas", "Courier New")


# ----------------------------------------------------------------------
# 二、颜色工具与派生令牌
# ----------------------------------------------------------------------

def _to_rgb(value):
    """把 #RRGGBB 或 #RGB 拆成三元组。"""
    text = (value or "").strip()
    if text.startswith("#"):
        text = text[1:]
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))


def _to_hex(rgb):
    """三元组转回 #RRGGBB。"""
    parts = []
    for channel in rgb:
        value = int(round(channel))
        value = 0 if value < 0 else (255 if value > 255 else value)
        parts.append("%02X" % value)
    return "#" + "".join(parts)


def mix(color_a, color_b, ratio):
    """按比例调色：ratio=0 全取 a，ratio=1 全取 b。"""
    first = _to_rgb(color_a)
    second = _to_rgb(color_b)
    return _to_hex([first[i] * (1.0 - ratio) + second[i] * ratio for i in range(3)])


def _derive(base):
    """从 12 个合同令牌推导出界面真正要用的一批辅助令牌。

    合同只规定了 12 个主色，剩下的（按压态、表头、选中行、日志底…）一律
    由主色调出来，保证深浅两套皮肤的字面量都只出现在本文件里。
    """
    palette = dict(base)
    palette["on_accent"] = _WHITE                     # 主色块上的文字
    palette["accent_press"] = mix(base["accent_hover"], _BLACK, 0.16)
    palette["danger_hover"] = mix(base["danger"], _BLACK, 0.12)
    palette["danger_press"] = mix(base["danger"], _BLACK, 0.22)
    palette["surface_hi"] = mix(base["surface"], base["text"], 0.07)      # 悬停面
    palette["surface_low"] = mix(base["surface"], base["bg"], 0.55)       # 内嵌面
    palette["border_soft"] = mix(base["border"], base["surface"], 0.55)
    palette["selection"] = mix(base["surface"], base["accent"], 0.30)     # 表格选中行
    palette["row_alt"] = mix(base["surface"], base["surface_alt"], 0.55)  # 斑马纹
    palette["track"] = base["surface_alt"]                                # 进度条轨道
    palette["disabled_fill"] = mix(base["surface"], base["surface_alt"], 0.65)
    palette["disabled_text"] = mix(base["text_dim"], base["surface"], 0.55)
    palette["focus_ring"] = base["accent"]
    palette["log_bg"] = mix(base["bg"], base["surface"], 0.45)            # 日志深色块
    palette["scrollbar"] = mix(base["border"], base["text_dim"], 0.30)
    palette["scrollbar_hover"] = mix(base["border"], base["text_dim"], 0.60)
    return palette


_PALETTES = {
    MODE_DARK: _derive(DARK),
    MODE_LIGHT: _derive(LIGHT),
}

_current_mode = MODE_DARK
_current = _PALETTES[MODE_DARK]


def palette_for(mode):
    """取某个模式的完整令牌字典（含派生令牌）的副本。"""
    if mode not in _PALETTES:
        mode = MODE_DARK
    return dict(_PALETTES[mode])


def current():
    """当前生效的令牌字典。界面里所有颜色都必须从这里取。"""
    return _current


def mode():
    """当前模式名：``dark`` 或 ``light``。"""
    return _current_mode


def set_mode(new_mode):
    """切换模式并返回新的令牌字典（只切令牌，样式重建见 ``apply_style``）。"""
    global _current_mode, _current
    if new_mode not in _PALETTES:
        new_mode = MODE_DARK
    _current_mode = new_mode
    _current = _PALETTES[new_mode]
    return _current


def toggle_mode():
    """深色 ↔ 浅色，返回切换后的模式名。"""
    target = MODE_LIGHT if _current_mode == MODE_DARK else MODE_DARK
    set_mode(target)
    return target


# ----------------------------------------------------------------------
# 三、字体探测
# ----------------------------------------------------------------------

class Fonts(object):
    """一套字体对象。全部用 ``tkfont.Font``，便于度量文字宽度做自适应布局。"""

    def __init__(self, root, family, mono_family):
        self.family = family
        self.mono_family = mono_family
        # 单位是磅：Windows 96 DPI 下 10 磅约等于 13 像素，正文字号按合同「10」
        self.title = tkfont.Font(root=root, family=family, size=16, weight="bold")
        self.head = tkfont.Font(root=root, family=family, size=13, weight="bold")
        self.body = tkfont.Font(root=root, family=family, size=10)
        self.body_bold = tkfont.Font(root=root, family=family, size=10, weight="bold")
        self.small = tkfont.Font(root=root, family=family, size=9)
        self.small_bold = tkfont.Font(root=root, family=family, size=9, weight="bold")
        self.big = tkfont.Font(root=root, family=family, size=30, weight="bold")
        self.mono = tkfont.Font(root=root, family=mono_family, size=10)
        self.table = tkfont.Font(root=root, family=family, size=10)

    def line_height(self, font=None):
        """某字体的行高（像素），用于 Treeview 行高与文本区边距。"""
        target = font or self.body
        try:
            return int(target.metrics("linespace"))
        except tk.TclError:
            return 18


def _family_map(root):
    """``{小写族名: 真实族名}``，来自 tkinter.font.families() 的实测结果。"""
    mapping = {}
    try:
        for name in tkfont.families(root=root):
            mapping[name.lower()] = name
    except Exception:
        pass
    return mapping


def _pick(mapping, candidates, fallback):
    for candidate in candidates:
        if candidate.lower() in mapping:
            return mapping[candidate.lower()]
    return fallback


def probe_fonts(root):
    """实测系统里有没有合同要求的字体，没有才退到可用字体。

    合同要求：中文优先 ``Microsoft YaHei UI`` → ``Microsoft YaHei`` → ``Segoe UI``；
    等宽日志 ``Consolas`` / ``Cascadia Mono``。这里一律用 families() 的实测结果，
    不做「名字对就一定存在」的假设。
    """
    mapping = _family_map(root)
    fallback_family = "TkDefaultFont"
    try:
        fallback_family = tkfont.nametofont("TkDefaultFont").actual("family")
    except Exception:
        pass
    family = _pick(mapping, CJK_FONT_CANDIDATES, fallback_family)
    mono = _pick(mapping, MONO_FONT_CANDIDATES, family)
    return Fonts(root, family, mono)


# ----------------------------------------------------------------------
# 四、ttk 样式
# ----------------------------------------------------------------------

def _style_scrollbar(style, palette):
    """去掉 clam 默认的箭头与灰白槽，改成细窄圆润的滚动条。"""
    for orient, element, sticky in (("Vertical", "ns", "ns"), ("Horizontal", "ew", "ew")):
        trough = "%s.Scrollbar.trough" % orient
        thumb = "%s.Scrollbar.thumb" % orient
        try:
            style.layout("%s.TScrollbar" % orient, [
                (trough, {"sticky": sticky, "children": [
                    (thumb, {"expand": "1", "sticky": "nswe"}),
                ]}),
            ])
        except tk.TclError:
            pass
    for orient in ("Vertical", "Horizontal"):
        try:
            style.configure(
                "%s.TScrollbar" % orient,
                background=palette["scrollbar"],
                troughcolor=palette["bg"],
                bordercolor=palette["bg"],
                lightcolor=palette["scrollbar"],
                darkcolor=palette["scrollbar"],
                arrowcolor=palette["text_dim"],
                gripcount=0,
                arrowsize=0,
                borderwidth=0,
                relief="flat",
                width=10,
            )
            style.map(
                "%s.TScrollbar" % orient,
                background=[("pressed", palette["text_dim"]),
                            ("active", palette["scrollbar_hover"])],
            )
        except tk.TclError:
            pass


def _style_treeview(style, palette, fonts):
    row_height = fonts.line_height(fonts.table) + 10
    try:
        # 去掉 clam 给 Treeview 画的外框，只留内容区，视觉上才是「卡片里的表」
        style.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
    except tk.TclError:
        pass
    try:
        style.configure(
            "Treeview",
            background=palette["surface"],
            fieldbackground=palette["surface"],
            foreground=palette["text"],
            borderwidth=0,
            relief="flat",
            rowheight=row_height,
            font=fonts.table,
        )
        style.map(
            "Treeview",
            background=[("selected", palette["selection"])],
            foreground=[("selected", palette["text"])],
        )
        style.configure(
            "Treeview.Heading",
            background=palette["surface_alt"],
            foreground=palette["text_dim"],
            relief="flat",
            borderwidth=0,
            padding=(10, 8),
            font=fonts.small_bold,
        )
        style.map(
            "Treeview.Heading",
            background=[("active", palette["surface_hi"])],
            foreground=[("active", palette["text"])],
            relief=[("active", "flat"), ("pressed", "flat")],
        )
    except tk.TclError:
        pass


def _style_notebook(style, palette, fonts):
    try:
        style.configure(
            "TNotebook",
            background=palette["bg"],
            borderwidth=0,
            tabmargins=(0, 4, 0, 0),
            padding=0,
        )
        style.configure(
            "TNotebook.Tab",
            background=palette["bg"],
            foreground=palette["text_dim"],
            borderwidth=0,
            padding=(18, 9),
            font=fonts.body_bold,
            lightcolor=palette["surface_alt"],
            darkcolor=palette["surface_alt"],
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", palette["surface"]),
                        ("active", palette["surface_alt"])],
            foreground=[("selected", palette["text"]),
                        ("active", palette["text"])],
            lightcolor=[("selected", palette["surface"])],
            darkcolor=[("selected", palette["surface"])],
        )
    except tk.TclError:
        pass


def apply_style(root, new_mode=None, fonts=None):
    """构建/刷新整套 ttk 样式，返回 ``(令牌字典, 字体对象)``。

    使用 ``clam`` 主题：它是 Tk 里唯一一个几乎所有元素都能改颜色的内置主题，
    默认的 ``vista`` 主题会把背景色写死在 Windows 主题引擎里，改不动。
    """
    global _current, _current_mode
    if new_mode is not None:
        set_mode(new_mode)
    palette = _current
    if fonts is None:
        fonts = probe_fonts(root)

    style = ttk.Style(root)
    try:
        if "clam" in style.theme_names():
            style.theme_use("clam")
    except tk.TclError:
        pass

    try:
        style.configure(
            ".",
            background=palette["bg"],
            foreground=palette["text"],
            fieldbackground=palette["surface_alt"],
            bordercolor=palette["border"],
            lightcolor=palette["border"],
            darkcolor=palette["border"],
            troughcolor=palette["bg"],
            focuscolor=palette["accent"],
            selectbackground=palette["selection"],
            selectforeground=palette["text"],
            font=fonts.body,
        )
    except tk.TclError:
        pass

    # 面
    for name, bg in (("TFrame", "bg"), ("Card.TFrame", "surface"),
                     ("Bar.TFrame", "surface_alt"), ("Page.TFrame", "surface")):
        try:
            style.configure(name, background=palette[bg], borderwidth=0)
        except tk.TclError:
            pass
    for name, bg, fg in (("TLabel", "bg", "text"),
                         ("Surface.TLabel", "surface", "text"),
                         ("Dim.TLabel", "bg", "text_dim"),
                         ("DimSurface.TLabel", "surface", "text_dim")):
        try:
            style.configure(name, background=palette[bg], foreground=palette[fg])
        except tk.TclError:
            pass

    # 文本输入与下拉（界面主输入框用 tk.Entry，这里保留给备用控件）
    try:
        style.configure(
            "TEntry",
            fieldbackground=palette["surface_alt"],
            foreground=palette["text"],
            bordercolor=palette["border"],
            lightcolor=palette["border"],
            darkcolor=palette["border"],
            insertcolor=palette["text"],
            padding=(8, 6),
        )
        style.map(
            "TEntry",
            fieldbackground=[("focus", palette["surface_hi"])],
            bordercolor=[("focus", palette["accent"])],
            lightcolor=[("focus", palette["accent"])],
            darkcolor=[("focus", palette["accent"])],
        )
        style.configure(
            "TCombobox",
            fieldbackground=palette["surface_alt"],
            background=palette["surface_alt"],
            foreground=palette["text"],
            bordercolor=palette["border"],
            arrowcolor=palette["text_dim"],
            padding=(8, 5),
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", palette["surface_alt"])],
            foreground=[("readonly", palette["text"])],
            bordercolor=[("focus", palette["accent"])],
            arrowcolor=[("active", palette["accent"])],
        )
        style.configure("TCheckbutton", background=palette["bg"],
                        foreground=palette["text"], focuscolor=palette["accent"])
        style.map("TCheckbutton",
                  background=[("active", palette["bg"]), ("selected", palette["bg"])],
                  foreground=[("active", palette["text"])],
                  indicatorcolor=[("selected", palette["accent"])])
        style.configure("TSeparator", background=palette["border"])
    except tk.TclError:
        pass

    _style_notebook(style, palette, fonts)
    _style_treeview(style, palette, fonts)
    _style_scrollbar(style, palette)

    # 下拉弹层、选中色这类由 Tk 选项全局控制，只能走 option_add
    try:
        root.option_add("*TCombobox*Listbox.background", palette["surface"])
        root.option_add("*TCombobox*Listbox.foreground", palette["text"])
        root.option_add("*TCombobox*Listbox.selectBackground", palette["selection"])
        root.option_add("*TCombobox*Listbox.selectForeground", palette["text"])
        root.option_add("*selectBackground", palette["selection"])
        root.option_add("*selectForeground", palette["text"])
    except tk.TclError:
        pass

    return palette, fonts


# ----------------------------------------------------------------------
# 五、高 DPI
# ----------------------------------------------------------------------

_DPI_DONE = False
_DPI_NOTE = ""


def set_dpi_awareness():
    """开启 Windows 的 DPI 感知，避免高分屏下界面被系统拉伸成糊字。

    必须在创建 ``Tk`` 之前调用效果最好；重复调用是安全的。
    """
    global _DPI_DONE, _DPI_NOTE
    if _DPI_DONE:
        return _DPI_NOTE
    _DPI_DONE = True
    if sys.platform != "win32":
        _DPI_NOTE = "非 Windows 平台，跳过 DPI 感知设置。"
        return _DPI_NOTE
    try:
        import ctypes  # 仅 Windows 需要
        try:
            # 1 = 系统级 DPI 感知（Win8.1+）
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
            _DPI_NOTE = "已开启系统级 DPI 感知。"
        except Exception:
            # Win7 只有老接口
            ctypes.windll.user32.SetProcessDPIAware()
            _DPI_NOTE = "已用旧接口开启 DPI 感知。"
    except Exception as exc:
        _DPI_NOTE = "DPI 感知设置失败：%s" % exc
    return _DPI_NOTE


def dpi_note():
    """DPI 设置的说明文字（诊断页显示用）。"""
    return _DPI_NOTE or "未设置"


def apply_dpi_scaling(root):
    """DPI 感知开启后，把 Tk 的「磅 → 像素」换算修正到真实 DPI。

    不修正的话，150% 缩放下 Tk 仍按 96 DPI 换算，字会明显偏小。
    """
    try:
        pixels_per_inch = root.winfo_fpixels("1i")
        if pixels_per_inch and pixels_per_inch > 0:
            root.tk.call("tk", "scaling", float(pixels_per_inch) / 72.0)
    except Exception:
        pass


def initial_geometry(root, width=1180, height=780, screen_ratio=0.86):
    """按屏幕尺寸给一个不会超出工作区的初始窗口大小（不小于合同要求的 980×680）。"""
    try:
        screen_w = int(root.winfo_screenwidth())
        screen_h = int(root.winfo_screenheight())
    except Exception:
        screen_w = screen_h = 0
    if screen_w and screen_h:
        width = min(width, int(screen_w * screen_ratio))
        height = min(height, int(screen_h * screen_ratio))
    return max(980, width), max(680, height)
