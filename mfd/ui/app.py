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

import json
import os
import queue
import re
import subprocess
import threading
import time
import traceback
import webbrowser
import zipfile
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
# 二·五、界面自用的小工具（版本号比较、链接切分、设置读写适配层）
# ----------------------------------------------------------------------

#: 「全部下载完成后做什么」下拉框的取值（默认第一项，绝不默认关机）
FINISH_ACTIONS = ("不做事", "打开文件夹", "关机")
#: 队列状态筛选下拉框的取值（第一项表示不筛）
STATUS_FILTERS = ("全部", "排队中", "下载中", "已完成", "失败", "已跳过")
#: 中文筛选项 → 引擎里的状态码
STATUS_FILTER_CODE = {
    "排队中": "waiting",
    "下载中": "running",
    "已完成": "done",
    "失败": "failed",
    "已跳过": "skipped",
}
DIR_HISTORY_LIMIT = 8        # 保存位置历史最多记 8 条
SEARCH_DEBOUNCE_MS = 150     # 搜索框防抖（契约：150ms）
OPTIONS_DEBOUNCE_MS = 400    # 并发/间隔/限速改动后的应用延迟
DEFAULT_THREADS = 2          # 并发数的兜底默认值
DEFAULT_GAP = 1.0            # 间隔秒数的兜底默认值

_GEOMETRY_RE = re.compile(r"^\d{2,5}x\d{2,5}([+-]{1,2}\d{1,5}[+-]{1,2}\d{1,5})?$")

_BUNDLE_README = """MediaFire 批量下载器 —— 诊断包说明
========================================

本压缩包用于反馈问题，里面有四份文件：

1. diagnostics.json
   《诊断》页签里那份报告的结构化版本：系统版本、Python/Tk 版本、
   DPI 与字体、代理设置、失败按类型与平台的交叉统计，以及程序自己
   记下的事件流水。排查「为什么下不动」主要看这份。

2. failures.json
   《失败清单》页签的原始记录：每个失败文件的名字、链接、错误类型、
   错误原因、尝试次数、发生时间。已经成功的文件不会出现在这里。

3. app.log
   本次运行界面日志区的全文（和你在《日志》页签里看到的一样），
   按时间顺序，INFO / WARN / ERROR 混排。

4. README.txt
   就是你正在读的这一份。

隐私提醒
--------
diagnostics.json 与 app.log 里可能包含你的下载目录路径、系统用户名、
内网代理地址等本机信息。对外发送之前请自己过一眼，觉得敏感的段落
可以直接删掉，不影响其余内容被看懂。
"""


def _version_tuple(text):
    """把 ``2.1.3`` 这类版本号切成可比较的整数元组。"""
    parts = []
    for chunk in re.split(r"[^0-9]+", str(text or "")):
        if chunk:
            try:
                parts.append(int(chunk))
            except ValueError:
                continue
    return tuple(parts)


def _version_newer(candidate, current):
    """候选版本号是否比当前版本新（拿不到数字就当没有新版）。"""
    new = _version_tuple(candidate)
    old = _version_tuple(current)
    if not new:
        return False
    return new > old


def _parse_update_result(result, current):
    """把 ``check_latest_version()`` 的返回值揉成 ``(有新版, 最新版本, 下载页)``。

    更新模块由别人实现，返回形状未定：字典、二元组、纯版本号字符串都可能。
    这里全部兼容，实在看不懂就抛 ``ValueError``，由调用方转成「检查更新失败」，
    不让界面猜。
    """
    latest = ""
    page = ""
    explicit = None

    if isinstance(result, dict):
        # 现行 update.check_latest_version() 用 ok/error 表示「这次检查本身成不成功」。
        # 检查失败时它不抛异常，只是把原因写进 error——这里要当成失败处理。
        if result.get("ok") is False:
            raise ValueError(str(result.get("error") or "检查更新失败").strip())
        for key in ("latest", "latest_version", "new_version", "version",
                    "tag_name", "tag"):
            value = result.get(key)
            if value:
                latest = str(value).strip()
                break
        for key in ("url", "download_url", "page", "html_url",
                    "release_url", "link", "href"):
            value = result.get(key)
            if value:
                page = str(value).strip()
                break
        for key in ("newer", "has_new", "is_new", "update_available",
                    "need_update", "outdated"):
            if key in result:
                explicit = bool(result[key])
                break
    elif isinstance(result, (tuple, list)):
        values = list(result)
        if len(values) >= 1 and values[0] is not None:
            latest = str(values[0]).strip()
        if len(values) >= 2 and values[1]:
            page = str(values[1]).strip()
        if len(values) >= 3 and isinstance(values[2], bool):
            explicit = values[2]
    elif isinstance(result, str):
        latest = result.strip()
    elif result is None:
        raise ValueError("更新接口返回了 None")
    else:
        raise ValueError("更新接口返回了 %r" % (result,))

    if explicit is None:
        has_new = _version_newer(latest, current) if latest else False
    else:
        has_new = bool(explicit) and bool(latest or page)
    return has_new, latest, page


def _valid_geometry(text):
    """这个几何字符串能不能直接交给 ``root.geometry()``。

    除了格式合法，还要求尺寸像样：窗口在 ``withdraw()``、还没布局或者被
    最小化的时候，``geometry()`` 会返回 ``1x1+0+0`` 这种值——照着恢复，
    下次启动用户看到的就是一个一像素的窗口。
    """
    value = str(text or "").strip()
    if not value or len(value) > 40:
        return False
    if not _GEOMETRY_RE.match(value):
        return False
    width, height = _geometry_size(value)
    return width >= 600 and height >= 400


def _geometry_size(text):
    """从 ``800x600+10+20`` 里取出 ``(800, 600)``；解析不出来就是 ``(0, 0)``。"""
    head = str(text or "").strip().split("+")[0].split("-")[0]
    try:
        width, height = head.split("x")
        return int(width), int(height)
    except (ValueError, AttributeError):
        return (0, 0)


def _split_links(raw):
    """把多行文本切成链接列表：去空行、去首尾空格、按出现顺序去重。"""
    links = []
    seen = set()
    for line in str(raw or "").splitlines():
        text = line.strip().strip(",;，；、")
        if not text or text in seen:
            continue
        seen.add(text)
        links.append(text)
    return links


def _is_text_widget(widget):
    """是不是 ``tk.Text``（链接输入框从 2.0.3 起换成了多行文本框）。"""
    try:
        return isinstance(widget, tk.Text)
    except Exception:
        return False


def _load_settings_module():
    """拿到 ``mfd.settings``；还没写出来就返回 None（整块功能静默跳过）。"""
    try:
        from .. import settings as settings_mod
        return settings_mod
    except Exception:
        return None


class _SettingsBridge(object):
    """对 ``mfd.settings`` 的薄适配层。

    为什么要有这一层：``settings.py`` 由别人并行实现，接口形状还没定死——
    可能是 ``Settings()`` 对象带 ``get/set/save``，可能是模块级函数，也可能
    直接就是属性。这里把三种形状都试一遍，任何一种失败就当「没有这项设置」，
    绝不让界面因为读不到设置而打不开。
    """

    #: 界面会读写的全部键。启动时一次性读进内存缓存，之后读操作不再碰磁盘。
    KEYS = ("theme", "window_geometry", "last_url", "download_dir",
            "max_threads", "gap_seconds", "speed_limit_kbps", "finish_action",
            "auto_folder", "include_subfolders", "autostart",
            "download_dir_history")

    def __init__(self, module=None):
        self.module = module
        self.store = None
        self._cache = {}
        self._jobs = queue.Queue()
        self._writer = None
        if module is None:
            return
        try:
            factory = getattr(module, "Settings", None)
            if callable(factory):
                self.store = factory()
        except Exception:
            self.store = None
        self._preload()
        self._start_writer()

    # -- 内存缓存读写 ----------------------------------------------------
    def _preload(self):
        """把已知键一次读进缓存。

        **只在这里读盘**：之后 ``get()`` 全走内存。这样即使后台写盘卡在某个
        坏掉的文件系统上（本机就遇到过），界面读设置也永远不会被拖住。
        """
        for key in self.KEYS:
            value = None
            try:
                value = self._read_target(key, None)
            except Exception:
                value = None
            if value is not None:
                self._cache[key] = value

    def _read_target(self, key, default=None):
        """从真正的存储对象上读一项（可能加锁，只允许启动时调用）。"""
        target = self._target()
        try:
            getter = getattr(target, "get", None)
            if callable(getter):
                value = getter(key, default)
                if value is not None:
                    return value
        except Exception:
            pass
        try:
            value = getattr(target, key)
            if callable(value):
                return default
            return value
        except Exception:
            return default

    # -- 后台写盘线程 ----------------------------------------------------
    def _start_writer(self):
        if self._writer is not None:
            return
        try:
            self._writer = threading.Thread(target=self._write_loop,
                                            name="mfd-settings", daemon=True)
            self._writer.start()
        except Exception:
            self._writer = None

    def _write_loop(self):
        """守护线程：把设置写进磁盘。

        做成后台线程的唯一理由是**防卡死**——本机实测有个目录里建文件会直接
        挂住，写在界面线程里就是整个窗口假死。这里挂住也只挂这一条线程。
        """
        while True:
            try:
                job = self._jobs.get()
            except Exception:
                return
            try:
                kind = job[0]
                if kind == "set":
                    self._write_set(job[1], job[2])
                elif kind == "history":
                    self._write_set("download_dir_history", job[1])
                    self._write_save()
                elif kind == "save":
                    self._write_save()
            except Exception:
                pass

    def _write_set(self, key, value):
        target = self._target()
        setter = getattr(target, "set", None)
        if callable(setter):
            try:
                setter(key, value, save=False)
                return True
            except TypeError:
                pass
            except Exception:
                return False
            try:
                setter(key, value)
                return True
            except Exception:
                return False
        updater = getattr(target, "update", None)
        if callable(updater):
            try:
                updater({key: value})
                return True
            except Exception:
                pass
        try:
            setattr(target, key, value)
            return True
        except Exception:
            return False

    def _write_save(self):
        if self.store is not None:
            saver = getattr(self.store, "save", None)
            if callable(saver):
                try:
                    saver()
                    return True
                except Exception:
                    pass
        saver = getattr(self.module, "save", None) if self.module else None
        if callable(saver):
            try:
                saver()
                return True
            except Exception:
                pass
        return False

    # -- 基础读写 --------------------------------------------------------
    def _target(self):
        """真正承载键值的东西：优先 Settings 实例，否则退回模块本身。"""
        return self.store if self.store is not None else self.module

    def get(self, key, default=None):
        """读一项设置——**只读内存缓存，不碰磁盘**，因此永远不会卡界面。"""
        if self.module is None:
            return default
        value = self._cache.get(key, None)
        return default if value is None else value

    def set(self, key, value):
        """写一项设置：立刻更新缓存，写盘丢给后台线程。"""
        if self.module is None:
            return False
        self._cache[key] = value
        try:
            self._jobs.put(("set", key, value))
        except Exception:
            return False
        return True

    def save(self):
        """请求落盘；真正写盘在后台线程里做。"""
        if self.module is None:
            return False
        try:
            self._jobs.put(("save",))
        except Exception:
            return False
        return True

    def flush(self, timeout=0.3):
        """退出前尽力等一会儿写盘线程，最多 ``timeout`` 秒。

        写不动（坏盘、被杀软锁住）也只放弃等待——关窗不能因为存设置而卡住。
        """
        self.save()
        writer = self._writer
        if writer is None:
            return False
        try:
            writer.join(timeout)
        except Exception:
            return False
        return not writer.is_alive()

    # -- 保存位置历史 ----------------------------------------------------
    def history(self, limit=DIR_HISTORY_LIMIT):
        """读目录历史（永远是字符串列表，最多 ``limit`` 条）。"""
        try:
            raw = self.get("download_dir_history", [])
        except Exception:
            raw = []
        if isinstance(raw, str):
            raw = [part for part in raw.splitlines()]
        values = []
        try:
            for item in list(raw or []):
                text = str(item or "").strip()
                if text and text not in values:
                    values.append(text)
                if len(values) >= limit:
                    break
        except Exception:
            return []
        return values

    def push_dir(self, path, limit=DIR_HISTORY_LIMIT):
        """把一次实际用到的目录记进历史最前面，立刻返回最新的历史列表。

        去重、置顶、截断都在内存里算完；写盘交给后台线程，界面不用等。
        """
        text = str(path or "").strip()
        if not text:
            return self.history(limit)
        values = [item for item in self.history(limit) if item != text]
        values.insert(0, text)
        values = values[:limit]
        if self.module is None:
            return values
        self._cache["download_dir_history"] = values
        try:
            self._jobs.put(("history", values))
        except Exception:
            pass
        return values


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

        # 设置桥（mfd.settings 可能还没写出来：拿不到就整块静默跳过）
        self.settings_bridge = _SettingsBridge(_load_settings_module())

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

        # 2.0.3 新增的运行态
        self._last_url = ""          # 最近一次解析用的链接（建子目录名时要用）
        self._filter_job = None      # 搜索防抖的 after id
        self._options_job = None     # 并发/间隔/限速防抖的 after id
        self._update_checking = False
        self._typing_urls = []       # 最近一次送进解析线程的链接列表
        self._last_options = None    # 上一次已写进引擎的 (并发, 间隔, 限速)

        self._build_window()
        self._build_ui()
        self._bind_shortcuts()
        # 恢复上次的窗口几何、主题、链接与各项开关（任何一项失败都不影响启动）
        self._restore_ui_state()

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

        # 检查更新：放在主题按钮左边（pack 顺序决定左右）
        self.update_btn = self._register_tint(widgets.RoundedButton(
            right, "检查更新", command=self._on_check_update,
            kind="ghost", palette=palette, fonts=self.fonts, height=32,
            pad_x=12, min_width=0,
            tooltip="问一下服务器有没有新版本"))
        self.update_btn.pack(side="right", padx=(0, 8), pady=8)

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
    def _build_link_text(self, parent):
        """多行链接输入框（替换掉 2.0.2 的单行 Entry）。

        属性名仍然是 ``self.link_entry``：外部读内容走 ``_entry_text()``，
        写内容走 ``_set_entry_text()``，等价于原来 Entry 的 ``get`` /
        ``delete+insert`` 语义，别处代码不用跟着改。
        """
        text = tk.Text(parent, height=3, wrap="none", undo=True,
                       font=self.fonts.body, relief="flat", borderwidth=0,
                       highlightthickness=1, padx=10, pady=8, insertwidth=1)
        text.configure(bg=self.palette["surface_alt"],
                       fg=self.palette["text"],
                       highlightbackground=self.palette["border"],
                       highlightcolor=self.palette["accent"],
                       insertbackground=self.palette["text"],
                       selectbackground=self.palette["selection"],
                       selectforeground=self.palette["text"])

        def theme_job(palette):
            try:
                text.configure(bg=palette["surface_alt"],
                               fg=palette["text"],
                               highlightbackground=palette["border"],
                               highlightcolor=palette["accent"],
                               insertbackground=palette["text"],
                               selectbackground=palette["selection"],
                               selectforeground=palette["text"])
            except tk.TclError:
                pass

        self._theme_jobs.append(theme_job)
        return text

    # ------------------------------------------------------------------
    # 2.1 保存位置：下拉框 + 历史
    # ------------------------------------------------------------------
    def _dir_text(self):
        """读保存位置（Combobox 的可编辑输入框也是 ``.get()``）。"""
        try:
            return str(self.dir_entry.get() or "").strip()
        except Exception:
            return ""

    def _set_dir_text(self, text):
        """写保存位置：等价于原来的 ``delete(0,'end') + insert(0, text)``。"""
        try:
            self.dir_entry.delete(0, "end")
            self.dir_entry.insert(0, text or "")
        except Exception:
            pass

    def _reload_dir_history(self):
        """把设置里的历史目录灌进下拉框（读不到就只留当前值）。"""
        try:
            values = self.settings_bridge.history(DIR_HISTORY_LIMIT)
        except Exception:
            values = []
        try:
            self.dir_entry.configure(values=list(values))
        except Exception:
            pass
        return values

    def _remember_dir(self, path):
        """记一次实际用过的目录，并刷新下拉候选。"""
        text = str(path or "").strip()
        if not text:
            return
        try:
            values = self.settings_bridge.push_dir(text, DIR_HISTORY_LIMIT)
        except Exception:
            values = []
        try:
            self.dir_entry.configure(values=list(values))
        except Exception:
            pass

    def _build_input_card(self):
        palette = self.palette
        card = self._register_tint(widgets.Card(self.root, palette=palette))
        card.pack(side="top", fill="x", padx=16, pady=(0, 12))
        body = card.body
        body.configure(padx=16, pady=16)

        # 第一行：多行链接输入 + 主按钮
        link_row = self._frame(body, "surface")
        link_row.pack(fill="x")
        self.link_entry = self._build_link_text(link_row)
        self.link_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self._attach_placeholder(
            self.link_entry,
            "一行一个链接，Ctrl+V 可以一次粘多条\n"
            "支持单个文件，也支持整个文件夹\n"
            "粘好后按 Ctrl+Enter 解析")
        self.parse_btn = self._register_tint(widgets.RoundedButton(
            link_row, "解析并加入队列", command=self._on_parse, kind="primary",
            palette=palette, fonts=self.fonts, height=34, min_width=136))
        self.parse_btn.pack(side="right", anchor="n")

        # 第二行：目录 / 线程 / 间隔 / 含子文件夹
        options_row = self._frame(body, "surface")
        options_row.pack(fill="x", pady=(12, 0))

        self._label(options_row, "保存到", fg_token="text_dim",
                    font=self.fonts.small).pack(side="left", padx=(0, 8))
        # 可编辑下拉框：值取历史目录，仍可手打任意路径
        self.dir_entry = ttk.Combobox(options_row, style="TCombobox",
                                      font=self.fonts.body, width=32)
        self.dir_entry.pack(side="left", fill="x", expand=True, ipady=3)
        try:
            self.dir_entry.insert(0, self.kernel.default_download_dir())
        except Exception:
            pass
        self._reload_dir_history()
        self.browse_btn = self._register_tint(widgets.RoundedButton(
            options_row, "浏览…", command=self._choose_dir, kind="subtle",
            palette=palette, fonts=self.fonts, height=30, pad_x=12))
        self.browse_btn.pack(side="left", padx=(8, 8))

        # 一键打开下载文件夹（用户 2026-11-02 要「快速打开下载文件夹」）
        self.open_dir_btn = self._register_tint(widgets.RoundedButton(
            options_row, "打开文件夹", command=self._open_dir, kind="subtle",
            palette=palette, fonts=self.fonts, height=30, pad_x=12))
        self.open_dir_btn.pack(side="left", padx=(0, 16))

        self._label(options_row, "并发", fg_token="text_dim",
                    font=self.fonts.small).pack(side="left", padx=(0, 8))
        self.threads_spin = self._spinbox(options_row, 1, 8, 1, width=3)
        self.threads_spin.delete(0, "end")
        self.threads_spin.insert(0, str(DEFAULT_THREADS))
        self.threads_spin.pack(side="left", ipady=4)

        self._label(options_row, "间隔(秒)", fg_token="text_dim",
                    font=self.fonts.small).pack(side="left", padx=(16, 8))
        self.gap_spin = self._spinbox(options_row, 0.0, 5.0, 0.5, width=4)
        self.gap_spin.delete(0, "end")
        self.gap_spin.insert(0, "%.1f" % DEFAULT_GAP)
        self.gap_spin.pack(side="left", ipady=4)

        self.subfolder_var = tk.BooleanVar(value=True)
        self.subfolder_chk = self._checkbutton(options_row, "含子文件夹", self.subfolder_var)
        self.subfolder_chk.pack(side="left", padx=(16, 0))

        # 解析完自动开始：默认勾上（用户 2026-11-02 要求「别让我再点一次」），
        # 想手动控制的人可以取消勾选。
        self.autostart_var = tk.BooleanVar(value=True)
        self.autostart_chk = self._checkbutton(options_row, "解析后自动开始", self.autostart_var)
        self.autostart_chk.pack(side="left", padx=(16, 0))

        # 2.5 行：限速 / 完成后动作 / 自动建子目录
        options_row2 = self._frame(body, "surface")
        options_row2.pack(fill="x", pady=(10, 0))

        self._label(options_row2, "限速", fg_token="text_dim",
                    font=self.fonts.small).pack(side="left", padx=(0, 8))
        self.speed_entry = self._entry(options_row2, "surface_alt", width=7)
        self.speed_entry.pack(side="left", ipady=4)
        try:
            self.speed_entry.insert(0, "0")
        except tk.TclError:
            pass
        self._label(options_row2, "KB/s（0＝不限）", fg_token="text_dim",
                    font=self.fonts.small).pack(side="left", padx=(6, 24))

        self._label(options_row2, "全部完成后", fg_token="text_dim",
                    font=self.fonts.small).pack(side="left", padx=(0, 8))
        self.finish_var = tk.StringVar(value=FINISH_ACTIONS[0])
        self.finish_combo = ttk.Combobox(
            options_row2, textvariable=self.finish_var, state="readonly",
            values=list(FINISH_ACTIONS), font=self.fonts.body, width=10)
        self.finish_combo.pack(side="left")
        self.finish_combo.bind("<<ComboboxSelected>>", self._on_finish_action_change)

        # 用户最在意的功能：按 MediaFire 文件夹名建子目录（默认勾上）
        self.auto_folder_var = tk.BooleanVar(value=True)
        self.auto_folder_chk = self._checkbutton(
            options_row2, "按 MediaFire 文件夹名建子目录", self.auto_folder_var)
        self.auto_folder_chk.configure(command=self._on_auto_folder_toggle)
        self.auto_folder_chk.pack(side="left", padx=(24, 0))

        # 实际落盘位置（建了子目录之后和输入框里的目录不是一回事，得让用户看见）
        self.effective_label = self._label(
            body, "实际存到：—", bg_token="surface", fg_token="text_dim",
            font=self.fonts.small, anchor="w")
        self.effective_label.pack(fill="x", pady=(6, 0))

        # 并发/间隔/限速改动 → 延迟写回引擎
        for widget in (self.threads_spin, self.gap_spin):
            for sequence in ("<FocusOut>", "<KeyRelease>",
                             "<<Increment>>", "<<Decrement>>"):
                widget.bind(sequence, self._on_options_changed, add="+")
        for sequence in ("<FocusOut>", "<KeyRelease>", "<Return>"):
            self.speed_entry.bind(sequence, self._on_options_changed, add="+")

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

        # 工具条：文件名搜索 + 状态筛选（两者是 AND 关系）
        toolbar = self._frame(page, "surface")
        toolbar.pack(fill="x", padx=16, pady=(16, 0))

        self._label(toolbar, "搜索", fg_token="text_dim",
                    font=self.fonts.small).pack(side="left", padx=(0, 8))
        self.search_entry = self._entry(toolbar, "surface_alt", width=26)
        self.search_entry.pack(side="left", ipady=4)
        self._attach_placeholder(self.search_entry, "搜索文件名…")
        self.search_entry.bind("<KeyRelease>", self._on_search_key, add="+")

        self._label(toolbar, "状态", fg_token="text_dim",
                    font=self.fonts.small).pack(side="left", padx=(16, 8))
        self.status_filter_var = tk.StringVar(value=STATUS_FILTERS[0])
        self.status_combo = ttk.Combobox(
            toolbar, textvariable=self.status_filter_var, state="readonly",
            values=list(STATUS_FILTERS), font=self.fonts.body, width=8)
        self.status_combo.pack(side="left")
        self.status_combo.bind("<<ComboboxSelected>>", self._on_status_filter)

        self.filter_hint = self._label(toolbar, "", fg_token="text_dim",
                                       font=self.fonts.small, anchor="e")
        self.filter_hint.pack(side="right", pady=(6, 0))

        wrapper = self._frame(page, "surface")
        wrapper.pack(fill="both", expand=True, padx=16, pady=(8, 16))

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
        self._empty_title = self._label(
            self.empty_state, "还没有任务", bg_token="surface",
            fg_token="text", font=self.fonts.head, anchor="center")
        self._empty_title.pack(side="top")
        self._empty_hint = self._label(
            self.empty_state,
            "在上面粘一个 MediaFire 分享链接，按 Ctrl+Enter",
            bg_token="surface", fg_token="text_dim",
            font=self.fonts.body, anchor="center")
        self._empty_hint.pack(side="top", pady=(8, 0))
        self._empty_sub = self._label(
            self.empty_state, "支持单文件与整个文件夹",
            bg_token="surface", fg_token="text_dim",
            font=self.fonts.small, anchor="center")
        self._empty_sub.pack(side="top", pady=(4, 0))
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
        # 反馈问题时一份 zip 打包带走：诊断 JSON + 失败清单 + 日志 + 说明
        self.diag_bundle_btn = self._register_tint(widgets.RoundedButton(
            toolbar, "导出诊断包", command=self._on_export_bundle, kind="ghost",
            palette=self.palette, fonts=self.fonts, height=34,
            tooltip="把诊断报告、失败清单、日志和一份说明打成一个 zip"))
        self.diag_bundle_btn.pack(side="left", padx=(8, 0))
        # 失败清单和诊断报告都写在数据目录里，给个按钮直接打开
        self.diag_data_btn = self._register_tint(widgets.RoundedButton(
            toolbar, "打开数据目录", command=self._on_open_data_dir, kind="ghost",
            palette=self.palette, fonts=self.fonts, height=34,
            tooltip="打开存放失败清单与诊断报告的目录"))
        self.diag_data_btn.pack(side="left", padx=(8, 0))
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
        self._repaint_theme(target)
        self._log("INFO 已切换到%s主题" % theme.MODE_TEXT.get(theme.mode(), ""))
        self._settings_set("theme", theme.mode())

    def _repaint_theme(self, mode=None):
        """把整套配色重刷一遍（切主题和启动时恢复设置都走这里）。

        ``mode`` 为空表示保持当前模式只重刷；给值时先切换主题再重刷。
        """
        if mode:
            theme.apply_style(self.root, mode)
        self.palette = theme.current()
        for job in list(self._theme_jobs):
            try:
                job(self.palette)
            except tk.TclError:
                pass
            except Exception:
                pass
        try:
            self.root.configure(bg=self.palette["bg"])
        except tk.TclError:
            pass
        try:
            self.theme_btn.set_text(self._theme_button_text())
        except Exception:
            pass
        self._retint_trees()
        self._retint_text()
        return self.palette

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
    def _attach_placeholder(self, widget, hint):
        """给输入控件挂一段灰色占位提示。

        ``tk.Entry`` 与 ``tk.Text`` 的索引写法不同（Text 没有 ``0`` 这个索引），
        所以位置一律通过 ``start()`` / ``end()`` 换算；Text 的占位文字用 tag 上色，
        免得整段内容都跟着变灰。
        """
        is_text = _is_text_widget(widget)
        state = {"active": False, "hint": hint, "text": is_text}

        def start():
            return "1.0" if is_text else 0

        def finish():
            return "end-1c" if is_text else "end"

        def paint(token):
            if is_text:
                try:
                    widget.tag_configure("placeholder", foreground=self.palette[token])
                    widget.tag_add("placeholder", "1.0", "end-1c")
                except tk.TclError:
                    pass
                return
            try:
                widget.configure(fg=self.palette[token])
            except tk.TclError:
                pass

        def current():
            """控件里现在的文字（去掉尾部换行）。"""
            if is_text:
                return widget.get("1.0", "end-1c").strip()
            return widget.get()

        def show():
            if state["active"] or current():
                return
            state["active"] = True
            widget.insert(start(), hint)
            paint("text_dim")

        def hide():
            if not state["active"]:
                return
            state["active"] = False
            widget.delete(start(), finish())
            if is_text:
                try:
                    widget.tag_remove("placeholder", "1.0", "end")
                except tk.TclError:
                    pass
            else:
                try:
                    widget.configure(fg=self.palette["text"])
                except tk.TclError:
                    pass

        widget.bind("<FocusIn>", lambda _event: hide(), add="+")
        widget.bind("<FocusOut>", lambda _event: show(), add="+")

        def theme_job(palette):
            if state["active"]:
                try:
                    if is_text:
                        widget.tag_configure("placeholder",
                                             foreground=palette["text_dim"])
                        widget.tag_add("placeholder", "1.0", "end-1c")
                    else:
                        widget.configure(fg=palette["text_dim"])
                except tk.TclError:
                    pass

        self._theme_jobs.append(theme_job)
        widget._mfd_placeholder = state
        show()

    def _entry_text(self, entry):
        state = getattr(entry, "_mfd_placeholder", None)
        if state and state["active"]:
            return ""
        if _is_text_widget(entry):
            try:
                return entry.get("1.0", "end-1c").strip()
            except tk.TclError:
                return ""
        return entry.get().strip()

    def _set_entry_text(self, entry, value, replace=True):
        is_text = _is_text_widget(entry)
        state = getattr(entry, "_mfd_placeholder", None)
        was_placeholder = bool(state and state["active"])
        if was_placeholder:
            state["active"] = False
            if is_text:
                try:
                    entry.tag_remove("placeholder", "1.0", "end")
                except tk.TclError:
                    pass
            else:
                try:
                    entry.configure(fg=self.palette["text"])
                except tk.TclError:
                    pass
        # 占位文字留在里面会让新内容接在提示后面，所以清掉它的那一轮也要清空
        if replace or was_placeholder:
            if is_text:
                entry.delete("1.0", "end")
            else:
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
        # 别的输入框（目录、线程数、日志区）走 Tk 默认粘贴，不在这里抢
        if widget is not None and widget is not self.link_entry:
            if isinstance(widget, (tk.Entry, tk.Spinbox, tk.Text)):
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
            if _is_text_widget(self.link_entry):
                self.link_entry.mark_set("insert", "end")
                self.link_entry.see("end")
            else:
                self.link_entry.icursor("end")
        except tk.TclError:
            pass
        links = _split_links(data)
        if len(links) > 1:
            self._set_status("已粘贴 %d 个链接，按 Ctrl+Enter 开始解析" % len(links), "info")
        else:
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
        elif kind == "bundle_ready":
            self._on_bundle_ready(*payload)
        elif kind == "update_result":
            self._on_update_result(*payload)

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
        """从界面读出一次下载配置；不合法就返回兜底值并提示。"""
        directory = self._dir_text()
        if not directory:
            try:
                directory = self.kernel.default_download_dir()
            except Exception:
                directory = _fallback_download_dir()
        threads = self._threads_value()
        gap = self._gap_value()
        return {
            "dir": directory,
            "threads": threads,
            "gap": gap,
            "include": bool(self.subfolder_var.get()),
            "speed_kbps": self._speed_limit_value(),
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
        raw = self._entry_text(self.link_entry)
        links = _split_links(raw)
        if not links:
            self._set_status("请先粘贴 MediaFire 链接", "warning")
            try:
                self.link_entry.focus_set()
            except tk.TclError:
                pass
            return
        options = self._read_options()
        self._typing_urls = list(links)
        if len(links) == 1:
            self._last_url = links[0]
            self._set_busy(True, "正在解析链接…")
            self._log("INFO 开始解析：%s" % links[0])
            thread = threading.Thread(target=self._resolve_worker, args=(links[0], options),
                                      name="mfd-resolve", daemon=True)
            thread.start()
            return
        # 多行：一条一条解析，进度经事件队列回主线程
        self._last_url = links[0]
        self._set_busy(True, "正在解析 1/%d …" % len(links))
        self._log("INFO 开始批量解析：共 %d 条链接" % len(links))
        thread = threading.Thread(target=self._resolve_worker_batch,
                                  args=(list(links), options),
                                  name="mfd-resolve-batch", daemon=True)
        thread.start()

    def _resolve_worker_batch(self, urls, options):
        """后台：一条一条解析多行链接，最后合起来建队列。

        单条失败只记一笔、不中断整批——十个链接里坏一个，剩下九个照样入库。
        """
        total = len(urls)
        collected = []
        failed = []
        for index, url in enumerate(urls, 1):
            self._post("status", "正在解析 %d/%d …" % (index, total))
            try:
                items, _description = self.kernel.resolve_link(
                    url,
                    include_subfolders=options["include"],
                    on_progress=self._on_resolve_progress,
                )
            except Exception as exc:
                message = (getattr(exc, "user_message", "") or str(exc)
                           or exc.__class__.__name__)
                failed.append((url, message))
                self._post("log", "WARN 第 %d/%d 条解析失败：%s" % (index, total, message))
                continue
            merged = list(items or [])
            collected.extend(merged)
            self._post("log", "INFO 第 %d/%d 条：%d 个文件" % (index, total, len(merged)))

        if not collected:
            detail = "\n".join("%s → %s" % (url, msg) for url, msg in failed[:6])
            self._post("resolve_error", ("这几条链接都没能解析出文件。", detail))
            return

        try:
            downloader = self._ensure_downloader(options)
            tasks = downloader.load_tasks(collected)
        except Exception:
            self._post("resolve_error",
                       ("链接已解析，但建立下载队列时出错。", traceback.format_exc()))
            return

        description = "%d 条链接共 %d 个文件" % (total, len(collected))
        if failed:
            description += "（%d 条失败）" % len(failed)
        # 子目录名只取第一条链接的文件夹名：引擎一次只支持一个 collection_name
        self._post("tasks_loaded", (list(tasks or []), description, urls[0]))

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

        # 新队列进来：先复位筛选，否则筛在「已完成」上时一条新任务都看不见
        self._reset_queue_filter()
        # 记住这次的链接，并按开关决定要不要建「文件夹名」子目录
        self._last_url = str(url or "")
        self._apply_collection_name(self._last_url)
        self._apply_options_to_engine(verbose=False)
        self._refresh_effective_root()

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
        """队列空了就把三行引导摆出来，有任务就收掉。

        注意「空了」有两种：真没任务，和筛完一条不剩。后者文案要换，
        否则用户会以为任务丢了。
        """
        empty = getattr(self, "empty_state", None)
        if empty is None:
            return
        try:
            has_rows = bool(self.queue_tree.get_children(""))
        except tk.TclError:
            return
        self._set_empty_text(filtered=bool(self._tasks) and not has_rows)
        try:
            if has_rows:
                empty.grid_remove()
            else:
                empty.grid()
                empty.lift()
        except tk.TclError:
            pass

    def _set_empty_text(self, filtered=False):
        """空状态三行文案：默认引导，筛空时改成「没匹配到」。"""
        if filtered:
            pairs = (("没有匹配的任务", getattr(self, "_empty_title", None)),
                     ("换个关键词，或者把状态筛选调回「全部」",
                      getattr(self, "_empty_hint", None)),
                     ("队列里共有 %d 个任务" % len(self._tasks),
                      getattr(self, "_empty_sub", None)))
        else:
            pairs = (("还没有任务", getattr(self, "_empty_title", None)),
                     ("在上面粘一个 MediaFire 分享链接，按 Ctrl+Enter",
                      getattr(self, "_empty_hint", None)),
                     ("支持单文件与整个文件夹", getattr(self, "_empty_sub", None)))
        for text, label in pairs:
            if label is None:
                continue
            try:
                label.configure(text=text)
            except Exception:
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
            # 限速只在引擎支持时才生效；不支持就当没设置过，不打断建队
            self._apply_speed_limit(options.get("speed_kbps", 0))
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
            self.downloader.set_download_dir(self._dir_text())
        except Exception:
            self._log("WARN 切换保存位置时出错：%s" % traceback.format_exc(limit=1))
        # 开始之前把目录、并发、间隔、限速、子目录名一并推给引擎（它们的
        # setter 都允许在未运行时调用），这样这一轮就是按界面上的数字跑的。
        try:
            self._remember_dir(self._dir_text())
        except Exception:
            pass
        try:
            self._apply_collection_name(self._last_url)
        except Exception:
            pass
        try:
            self._apply_options_to_engine(verbose=False)
        except Exception:
            pass
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
        if started and actual_dir and actual_dir != self._dir_text():
            self._set_dir_text(actual_dir)
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
                  % (len(self._tasks), self._threads_value(), self._gap_value()))
        # 引擎里真正会落盘的目录（含子目录）现在才定下来，刷一行给用户看
        self._refresh_effective_root()
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
        # 「全部完成后」的收尾动作（不做事 / 打开文件夹 / 关机）。
        # 放在三个分支弹窗之前：先干完活，再让用户看结果。
        self._maybe_finish_action(done, failed)

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
        current = self._dir_text()
        try:
            chosen = filedialog.askdirectory(parent=self.root, title="选择下载目录",
                                             initialdir=current or None)
        except tk.TclError:
            return
        if chosen:
            self._set_dir_text(chosen)
            self._set_status("下载目录已改为 %s" % chosen, "info")
            self._refresh_effective_root()

    def _open_dir(self):
        directory = self._dir_text()
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
            self._open_in_explorer(directory)
        except OSError as exc:
            self._set_status("打开目录失败：%s" % exc, "danger")
            return
        self._set_status("已打开 %s" % directory, "success")

    def _open_in_explorer(self, directory):
        """打开一个目录。

        优先用 explorer.exe：``os.startfile`` 对目录走 ShellExecute，在部分环境
        （权限受限、被安全软件挂钩）会返回 ``[WinError 5] 拒绝访问``，而直接把
        路径交给 explorer.exe 走的是另一条路，实测更稳。真不行再退回 startfile。
        """
        target = os.path.normpath(directory)
        if os.name != "nt":
            self._set_status("当前系统不支持直接打开目录", "warning")
            return
        try:
            subprocess.Popen(["explorer.exe", target])
            return
        except OSError:
            pass
        if hasattr(os, "startfile"):
            os.startfile(target)
        else:
            self._set_status("当前系统不支持直接打开目录", "warning")

    # ------------------------------------------------------------------
    # 4.1 队列筛选：文件名搜索 + 状态筛选（AND）
    # ------------------------------------------------------------------
    def _search_keyword(self):
        try:
            return self._entry_text(self.search_entry).lower()
        except Exception:
            return ""

    def _status_filter(self):
        try:
            return str(self.status_filter_var.get() or STATUS_FILTERS[0])
        except Exception:
            return STATUS_FILTERS[0]

    def _filter_is_active(self):
        return bool(self._search_keyword()) or self._status_filter() != STATUS_FILTERS[0]

    def _on_search_key(self, _event=None):
        # 每敲一下就把上一颗定时器取消、重排一颗，实现 150ms 防抖
        self._schedule_filter()

    def _on_status_filter(self, _event=None):
        # 下拉是离散选择，不需要防抖
        self._schedule_filter(immediate=True)

    def _schedule_filter(self, immediate=False):
        if self._filter_job is not None:
            try:
                self.root.after_cancel(self._filter_job)
            except Exception:
                pass
            self._filter_job = None
        if immediate:
            self._apply_queue_filter()
            return
        try:
            self._filter_job = self.root.after(SEARCH_DEBOUNCE_MS,
                                               self._apply_queue_filter)
        except Exception:
            self._filter_job = None

    def _row_matches(self, task):
        """这一行是否该显示（关键词 AND 状态）。"""
        keyword = self._search_keyword()
        if keyword:
            name = str(getattr(task, "name", "") or "").lower()
            if keyword not in name:
                return False
        code = STATUS_FILTER_CODE.get(self._status_filter())
        if code:
            status = str(getattr(task, "status", "") or "").lower()
            if status != code:
                return False
        return True

    def _apply_queue_filter(self):
        """按当前条件重排可见行。

        两条实测结论决定了写法：
        * 不能重建整棵树——选中态、滚动位置、斑马纹都会丢，所以只 ``detach``
          要藏起来的行、``reattach`` 要露出来的行；
        * ``move()`` 对已经 detach 的项完全不起作用，挂回去只能用 ``reattach``。
        """
        self._filter_job = None
        tree = getattr(self, "queue_tree", None)
        if tree is None:
            return
        try:
            position = 0
            for task in list(self._tasks):
                iid = self._task_rows.get(id(task))
                if not iid:
                    continue
                try:
                    if not tree.exists(iid):
                        continue
                except tk.TclError:
                    continue
                if self._row_matches(task):
                    try:
                        tree.reattach(iid, "", position)
                    except tk.TclError:
                        pass
                    position += 1
                else:
                    try:
                        tree.detach(iid)
                    except tk.TclError:
                        pass
            self._restripe_queue()
            self._update_empty_state()
            self._refresh_filter_hint()
        except Exception:
            pass

    def _refresh_filter_hint(self, visible=None):
        """工具条右边那句「筛选后 3 / 12」。"""
        label = getattr(self, "filter_hint", None)
        if label is None:
            return
        try:
            if not self._filter_is_active():
                label.configure(text="")
                return
            if visible is None:
                visible = len(self.queue_tree.get_children(""))
            label.configure(text="筛选后 %d / %d" % (visible, len(self._tasks)))
        except Exception:
            pass

    def _reset_queue_filter(self):
        """换了新队列就把筛选复位，免得新任务一条都看不见。"""
        if not self._filter_is_active():
            self._apply_queue_filter()
            return
        try:
            self._set_entry_text(self.search_entry, "")
        except Exception:
            pass
        try:
            self.status_filter_var.set(STATUS_FILTERS[0])
        except Exception:
            pass
        self._apply_queue_filter()
        self._log("INFO 新队列已载入，搜索与状态筛选已复位为「全部」")

    # ------------------------------------------------------------------
    # 4.2 队列顺序：上移一位 / 置顶
    # ------------------------------------------------------------------
    def _move_task(self, task, offset):
        """把任务在队列里往前挪（offset=-1 上移一位；置顶传一个很大的负数）。

        挪的是引擎的 ``downloader.tasks``：那个 list 就是取任务的顺序，所以
        改它就等于改下载先后。引擎内部没有再排一次队，不用碰别的东西。
        """
        if task is None:
            return False
        engine = getattr(self.downloader, "tasks", None)
        source = engine if isinstance(engine, list) and task in engine else self._tasks
        try:
            index = source.index(task)
        except (ValueError, AttributeError):
            self._set_status("这个任务不在队列里", "warning")
            return False
        target = max(0, min(len(source) - 1, index + int(offset)))
        if target == index:
            self._set_status("已经排在最前面了" if offset < 0 else "已经排在最后面了",
                             "text_dim")
            return False
        source.pop(index)
        source.insert(target, task)
        if source is not self._tasks:
            # 界面遍历用的是 self._tasks，顺序要跟着一致，否则筛选重排会打架
            try:
                self._tasks.remove(task)
                follower = source[target + 1] if target + 1 < len(source) else None
                position = (self._tasks.index(follower)
                            if follower is not None and follower in self._tasks
                            else len(self._tasks))
                self._tasks.insert(position, task)
            except (ValueError, AttributeError):
                pass
        iid = self._task_rows.get(id(task))
        try:
            if iid and self.queue_tree.exists(iid):
                self.queue_tree.move(iid, "", target)
        except tk.TclError:
            pass
        self._apply_queue_filter()
        name = str(getattr(task, "name", "") or "")
        self._set_status("已把「%s」移到第 %d 位" % (name, target + 1), "info")
        return True

    def _copy_failure_reason(self, task):
        """把失败原因抄进剪贴板（没有就复制「无」）。"""
        reason = str(getattr(task, "error", "") or "").strip() or "无"
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(reason)
        except tk.TclError:
            self._set_status("剪贴板不可用", "warning")
            return
        self._set_status("已复制失败原因：%s" % reason[:60], "info")

    def _open_task_url(self, task):
        """在浏览器里打开这条任务的源页面。"""
        url = str(getattr(task, "url", "") or "").strip()
        if not url:
            self._set_status("这个任务没有记录源页面地址", "warning")
            return
        try:
            webbrowser.open(url)
        except Exception as exc:
            self._set_status("打开浏览器失败：%s" % exc, "danger")
            return
        self._set_status("已在浏览器中打开源页面", "info")

    # ------------------------------------------------------------------
    # 4.3 并发 / 间隔 / 限速
    # ------------------------------------------------------------------
    @staticmethod
    def _int_or(value, default):
        """把界面上读来的字符串转成 int，转不动就用默认值。"""
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default

    def _threads_value(self):
        try:
            text = self.threads_spin.get()
        except Exception:
            text = ""
        return max(1, min(8, self._int_or(text, DEFAULT_THREADS)))

    def _gap_value(self):
        try:
            text = self.gap_spin.get()
        except Exception:
            text = ""
        try:
            value = float(text)
        except (TypeError, ValueError):
            value = DEFAULT_GAP
        return max(0.0, min(5.0, value))

    def _speed_limit_value(self):
        """限速（KB/s），0 表示不限；填了乱码也按不限处理。"""
        try:
            text = self.speed_entry.get()
        except Exception:
            text = ""
        return max(0, self._int_or(text, 0))

    def _on_options_changed(self, _event=None):
        self._schedule_options_apply()

    def _schedule_options_apply(self):
        if self._options_job is not None:
            try:
                self.root.after_cancel(self._options_job)
            except Exception:
                pass
        try:
            self._options_job = self.root.after(OPTIONS_DEBOUNCE_MS,
                                                self._apply_options_to_engine)
        except Exception:
            self._options_job = None

    def _apply_options_to_engine(self, verbose=True):
        """把并发 / 间隔 / 限速写回引擎。

        引擎的 ``set_options`` 只改**下一轮**要起的线程，正在跑的那几个线程
        不受影响——这点必须写进日志，否则用户会觉得拖动数字没反应。
        """
        self._options_job = None
        threads = self._threads_value()
        gap = self._gap_value()
        speed = self._speed_limit_value()
        downloader = self.downloader
        if downloader is None:
            return False
        try:
            downloader.set_options(max_threads=threads, gap_seconds=gap)
        except Exception:
            pass
        supported = self._apply_speed_limit(speed)
        current = (threads, gap, speed)
        if verbose and current != self._last_options:
            self._last_options = current
            text = "并发 %d，间隔 %.1f 秒，限速 %s" % (
                threads, gap, ("不限" if not speed else "%d KB/s" % speed))
            if speed and not supported:
                text += "（当前引擎还不支持限速，这一项被忽略）"
            self._log("INFO 参数已更新：%s；对正在下载的线程不生效，下一轮才完全起作用"
                      % text)
        return True

    def _apply_speed_limit(self, speed):
        """限速单独走一次 set_options，好精确兜住「引擎还没加这个参数」。"""
        downloader = self.downloader
        if downloader is None:
            return False
        try:
            downloader.set_options(speed_limit_kbps=int(speed))
            return True
        except TypeError:
            # 旧引擎没有 speed_limit_kbps：吞掉，界面照常用
            return False
        except Exception:
            return False

    # ------------------------------------------------------------------
    # 4.4 按文件夹名建子目录
    # ------------------------------------------------------------------
    @staticmethod
    def _folder_name_from_url(url):
        """兜底解析：从 ``/folder/<key>/<名字>`` 里取名字。

        比自己拼字符串更难错的做法是交给 ``urlparse``，顺便把 %xx 解码掉。
        取不到就返回空串（＝不建子目录），绝不瞎猜一个名字出来。
        """
        text = str(url or "").strip()
        if not text:
            return ""
        try:
            from urllib.parse import unquote, urlparse
        except ImportError:  # pragma: no cover - 3.x 一定有
            return ""
        try:
            path = urlparse(text).path or ""
        except Exception:
            return ""
        parts = [part for part in path.split("/") if part]
        if len(parts) >= 3 and parts[0].lower() == "folder":
            name = unquote(parts[2]).strip()
            return name
        return ""

    def _collection_name_for(self, url):
        """拿这份链接对应的 MediaFire 文件夹名。

        优先问内核（``mfd.mediafire.folder_display_name``），那份还没写出来
        或者参数对不上时，退回自己的简易解析。
        """
        text = str(url or "").strip()
        if not text:
            return ""
        try:
            from .. import mediafire as mediafire_mod
        except Exception:
            mediafire_mod = None
        if mediafire_mod is not None:
            func = getattr(mediafire_mod, "folder_display_name", None)
            if callable(func):
                key = ""
                try:
                    extract = getattr(mediafire_mod, "extract_folder_key", None)
                    if callable(extract):
                        key = str(extract(text) or "")
                except Exception:
                    key = ""
                for args in ((text, key), (text,)):
                    try:
                        name = str(func(*args) or "").strip()
                    except TypeError:
                        continue
                    except Exception:
                        break
                    if name:
                        return name
        return self._folder_name_from_url(text)

    def _apply_collection_name(self, url=None):
        """按开关把子目录名交给引擎，并刷新「实际存到」。"""
        downloader = self.downloader
        if downloader is None:
            self._refresh_effective_root()
            return ""
        try:
            enabled = bool(self.auto_folder_var.get())
        except Exception:
            enabled = True
        name = ""
        if enabled:
            name = self._collection_name_for(url if url is not None else self._last_url)
        try:
            downloader.set_collection_name(name if enabled else "")
        except AttributeError:
            # 旧引擎没有这个接口：只是不建子目录，不算错误
            self._refresh_effective_root()
            return ""
        except Exception:
            self._refresh_effective_root()
            return ""
        if enabled and name:
            self._log("INFO 将建子目录：%s" % name)
        elif enabled and url:
            self._log("INFO 这条链接里没有文件夹名，文件直接存到保存位置")
        self._refresh_effective_root()
        return name

    def _refresh_effective_root(self):
        """把「文件实际会存到哪」写进那行小字（有子目录时和输入框不一样）。"""
        label = getattr(self, "effective_label", None)
        path = ""
        try:
            if self.downloader is not None:
                path = str(self.downloader.effective_root() or "")
        except Exception:
            path = ""
        if not path:
            path = self._dir_text()
        if label is None:
            return
        try:
            label.configure(text="实际存到：%s" % (path or "—"))
        except Exception:
            pass

    def _on_auto_folder_toggle(self):
        """勾选框回调：关掉就立刻让引擎别建子目录。"""
        try:
            enabled = bool(self.auto_folder_var.get())
        except Exception:
            enabled = True
        self._settings_set("auto_folder", enabled)
        if enabled:
            self._apply_collection_name()
            return
        try:
            if self.downloader is not None:
                self.downloader.set_collection_name("")
        except Exception:
            pass
        self._log("INFO 已关闭「按 MediaFire 文件夹名建子目录」")
        self._refresh_effective_root()

    # ------------------------------------------------------------------
    # 4.5 全部完成后做什么
    # ------------------------------------------------------------------
    def _finish_action(self):
        try:
            value = str(self.finish_var.get() or "").strip()
        except Exception:
            value = FINISH_ACTIONS[0]
        return value if value in FINISH_ACTIONS else FINISH_ACTIONS[0]

    def _on_finish_action_change(self, _event=None):
        action = self._finish_action()
        if action == "关机":
            # 关机是不可逆的，选它必须当场确认一次；没确认就退回「不做事」
            confirmed = False
            try:
                confirmed = bool(messagebox.askyesno(
                    "确认关机",
                    "全部下载完成后将自动关机，确定吗？",
                    parent=self.root))
            except Exception:
                confirmed = False
            if not confirmed:
                try:
                    self.finish_var.set(FINISH_ACTIONS[0])
                except Exception:
                    pass
                self._set_status("已保持「完成后不做事」", "text_dim")
                return
        self._settings_set("finish_action", action)
        self._log("INFO 全部下载完成后：%s" % action)

    def _maybe_finish_action(self, done, failed):
        """一轮下载结束后的收尾动作；有失败就什么都不做。"""
        if failed:
            return
        action = self._finish_action()
        if action == "打开文件夹":
            try:
                self._open_dir()
            except Exception as exc:
                self._log("WARN 打开下载文件夹失败：%s" % exc)
            return
        if action != "关机" or not done:
            return
        try:
            os.system("shutdown /s /t 60")
        except Exception as exc:
            self._log("ERROR 关机命令没能执行：%s" % exc)
            return
        self._log("WARN 60 秒后关机，可用 shutdown /a 取消")

    # ------------------------------------------------------------------
    # 4.6 检查更新
    # ------------------------------------------------------------------
    def _on_check_update(self):
        if self._update_checking:
            return
        self._update_checking = True
        try:
            self.update_btn.set_enabled(False)
            self.update_btn.set_text("检查中…")
        except Exception:
            pass
        self._set_status("正在检查更新…", "info")
        version = str(getattr(self.kernel, "VERSION", "") or "")
        try:
            threading.Thread(target=self._update_worker, args=(version,),
                             name="mfd-update", daemon=True).start()
        except Exception as exc:
            self._on_update_result(False, "", "", "线程起不来：%s" % exc)

    def _update_worker(self, version):
        """后台线程：只调接口，结果经事件队列回主线程。"""
        try:
            from .. import update as update_mod
        except Exception as exc:
            self._post("update_result", (False, "", "", "更新模块不可用（%s）" % exc))
            return
        func = getattr(update_mod, "check_latest_version", None)
        if not callable(func):
            self._post("update_result", (False, "", "", "更新模块里没有 check_latest_version"))
            return
        try:
            result = func(version)
        except Exception as exc:
            detail = str(exc) or exc.__class__.__name__
            self._post("update_result", (False, "", "", detail))
            return
        try:
            has_new, latest, page = _parse_update_result(result, version)
        except Exception as exc:
            self._post("update_result", (False, "", "", str(exc) or "返回值看不懂"))
            return
        self._post("update_result", (has_new, latest, page, ""))

    def _on_update_result(self, has_new, latest, page, error=""):
        self._update_checking = False
        try:
            self.update_btn.set_enabled(True)
            self.update_btn.set_text("检查更新")
        except Exception:
            pass
        version = str(getattr(self.kernel, "VERSION", "") or "")
        if error:
            self._set_status("检查更新失败", "warning")
            self._log("WARN 检查更新失败：%s" % error)
            if self._is_viewable():
                try:
                    messagebox.showwarning("检查更新失败",
                                           "网络不通或接口变了。\n%s" % error,
                                           parent=self.root)
                except Exception:
                    pass
            return
        if not has_new:
            self._set_status("已是最新版本", "success")
            self._log("OK 已是最新版本：v%s" % (version or "?"))
            if self._is_viewable():
                try:
                    messagebox.showinfo(
                        "已是最新版本",
                        "当前 v%s 就是最新版本。" % (version or "?"),
                        parent=self.root)
                except Exception:
                    pass
            return
        self._set_status("发现新版本 v%s" % (latest or "?"), "info")
        self._log("INFO 发现新版本：v%s（当前 v%s）" % (latest or "?", version or "?"))
        if not self._is_viewable():
            return
        open_page = False
        try:
            # 用 askyesno 才拿得到「是/否」；showinfo 只有确定，没法据此开浏览器
            open_page = bool(messagebox.askyesno(
                "发现新版本",
                "最新 v%s\n\n是否打开下载页？" % (latest or "?"),
                parent=self.root))
        except Exception:
            open_page = False
        if not open_page:
            return
        if not page:
            self._set_status("新版本没有提供下载页地址", "warning")
            return
        try:
            webbrowser.open(page)
        except Exception as exc:
            self._set_status("打开下载页失败：%s" % exc, "danger")

    # ------------------------------------------------------------------
    # 4.7 导出诊断包 / 打开数据目录
    # ------------------------------------------------------------------
    def _on_export_bundle(self):
        """把诊断报告、失败清单、日志和说明打成一个 zip。"""
        default = "MediaFire诊断包_%s.zip" % time.strftime("%Y%m%d_%H%M%S")
        try:
            path = filedialog.asksaveasfilename(
                parent=self.root, title="导出诊断包",
                initialfile=default, defaultextension=".zip",
                filetypes=[("压缩包", "*.zip"), ("所有文件", "*.*")])
        except Exception as exc:
            self._set_status("选择保存位置失败：%s" % exc, "danger")
            return
        if not path:
            return

        # 这三样都在主线程里取好再交给后台线程——tkinter 控件不能跨线程碰
        try:
            self._ensure_diagnostics()
            report = self.diagnostics.build_report(failures=self.failures) or {}
        except Exception as exc:
            report = {"error": "诊断报告生成失败：%s" % exc}
        try:
            records = self._failure_records()
            failures = [self._failure_values(record) for record in records]
        except Exception:
            failures = []
        try:
            log_text = self.log_text.get("1.0", "end-1c")
        except Exception:
            log_text = ""

        try:
            self.diag_bundle_btn.set_enabled(False)
        except Exception:
            pass
        self._set_status("正在打包诊断信息…", "info")
        try:
            threading.Thread(target=self._bundle_worker,
                             args=(path, report, failures, log_text),
                             name="mfd-bundle", daemon=True).start()
        except Exception as exc:
            try:
                self.diag_bundle_btn.set_enabled(True)
            except Exception:
                pass
            self._set_status("起线程失败：%s" % exc, "danger")

    def _bundle_worker(self, path, report, failures, log_text):
        """后台写 zip。文件名里不带目录，所以天然不会有反斜杠分隔符。"""
        try:
            directory = os.path.dirname(os.path.abspath(path))
            if directory and not os.path.isdir(directory):
                os.makedirs(directory)
            payload = (
                # (包内路径, 内容)；将来若要加子目录，分隔符必须写 "/"
                ("diagnostics.json",
                 json.dumps(report, ensure_ascii=False, indent=2, default=str)),
                ("failures.json",
                 json.dumps(failures, ensure_ascii=False, indent=2, default=str)),
                ("app.log", log_text or ""),
                ("README.txt", _BUNDLE_README),
            )
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as bundle:
                for name, content in payload:
                    bundle.writestr(name, content)
        except Exception as exc:
            self._post("bundle_ready", ("", str(exc) or exc.__class__.__name__))
            return
        self._post("bundle_ready", (path, ""))

    def _on_bundle_ready(self, path, error=""):
        try:
            self.diag_bundle_btn.set_enabled(True)
        except Exception:
            pass
        if error:
            self._set_status("导出诊断包失败", "danger")
            self._log("ERROR 导出诊断包失败：%s" % error)
            if self._is_viewable():
                try:
                    messagebox.showerror("导出失败",
                                         "没能写出诊断包。\n%s" % error,
                                         parent=self.root)
                except Exception:
                    pass
            return
        self._set_status("诊断包已导出", "success")
        self._log("OK 诊断包已导出：%s" % path)
        if self._is_viewable():
            try:
                messagebox.showinfo("导出完成", "诊断包已保存到：\n%s" % path,
                                    parent=self.root)
            except Exception:
                pass

    def _on_open_data_dir(self):
        """打开数据目录（失败清单、诊断报告都写在这里）。"""
        try:
            from ..paths import data_dir
            path = data_dir(create=True)
        except Exception as exc:
            self._set_status("找不到数据目录：%s" % exc, "danger")
            return
        if not path:
            self._set_status("数据目录还没建好，稍后再试", "warning")
            return
        try:
            self._open_in_explorer(path)
        except OSError as exc:
            self._set_status("打开目录失败：%s" % exc, "danger")
            return
        self._set_status("已打开数据目录", "info")

    # ------------------------------------------------------------------
    # 4.8 设置读写与界面状态恢复
    # ------------------------------------------------------------------
    def _settings_get(self, key, default=None):
        try:
            return self.settings_bridge.get(key, default)
        except Exception:
            return default

    def _settings_set(self, key, value, save=True):
        try:
            ok = self.settings_bridge.set(key, value)
        except Exception:
            ok = False
        if ok and save:
            try:
                self.settings_bridge.save()
            except Exception:
                pass
        return ok

    def _restore_ui_state(self):
        """把上次退出时的几何、主题、链接与各项开关放回去。

        每一项都单独 try/except：设置文件缺失、字段写坏、几何字符串不合法，
        都只跳过那一项，绝不影响窗口打开。
        """
        if getattr(self.settings_bridge, "module", None) is None:
            # mfd.settings 还不存在：保持默认值即可
            self._refresh_effective_root()
            return
        try:
            mode = str(self._settings_get("theme", "") or "")
            if mode in (theme.MODE_DARK, theme.MODE_LIGHT) and mode != theme.mode():
                self._repaint_theme(mode)
        except Exception:
            pass
        try:
            geometry = str(self._settings_get("window_geometry", "") or "")
            if _valid_geometry(geometry):
                self.root.geometry(geometry)
        except Exception:
            pass
        try:
            last_url = str(self._settings_get("last_url", "") or "")
            if last_url.strip():
                self._set_entry_text(self.link_entry, last_url)
        except Exception:
            pass
        try:
            threads = self._int_or(self._settings_get("max_threads", DEFAULT_THREADS),
                                   DEFAULT_THREADS)
            threads = max(1, min(8, threads))
            self.threads_spin.delete(0, "end")
            self.threads_spin.insert(0, str(threads))
        except Exception:
            pass
        try:
            gap = float(self._settings_get("gap_seconds", DEFAULT_GAP))
            gap = max(0.0, min(5.0, gap))
            self.gap_spin.delete(0, "end")
            self.gap_spin.insert(0, "%.1f" % gap)
        except Exception:
            pass
        try:
            speed = max(0, self._int_or(self._settings_get("speed_limit_kbps", 0), 0))
            self._set_entry_text(self.speed_entry, str(speed))
        except Exception:
            pass
        try:
            action = str(self._settings_get("finish_action", FINISH_ACTIONS[0]) or "")
            self.finish_var.set(action if action in FINISH_ACTIONS else FINISH_ACTIONS[0])
        except Exception:
            pass
        try:
            self.auto_folder_var.set(bool(self._settings_get("auto_folder", True)))
        except Exception:
            pass
        try:
            for key, var in (("include_subfolders", self.subfolder_var),
                             ("autostart", self.autostart_var)):
                value = self._settings_get(key, None)
                if isinstance(value, bool):
                    var.set(value)
        except Exception:
            pass
        self._refresh_effective_root()

    def _save_ui_state(self):
        """退出前把界面状态记下来；哪一步失败都只跳过那一步。"""
        if getattr(self.settings_bridge, "module", None) is None:
            return
        try:
            geometry = self.root.geometry()
            # 窗口被隐藏、还没布局时 geometry() 是 1x1+0+0；存下去下次启动就毁了
            if _valid_geometry(geometry):
                self._settings_set("window_geometry", geometry, save=False)
        except Exception:
            pass
        try:
            self._settings_set("theme", theme.mode(), save=False)
        except Exception:
            pass
        try:
            self._settings_set("last_url", self._entry_text(self.link_entry), save=False)
        except Exception:
            pass
        try:
            self._settings_set("download_dir", self._dir_text(), save=False)
            self._settings_set("max_threads", self._threads_value(), save=False)
            self._settings_set("gap_seconds", self._gap_value(), save=False)
            self._settings_set("speed_limit_kbps", self._speed_limit_value(), save=False)
        except Exception:
            pass
        try:
            self._settings_set("finish_action", self._finish_action(), save=False)
            self._settings_set("auto_folder", bool(self.auto_folder_var.get()),
                               save=False)
            self._settings_set("include_subfolders", bool(self.subfolder_var.get()),
                               save=False)
            self._settings_set("autostart", bool(self.autostart_var.get()), save=False)
        except Exception:
            pass
        try:
            # 等 2 秒：mkstemp 那类会挂起的写法已经换掉了，正常写盘只要几毫秒；
            # 真写不动（只读盘/被占）也最多等 2 秒就放弃，关窗不卡死。
            self.settings_bridge.flush(2.0)
        except Exception:
            pass

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
            menu.add_command(label="复制文件名",
                             command=lambda: self._copy_to_clipboard(
                                 getattr(task, "name", "")))
            menu.add_command(label="复制失败原因",
                             command=lambda: self._copy_failure_reason(task))
            menu.add_command(label="在浏览器中打开源页",
                             command=lambda: self._open_task_url(task))
            if str(getattr(task, "status", "")) == "failed":
                menu.add_separator()
                menu.add_command(label="重试这个任务",
                                 command=lambda: self._retry_single(task))
            menu.add_separator()
            # 队列顺序就是引擎取任务的顺序，所以挪位置＝改下载先后
            menu.add_command(label="上移一位",
                             command=lambda: self._move_task(task, -1))
            menu.add_command(label="置顶",
                             command=lambda: self._move_task(task, -len(self._tasks) - 1))
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
        # 有「文件夹名子目录」时，effective_root() 才是真正的落盘根目录
        root_dir = ""
        try:
            if self.downloader is not None:
                root_dir = str(self.downloader.effective_root() or "")
        except Exception:
            root_dir = ""
        if not root_dir:
            try:
                root_dir = getattr(self.downloader, "download_dir", "") or ""
            except Exception:
                root_dir = ""
        if not root_dir:
            root_dir = self._dir_text()
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
                self._open_in_explorer(directory)
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
        for attr in ("_pump_id", "_tick_id", "_filter_job", "_options_job"):
            after_id = getattr(self, attr, None)
            if after_id is not None:
                try:
                    self.root.after_cancel(after_id)
                except Exception:
                    pass
                setattr(self, attr, None)
        # 先记界面状态，再关窗：geometry() 在 destroy 之后就读不到了
        try:
            self._save_ui_state()
        except Exception:
            pass
        try:
            if self.failures is not None:
                self.failures.save()
        except Exception:
            pass
        try:
            self.root.destroy()
        except tk.TclError:
            pass
