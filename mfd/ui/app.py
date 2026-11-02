#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""界面主体 —— 窗口装配、五个区块、四个页签、事件泵、全部交互。

线程模型
--------
解析链接、建队列、下载、重试全部在后台线程跑；所有回调只做一件事：把事件塞进
``queue.Queue``，再由 ``root.after(80, self._pump)`` 泵回主线程执行。**本文件里
任何后台线程都不得直接碰 tkinter 控件**（Tk 不是线程安全的）。

依赖边界
--------
只调 UI 契约第 1 节列出的内核 API，经由 ``load_kernel()`` 集中导入；
某个内核模块还没写出来时，用占位实现顶上，界面照样能开，并在日志里如实说明，
不会因为 import 失败而「双击没反应」。
"""

import os
import queue
import subprocess
import threading
import time
import traceback
import tkinter as tk
import tkinter.font as tkfont  # noqa: F401  （保持与 theme/widgets 一致的字体入口）
from tkinter import filedialog
from tkinter import messagebox
from tkinter import ttk

from . import theme
from . import widgets


# ----------------------------------------------------------------------
# 一、格式化小工具（不引内核的 util，界面层自带一份，避免越界依赖）
# ----------------------------------------------------------------------

def _fmt_size(num_bytes):
    """字节数 → 人读的大小。"""
    try:
        value = float(num_bytes)
    except (TypeError, ValueError):
        return "未知"
    if value < 0:
        return "未知"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024.0 or unit == "TB":
            if unit == "B":
                return "%d B" % int(value)
            return "%.1f %s" % (value, unit)
        value /= 1024.0
    return "未知"


def _fmt_speed(kb_per_second):
    """速度（KB/s）→ 字符串。"""
    try:
        value = float(kb_per_second)
    except (TypeError, ValueError):
        return "—"
    if value <= 0.05:
        return "— KB/s"
    if value >= 1024.0:
        return "%.2f MB/s" % (value / 1024.0)
    return "%.1f KB/s" % value


def _fmt_seconds(seconds):
    """秒数 → 中文短串。"""
    try:
        total = int(seconds)
    except (TypeError, ValueError):
        return "--"
    if total < 0:
        return "--"
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return "%d小时%02d分" % (hours, minutes)
    if minutes:
        return "%d分%02d秒" % (minutes, secs)
    return "%d秒" % secs


def _fmt_timestamp(value):
    """时间 → ``月-日 时:分``。

    内核的 ``FailureRecord.created_at`` 是可读字符串（``YYYY-mm-dd HH:MM:SS``），
    这里同时兼容时间戳，避免两边格式一变界面就显示成 ``--``。
    """
    if value is None or value == "":
        return "--"
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return "--"
        if len(text) >= 16 and text[4] == "-" and text[7] == "-":
            return text[5:16]      # 去掉年份与秒，表格更紧凑
        return text
    try:
        stamp = float(value)
    except (TypeError, ValueError):
        return "--"
    if stamp <= 0:
        return "--"
    try:
        return time.strftime("%m-%d %H:%M", time.localtime(stamp))
    except (ValueError, OSError):
        return "--"


def _empty_stats():
    """与契约 1.2 同形的空统计。"""
    return {
        "counts": {"waiting": 0, "running": 0, "done": 0,
                   "skipped": 0, "failed": 0, "canceled": 0},
        "total": 0,
        "finished": 0,
        "bytes": 0,
        "elapsed": 0.0,
        "speed": 0.0,
        "paused": False,
        "failed_items": 0,
    }


# 内核还没就绪时的兜底文案（真内核在时会被覆盖）
FALLBACK_STATUS_TEXT = {
    "waiting": "等待中",
    "running": "下载中",
    "done": "已完成",
    "skipped": "已跳过",
    "failed": "失败",
    "canceled": "已取消",
}

FALLBACK_KIND_TEXT = {
    "network": "网络错误",
    "http": "服务器拒绝",
    "disk": "磁盘写入",
    "timeout": "超时",
    "unknown": "未知",
}


# ----------------------------------------------------------------------
# 二、内核装载（唯一 import 点；缺失模块降级为占位实现）
# ----------------------------------------------------------------------

class FallbackMediaFireError(Exception):
    """``mfd.mediafire`` 缺席时的替身，字段与真身一致。"""

    def __init__(self, user_message, detail=""):
        Exception.__init__(self, user_message)
        self.user_message = user_message
        self.detail = detail or ""

    def __str__(self):
        return self.user_message


class FallbackTask(object):
    """``DownloadTask`` 缺席时的替身：字段名与构造签名都照真身抄一份。"""

    def __init__(self, name="", url="", size=0, quickkey="", folder="", hash_value=""):
        self.name = name or ""
        self.url = url or ""
        try:
            self.size = int(size or 0)
        except (TypeError, ValueError):
            self.size = 0
        self.quickkey = quickkey or ""
        self.folder = folder or ""
        self.hash_value = (hash_value or "").strip().upper()
        self.status = "waiting"
        self.error = ""
        self.fail_kind = ""
        self.saved_path = ""
        self.downloaded = 0
        self.speed = 0.0
        self.started_at = 0.0
        self.finished_at = 0.0
        self.attempts = 0

    @property
    def percent(self):
        """与真身一致：返回 0~100。"""
        if self.size > 0:
            return min(100.0, self.downloaded * 100.0 / self.size)
        return 0.0

    def eta_seconds(self):
        if self.status != "running" or self.speed <= 1 or self.size <= 0:
            return None
        return max(0, self.size - self.downloaded) / (self.speed * 1024.0)


class FallbackDownloader(object):
    """``Downloader`` 缺席时的替身：能收任务，但不真的下载。"""

    def __init__(self, download_dir="", history=None, max_threads=2, gap_seconds=1.0,
                 on_event=None, max_retries=3, failures=None):
        self.download_dir = download_dir
        self.history = history
        self.max_threads = max_threads
        self.gap_seconds = gap_seconds
        self.on_event = on_event
        self.max_retries = max_retries
        self.failures = failures
        self.tasks = []

    def load_tasks(self, items, append=False):
        built = []
        for item in (items or []):
            built.append(FallbackTask(
                name=item.get("name", ""), url=item.get("url", ""),
                size=item.get("size", 0), quickkey=item.get("quickkey", ""),
                folder=item.get("folder", ""), hash_value=item.get("hash", ""),
            ))
        self.tasks = (self.tasks + built) if append else built
        return built

    def start(self):
        return False

    def stop(self):
        return None

    def stop_and_wait(self, timeout=5.0):
        return None

    def pause(self):
        return None

    def resume(self):
        return None

    def stats(self):
        return _empty_stats()

    def retry_failed(self):
        return 0


class FallbackFailureBook(object):
    """``FailureBook`` 缺席时的替身。"""

    def __init__(self, path=None):
        self.path = path
        self.records = []

    def add(self, record):
        self.records.append(record)

    def by_kind(self):
        return {}

    def by_platform(self):
        return {}

    def to_items(self):
        return []

    def export_json(self):
        return "[]"

    def export_markdown(self):
        return ""

    def export_links_txt(self):
        return ""

    def clear(self):
        self.records = []

    def save(self):
        return None

    def load(self):
        return None


class FallbackDiagnostics(object):
    """``Diagnostics`` 缺席时的替身。"""

    def collect_environment(self):
        return {"内核诊断": "mfd/diagnostics.py 尚未就绪"}

    def note(self, *args, **kwargs):
        return None

    def save_report(self, failures=None):
        return ""


class Kernel(object):
    """契约第 1 节 API 的集中容器。"""

    def __init__(self):
        self.APP_NAME = "MediaFire 批量下载器"
        self.VERSION = "2.0.0"
        self.SHORT_VERSION = "2.0"
        self.TAGLINE = "批量解析 · 断点续传 · 失败清单"
        self.missing = []
        self.notes = []
        # 以下是装载失败时的兜底默认值，避免界面拿到 None 才炸
        self.MediaFireError = FallbackMediaFireError
        self.resolve_link = _missing_resolve
        self.describe_key_kind = lambda url: "unknown"
        self.Downloader = FallbackDownloader
        self.DownloadTask = FallbackTask
        self.STATUS_TEXT = dict(FALLBACK_STATUS_TEXT)
        self.FailureBook = FallbackFailureBook
        self.FailureRecord = None
        self.KIND_TEXT = dict(FALLBACK_KIND_TEXT)
        self.History = None
        self.default_download_dir = _fallback_download_dir
        self.Diagnostics = FallbackDiagnostics


def _missing_resolve(url, include_subfolders=True, on_progress=None):
    raise FallbackMediaFireError(
        "解析模块 mfd/mediafire.py 没能加载，暂时无法解析链接。",
        "resolve_link 不可用",
    )


def _fallback_download_dir():
    base = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    target = os.path.join(base, "Downloads", "MediaFireDownloads")
    try:
        os.makedirs(target, exist_ok=True)
    except OSError:
        target = os.getcwd()
    return target


def load_kernel():
    """把契约里的 API 一次性导入；缺哪个就换替身并记进 ``missing``。"""
    kernel = Kernel()

    try:
        from .. import APP_NAME, APP_TAGLINE, __version__
        kernel.APP_NAME = APP_NAME
        kernel.VERSION = __version__
        kernel.TAGLINE = APP_TAGLINE
        kernel.SHORT_VERSION = ".".join(str(__version__).split(".")[:2])
    except Exception as exc:
        kernel.notes.append("mfd/__init__.py 读取失败：%s" % exc)

    # 解析层
    try:
        from ..mediafire import MediaFireError, describe_key_kind, resolve_link
        kernel.MediaFireError = MediaFireError
        kernel.resolve_link = resolve_link
        kernel.describe_key_kind = describe_key_kind
    except Exception as exc:
        kernel.MediaFireError = FallbackMediaFireError
        kernel.resolve_link = _missing_resolve
        kernel.describe_key_kind = lambda url: "unknown"
        kernel.missing.append("mfd/mediafire.py（%s）" % exc)

    # 下载引擎
    try:
        from ..downloader import STATUS_TEXT, DownloadTask, Downloader
        kernel.Downloader = Downloader
        kernel.DownloadTask = DownloadTask
        merged = dict(FALLBACK_STATUS_TEXT)
        if isinstance(STATUS_TEXT, dict):
            merged.update(STATUS_TEXT)
        kernel.STATUS_TEXT = merged
    except Exception as exc:
        kernel.Downloader = FallbackDownloader
        kernel.DownloadTask = FallbackTask
        kernel.STATUS_TEXT = dict(FALLBACK_STATUS_TEXT)
        kernel.missing.append("mfd/downloader.py（%s）" % exc)

    # 失败清单
    try:
        from ..failures import KIND_TEXT, FailureBook, FailureRecord
        kernel.FailureBook = FailureBook
        kernel.FailureRecord = FailureRecord
        merged_kinds = dict(FALLBACK_KIND_TEXT)
        if isinstance(KIND_TEXT, dict):
            merged_kinds.update(KIND_TEXT)
        kernel.KIND_TEXT = merged_kinds
    except Exception as exc:
        kernel.FailureBook = FallbackFailureBook
        kernel.FailureRecord = None
        kernel.KIND_TEXT = dict(FALLBACK_KIND_TEXT)
        kernel.missing.append("mfd/failures.py（%s）" % exc)

    # 历史记录与默认目录
    try:
        from ..storage import History, default_download_dir
        kernel.History = History
        kernel.default_download_dir = default_download_dir
    except Exception as exc:
        kernel.History = None
        kernel.default_download_dir = _fallback_download_dir
        kernel.missing.append("mfd/storage.py（%s）" % exc)

    # 诊断
    try:
        from ..diagnostics import Diagnostics, build_report  # noqa: F401
        kernel.Diagnostics = Diagnostics
    except Exception as exc:
        kernel.Diagnostics = FallbackDiagnostics
        kernel.missing.append("mfd/diagnostics.py（%s）" % exc)

    return kernel


# ----------------------------------------------------------------------
# 三、App
# ----------------------------------------------------------------------

PUMP_INTERVAL_MS = 80     # 事件泵间隔（契约规定 80）
TICK_INTERVAL_MS = 500    # 下载中统计轮询间隔
LOG_MAX_LINES = 2000      # 日志区保留行数上限


class App(object):
    """主窗口。构造即可用，不进 mainloop（由 ``mfd.ui.run`` 负责）。"""

    def __init__(self, root):
        self.root = root
        self.kernel = load_kernel()
        theme.set_dpi_awareness()
        self.palette, self.fonts = theme.apply_style(root, theme.mode())
        theme.apply_dpi_scaling(root)

        # 事件与定时器
        self._queue = queue.Queue()
        self._pump_id = None
        self._tick_id = None
        self._theme_jobs = []
        self._lock = threading.Lock()

        # 内核对象（懒建）
        self.history = None
        self.downloader = None
        self.failures = None
        self.diagnostics = None

        # 运行态
        self._tasks = []
        self._items = []
        self._task_rows = {}
        self._stats = _empty_stats()
        self._running = False
        self._paused = False
        self._busy = False
        self._background = False
        self._log_lines = 0

        self._build_window()
        self._build_ui()
        self._bind_shortcuts()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._ensure_failures()
        self._refresh_failures()
        self._refresh_diagnostics()
        self._report_kernel_state()

        self._pump_id = self.root.after(PUMP_INTERVAL_MS, self._pump)

    # ------------------------------------------------------------------
    # 3.1 窗口与布局
    # ------------------------------------------------------------------
    def _build_window(self):
        palette = self.palette
        self.root.title("%s %s" % (self.kernel.APP_NAME, self.kernel.VERSION))
        width, height = theme.initial_geometry(self.root)
        self.root.geometry("%dx%d" % (width, height))
        self.root.minsize(980, 680)
        self.root.configure(bg=palette["bg"])

    def _build_ui(self):
        self._build_header()
        self._build_input_card()
        self._build_overview()
        self._build_status_bar()   # 先占住底部，再让 Notebook 吃满剩余空间
        self._build_notebook()

    # -- 主题登记小工具 --------------------------------------------------
    def _frame(self, parent, bg_token="surface", **kwargs):
        frame = tk.Frame(parent, bg=self.palette[bg_token], **kwargs)

        def job(palette):
            try:
                frame.configure(bg=palette[bg_token])
            except tk.TclError:
                pass

        self._theme_jobs.append(job)
        return frame

    def _label(self, parent, text="", bg_token="surface", fg_token="text",
               font=None, **kwargs):
        label = tk.Label(parent, text=text, bg=self.palette[bg_token],
                         fg=self.palette[fg_token], font=font or self.fonts.body, **kwargs)

        def job(palette):
            try:
                label.configure(bg=palette[bg_token], fg=palette[fg_token])
            except tk.TclError:
                pass

        self._theme_jobs.append(job)
        return label

    def _entry(self, parent, bg_token="surface_alt", width=None):
        options = {
            "bg": self.palette[bg_token],
            "fg": self.palette["text"],
            "insertbackground": self.palette["text"],
            "relief": "flat",
            "bd": 0,
            "highlightthickness": 1,
            "highlightbackground": self.palette["border"],
            "highlightcolor": self.palette["accent"],
            "disabledbackground": self.palette["disabled_fill"],
            "disabledforeground": self.palette["disabled_text"],
            "font": self.fonts.body,
        }
        if width:
            options["width"] = width
        entry = tk.Entry(parent, **options)

        def job(palette):
            try:
                entry.configure(
                    bg=palette[bg_token],
                    fg=palette["text"],
                    insertbackground=palette["text"],
                    highlightbackground=palette["border"],
                    highlightcolor=palette["accent"],
                    disabledbackground=palette["disabled_fill"],
                    disabledforeground=palette["disabled_text"],
                )
            except tk.TclError:
                pass

        self._theme_jobs.append(job)
        return entry

    def _spinbox(self, parent, from_, to, increment=1, width=4):
        options = {
            "from_": from_,
            "to": to,
            "increment": increment,
            "width": width,
            "bg": self.palette["surface_alt"],
            "fg": self.palette["text"],
            "insertbackground": self.palette["text"],
            "relief": "flat",
            "bd": 0,
            "highlightthickness": 1,
            "highlightbackground": self.palette["border"],
            "highlightcolor": self.palette["accent"],
            "font": self.fonts.body,
            "justify": "center",
        }
        try:
            spin = tk.Spinbox(parent, buttonbackground=self.palette["surface_alt"], **options)
        except tk.TclError:
            spin = tk.Spinbox(parent, **options)

        def job(palette):
            try:
                spin.configure(bg=palette["surface_alt"], fg=palette["text"],
                               insertbackground=palette["text"],
                               buttonbackground=palette["surface_alt"],
                               highlightbackground=palette["border"],
                               highlightcolor=palette["accent"])
            except tk.TclError:
                pass

        self._theme_jobs.append(job)
        return spin

    def _checkbutton(self, parent, text, variable):
        check = tk.Checkbutton(
            parent, text=text, variable=variable,
            bg=self.palette["surface"], fg=self.palette["text"],
            activebackground=self.palette["surface"],
            activeforeground=self.palette["text"],
            selectcolor=self.palette["surface_alt"],
            highlightthickness=0, bd=0, font=self.fonts.body,
        )

        def job(palette):
            try:
                check.configure(bg=palette["surface"], fg=palette["text"],
                                activebackground=palette["surface"],
                                activeforeground=palette["text"],
                                selectcolor=palette["surface_alt"])
            except tk.TclError:
                pass

        self._theme_jobs.append(job)
        return check

    def _register_tint(self, widget):
        """Canvas 类组件（按钮/胶囊/进度条/状态点/卡片）换主题时统一重新上色。"""
        self._theme_jobs.append(lambda palette: widget.tint(palette, self.fonts))
        return widget

    # -- 1. 顶部 Header --------------------------------------------------
    def _build_header(self):
        palette = self.palette
        bar = self._frame(self.root, "bg", height=56)
        bar.pack(side="top", fill="x", padx=16, pady=(16, 8))
        bar.pack_propagate(False)

        left = self._frame(bar, "bg")
        left.pack(side="left", fill="y")
        title = self._label(left, self.kernel.APP_NAME, bg_token="bg",
                            font=self.fonts.title, anchor="w")
        title.pack(side="left")

        # 版本胶囊：8 磅字 + 1px 描边 + 圆角 10，比原来那块实心色块轻一档
        self.version_pill = self._register_tint(widgets.Pill(
            left, self.kernel.SHORT_VERSION, palette=palette, fonts=self.fonts,
            fg="accent", bg="surface_alt", outline="border",
            font=self.fonts.tiny_bold, height=20, pad_x=10))
        self.version_pill.pack(side="left", padx=(10, 12), pady=(8, 0))

        self.tagline_label = self._label(left, self.kernel.TAGLINE, bg_token="bg",
                                         fg_token="text_dim", font=self.fonts.small,
                                         anchor="w")
        self.tagline_label.pack(side="left", pady=(10, 0))

        right = self._frame(bar, "bg")
        right.pack(side="right", fill="y")
        self.theme_btn = self._register_tint(widgets.RoundedButton(
            right, self._theme_button_text(), command=self._toggle_theme,
            kind="ghost", palette=palette, fonts=self.fonts, height=32,
            pad_x=12, min_width=0))
        self.theme_btn.pack(side="right", pady=8)

        # 内核缺模块时的提示胶囊（正常时创建但隐藏，免得布局跳动）
        self.kernel_pill = self._register_tint(widgets.Pill(
            right, "内核未就绪", palette=palette, fonts=self.fonts,
            fg="warning", bg="surface_alt", outline="border",
            font=self.fonts.tiny_bold, height=20, pad_x=10))
        if self.kernel.missing:
            self.kernel_pill.pack(side="right", padx=(0, 8), pady=8)

    def _theme_button_text(self):
        # 图标式：月亮＝现在是深色、点了会变浅；太阳反之
        return "☀ 浅色" if theme.mode() == theme.MODE_DARK else "☾ 深色"

    # -- 2. 输入卡 -------------------------------------------------------
    def _build_input_card(self):
        palette = self.palette
        card = self._register_tint(widgets.Card(self.root, palette=palette))
        card.pack(side="top", fill="x", padx=16, pady=(0, 12))
        body = card.body
        body.configure(padx=16, pady=16)

        # 第一行：链接 + 主按钮
        link_row = self._frame(body, "surface")
        link_row.pack(fill="x")
        self.link_entry = self._entry(link_row, "surface_alt")
        self.link_entry.pack(side="left", fill="x", expand=True, ipady=8, padx=(0, 8))
        self._attach_placeholder(self.link_entry, "在这里粘贴 MediaFire 文件或文件夹链接（Ctrl+V）")
        self.parse_btn = self._register_tint(widgets.RoundedButton(
            link_row, "解析并加入队列", command=self._on_parse, kind="primary",
            palette=palette, fonts=self.fonts, height=34, min_width=136))
        self.parse_btn.pack(side="right")

        # 第二行：目录 / 线程 / 间隔 / 含子文件夹
        options_row = self._frame(body, "surface")
        options_row.pack(fill="x", pady=(12, 0))

        self._label(options_row, "保存到", fg_token="text_dim",
                    font=self.fonts.small).pack(side="left", padx=(0, 8))
        self.dir_entry = self._entry(options_row, "surface_alt")
        self.dir_entry.pack(side="left", fill="x", expand=True, ipady=6)
        try:
            self.dir_entry.insert(0, self.kernel.default_download_dir())
        except Exception:
            pass
        self.browse_btn = self._register_tint(widgets.RoundedButton(
            options_row, "浏览…", command=self._choose_dir, kind="subtle",
            palette=palette, fonts=self.fonts, height=30, pad_x=12))
        self.browse_btn.pack(side="left", padx=(8, 8))

        # 一键打开下载文件夹（用户 2026-11-02 要「快速打开下载文件夹」）
        self.open_dir_btn = self._register_tint(widgets.RoundedButton(
            options_row, "打开文件夹", command=self._open_dir, kind="subtle",
            palette=palette, fonts=self.fonts, height=30, pad_x=12))
        self.open_dir_btn.pack(side="left", padx=(0, 16))

        self._label(options_row, "线程", fg_token="text_dim",
                    font=self.fonts.small).pack(side="left", padx=(0, 8))
        self.threads_spin = self._spinbox(options_row, 1, 8, 1, width=3)
        self.threads_spin.delete(0, "end")
        self.threads_spin.insert(0, "2")
        self.threads_spin.pack(side="left", ipady=4)

        self._label(options_row, "间隔(秒)", fg_token="text_dim",
                    font=self.fonts.small).pack(side="left", padx=(16, 8))
        self.gap_spin = self._spinbox(options_row, 0.2, 10.0, 0.5, width=4)
        self.gap_spin.delete(0, "end")
        self.gap_spin.insert(0, "1.0")
        self.gap_spin.pack(side="left", ipady=4)

        self.subfolder_var = tk.BooleanVar(value=True)
        self.subfolder_chk = self._checkbutton(options_row, "含子文件夹", self.subfolder_var)
        self.subfolder_chk.pack(side="left", padx=(16, 0))

        # 解析完自动开始：默认勾上（用户 2026-11-02 要求「别让我再点一次」），
        # 想手动控制的人可以取消勾选。
        self.autostart_var = tk.BooleanVar(value=True)
        self.autostart_chk = self._checkbutton(options_row, "解析后自动开始", self.autostart_var)
        self.autostart_chk.pack(side="left", padx=(16, 0))

        # 第三行：动作按钮 + 快捷键提示
        action_row = self._frame(body, "surface")
        action_row.pack(fill="x", pady=(16, 0))

        self.start_btn = self._register_tint(widgets.RoundedButton(
            action_row, "开始下载", command=self._on_start, kind="primary",
            palette=palette, fonts=self.fonts, height=34, min_width=104))
        self.start_btn.pack(side="left")
        self.pause_btn = self._register_tint(widgets.RoundedButton(
            action_row, "暂停", command=self._on_pause_toggle, kind="ghost",
            palette=palette, fonts=self.fonts, height=34, min_width=80))
        self.pause_btn.pack(side="left", padx=(8, 0))
        self.stop_btn = self._register_tint(widgets.RoundedButton(
            action_row, "停止", command=self._on_stop, kind="ghost",
            palette=palette, fonts=self.fonts, height=34, min_width=80))
        self.stop_btn.pack(side="left", padx=(8, 0))

        for button in (self.start_btn, self.pause_btn, self.stop_btn):
            button.set_enabled(False)
        self.pause_btn.set_text("暂停")

        self.hint_label = self._label(
            action_row,
            "Ctrl+Enter 解析 · Ctrl+V 粘贴 · F5 刷新失败清单 · Ctrl+D 打开文件夹 · 右键任务可定位文件",
            fg_token="text_dim", font=self.fonts.small, anchor="e")
        self.hint_label.pack(side="right", pady=(8, 0))

    # -- 3. 概览条 -------------------------------------------------------
    def _readout_block(self, parent, caption, value, padx):
        """右上角的小读数块：上面一行 9pt 说明，下面一行 11pt 数值。

        返回的是**数值那个 Label**，好让 `_apply_stats` 直接 configure(text=...)。
        """
        box = self._frame(parent, "surface")
        box.pack(side="left", padx=padx)
        self._label(box, caption, fg_token="text_dim", font=self.fonts.tiny,
                    anchor="e").pack(side="top", anchor="e")
        value_label = self._label(box, value, fg_token="text",
                                  font=self.fonts.value, anchor="e")
        value_label.pack(side="top", anchor="e")
        return value_label

    def _build_overview(self):
        palette = self.palette
        card = self._register_tint(widgets.Card(self.root, palette=palette))
        card.pack(side="top", fill="x", padx=16, pady=(0, 12))
        body = card.body
        body.configure(padx=16, pady=16)

        # ---- 第一行：大号百分比 + 细进度条 + 速度/剩余 ----
        top = self._frame(body, "surface")
        top.pack(fill="x")

        self.percent_label = self._label(top, "0%", font=self.fonts.big,
                                         width=4, anchor="w")
        self.percent_label.pack(side="left")

        # 8px 细条，上下各留 16，正好贴着 28pt 数字垂直居中
        self.progress = self._register_tint(widgets.ProgressBar(
            top, palette=palette, fonts=self.fonts, height=8, radius=4,
            show_text=False))
        self.progress.pack(side="left", fill="x", expand=True,
                           padx=(16, 20), pady=16)

        # 右对齐两块读数：速度在左、预计剩余在右
        self.speed_label = self._readout_block(top, "速度", "— KB/s", (0, 20))
        self.eta_label = self._readout_block(top, "预计剩余", "--", (0, 0))

        # ---- 第二行：四个指标卡，中间 1px 竖线 ----
        bottom = self._frame(body, "surface")
        bottom.pack(fill="x", pady=(16, 0))

        metrics = (
            ("pill_done", "完成 0", None),
            ("pill_failed", "失败 0", "danger"),
            ("pill_skipped", "跳过 0", None),
            ("pill_total", "总计 0", None),
        )
        for index, (attr, text, warn_token) in enumerate(metrics):
            if index:
                self._frame(bottom, "border", width=1).pack(
                    side="left", fill="y", padx=20)
            tile = self._register_tint(widgets.MetricTile(
                bottom, text, palette=palette, fonts=self.fonts,
                value_token="text", warn_token=warn_token))
            tile.pack(side="left")
            setattr(self, attr, tile)

    # -- 4. 四页签 -------------------------------------------------------
    def _build_notebook(self):
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(side="top", fill="both", expand=True, padx=16, pady=(0, 12))

        self.queue_tab = self._build_queue_tab()
        self.failure_tab = self._build_failure_tab()
        self.log_tab = self._build_log_tab()
        self.diag_tab = self._build_diag_tab()

        self.notebook.add(self.queue_tab, text="队列")
        self.notebook.add(self.failure_tab, text="失败清单")
        self.notebook.add(self.log_tab, text="日志")
        self.notebook.add(self.diag_tab, text="诊断")
        self._update_failure_badge()
        self._retint_trees()

    def _build_queue_tab(self):
        palette = self.palette
        page = self._frame(self.notebook, "surface")
        wrapper = self._frame(page, "surface")
        wrapper.pack(fill="both", expand=True, padx=16, pady=16)

        columns = ("name", "size", "progress", "speed", "status", "path")
        headings = {"name": "文件名", "size": "大小", "progress": "进度",
                    "speed": "速度", "status": "状态", "path": "保存位置"}
        widths = {"name": 360, "size": 92, "progress": 78, "speed": 100,
                  "status": 82, "path": 300}
        anchors = {"name": "w", "size": "e", "progress": "e", "speed": "e",
                   "status": "center", "path": "w"}

        tree = ttk.Treeview(wrapper, columns=columns, show="headings",
                            selectmode="browse")
        for column in columns:
            tree.heading(column, text=headings[column], anchor="w")
            tree.column(column, width=widths[column], minwidth=60,
                        anchor=anchors[column],
                        stretch=column in ("name", "path"))
        scroll = ttk.Scrollbar(wrapper, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        wrapper.rowconfigure(0, weight=1)
        wrapper.columnconfigure(0, weight=1)
        self.queue_tree = tree

        # 右键菜单 + 双击定位（用户 2026-11-02 要「右键任务列表能在文件夹中显示下载文件」）
        tree.bind("<Button-3>", self._on_queue_menu)
        tree.bind("<Double-1>", self._on_queue_double)

        # 空状态：和表格同一格居中叠着，有任务时 grid_remove()
        self.empty_state = self._frame(wrapper, "surface")
        self.empty_state.grid(row=0, column=0, sticky="")
        self._label(self.empty_state, "还没有任务", bg_token="surface",
                    fg_token="text", font=self.fonts.head,
                    anchor="center").pack(side="top")
        self._label(self.empty_state,
                    "在上面粘一个 MediaFire 分享链接，按 Ctrl+Enter",
                    bg_token="surface", fg_token="text_dim",
                    font=self.fonts.body, anchor="center").pack(side="top", pady=(8, 0))
        self._label(self.empty_state, "支持单文件与整个文件夹",
                    bg_token="surface", fg_token="text_dim",
                    font=self.fonts.small, anchor="center").pack(side="top", pady=(4, 0))
        self.empty_state.lift()
        return page

    def _build_failure_tab(self):
        palette = self.palette
        page = self._frame(self.notebook, "surface")

        toolbar = self._frame(page, "surface")
        toolbar.pack(fill="x", padx=16, pady=(16, 12))
        self.retry_btn = self._register_tint(widgets.RoundedButton(
            toolbar, "重试全部失败", command=self._on_retry_all, kind="primary",
            palette=palette, fonts=self.fonts, height=34, min_width=128))
        self.retry_btn.pack(side="left")
        self.copy_btn = self._register_tint(widgets.RoundedButton(
            toolbar, "复制失败链接", command=self._on_copy_failures, kind="ghost",
            palette=palette, fonts=self.fonts, height=34))
        self.copy_btn.pack(side="left", padx=(8, 0))
        self.export_json_btn = self._register_tint(widgets.RoundedButton(
            toolbar, "导出 JSON", command=self._on_export_failures_json, kind="ghost",
            palette=palette, fonts=self.fonts, height=34))
        self.export_json_btn.pack(side="left", padx=(8, 0))
        self.export_md_btn = self._register_tint(widgets.RoundedButton(
            toolbar, "导出 Markdown", command=self._on_export_failures_markdown,
            kind="ghost", palette=palette, fonts=self.fonts, height=34))
        self.export_md_btn.pack(side="left", padx=(8, 0))
        self.clear_btn = self._register_tint(widgets.RoundedButton(
            toolbar, "清空", command=self._on_clear_failures, kind="subtle",
            palette=palette, fonts=self.fonts, height=34, min_width=72))
        self.clear_btn.pack(side="right")

        wrapper = self._frame(page, "surface")
        wrapper.pack(fill="both", expand=True, padx=16, pady=(0, 16))

        columns = ("name", "kind", "reason", "attempts", "time")
        headings = {"name": "文件名", "kind": "错误类型", "reason": "原因",
                    "attempts": "尝试次数", "time": "时间"}
        widths = {"name": 340, "kind": 120, "reason": 420, "attempts": 90, "time": 110}
        anchors = {"name": "w", "kind": "w", "reason": "w",
                   "attempts": "center", "time": "center"}

        tree = ttk.Treeview(wrapper, columns=columns, show="headings",
                            selectmode="extended")
        for column in columns:
            tree.heading(column, text=headings[column], anchor="w")
            tree.column(column, width=widths[column], minwidth=60,
                        anchor=anchors[column], stretch=column in ("name", "reason"))
        scroll = ttk.Scrollbar(wrapper, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        wrapper.rowconfigure(0, weight=1)
        wrapper.columnconfigure(0, weight=1)
        self.fail_tree = tree
        self.retry_btn.set_enabled(False)
        # 置灰时把原因说清楚（合同要求：悬停给出提示）
        self.retry_btn.bind("<Enter>", self._on_retry_hover, add="+")
        return page

    def _build_log_tab(self):
        palette = self.palette
        page = self._frame(self.notebook, "surface")
        wrapper = self._frame(page, "surface")
        wrapper.pack(fill="both", expand=True, padx=16, pady=16)

        text = tk.Text(wrapper, wrap="none", relief="flat", bd=0,
                       highlightthickness=1,
                       bg=palette["log_bg"], fg=palette["text"],
                       insertbackground=palette["text"],
                       selectbackground=palette["selection"],
                       selectforeground=palette["text"],
                       font=self.fonts.mono, padx=12, pady=10,
                       spacing1=2, spacing2=0, spacing3=2)
        scroll = ttk.Scrollbar(wrapper, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=scroll.set, state="disabled")
        text.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        wrapper.rowconfigure(0, weight=1)
        wrapper.columnconfigure(0, weight=1)
        self.log_text = text
        self._retint_text()
        return page

    def _build_diag_tab(self):
        page = self._frame(self.notebook, "surface")

        toolbar = self._frame(page, "surface")
        toolbar.pack(fill="x", padx=16, pady=(16, 12))
        self.diag_export_btn = self._register_tint(widgets.RoundedButton(
            toolbar, "导出诊断报告", command=self._on_export_diagnostics, kind="primary",
            palette=self.palette, fonts=self.fonts, height=34, min_width=136))
        self.diag_export_btn.pack(side="left")
        self.diag_refresh_btn = self._register_tint(widgets.RoundedButton(
            toolbar, "重新采集", command=self._refresh_diagnostics, kind="ghost",
            palette=self.palette, fonts=self.fonts, height=34))
        self.diag_refresh_btn.pack(side="left", padx=(8, 0))
        self.diag_hint = self._label(
            toolbar, "按系统与错误类型交叉归集，反馈问题时请附上这份报告",
            fg_token="text_dim", font=self.fonts.small, anchor="e")
        self.diag_hint.pack(side="right", pady=(8, 0))

        panes = self._frame(page, "surface")
        panes.pack(fill="both", expand=True, padx=16, pady=(0, 16))
        panes.rowconfigure(0, weight=3)
        panes.rowconfigure(1, weight=2)
        panes.columnconfigure(0, weight=1)

        env_wrap = self._frame(panes, "surface")
        env_wrap.grid(row=0, column=0, sticky="nsew")
        env_tree = ttk.Treeview(env_wrap, columns=("key", "value"),
                                show="headings", selectmode="browse")
        env_tree.heading("key", text="环境项", anchor="w")
        env_tree.heading("value", text="值", anchor="w")
        env_tree.column("key", width=220, minwidth=120, anchor="w", stretch=False)
        env_tree.column("value", width=780, minwidth=200, anchor="w", stretch=True)
        env_scroll = ttk.Scrollbar(env_wrap, orient="vertical", command=env_tree.yview)
        env_tree.configure(yscrollcommand=env_scroll.set)
        env_tree.grid(row=0, column=0, sticky="nsew")
        env_scroll.grid(row=0, column=1, sticky="ns")
        env_wrap.rowconfigure(0, weight=1)
        env_wrap.columnconfigure(0, weight=1)
        self.env_tree = env_tree

        stat_wrap = self._frame(panes, "surface")
        stat_wrap.grid(row=1, column=0, sticky="nsew", pady=(12, 0))
        stat_tree = ttk.Treeview(stat_wrap, columns=("category", "label", "count"),
                                 show="headings", selectmode="browse")
        stat_tree.heading("category", text="分类维度", anchor="w")
        stat_tree.heading("label", text="项目", anchor="w")
        stat_tree.heading("count", text="数量", anchor="e")
        stat_tree.column("category", width=220, minwidth=120, anchor="w", stretch=False)
        stat_tree.column("label", width=680, minwidth=200, anchor="w", stretch=True)
        stat_tree.column("count", width=100, minwidth=60, anchor="e", stretch=False)
        stat_scroll = ttk.Scrollbar(stat_wrap, orient="vertical", command=stat_tree.yview)
        stat_tree.configure(yscrollcommand=stat_scroll.set)
        stat_tree.grid(row=0, column=0, sticky="nsew")
        stat_scroll.grid(row=0, column=1, sticky="ns")
        stat_wrap.rowconfigure(0, weight=1)
        stat_wrap.columnconfigure(0, weight=1)
        self.stat_tree = stat_tree
        return page

    # -- 5. 底部状态栏 ---------------------------------------------------
    def _build_status_bar(self):
        palette = self.palette
        bar = self._frame(self.root, "surface_alt", height=32)
        bar.pack(side="bottom", fill="x")
        bar.pack_propagate(False)

        inner = self._frame(bar, "surface_alt")
        inner.pack(fill="both", expand=True, padx=16)

        self.status_dot = self._register_tint(widgets.StatusDot(
            inner, palette=palette, size=12, state="idle"))
        self.status_dot.pack(side="left", pady=8)

        self.status_label = self._label(inner, "就绪", bg_token="surface_alt",
                                        fg_token="text_dim", font=self.fonts.small)
        self.status_label.pack(side="left", padx=(8, 0))

        self.open_dir_btn = self._register_tint(widgets.RoundedButton(
            inner, "打开下载目录", command=self._open_dir, kind="subtle",
            palette=palette, fonts=self.fonts, height=26, pad_x=10))
        self.open_dir_btn.pack(side="right", pady=4)

        version_text = "v%s · %s" % (self.kernel.VERSION, theme.dpi_note())
        self.version_label = self._label(inner, version_text, bg_token="surface_alt",
                                         fg_token="text_dim", font=self.fonts.small,
                                         anchor="e")
        self.version_label.pack(side="right", padx=(0, 12))

    # ------------------------------------------------------------------
    # 3.2 主题切换
    # ------------------------------------------------------------------
    def _toggle_theme(self):
        target = theme.MODE_LIGHT if theme.mode() == theme.MODE_DARK else theme.MODE_DARK
        theme.apply_style(self.root, target)
        self.palette = theme.current()
        for job in list(self._theme_jobs):
            try:
                job(self.palette)
            except tk.TclError:
                pass
            except Exception:
                pass
        self.root.configure(bg=self.palette["bg"])
        self.theme_btn.set_text(self._theme_button_text())
        self._retint_trees()
        self._retint_text()
        self._log("INFO 已切换到%s主题" % theme.MODE_TEXT.get(theme.mode(), ""))

    def _retint_trees(self):
        palette = self.palette
        for tree in (getattr(self, "queue_tree", None), getattr(self, "fail_tree", None)):
            if tree is None:
                continue
            for status, token in theme.STATUS_TOKEN.items():
                try:
                    tree.tag_configure(status, foreground=palette[token])
                except tk.TclError:
                    pass
            # 斑马纹：只换底色不改字色，行高亮交给选中态
            try:
                tree.tag_configure("zebra", background=palette["row_alt"])
            except tk.TclError:
                pass
        for tree in (getattr(self, "env_tree", None), getattr(self, "stat_tree", None)):
            if tree is None:
                continue
            try:
                tree.tag_configure("warning", foreground=palette["warning"])
            except tk.TclError:
                pass

    def _retint_text(self):
        palette = self.palette
        text = getattr(self, "log_text", None)
        if text is None:
            return
        try:
            text.configure(bg=palette["log_bg"], fg=palette["text"],
                           insertbackground=palette["text"],
                           selectbackground=palette["selection"],
                           selectforeground=palette["text"],
                           highlightbackground=palette["border"])
        except tk.TclError:
            pass
        # INFO 降一档到 text_dim：日志里真正该跳出来的是 OK / WARN / ERROR
        for level, token in (("info", "text_dim"), ("warn", "warning"),
                             ("error", "danger"), ("ok", "success"),
                             ("dim", "text_dim")):
            try:
                text.tag_configure(level, foreground=palette[token])
            except tk.TclError:
                pass

    # ------------------------------------------------------------------
    # 3.3 占位提示
    # ------------------------------------------------------------------
    def _attach_placeholder(self, entry, hint):
        state = {"active": False, "hint": hint}

        def show():
            if state["active"] or entry.get():
                return
            state["active"] = True
            try:
                entry.configure(fg=self.palette["text_dim"])
            except tk.TclError:
                pass
            entry.insert(0, hint)

        def hide():
            if not state["active"]:
                return
            state["active"] = False
            entry.delete(0, "end")
            try:
                entry.configure(fg=self.palette["text"])
            except tk.TclError:
                pass

        entry.bind("<FocusIn>", lambda _event: hide(), add="+")
        entry.bind("<FocusOut>", lambda _event: show(), add="+")

        def theme_job(palette):
            if state["active"]:
                try:
                    entry.configure(fg=palette["text_dim"])
                except tk.TclError:
                    pass

        self._theme_jobs.append(theme_job)
        entry._mfd_placeholder = state
        show()

    def _entry_text(self, entry):
        state = getattr(entry, "_mfd_placeholder", None)
        if state and state["active"]:
            return ""
        return entry.get().strip()

    def _set_entry_text(self, entry, value, replace=True):
        state = getattr(entry, "_mfd_placeholder", None)
        if state and state["active"]:
            state["active"] = False
            entry.delete(0, "end")
            try:
                entry.configure(fg=self.palette["text"])
            except tk.TclError:
                pass
        if replace:
            entry.delete(0, "end")
        entry.insert("end", value)

    # ------------------------------------------------------------------
    # 3.4 快捷键
    # ------------------------------------------------------------------
    def _bind_shortcuts(self):
        # 三层都挂：控件层（Entry/Text 类绑定会先吃掉按键）→ 顶层 → all
        for sequence, handler in (
                ("<Control-Return>", self._shortcut_parse),
                ("<F5>", self._shortcut_refresh_failures),
                ("<Control-d>", self._shortcut_open_dir),
                ("<Control-D>", self._shortcut_open_dir)):
            self.root.bind(sequence, handler)
            self.root.bind_all(sequence, handler)
        # Ctrl+V：Entry/Text 的类绑定带 break，会把 bind_all 挡掉，
        # 所以在实际输入控件上再显式绑一份（实例绑定优先级最高）。
        for widget in (self.link_entry, self.log_text):
            for sequence in ("<Control-v>", "<Control-V>"):
                widget.bind(sequence, self._on_paste, add="+")
        self.root.bind_all("<Control-v>", self._on_paste)
        self.root.bind_all("<Control-V>", self._on_paste)

    def _on_paste(self, _event=None):
        widget = self.root.focus_get()
        # 其它输入框（目录、线程数）走 Tk 默认粘贴，不在这里抢
        if isinstance(widget, (tk.Entry, tk.Spinbox)) and widget is not self.link_entry:
            return None
        try:
            data = self.root.clipboard_get()
        except tk.TclError:
            self._set_status("剪贴板里没有可用文本", "warning")
            return "break"
        data = (data or "").strip()
        if not data:
            return "break"
        self._set_entry_text(self.link_entry, data, replace=True)
        try:
            self.link_entry.focus_set()
            self.link_entry.icursor("end")
        except tk.TclError:
            pass
        self._set_status("已粘贴链接，按 Ctrl+Enter 开始解析", "info")
        return "break"

    def _shortcut_parse(self, _event=None):
        self._on_parse()
        return "break"

    def _shortcut_refresh_failures(self, _event=None):
        self._refresh_failures(force_reload=True)
        return "break"

    def _shortcut_open_dir(self, _event=None):
        self._open_dir()
        return "break"

    # ------------------------------------------------------------------
    # 3.5 事件泵
    # ------------------------------------------------------------------
    def _post(self, kind, payload=None):
        """任何线程都可以调：只往队列里塞，不碰控件。"""
        self._queue.put((kind, payload))

    def _pump(self):
        self._pump_id = None
        drained = 0
        while drained < 400:
            try:
                kind, payload = self._queue.get_nowait()
            except queue.Empty:
                break
            drained += 1
            try:
                self._dispatch(kind, payload)
            except Exception:
                # 界面线程不能因为一条坏事件整个停摆
                try:
                    self._log("ERROR 处理事件 %s 时出错：%s" % (kind, traceback.format_exc(limit=2)))
                except Exception:
                    pass
        if self._background:
            # 后台模式下主循环仍在跑，但不再排新泵
            pass
        try:
            self._pump_id = self.root.after(PUMP_INTERVAL_MS, self._pump)
        except tk.TclError:
            self._pump_id = None

    def _dispatch(self, kind, payload):
        if kind == "kernel":
            self._on_kernel_event(*payload)
        elif kind == "log":
            self._log(payload)
        elif kind == "status":
            self._set_status(str(payload), "info")
        elif kind == "resolve_error":
            self._on_resolve_error(payload)
        elif kind == "tasks_loaded":
            self._on_tasks_loaded(*payload)
        elif kind == "stopped":
            self._on_stopped()
        elif kind == "retry_done":
            self._on_retry_done(payload)
        elif kind == "export_ready":
            self._on_export_ready(*payload)
        elif kind == "diag_ready":
            self._on_diag_ready(*payload)

    def _on_kernel_event(self, event, payload):
        """内核回调（后台线程）→ 一律转主线程。"""
        if event == "log":
            self._log(str(payload))
        elif event == "task":
            self._upsert_task_row(payload)
            self._refresh_pills_from_tasks()
        elif event == "progress":
            if isinstance(payload, dict):
                self._apply_stats(payload)
        elif event == "failure":
            self._on_failure_event(payload)
        elif event == "finished":
            self._on_finished(payload if isinstance(payload, dict) else _empty_stats())
        else:
            self._log("INFO 事件 %s" % event)

    # ------------------------------------------------------------------
    # 3.6 状态栏与日志
    # ------------------------------------------------------------------
    def _set_status(self, text, token="text_dim"):
        try:
            self.status_label.configure(text=text,
                                        fg=self.palette.get(token, self.palette["text_dim"]))
        except tk.TclError:
            pass

    def _set_busy(self, busy, reason=""):
        self._busy = bool(busy)
        if busy:
            self.status_dot.set_state("busy")
            if reason:
                self._set_status(reason, "info")
            self.parse_btn.set_enabled(False)
            self.start_btn.set_enabled(False)
        else:
            self.status_dot.set_state("busy" if self._running else "idle")
            self.parse_btn.set_enabled(True)
            self.start_btn.set_enabled(bool(self._tasks) and not self._running)
            self.pause_btn.set_enabled(self._running)
            self.stop_btn.set_enabled(self._running)

    def _log(self, line):
        text = getattr(self, "log_text", None)
        if text is None:
            return
        raw = str(line)
        level = self._log_level(raw)
        try:
            text.configure(state="normal")
            text.insert("end", raw.rstrip() + "\n", level)
            self._log_lines += 1
            if self._log_lines > LOG_MAX_LINES:
                drop = LOG_MAX_LINES // 3
                text.delete("1.0", "%d.0" % (drop + 1))
                self._log_lines -= drop
            text.see("end")
            text.configure(state="disabled")
        except tk.TclError:
            pass

    @staticmethod
    def _log_level(line):
        head = line.lstrip().upper()
        if head.startswith("ERROR") or head.startswith("错误"):
            return "error"
        if head.startswith("WARN") or head.startswith("警告"):
            return "warn"
        if head.startswith("OK") or head.startswith("成功"):
            return "ok"
        if head.startswith("INFO") or head.startswith("提示"):
            return "info"
        return "dim"

    def _report_kernel_state(self):
        self._log("INFO %s %s 启动，界面已就绪" % (self.kernel.APP_NAME, self.kernel.VERSION))
        self._log("INFO 字体：%s / 等宽 %s" % (self.fonts.family, self.fonts.mono_family))
        for note in self.kernel.notes:
            self._log("WARN %s" % note)
        if self.kernel.missing:
            for item in self.kernel.missing:
                self._log("WARN 内核模块未就绪：%s" % str(item).splitlines()[0])
            self._set_status("部分内核模块尚未就绪，界面已进入降级模式", "warning")
            self.status_dot.set_state("warn")
        else:
            self._set_status("就绪", "text_dim")
            self.status_dot.set_state("online")

    # ------------------------------------------------------------------
    # 3.7 选项读取
    # ------------------------------------------------------------------
    def _read_options(self):
        """从界面读出一次下载配置；不合法就返回 None 并提示。"""
        directory = self._entry_text(self.dir_entry)
        if not directory:
            try:
                directory = self.kernel.default_download_dir()
            except Exception:
                directory = _fallback_download_dir()
        try:
            threads = int(float(self.threads_spin.get() or 2))
        except (TypeError, ValueError):
            threads = 2
        threads = max(1, min(8, threads))
        try:
            gap = float(self.gap_spin.get() or 1.0)
        except (TypeError, ValueError):
            gap = 1.0
        gap = max(0.0, min(10.0, gap))
        return {
            "dir": directory,
            "threads": threads,
            "gap": gap,
            "include": bool(self.subfolder_var.get()),
        }

    # ------------------------------------------------------------------
    # 3.8 解析
    # ------------------------------------------------------------------
    def _on_parse(self):
        if self._busy:
            return
        if self._running:
            self._set_status("正在下载中，请先停止再解析新链接", "warning")
            return
        url = self._entry_text(self.link_entry)
        if not url:
            self._set_status("请先粘贴 MediaFire 链接", "warning")
            try:
                self.link_entry.focus_set()
            except tk.TclError:
                pass
            return
        options = self._read_options()
        self._set_busy(True, "正在解析链接…")
        self._log("INFO 开始解析：%s" % url)
        thread = threading.Thread(target=self._resolve_worker, args=(url, options),
                                  name="mfd-resolve", daemon=True)
        thread.start()

    def _resolve_worker(self, url, options):
        """后台：解析 + 建任务列表。全程只往队列里塞结果。"""
        try:
            items, description = self.kernel.resolve_link(
                url,
                include_subfolders=options["include"],
                on_progress=self._on_resolve_progress,
            )
        except self.kernel.MediaFireError as exc:
            self._post("resolve_error",
                       (getattr(exc, "user_message", str(exc)),
                        getattr(exc, "detail", "")))
            return
        except Exception:
            self._post("resolve_error",
                       ("解析时出现未预期的错误，详情见日志。",
                        traceback.format_exc()))
            return

        try:
            downloader = self._ensure_downloader(options)
            tasks = downloader.load_tasks(items)
        except Exception:
            self._post("resolve_error",
                       ("链接已解析，但建立下载队列时出错。", traceback.format_exc()))
            return

        self._post("tasks_loaded", (list(tasks or []), description or "", url))

    def _on_resolve_progress(self, *args, **kwargs):
        """内核的解析进度回调（后台线程调用）。签名未定，按可变参数兜住。"""
        parts = []
        for item in args:
            parts.append(str(item))
        for key, value in kwargs.items():
            parts.append("%s=%s" % (key, value))
        text = " ".join(parts).strip()
        if text:
            self._post("status", "解析中：%s" % text)

    def _on_resolve_error(self, payload):
        message, detail = payload
        self._set_busy(False)
        self.status_dot.set_state("error")
        self._set_status(message, "danger")
        self._log("ERROR %s" % message)
        if detail:
            for line in str(detail).strip().splitlines()[-6:]:
                self._log("ERROR   %s" % line)
        if self._is_viewable():
            messagebox.showerror("解析失败", message, parent=self.root)

    def _on_tasks_loaded(self, tasks, description, url):
        self._tasks = list(tasks)
        self._items = []
        self._clear_tree(self.queue_tree)
        self._task_rows = {}
        for index, task in enumerate(self._tasks):
            self._insert_task_row(task, index)
        self._restripe_queue()
        self._update_empty_state()
        self._set_busy(False)
        self.start_btn.set_enabled(bool(self._tasks))
        self.pause_btn.set_enabled(False)
        self.stop_btn.set_enabled(False)
        self.status_dot.set_state("online")

        skipped = len([t for t in self._tasks
                       if getattr(t, "status", "") == "skipped"])
        summary = description or ("%d 个任务" % len(self._tasks))
        self._log("OK 解析完成：%s（来自 %s）" % (summary, url))
        if skipped:
            self._log("INFO 其中 %d 个文件历史已下载过，已标记为跳过" % skipped)
        self._set_status("已加入队列：%s" % summary, "success")
        self._apply_stats(self._local_stats())

        # 解析完直接开始，省掉"再点一次"（用户 2026-11-02 明确要求）。
        # 用 after 让界面先把队列行画出来再启动，否则用户会觉得"点了没反应"。
        if self._tasks and self.autostart_var.get():
            self._log("INFO 解析完成，按设置自动开始下载")
            self.root.after(150, self._autostart_fire)

    def _autostart_fire(self):
        """自动开始的延时入口：这一轮里用户可能已经手动点了开始或改了设置。"""
        if not self.autostart_var.get():
            return
        if self._running or self._busy:
            return
        self._on_start()

    # ------------------------------------------------------------------
    # 3.9 队列表格
    # ------------------------------------------------------------------
    @staticmethod
    def _clear_tree(tree):
        try:
            children = tree.get_children("")
            if children:
                tree.delete(*children)
        except tk.TclError:
            pass

    def _restripe_queue(self):
        """按当前行序重排斑马纹 tag，保证删行之后还是一深一浅。"""
        try:
            children = self.queue_tree.get_children("")
            striped = set(self.queue_tree.tag_has("zebra"))
        except tk.TclError:
            return
        for index, iid in enumerate(children):
            want = (index % 2) == 1
            if want == (iid in striped):
                continue
            tags = [t for t in self._queue_tags(iid) if t != "zebra"]
            if want:
                tags.append("zebra")
            try:
                self.queue_tree.item(iid, tags=tuple(tags))
            except tk.TclError:
                pass

    def _queue_tags(self, iid):
        """读某一行的 tag 列表；Tk 版本差异下 item(...,'tags') 可能回字符串。"""
        try:
            raw = self.queue_tree.item(iid, "tags")
        except tk.TclError:
            return []
        if not raw:
            return []
        if isinstance(raw, str):
            return [part for part in raw.replace("(", "").replace(")", "")
                    .replace(",", " ").split() if part]
        return [str(part) for part in raw]

    def _update_empty_state(self):
        """队列空了就把三行引导摆出来，有任务就收掉。"""
        empty = getattr(self, "empty_state", None)
        if empty is None:
            return
        try:
            has_rows = bool(self.queue_tree.get_children(""))
        except tk.TclError:
            return
        try:
            if has_rows:
                empty.grid_remove()
            else:
                empty.grid()
                empty.lift()
        except tk.TclError:
            pass

    def _insert_task_row(self, task, index):
        values = self._task_row_values(task)
        tags = [str(getattr(task, "status", "waiting") or "waiting")]
        try:
            existing = len(self.queue_tree.get_children(""))
        except tk.TclError:
            existing = 0
        if existing % 2:
            tags.append("zebra")
        try:
            iid = self.queue_tree.insert("", "end", values=values, tags=tuple(tags))
        except tk.TclError:
            return None
        self._task_rows[id(task)] = iid
        self._update_empty_state()
        return iid

    def _upsert_task_row(self, task):
        if task is None:
            return
        key = id(task)
        values = self._task_row_values(task)
        tags = [str(getattr(task, "status", "waiting") or "waiting")]
        iid = self._task_rows.get(key)
        try:
            if iid and self.queue_tree.exists(iid):
                try:
                    if iid in set(self.queue_tree.tag_has("zebra")):
                        tags.append("zebra")
                except tk.TclError:
                    pass
                self.queue_tree.item(iid, values=values, tags=tuple(tags))
            else:
                self._insert_task_row(task, len(self._task_rows))
                if task not in self._tasks:
                    self._tasks.append(task)
        except tk.TclError:
            pass

    def _task_row_values(self, task):
        name = getattr(task, "name", "") or "(未命名)"
        size = getattr(task, "size", 0) or 0
        percent = self._percent_of(task)
        speed = getattr(task, "speed", 0) or 0
        status = str(getattr(task, "status", "waiting") or "waiting")
        status_text = self.kernel.STATUS_TEXT.get(status, status)
        error = getattr(task, "error", "") or ""
        if error and status == "failed":
            status_text = "%s：%s" % (status_text, error[:40])
        path = getattr(task, "saved_path", "") or "-"
        return (name, _fmt_size(size), "%d%%" % int(round(percent * 100)),
                _fmt_speed(speed), status_text, path)

    @staticmethod
    def _percent_of(task):
        size = getattr(task, "size", 0) or 0
        downloaded = getattr(task, "downloaded", 0) or 0
        if size > 0:
            try:
                ratio = float(downloaded) / float(size)
            except (TypeError, ValueError, ZeroDivisionError):
                ratio = 0.0
            return max(0.0, min(1.0, ratio))
        try:
            value = float(getattr(task, "percent", 0) or 0)
        except (TypeError, ValueError):
            value = 0.0
        if value > 1.0:
            value = value / 100.0
        return max(0.0, min(1.0, value))

    def _local_stats(self):
        """本地按任务状态重算一遍统计（不依赖引擎是否已启动）。"""
        counts = {"waiting": 0, "running": 0, "done": 0,
                  "skipped": 0, "failed": 0, "canceled": 0}
        for task in self._tasks:
            status = str(getattr(task, "status", "waiting") or "waiting")
            counts[status] = counts.get(status, 0) + 1
        total = len(self._tasks)
        finished = (counts.get("done", 0) + counts.get("skipped", 0)
                    + counts.get("failed", 0) + counts.get("canceled", 0))
        stats = _empty_stats()
        stats["counts"] = counts
        stats["total"] = total
        stats["finished"] = finished
        stats["failed_items"] = counts.get("failed", 0)
        return stats

    def _refresh_pills_from_tasks(self):
        self._apply_stats(self._local_stats())

    # ------------------------------------------------------------------
    # 3.10 统计展示
    # ------------------------------------------------------------------
    def _apply_stats(self, stats):
        if not isinstance(stats, dict):
            return
        self._stats = stats
        counts = stats.get("counts") or {}
        try:
            total = int(stats.get("total") or 0)
            finished = int(stats.get("finished") or 0)
        except (TypeError, ValueError):
            total = finished = 0
        fraction = (float(finished) / float(total)) if total else 0.0
        self.progress.set_value(fraction)
        try:
            self.percent_label.configure(text="%d%%" % int(round(fraction * 100)))
        except tk.TclError:
            pass
        self.pill_done.set_text("完成 %d" % int(counts.get("done") or 0))
        self.pill_failed.set_text("失败 %d" % int(counts.get("failed") or 0))
        self.pill_skipped.set_text("跳过 %d" % int(counts.get("skipped") or 0))
        self.pill_total.set_text("总计 %d" % total)
        try:
            speed = float(stats.get("speed") or 0.0)
        except (TypeError, ValueError):
            speed = 0.0
        try:
            self.speed_label.configure(text=_fmt_speed(speed))
            self.eta_label.configure(text=self._eta_text(speed))
        except tk.TclError:
            pass
        # 暂停态以引擎为准（用户也可能在别处触发暂停），按钮文案跟着走
        if self._running:
            paused = bool(stats.get("paused"))
            if paused != self._paused:
                self._paused = paused
                self.pause_btn.set_text("继续" if paused else "暂停")
                self.status_dot.set_state("warn" if paused else "busy")
        self._update_failure_badge()
        self._update_empty_state()

    def _eta_text(self, speed):
        # 只回数值本身，前缀由旁边的「预计剩余」小标签负责，避免重复两个字
        if speed <= 0.05:
            return "--"
        remaining = 0
        for task in self._tasks:
            status = str(getattr(task, "status", "") or "")
            if status in ("done", "skipped", "failed", "canceled"):
                continue
            size = getattr(task, "size", 0) or 0
            done = getattr(task, "downloaded", 0) or 0
            remaining += max(0, int(size) - int(done))
        if remaining <= 0:
            return "--"
        return _fmt_seconds(remaining / (speed * 1024.0))

    # ------------------------------------------------------------------
    # 3.11 下载控制
    # ------------------------------------------------------------------
    def _ensure_downloader(self, options):
        """懒建下载引擎（可能在工作线程里调用，只做数据操作）。"""
        with self._lock:
            if self.downloader is not None:
                return self.downloader
            history = None
            if self.kernel.History is not None:
                try:
                    history = self.kernel.History()
                except Exception:
                    history = None
            self.history = history
            failures = self._ensure_failures()
            downloader = self.kernel.Downloader(
                download_dir=options["dir"],
                history=history,
                max_threads=options["threads"],
                gap_seconds=options["gap"],
                on_event=self._on_kernel_event,
                max_retries=3,
                failures=failures,
            )
            self.downloader = downloader
            return downloader

    def _on_start(self):
        if self._busy:
            self._set_status("正在解析链接，稍等一下再点开始", "warning")
            return
        if not self._tasks:
            self._set_status("队列是空的，先解析一个链接", "warning")
            return
        if self.downloader is None:
            self._set_status("下载引擎还没有建立，请重新解析链接", "warning")
            return
        # 保存位置可能已经被用户改过，而引擎记的还是解析那一刻的目录。
        # 不同步的话，用户看到的是「我换了地方，它偏说没有写入权限」。
        try:
            self.downloader.set_download_dir(self._entry_text(self.dir_entry))
        except Exception:
            self._log("WARN 切换保存位置时出错：%s" % traceback.format_exc(limit=1))
        try:
            started = bool(self.downloader.start())
        except Exception:
            detail = traceback.format_exc(limit=3)
            self._log("ERROR 启动下载失败：%s" % detail)
            self._set_status("启动下载失败，详情见日志", "danger")
            self._alert_start_failed("启动下载时出现意外错误。", detail)
            return
        # 引擎可能因为原目录写不进去，自动换到了别的目录。输入框要跟着改，
        # 否则用户看到的是"我填的路径"，文件却落在别处，又是一次迷惑。
        try:
            actual_dir = str(getattr(self.downloader, "download_dir", "") or "")
        except Exception:
            actual_dir = ""
        if started and actual_dir and actual_dir != self._entry_text(self.dir_entry):
            try:
                self.dir_entry.delete(0, "end")
                self.dir_entry.insert(0, actual_dir)
            except Exception:
                pass
            self._set_status("原目录写不进去，已自动改存到 %s" % actual_dir, "warning")
        if not started:
            # 启动失败可能有很多原因（目录不可写最常见），引擎会把真正的
            # 那句原因放在 last_error 里，直接显示比"没有可下载的任务"有用
            last_error = str(getattr(self.downloader, "last_error", "") or "")
            if last_error:
                self._set_status(last_error.splitlines()[0], "danger")
                self._alert_start_failed(last_error.splitlines()[0], last_error)
                return
            running = False
            try:
                running = bool(getattr(self.downloader, "is_running", False))
            except Exception:
                running = False
            if running:
                self._set_status("下载已经在进行中", "warning")
            else:
                self._set_status("没有可下载的任务（可能全部已完成或已跳过）", "warning")
                self._alert_start_failed(
                    "没有可下载的任务。",
                    "队列里的文件要么已经下完、要么被记为已存在。\n"
                    "如果是失败的，去「失败清单」点「重试全部失败」。")
            return
        self._running = True
        self._paused = False
        self._set_busy(False)
        self.stop_btn.set_enabled(True)
        self.pause_btn.set_enabled(True)
        self.pause_btn.set_text("暂停")
        self.start_btn.set_enabled(False)
        self.status_dot.set_state("busy")
        self._set_status("下载中…", "info")
        self._log("INFO 开始下载：%d 个任务，%d 线程，间隔 %.1f 秒"
                  % (len(self._tasks), int(self.threads_spin.get() or 2),
                     float(self.gap_spin.get() or 1.0)))
        self._schedule_tick()

    def _alert_start_failed(self, title_text, detail):
        """启动失败必须弹出来。

        2.0.2 的教训：只往状态栏写一行红字，用户盯着进度条根本不会看，
        事后总结就是"点了没反应"。所以这里强制弹窗，并且把下一步该做什么
        一并写清楚——只说"失败"不说"怎么办"等于没说。
        """
        message = str(detail or title_text or "").strip()
        hint = ""
        if any(word in message for word in ("无法写入", "写入权限", "只读", "权限")):
            hint = ("\n\n怎么处理：\n"
                    "1. 点「浏览」换一个文件夹——桌面、或用户目录下任意位置都可以\n"
                    "2. 也可以换成 D:\\MediaFireDownloads 这种刚建的空目录\n"
                    "3. 换好之后直接点「开始下载」，不用重新解析链接")
        if not self._is_viewable():
            return
        try:
            messagebox.showerror("无法开始下载", message + hint, parent=self.root)
        except tk.TclError:
            pass

    def _on_pause_toggle(self):
        if self.downloader is None or not self._running:
            return
        try:
            if self._paused:
                self.downloader.resume()
                self._paused = False
                self.pause_btn.set_text("暂停")
                self.status_dot.set_state("busy")
                self._set_status("已继续下载", "info")
                self._log("INFO 已继续下载")
            else:
                self.downloader.pause()
                self._paused = True
                self.pause_btn.set_text("继续")
                self.status_dot.set_state("warn")
                self._set_status("已暂停", "warning")
                self._log("INFO 已暂停")
        except Exception:
            self._log("ERROR 暂停/继续失败：%s" % traceback.format_exc(limit=2))

    def _on_stop(self):
        if self.downloader is None:
            return
        self._set_busy(True, "正在停止…")
        threading.Thread(target=self._stop_worker, name="mfd-stop", daemon=True).start()

    def _stop_worker(self):
        try:
            self.downloader.stop()
            self.downloader.stop_and_wait(5.0)
        except Exception:
            self._post("log", "WARN 停止时出错：%s" % traceback.format_exc(limit=2))
        self._post("stopped", None)

    def _on_stopped(self):
        self._running = False
        self._paused = False
        self._set_busy(False)
        self.pause_btn.set_text("暂停")
        self.pause_btn.set_enabled(False)
        self.stop_btn.set_enabled(False)
        self.start_btn.set_enabled(bool(self._tasks))
        self.status_dot.set_state("online")
        self._set_status("已停止", "text_dim")
        self._log("INFO 已停止下载")
        self._apply_stats(self._local_stats())

    def _schedule_tick(self):
        if self._tick_id is not None:
            return
        self._tick_id = self.root.after(TICK_INTERVAL_MS, self._tick)

    def _tick(self):
        self._tick_id = None
        if not self._running or self.downloader is None:
            return
        try:
            stats = self.downloader.stats()
        except Exception:
            stats = None
        if isinstance(stats, dict):
            self._apply_stats(stats)
        try:
            self._tick_id = self.root.after(TICK_INTERVAL_MS, self._tick)
        except tk.TclError:
            self._tick_id = None

    def _on_finished(self, stats):
        self._running = False
        self._paused = False
        self._apply_stats(stats)
        self._set_busy(False)
        self.pause_btn.set_enabled(False)
        self.pause_btn.set_text("暂停")
        self.stop_btn.set_enabled(False)
        self.start_btn.set_enabled(bool(self._tasks))
        self.status_dot.set_state("online")

        counts = (stats or {}).get("counts") or {}
        done = int(counts.get("done", 0) or 0)
        failed = int(counts.get("failed", 0) or 0)
        skipped = int(counts.get("skipped", 0) or 0)
        total = int((stats or {}).get("total", 0) or 0)
        summary = "完成 %s · 失败 %s · 跳过 %s · 总计 %s" % (done, failed, skipped, total)
        self._log("OK 下载结束：%s" % summary)
        self._set_status("下载结束：%s" % summary, "success" if not failed else "warning")
        self._refresh_failures()

        if self._background:
            self._background = False
            try:
                self.root.deiconify()
                self.root.lift()
            except tk.TclError:
                pass
        if not self._is_viewable():
            return
        try:
            if failed:
                messagebox.showwarning(
                    "下载结束，有失败项",
                    summary + "\n\n去「失败清单」页签看看原因。"
                              "修好之后点「重试全部失败」，只会补失败的那几个。",
                    parent=self.root)
            elif done == 0 and skipped == 0:
                messagebox.showinfo("已停止", summary + "\n\n本轮没有任何文件完成。", parent=self.root)
            else:
                messagebox.showinfo("下载结束", summary, parent=self.root)
        except tk.TclError:
            pass

    # ------------------------------------------------------------------
    # 3.12 失败清单
    # ------------------------------------------------------------------
    def _ensure_failures(self):
        if self.failures is not None:
            return self.failures
        try:
            self.failures = self.kernel.FailureBook()
        except Exception:
            self.failures = FallbackFailureBook()
        return self.failures

    def _failure_kind_text(self, record):
        """错误类型的可读文案。

        内核的真身用 ``kind_label``（属性），契约文字里写的是 ``kind_text``；
        两个都试，最后退回 ``KIND_TEXT`` 映射，谁在场都不会空白。
        """
        for attr in ("kind_label", "kind_text"):
            try:
                value = getattr(record, attr, "")
            except Exception:
                value = ""
            if value:
                return str(value)
        code = str(getattr(record, "kind", "") or "")
        if code:
            return self.kernel.KIND_TEXT.get(code, code)
        return "未知"

    def _failure_values(self, record):
        name = getattr(record, "name", "") or "(未知文件)"
        kind = self._failure_kind_text(record)
        message = getattr(record, "message", "") or ""
        if len(message) > 90:
            message = message[:90] + "…"
        attempts = getattr(record, "attempts", 0) or 0
        when = _fmt_timestamp(getattr(record, "created_at", ""))
        return (name, kind, message, str(attempts), when)

    def _insert_failure_row(self, record, index="end"):
        try:
            self.fail_tree.insert("", index, values=self._failure_values(record),
                                  tags=("failed",))
        except tk.TclError:
            pass

    def _on_failure_event(self, record):
        if record is None:
            return
        self._insert_failure_row(record, index=0)
        self._remember_failure(record)
        self._update_failure_badge()
        has_any = bool(self._failure_records())
        self.retry_btn.set_enabled(has_any)
        self.retry_btn.set_tooltip("" if has_any else "当前没有失败项")
        name = getattr(record, "name", "") or "(未知文件)"
        self._log("ERROR 下载失败：%s（%s）" % (name, self._failure_kind_text(record)))
        if self._is_viewable():
            self.status_dot.set_state("warn")
            self._set_status("有文件下载失败，已记入失败清单", "warning")

    def _remember_failure(self, record):
        """把失败项补进失败清单（引擎可能已经记过，这里按身份与键位去重）。"""
        book = self._ensure_failures()
        try:
            existing = list(getattr(book, "records", []) or [])
        except Exception:
            existing = []
        if record in existing:
            return
        url = getattr(record, "url", "")
        stamp = getattr(record, "created_at", "")
        for old in existing:
            if (getattr(old, "url", "") == url
                    and getattr(old, "created_at", "") == stamp):
                return
        try:
            book.add(record)
        except Exception:
            pass

    def _failure_limit(self):
        return 999

    def _failure_records(self):
        book = self._ensure_failures()
        try:
            records = list(getattr(book, "records", []) or [])
        except Exception:
            records = []
        return records

    def _refresh_failures(self, force_reload=False):
        if force_reload:
            book = self._ensure_failures()
            try:
                book.load()
            except Exception:
                pass
        records = self._failure_records()
        self._clear_tree(self.fail_tree)
        for record in records:
            self._insert_failure_row(record)
        self._update_failure_badge()
        can_retry = bool(records)
        self.retry_btn.set_enabled(can_retry)
        self.retry_btn.set_tooltip("" if can_retry else "当前没有失败项")
        if force_reload:
            self._set_status("已刷新失败清单：%d 条" % len(records),
                             "info" if records else "text_dim")
            self._log("INFO 失败清单已刷新：%d 条" % len(records))

    def _update_failure_badge(self):
        records = self._failure_records()
        count = len(records)
        label = "失败清单" if not count else "失败清单 (%d)" % count
        try:
            self.notebook.tab(1, text=label)
        except tk.TclError:
            pass

    def _on_retry_hover(self, _event=None):
        if self.retry_btn.is_enabled():
            self._set_status("把失败清单里的链接全部重新排队", "text_dim")
            return
        self._set_status("当前没有失败项可重试", "warning")

    def _on_retry_all(self):
        records = self._failure_records()
        if not records:
            self._set_status("当前没有失败项可重试", "warning")
            return
        book = self._ensure_failures()
        try:
            items = list(book.to_items())
        except Exception:
            items = []
        if not items:
            items = [{"name": getattr(r, "name", ""), "url": getattr(r, "url", ""),
                      "size": getattr(r, "size", 0), "quickkey": getattr(r, "quickkey", ""),
                      "folder": getattr(r, "folder", ""),
                      "hash": getattr(r, "hash_value", "")}
                     for r in records]
        options = self._read_options()
        self._set_busy(True, "正在重新排队失败项…")
        self._log("INFO 开始重试 %d 个失败项" % len(items))
        threading.Thread(target=self._retry_worker, args=(options, items),
                         name="mfd-retry", daemon=True).start()

    def _retry_worker(self, options, items):
        try:
            downloader = self._ensure_downloader(options)
            queued = 0
            if self._tasks:
                # 真身的 retry_failed() 内部会自己调 start()，这里不能重复起动
                queued = int(downloader.retry_failed() or 0)
            if queued <= 0:
                tasks = downloader.load_tasks(items)
                queued = len(tasks or [])
                self._post("tasks_loaded", (list(tasks or []),
                                            "%d 个失败项" % queued, "失败清单"))
                started = bool(downloader.start())
            else:
                started = True
            self._post("retry_done", (queued, started))
        except Exception:
            self._post("resolve_error", ("重试失败项时出错。", traceback.format_exc()))

    def _on_retry_done(self, payload):
        queued, started = payload
        self._set_busy(False)
        if started:
            self._running = True
            self._paused = False
            self.start_btn.set_enabled(False)
            self.pause_btn.set_enabled(True)
            self.stop_btn.set_enabled(True)
            self.pause_btn.set_text("暂停")
            self.status_dot.set_state("busy")
            self._set_status("已重新排队 %d 个失败任务并开始下载" % int(queued), "info")
            self._log("OK 已重新排队 %d 个失败任务" % int(queued))
            self._schedule_tick()
        else:
            self._set_status("重新排队 %d 个任务，点「开始下载」继续" % int(queued), "warning")
            self._log("WARN 已重新排队 %d 个任务，但下载没能自动启动" % int(queued))

    def _on_copy_failures(self):
        book = self._ensure_failures()
        text = ""
        try:
            text = book.export_links_txt() or ""
        except Exception:
            text = ""
        if not text.strip():
            text = "\n".join(getattr(r, "url", "") for r in self._failure_records())
        if not text.strip():
            self._set_status("没有失败链接可复制", "warning")
            return
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
        except tk.TclError:
            self._set_status("写剪贴板失败", "danger")
            return
        count = len([line for line in text.splitlines() if line.strip()])
        self._set_status("已复制 %d 条失败链接到剪贴板" % count, "success")
        self._log("INFO 已复制 %d 条失败链接" % count)

    def _ask_save_path(self, default_name, file_types):
        try:
            return filedialog.asksaveasfilename(
                parent=self.root, title="导出失败清单",
                initialfile=default_name, defaultextension=os.path.splitext(default_name)[1],
                filetypes=file_types,
            )
        except tk.TclError:
            return ""

    def _export_failures(self, kind):
        book = self._ensure_failures()
        try:
            if kind == "json":
                content = book.export_json() or ""
                default_name = "mediafire_failures.json"
                file_types = [("JSON 文件", "*.json"), ("全部文件", "*.*")]
            else:
                content = book.export_markdown() or ""
                default_name = "mediafire_failures.md"
                file_types = [("Markdown 文件", "*.md"), ("全部文件", "*.*")]
        except Exception:
            self._post("resolve_error", ("导出失败清单时出错。", traceback.format_exc()))
            return
        if not str(content).strip():
            self._set_status("失败清单是空的，没什么可导出", "warning")
            return
        path = self._ask_save_path(default_name, file_types)
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(content)
        except OSError as exc:
            self._set_status("写入文件失败：%s" % exc, "danger")
            self._log("ERROR 写入 %s 失败：%s" % (path, exc))
            return
        self._set_status("已导出到 %s" % path, "success")
        self._log("OK 已导出失败清单：%s" % path)

    def _on_export_failures_json(self):
        self._export_failures("json")

    def _on_export_failures_markdown(self):
        self._export_failures("markdown")

    def _on_clear_failures(self):
        if not self._failure_records():
            self._set_status("失败清单已经是空的", "text_dim")
            return
        if self._is_viewable():
            confirmed = messagebox.askyesno("清空失败清单",
                                            "确定清空全部失败记录吗？此操作不可撤销。",
                                            parent=self.root)
            if not confirmed:
                return
        book = self._ensure_failures()
        try:
            book.clear()
            book.save()
        except Exception:
            self._log("ERROR 清空失败清单时出错：%s" % traceback.format_exc(limit=2))
        self._refresh_failures()
        self._log("INFO 失败清单已清空")
        self._set_status("失败清单已清空", "text_dim")

    # ------------------------------------------------------------------
    # 3.13 诊断页
    # ------------------------------------------------------------------
    def _ensure_diagnostics(self):
        if self.diagnostics is not None:
            return self.diagnostics
        try:
            self.diagnostics = self.kernel.Diagnostics()
        except Exception:
            self.diagnostics = FallbackDiagnostics()
        return self.diagnostics

    def _refresh_diagnostics(self):
        diag = self._ensure_diagnostics()
        environment = {}
        try:
            environment = diag.collect_environment() or {}
        except Exception:
            environment = {"采集失败": traceback.format_exc(limit=2)}
        if not isinstance(environment, dict):
            environment = {"返回值": str(environment)}

        self._clear_tree(self.env_tree)
        for key, value in environment.items():
            try:
                self.env_tree.insert("", "end", values=(str(key), str(value)))
            except tk.TclError:
                pass
        # 界面自身的两条，任何环境采集里都不该缺
        for key, value in (("界面层字体", "%s / 等宽 %s" % (self.fonts.family, self.fonts.mono_family)),
                           ("界面层 DPI", theme.dpi_note()),
                           ("界面层主题", theme.MODE_TEXT.get(theme.mode(), ""))):
            try:
                self.env_tree.insert("", "end", values=(key, value))
            except tk.TclError:
                pass

        self._clear_tree(self.stat_tree)
        book = self._ensure_failures()
        rows = []
        try:
            for kind, records in (book.by_kind() or {}).items():
                label = self.kernel.KIND_TEXT.get(kind, kind)
                rows.append(("错误类型", "%s（%s）" % (label, kind), len(records)))
        except Exception:
            pass
        try:
            for platform, records in (book.by_platform() or {}).items():
                rows.append(("系统/平台", platform, len(records)))
        except Exception:
            pass
        if not rows:
            rows.append(("失败统计", "暂时没有失败记录", 0))
        for row in rows:
            try:
                self.stat_tree.insert("", "end", values=(row[0], row[1], str(row[2])))
            except tk.TclError:
                pass

    def _on_export_diagnostics(self):
        diag = self._ensure_diagnostics()
        book = self._ensure_failures()
        self._set_busy(True, "正在生成诊断报告…")
        threading.Thread(target=self._diag_worker, args=(diag, book),
                         name="mfd-diag", daemon=True).start()

    def _diag_worker(self, diag, book):
        try:
            path = diag.save_report(failures=book)
            self._post("diag_ready", (path or "", ""))
        except Exception:
            self._post("diag_ready", ("", traceback.format_exc()))

    def _on_diag_ready(self, path, error):
        self._set_busy(False)
        if error:
            self._set_status("生成诊断报告失败，详情见日志", "danger")
            self._log("ERROR 生成诊断报告失败：%s" % error.strip().splitlines()[-1])
            return
        if not path:
            self._set_status("诊断报告没能落盘", "warning")
            self._log("WARN 诊断报告返回空路径")
            return
        self._set_status("诊断报告已生成：%s" % path, "success")
        self._log("OK 诊断报告已生成：%s" % path)
        if self._is_viewable():
            messagebox.showinfo("诊断报告", "已生成：\n%s" % path, parent=self.root)

    def _on_export_ready(self, path, error):
        # 预留：将来导出若改到后台线程，结果从这里回来
        if error:
            self._log("ERROR %s" % error)
        elif path:
            self._log("OK %s" % path)

    # ------------------------------------------------------------------
    # 3.14 目录与关闭
    # ------------------------------------------------------------------
    def _choose_dir(self):
        current = self._entry_text(self.dir_entry) or ""
        try:
            chosen = filedialog.askdirectory(parent=self.root, title="选择下载目录",
                                             initialdir=current or None)
        except tk.TclError:
            return
        if chosen:
            self._set_entry_text(self.dir_entry, chosen, replace=True)
            self._set_status("下载目录已改为 %s" % chosen, "info")

    def _open_dir(self):
        directory = self._entry_text(self.dir_entry)
        if not directory:
            try:
                directory = self.kernel.default_download_dir()
            except Exception:
                directory = ""
        if not directory:
            self._set_status("下载目录还没有设置", "warning")
            return
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError as exc:
            self._set_status("目标目录不可用：%s" % exc, "danger")
            return
        try:
            if hasattr(os, "startfile"):
                os.startfile(directory)  # Windows
            else:
                self._set_status("当前系统不支持直接打开目录", "warning")
        except OSError as exc:
            self._set_status("打开目录失败：%s" % exc, "danger")
            return
        self._set_status("已打开 %s" % directory, "success")

    # -- 队列右键菜单与文件定位 ------------------------------------------
    def _task_for_iid(self, iid):
        if not iid:
            return None
        for task in self._tasks:
            if self._task_rows.get(id(task)) == iid:
                return task
        return None

    def _on_queue_menu(self, event):
        try:
            iid = self.queue_tree.identify_row(event.y)
            if iid:
                self.queue_tree.selection_set(iid)
        except tk.TclError:
            iid = ""
        self._popup_queue_menu(event, iid)

    def _on_queue_double(self, event):
        try:
            iid = self.queue_tree.identify_row(event.y)
        except tk.TclError:
            return
        task = self._task_for_iid(iid)
        if task is not None:
            self._reveal_task(task)

    def _popup_queue_menu(self, event, iid):
        task = self._task_for_iid(iid)
        menu = tk.Menu(self.root, tearoff=0)
        if task is not None:
            menu.add_command(label="在文件夹中显示",
                             command=lambda: self._reveal_task(task))
            menu.add_command(
                label="复制文件名",
                command=lambda: self._copy_to_clipboard(getattr(task, "name", "")))
            if str(getattr(task, "status", "")) == "failed":
                menu.add_separator()
                menu.add_command(label="重试这个任务",
                                 command=lambda: self._retry_single(task))
            menu.add_separator()
        menu.add_command(label="打开下载文件夹", command=self._open_dir)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            try:
                menu.grab_release()
            except tk.TclError:
                pass

    def _task_directory(self, task):
        try:
            root_dir = getattr(self.downloader, "download_dir", "") or ""
        except Exception:
            root_dir = ""
        if not root_dir:
            root_dir = self._entry_text(self.dir_entry)
        folder = getattr(task, "folder", "") or ""
        if folder and root_dir:
            return os.path.join(root_dir, folder)
        return root_dir or ""

    def _reveal_task(self, task):
        path = getattr(task, "saved_path", "") or ""
        if path and os.path.exists(path):
            self._reveal_in_explorer(path)
            return
        directory = self._task_directory(task)
        if directory and os.path.isdir(directory):
            try:
                os.startfile(directory)
            except OSError as exc:
                self._set_status("打开文件夹失败：%s" % exc, "danger")
                return
            self._set_status("文件还没下完，已打开它要去的文件夹", "info")
            return
        self._set_status("这个任务还没有落盘位置：等它下完再来", "warning")

    def _reveal_in_explorer(self, path):
        if os.name != "nt":
            self._set_status("只有 Windows 支持在资源管理器中定位", "warning")
            return
        try:
            subprocess.Popen('explorer /select,"%s"' % os.path.normpath(path))
        except OSError as exc:
            self._set_status("定位失败：%s" % exc, "danger")
            return
        self._set_status("已在资源管理器中定位：%s" % os.path.basename(path), "success")

    def _copy_to_clipboard(self, text):
        if not text:
            return
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
        except tk.TclError:
            return
        self._set_status("已复制：%s" % text[:60], "info")

    def _retry_single(self, task):
        try:
            self.downloader.retry_task(task)
        except Exception as exc:
            self._set_status("重试失败：%s" % exc, "danger")
            return
        self._set_status("已重新排队：%s" % (getattr(task, "name", "") or ""), "info")

    def _is_viewable(self):
        try:
            return bool(self.root.winfo_exists()) and bool(self.root.winfo_viewable())
        except tk.TclError:
            return False

    def _on_close(self):
        if self._running:
            choice = self._ask_close_choice()
            if choice == "cancel":
                return
            if choice == "background":
                self._background = True
                try:
                    self.root.withdraw()
                except tk.TclError:
                    pass
                self._log("INFO 窗口已转入后台，下载结束后会自动重新出现")
                return
            if choice == "stop":
                self._set_status("正在停止下载…", "warning")
                try:
                    if self.downloader is not None:
                        self.downloader.stop()
                        self.downloader.stop_and_wait(5.0)
                except Exception:
                    self._log("WARN 停止时出错：%s" % traceback.format_exc(limit=2))
                self._shutdown()
                return
        self._shutdown()

    def _ask_close_choice(self):
        """三选一对话框：继续后台 / 停止并退出 / 取消。返回 'background' | 'stop' | 'cancel'。"""
        palette = self.palette
        result = {"value": "cancel"}
        try:
            dialog = tk.Toplevel(self.root)
        except tk.TclError:
            return self._ask_close_fallback()
        dialog.withdraw()
        dialog.title("还在下载")
        dialog.configure(bg=palette["surface"])
        dialog.resizable(False, False)
        try:
            dialog.transient(self.root)
        except tk.TclError:
            pass

        body = self._frame(dialog, "surface")
        body.configure(padx=22, pady=18)
        body.pack(fill="both", expand=True)

        title = self._label(body, "还有任务正在下载", bg_token="surface",
                            font=self.fonts.head, anchor="w")
        title.pack(fill="x")
        hint = self._label(body,
                           "关掉窗口后下载可以继续在后台跑；也可以先停下再退出。",
                           bg_token="surface", fg_token="text_dim",
                           font=self.fonts.small, anchor="w")
        hint.pack(fill="x", pady=(6, 14))

        row = self._frame(body, "surface")
        row.pack(fill="x")

        def finish(value):
            result["value"] = value
            try:
                dialog.grab_release()
            except tk.TclError:
                pass
            try:
                dialog.destroy()
            except tk.TclError:
                pass

        background_btn = widgets.RoundedButton(
            row, "继续后台", command=lambda: finish("background"), kind="primary",
            palette=palette, fonts=self.fonts, height=32)
        background_btn.pack(side="left")
        stop_btn = widgets.RoundedButton(
            row, "停止并退出", command=lambda: finish("stop"), kind="danger",
            palette=palette, fonts=self.fonts, height=32)
        stop_btn.pack(side="left", padx=(8, 0))
        cancel_btn = widgets.RoundedButton(
            row, "取消", command=lambda: finish("cancel"), kind="ghost",
            palette=palette, fonts=self.fonts, height=32)
        cancel_btn.pack(side="left", padx=(8, 0))

        try:
            dialog.protocol("WM_DELETE_WINDOW", lambda: finish("cancel"))
            dialog.update_idletasks()
            width = dialog.winfo_reqwidth()
            height = dialog.winfo_reqheight()
            root_x = self.root.winfo_rootx()
            root_y = self.root.winfo_rooty()
            root_w = self.root.winfo_width()
            root_h = self.root.winfo_height()
            dialog.geometry("%dx%d+%d+%d" % (
                width, height,
                max(0, root_x + (root_w - width) // 2),
                max(0, root_y + (root_h - height) // 3),
            ))
            dialog.deiconify()
            dialog.grab_set()
            dialog.focus_set()
            self.root.wait_window(dialog)
        except tk.TclError:
            return self._ask_close_fallback()
        return result["value"]

    def _ask_close_fallback(self):
        """Toplevel 建不出来时的退路，保证关闭逻辑永远能走完。"""
        try:
            answer = messagebox.askyesnocancel(
                "还在下载",
                "还有任务正在下载。\n\n是：继续在后台下载\n否：停止并退出\n取消：返回",
                parent=self.root)
        except tk.TclError:
            return "stop"
        if answer is None:
            return "cancel"
        return "background" if answer else "stop"

    def _shutdown(self):
        for attr in ("_pump_id", "_tick_id"):
            after_id = getattr(self, attr, None)
            if after_id is not None:
                try:
                    self.root.after_cancel(after_id)
                except Exception:
                    pass
                setattr(self, attr, None)
        try:
            if self.failures is not None:
                self.failures.save()
        except Exception:
            pass
        try:
            self.root.destroy()
        except tk.TclError:
            pass
