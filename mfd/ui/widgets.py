#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自绘组件 —— 圆角按钮、卡片、胶囊标签、圆角进度条、状态点。

为什么用 Canvas 自绘而不是 ttk 控件：ttk 的按钮/进度条在 Windows 上会跟着
系统主题走（vista 主题的圆角与渐变由系统引擎画），既改不动颜色也控不住形状。
Canvas 画圆角矩形反而是最可控、最不容易在各版本 Tk 上跑偏的做法。

两条纪律：

1. 所有颜色来自 ``theme.current()``（或调用方传进来的令牌字典），本文件不写颜色字面量。
2. 每个组件的 ``destroy()`` 都要清掉自己排的 ``after`` 回调，否则窗口关了回调还在跑。
"""

import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk  # noqa: F401  （保留给调用方可能用到的样式组合）

from . import theme


# ----------------------------------------------------------------------
# 一、基础绘图工具
# ----------------------------------------------------------------------

def _parent_bg(master, palette):
    """取父容器的背景色，让 Canvas 与它「无缝」拼接。

    父容器是 ttk 控件时 ``cget("bg")`` 会抛 TclError，退回窗口底色。
    """
    try:
        value = master.cget("bg")
        if value:
            return value
    except Exception:
        pass
    return palette["bg"]


def _rounded_points(x1, y1, x2, y2, radius, steps=6):
    """生成圆角矩形的顶点序列（用多段折线逼近四分之一圆）。

    比 ``smooth=True`` 的样条靠谱：样条会把直边也带弯，圆角看起来发虚。
    """
    width = max(1.0, float(x2) - float(x1))
    height = max(1.0, float(y2) - float(y1))
    r = max(0.0, min(float(radius), width / 2.0, height / 2.0))
    if r <= 0.5:
        return [x1, y1, x2, y1, x2, y2, x1, y2]

    import math
    corners = (
        (x1 + r, y1 + r, 180.0, 270.0),  # 左上
        (x2 - r, y1 + r, 270.0, 360.0),  # 右上
        (x2 - r, y2 - r, 0.0, 90.0),     # 右下
        (x1 + r, y2 - r, 90.0, 180.0),   # 左下
    )
    points = []
    for cx, cy, start, end in corners:
        for index in range(steps + 1):
            angle = math.radians(start + (end - start) * index / float(steps))
            points.append(cx + math.cos(angle) * r)
            points.append(cy + math.sin(angle) * r)
    return points


def rounded_rect(canvas, x1, y1, x2, y2, radius, fill="", outline="", width=1, tags=None):
    """在 Canvas 上画一个圆角矩形，返回 item id。"""
    points = _rounded_points(x1, y1, x2, y2, radius)
    kwargs = {
        "fill": fill if fill else "",
        "outline": outline if outline else "",
        "width": width,
        "joinstyle": "round",
    }
    if tags:
        kwargs["tags"] = tags
    return canvas.create_polygon(points, **kwargs)


def cancel_afters(widget):
    """取消该控件排下的全部 ``after`` 回调。"""
    pending = getattr(widget, "_after_ids", None)
    if not pending:
        return
    for after_id in list(pending):
        try:
            widget.after_cancel(after_id)
        except Exception:
            pass
    pending.clear()


def _schedule(widget, delay_ms, callback):
    """记名排一次 ``after``，便于 destroy 时统一取消。"""
    bucket = getattr(widget, "_after_ids", None)
    if bucket is None:
        bucket = set()
        widget._after_ids = bucket

    def runner():
        bucket.discard(after_id)
        try:
            callback()
        except tk.TclError:
            pass

    after_id = widget.after(delay_ms, runner)
    bucket.add(after_id)
    return after_id


# ----------------------------------------------------------------------
# 二、圆角按钮
# ----------------------------------------------------------------------

# 每种外观用到的令牌名；None 表示「透明」（露出父容器底色）
#
# 2.0.3 口径：ghost 平时透明、悬停才浮出 surface_alt 底色，不再是「一直挂着
# 一块浅色块」；subtle 更弱，文字本身就用 text_dim。
KIND_TOKENS = {
    "primary": {"fill": "accent", "hover": "accent_hover", "press": "accent_press",
                "fg": "on_accent", "border": None},
    "ghost": {"fill": None, "hover": "ghost_hover", "press": "surface_hi",
              "fg": "text", "border": None},
    "outline": {"fill": None, "hover": "surface_alt", "press": "surface_hi",
                "fg": "text", "border": "border"},
    "subtle": {"fill": None, "hover": "surface_alt", "press": "surface_hi",
               "fg": "text_dim", "border": None},
    "danger": {"fill": "danger", "hover": "danger_hover", "press": "danger_press",
               "fg": "on_accent", "border": None},
}


class RoundedButton(tk.Canvas):
    """圆角按钮：hover / press / disabled 三态，支持键盘 Space 与 Enter。"""

    def __init__(self, master, text="", command=None, kind="primary",
                 palette=None, fonts=None, font=None, height=34, radius=8,
                 pad_x=18, min_width=0, tooltip=""):
        self._palette = palette or theme.current()
        self._fonts = fonts
        self._font = font or (fonts.body_bold if fonts else tkfont.nametofont("TkDefaultFont"))
        self._kind = kind if kind in KIND_TOKENS else "primary"
        self._text = text or ""
        self._command = command
        self._enabled = True
        self._hover = False
        self._pressed = False
        self._focused = False
        self._radius = radius
        self._pad_x = pad_x
        self._min_width = min_width
        self._height = height
        self._after_ids = set()
        self._tooltip = tooltip

        width = self._natural_width()
        tk.Canvas.__init__(
            self, master,
            width=width, height=height,
            bg=_parent_bg(master, self._palette),
            highlightthickness=0, bd=0,
            takefocus=1,
        )
        self.configure(cursor="hand2")

        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<space>", self._on_key)
        self.bind("<Return>", self._on_key)
        self.bind("<FocusIn>", self._on_focus_in)
        self.bind("<FocusOut>", self._on_focus_out)
        self._render()

    # -- 尺寸与文字 ------------------------------------------------------
    def _natural_width(self):
        try:
            text_width = self._font.measure(self._text)
        except tk.TclError:
            text_width = len(self._text) * 12
        return max(self._min_width, text_width + self._pad_x * 2)

    def _resize(self):
        self.configure(width=self._natural_width(), height=self._height)

    def set_text(self, text):
        """改文案并同步宽度（按钮宽度跟着文字走，避免文字被裁）。"""
        self._text = text or ""
        self._resize()
        self._render()

    def get_text(self):
        return self._text

    def set_kind(self, kind):
        if kind in KIND_TOKENS:
            self._kind = kind
            self._render()

    def set_enabled(self, enabled):
        """置灰/恢复。置灰时不再响应鼠标与键盘，也不触发回调。"""
        enabled = bool(enabled)
        if enabled == self._enabled:
            return
        self._enabled = enabled
        self._hover = False
        self._pressed = False
        try:
            self.configure(cursor="hand2" if enabled else "")
        except tk.TclError:
            pass
        self._render()

    def is_enabled(self):
        return self._enabled

    def set_tooltip(self, text):
        self._tooltip = text or ""

    def invoke(self):
        """以程序方式触发一次点击（自测与快捷键用）。"""
        self._fire()

    # -- 交互 ------------------------------------------------------------
    def _on_enter(self, _event=None):
        if not self._enabled:
            return
        self._hover = True
        self._render()

    def _on_leave(self, _event=None):
        self._hover = False
        self._pressed = False
        self._render()

    def _on_press(self, _event=None):
        if not self._enabled:
            return
        try:
            self.focus_set()  # 点击后拿到键盘焦点，Space/Enter 才能用
        except tk.TclError:
            pass
        self._pressed = True
        self._render()

    def _on_release(self, event=None):
        if not self._enabled:
            return
        was_pressed = self._pressed
        self._pressed = False
        self._render()
        if not was_pressed:
            return
        if event is not None:
            # 鼠标拖出按钮再松开视为取消
            inside = 0 <= event.x <= self.winfo_width() and 0 <= event.y <= self.winfo_height()
            if not inside:
                return
        self._fire()

    def _on_key(self, _event=None):
        if not self._enabled:
            return "break"
        self._fire()
        return "break"

    def _on_focus_in(self, _event=None):
        self._focused = True
        self._render()

    def _on_focus_out(self, _event=None):
        self._focused = False
        self._render()

    def _fire(self):
        if not self._enabled or self._command is None:
            return
        try:
            self._command()
        except tk.TclError:
            pass

    # -- 绘制 ------------------------------------------------------------
    def _colors(self):
        palette = self._palette
        spec = KIND_TOKENS.get(self._kind, KIND_TOKENS["primary"])
        if not self._enabled:
            return palette["disabled_fill"], palette["disabled_text"], "", False
        fill_token = spec["fill"]
        fill = palette[fill_token] if fill_token else ""
        if self._pressed:
            fill = palette[spec["press"]]
        elif self._hover:
            fill = palette[spec["hover"]]
        border = palette[spec["border"]] if spec["border"] else ""
        return fill, palette[spec["fg"]], border, True

    def _render(self):
        fill, fg, border, _active = self._colors()
        width = max(int(self._natural_width()), 8)
        height = int(self._height)
        # 按下时整体下移 1px：比只换颜色更有「按下去」的手感
        offset = 1.0 if (self._pressed and self._enabled) else 0.0
        self.delete("all")
        canvas_bg = _parent_bg(self.master, self._palette)
        if self._focused and self._enabled:
            rounded_rect(self, 0.5, 0.5, width - 0.5, height - 0.5, self._radius,
                         fill="", outline=self._palette["focus_ring"], width=1.5)
            inset = 2.0
        else:
            inset = 0.5
        rounded_rect(self, inset, inset + offset, width - inset, height - inset + offset,
                     self._radius, fill=fill or canvas_bg, outline=border, width=1)
        self.create_text(width / 2.0, height / 2.0 + offset, text=self._text, fill=fg,
                         font=self._font)

    # -- 主题 ------------------------------------------------------------
    def tint(self, palette, fonts=None):
        """换主题时重新上色。"""
        self._palette = palette
        if fonts is not None:
            self._fonts = fonts
        try:
            self.configure(bg=_parent_bg(self.master, palette))
        except tk.TclError:
            pass
        self._render()

    def destroy(self):
        cancel_afters(self)
        tk.Canvas.destroy(self)


# ----------------------------------------------------------------------
# 三、卡片
# ----------------------------------------------------------------------

class Card(tk.Frame):
    """圆角卡片容器：外层 1px 描边色 + 内层表面色，模拟圆角边框，不做重绘。

    用法：``card = Card(parent); card.body.pack(...)``——所有子控件放进 ``body``。
    """

    def __init__(self, master, palette=None, padding=14, border=1, **kwargs):
        self._palette = palette or theme.current()
        tk.Frame.__init__(self, master, bg=self._palette["border"], bd=0,
                          highlightthickness=0, **kwargs)
        # 外层与内层的 1px 差就是「边框」
        self.body = tk.Frame(self, bg=self._palette["surface"], bd=0,
                             highlightthickness=0)
        self.body.pack(fill="both", expand=True, padx=border, pady=border)
        self.padding = padding
        self._border = border

    def tint(self, palette, fonts=None):
        self._palette = palette
        try:
            self.configure(bg=palette["border"])
            self.body.configure(bg=palette["surface"])
        except tk.TclError:
            pass

    def inner(self):
        """返回带内边距的内容区（多数场景用它就不用自己管 padx/pady）。"""
        return self.body


# ----------------------------------------------------------------------
# 四、胶囊标签
# ----------------------------------------------------------------------

class Pill(tk.Canvas):
    """胶囊标签：版本号、统计数字、状态摘要都用它。"""

    def __init__(self, master, text="", palette=None, fonts=None, font=None,
                 fg="text_dim", bg="surface_alt", height=20, pad_x=9, dot=None,
                 outline=None):
        self._palette = palette or theme.current()
        self._fonts = fonts
        self._font = font or (fonts.small if fonts else tkfont.nametofont("TkDefaultFont"))
        self._text = text or ""
        self._fg_token = fg
        self._bg_token = bg
        self._outline_token = outline
        self._dot_token = dot
        self._height = height
        self._pad_x = pad_x
        self._radius = height / 2.0
        tk.Canvas.__init__(self, master, width=self._natural_width(), height=height,
                           bg=_parent_bg(master, self._palette),
                           highlightthickness=0, bd=0)
        self._render()

    def _natural_width(self):
        try:
            text_width = self._font.measure(self._text)
        except tk.TclError:
            text_width = len(self._text) * 9
        dot_part = 14 if self._dot_token else 0
        return int(text_width + self._pad_x * 2 + dot_part)

    def set_text(self, text):
        self._text = text or ""
        self.configure(width=self._natural_width())
        self._render()

    def get_text(self):
        return self._text

    def set_colors(self, fg=None, bg=None, dot=None):
        if fg is not None:
            self._fg_token = fg
        if bg is not None:
            self._bg_token = bg
        if dot is not None:
            self._dot_token = dot
        self.configure(width=self._natural_width())
        self._render()

    def _render(self):
        palette = self._palette
        width = int(self._natural_width())
        height = int(self._height)
        self.delete("all")
        fill = palette[self._bg_token] if self._bg_token else ""
        outline = palette[self._outline_token] if self._outline_token else ""
        rounded_rect(self, 0.5, 0.5, width - 0.5, height - 0.5, self._radius,
                     fill=fill or _parent_bg(self.master, palette), outline=outline, width=1)
        text_x = width / 2.0
        if self._dot_token:
            dot_r = 3.0
            dot_x = self._pad_x + dot_r
            self.create_oval(dot_x - dot_r, height / 2.0 - dot_r,
                             dot_x + dot_r, height / 2.0 + dot_r,
                             fill=palette[self._dot_token], outline="")
            text_x = (self._pad_x + dot_r * 2 + width) / 2.0
        self.create_text(text_x, height / 2.0, text=self._text,
                         fill=palette[self._fg_token], font=self._font)

    def tint(self, palette, fonts=None):
        self._palette = palette
        if fonts is not None:
            self._fonts = fonts
        try:
            self.configure(bg=_parent_bg(self.master, palette))
        except tk.TclError:
            pass
        self._render()

    def destroy(self):
        cancel_afters(self)
        tk.Canvas.destroy(self)


# ----------------------------------------------------------------------
# 五、圆角进度条
# ----------------------------------------------------------------------

class ProgressBar(tk.Canvas):
    """圆角进度条：数值变化时平滑过渡，默认在条内显示百分比。"""

    def __init__(self, master, palette=None, fonts=None, height=18,
                 radius=None, show_text=True, font=None):
        self._palette = palette or theme.current()
        self._fonts = fonts
        self._font = font or (fonts.small_bold if fonts else tkfont.nametofont("TkDefaultFont"))
        self._height = height
        self._radius = radius if radius is not None else height / 2.0
        self._show_text = show_text
        self._value = 0.0
        self._target = 0.0
        self._after_ids = set()
        tk.Canvas.__init__(self, master, height=height, bg=self._palette["surface"],
                           highlightthickness=0, bd=0)
        self.bind("<Configure>", self._on_configure)
        self._render()

    # -- 取值 ------------------------------------------------------------
    def set_value(self, value, animate=True):
        """value 取 0.0~1.0（超过按 1.0 处理）。"""
        try:
            value = float(value)
        except (TypeError, ValueError):
            value = 0.0
        if value < 0.0:
            value = 0.0
        if value > 1.0:
            value = 1.0
        self._target = value
        if not animate:
            self._value = value
            self._render()
            return
        if abs(self._value - value) < 0.002:
            self._value = value
            self._render()
            return
        self._tick()

    def get_value(self):
        return self._value

    def reset(self):
        cancel_afters(self)
        self._value = 0.0
        self._target = 0.0
        self._render()

    # -- 动画 ------------------------------------------------------------
    def _tick(self):
        def step():
            delta = self._target - self._value
            if abs(delta) < 0.002:
                self._value = self._target
                self._render()
                return
            self._value += delta * 0.30
            self._render()
            _schedule(self, 16, step)

        _schedule(self, 16, step)

    # -- 绘制 ------------------------------------------------------------
    def _on_configure(self, _event=None):
        self._render()

    def _render(self):
        palette = self._palette
        width = max(int(self.winfo_width()), 2)
        height = max(int(self._height), 4)
        self.delete("all")
        # 轨道
        rounded_rect(self, 0.5, 0.5, width - 0.5, height - 0.5, self._radius,
                     fill=palette["track"], outline="")
        # 已完成部分（贴左，右侧保持圆角）
        fill_width = (width - 1.0) * self._value
        if fill_width > 1.0:
            token = "success" if self._value >= 0.999 else "accent"
            right = max(fill_width, self._radius * 2)
            rounded_rect(self, 0.5, 0.5, right, height - 0.5,
                         self._radius, fill=palette[token], outline="")
            # 顶部 1px 高光：细条上多这一笔，纯色块才不至于发平
            if height >= 6:
                gloss = theme.mix(palette[token], palette["on_accent"], 0.34)
                self.create_line(self._radius, 1.5,
                                 max(right - self._radius, self._radius + 1.0), 1.5,
                                 fill=gloss, width=1)
        if self._show_text:
            percent = int(round(self._value * 100))
            label = "%d%%" % percent
            center_x = width / 2.0
            covered = center_x <= fill_width
            color = palette["on_accent"] if covered else palette["text_dim"]
            self.create_text(center_x, height / 2.0, text=label, fill=color,
                             font=self._font)

    def tint(self, palette, fonts=None):
        self._palette = palette
        if fonts is not None:
            self._fonts = fonts
        try:
            self.configure(bg=palette["surface"])
        except tk.TclError:
            pass
        self._render()

    def destroy(self):
        cancel_afters(self)
        tk.Canvas.destroy(self)


# ----------------------------------------------------------------------
# 六、状态点
# ----------------------------------------------------------------------

class StatusDot(tk.Canvas):
    """连接/运行状态小圆点，忙碌时带一圈呼吸光晕。"""

    def __init__(self, master, palette=None, size=12, state="idle"):
        self._palette = palette or theme.current()
        self._size = size
        self._state = state if state in theme.DOT_TOKEN else "idle"
        self._phase = 0
        self._after_ids = set()
        tk.Canvas.__init__(self, master, width=size, height=size,
                           bg=_parent_bg(master, palette),
                           highlightthickness=0, bd=0)
        self._render()
        self._sync_pulse()

    def set_state(self, state):
        self._state = state if state in theme.DOT_TOKEN else "idle"
        self._render()
        self._sync_pulse()

    def get_state(self):
        return self._state

    # -- 呼吸动画 --------------------------------------------------------
    def _sync_pulse(self):
        """只有忙碌态才排定时器；切到别的状态立刻停，避免空转。"""
        cancel_afters(self)
        if self._state == "busy":
            _schedule(self, 80, self._pulse_step)
        else:
            self._phase = 0

    def _pulse_step(self):
        self._phase = (self._phase + 1) % 12
        self._render()
        if self._state == "busy":
            _schedule(self, 80, self._pulse_step)

    def _render(self):
        palette = self._palette
        size = float(self._size)
        self.delete("all")
        color = palette[theme.DOT_TOKEN.get(self._state, "text_dim")]
        if self._state == "busy":
            # 光晕：半径在 0.38~0.48 之间来回
            wave = abs(6 - self._phase) / 6.0
            ring_r = size * (0.38 + 0.10 * wave)
            self.create_oval(size / 2.0 - ring_r, size / 2.0 - ring_r,
                             size / 2.0 + ring_r, size / 2.0 + ring_r,
                             outline=color, width=1)
        core = size * 0.26
        self.create_oval(size / 2.0 - core, size / 2.0 - core,
                         size / 2.0 + core, size / 2.0 + core,
                         fill=color, outline="")

    def tint(self, palette, fonts=None):
        self._palette = palette
        try:
            self.configure(bg=_parent_bg(self.master, palette))
        except tk.TclError:
            pass
        self._render()

    def destroy(self):
        cancel_afters(self)
        tk.Canvas.destroy(self)


# ----------------------------------------------------------------------
# 七、指标卡
# ----------------------------------------------------------------------

class MetricTile(tk.Frame):
    """概览条上的一格指标：上面大数字、下面小说明。

    沿用原来的 ``set_text("完成 3")`` 调用方式（调用方 ``_apply_stats`` 不必改），
    内部按最后一个空格把文案拆成「说明 + 数字」；数字大于 0 且给了 ``warn_token``
    时切成警示色（失败数就是这么变红的）。
    """

    def __init__(self, master, text="", palette=None, fonts=None,
                 value_token="text", warn_token=None):
        self._palette = palette or theme.current()
        self._fonts = fonts
        self._value_token = value_token
        self._warn_token = warn_token
        self._label = ""
        self._value = ""
        tk.Frame.__init__(self, master, bg=self._palette["surface"], bd=0,
                          highlightthickness=0)
        value_font = fonts.metric if fonts else tkfont.nametofont("TkDefaultFont")
        label_font = fonts.small if fonts else tkfont.nametofont("TkDefaultFont")
        self._value_label = tk.Label(self, text="0", bg=self._palette["surface"],
                                     fg=self._palette.get(value_token, self._palette["text"]),
                                     font=value_font, anchor="w")
        self._value_label.pack(side="top", anchor="w")
        self._text_label = tk.Label(self, text="", bg=self._palette["surface"],
                                    fg=self._palette["text_dim"],
                                    font=label_font, anchor="w")
        self._text_label.pack(side="top", anchor="w", pady=(2, 0))
        self.set_text(text)

    @staticmethod
    def _split(text):
        """``"完成 3"`` → ``("完成", "3")``；拆不开就把整串当说明。"""
        raw = (text or "").strip()
        if " " in raw:
            head, tail = raw.rsplit(" ", 1)
            cleaned = tail.replace(",", "").replace(".", "")
            if cleaned.isdigit():
                return head, tail
        return raw, ""

    def set_text(self, text):
        label, value = self._split(text)
        self._label = label
        self._value = value
        self._render()

    def get_text(self):
        if self._value:
            return "%s %s" % (self._label, self._value)
        return self._label

    def _render(self):
        token = self._value_token
        if self._warn_token and self._value.isdigit() and int(self._value) > 0:
            token = self._warn_token
        try:
            self._value_label.configure(text=self._value or "0",
                                        fg=self._palette.get(token, self._palette["text"]))
            self._text_label.configure(text=self._label,
                                       bg=self._palette["surface"],
                                       fg=self._palette["text_dim"])
            self.configure(bg=self._palette["surface"])
        except tk.TclError:
            pass

    def tint(self, palette, fonts=None):
        self._palette = palette
        if fonts is not None:
            self._fonts = fonts
            try:
                self._value_label.configure(font=fonts.metric)
                self._text_label.configure(font=fonts.small)
            except tk.TclError:
                pass
        self._render()

    def destroy(self):
        cancel_afters(self)
        tk.Frame.destroy(self)
