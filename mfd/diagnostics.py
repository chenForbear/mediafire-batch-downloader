#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""兼容性诊断：把「这台机器上的问题」按 **操作系统 × 错误类型** 两维归集。

为什么要这个模块
----------------
批量下载失败时，用户最需要知道的不是"失败了 37 个"，而是：
    · 这 37 个是同一个原因（换网络/换直链就能全好），还是各不相同？
    · 换一台 Win7 的机器，是不是就只出某几类错？
所以本模块把每条失败同时打到两个坐标轴上——平台一列、错误类型一行——
生成一张交叉表。单看任何一维都看不出规律，两维一起看，问题往往自己就现形了。

隐私红线（硬约束）
------------------
* 不联网：本模块一个网络调用都没有。
* 不读浏览器数据、不读剪贴板、不读账号信息。
* 所有含用户名的路径一律经 ``failures.mask_text`` 掩码成 ``***/<用户主目录>``。
* 只采集"排故障需要的最小集合"：系统版本、Python/Tk/SSL 版本、代理开关、
  证书路径、磁盘余量、内存、DPI、中文字体是否存在。

兼容性
------
只用标准库；Windows 专有的读取（注册表、内存、DPI）全部用 try/except 兜住，
在 Linux/macOS 上照样能 import、能出报告，只是对应字段为空。
"""

import ctypes
import json
import locale
import os
import platform
import shutil
import socket
import ssl
import struct
import sys
import threading
import time

from .failures import (KIND_HINT, KIND_TEXT, FailureBook, mask_text,
                       platform_label)

SCHEMA = 1

# 错误类型 → 大类。大类只有这 8 个，界面上按它分组显示。
CATEGORY_TEXT = {
    "network": "网络",
    "tls": "TLS / 证书",
    "http": "HTTP 协议",
    "parse": "链接与接口解析",
    "filesystem": "文件系统",
    "encoding": "编码",
    "ui": "界面",
    "unknown": "未知",
}

# 一个失败 kind 归到哪个大类（kind 清单见 failures.KIND_TEXT，共 21 类）
KIND_CATEGORY = {
    "net_connect": "network",
    "net_timeout": "network",
    "net_reset": "network",
    "net_dns": "network",
    "proxy": "network",
    "tls": "tls",
    "http_403": "http",
    "http_404": "http",
    "http_429": "http",
    "http_5xx": "http",
    "link_expired": "parse",
    "link_missing": "parse",
    "api_error": "parse",
    "disk_full": "filesystem",
    "disk_perm": "filesystem",
    "path_too_long": "filesystem",
    "filename": "filesystem",
    "hash_mismatch": "filesystem",
    "size_mismatch": "filesystem",
    "canceled": "unknown",
    "unknown": "unknown",
}

_EVENT_LIMIT = 500          # 事件表上限：够看清规律，又不至于报告文件炸掉


# ---------------------------------------------------------------------------
# 异常 / 失败类型 → (大类, 代码, 处置建议)
# ---------------------------------------------------------------------------

def classify(kind_or_exception):
    """把「失败类型字符串」或「异常对象」映射成 ``(category, code, hint)``。

    传字符串时按 failures.KIND_TEXT 的口径解释（那是全项目唯一的错误词表）；
    传异常时按异常类型与 errno 判断，最后回落到 common 类型名。
    任何输入都不应抛异常——诊断代码自己崩掉是最难看的失败方式。
    """
    if kind_or_exception is None:
        return "unknown", "unknown", ""

    if isinstance(kind_or_exception, str):
        kind = kind_or_exception.strip() or "unknown"
        return (KIND_CATEGORY.get(kind, "unknown"), kind,
                KIND_HINT.get(kind, ""))

    exc = kind_or_exception

    # 1) MediaFire 自己的异常：它带有给用户看的文案，先按文案分类
    user_message = getattr(exc, "user_message", None)
    if user_message:
        try:
            from .failures import classify_failure
            kind, hint = classify_failure(exc, str(user_message),
                                          str(getattr(exc, "detail", "") or ""))
            return KIND_CATEGORY.get(kind, "unknown"), kind, hint
        except Exception:
            return "unknown", "unknown", str(user_message)

    name = type(exc).__name__

    # 2) TLS / 证书
    if name in ("SSLError", "SSLCertVerificationError", "CertificateError") \
            or isinstance(exc, getattr(ssl, "SSLError", ())):
        return "tls", "tls", KIND_HINT.get("tls", "")

    # 3) 网络
    if isinstance(exc, socket.timeout) or name in ("timeout", "TimeoutError"):
        return "network", "net_timeout", KIND_HINT.get("net_timeout", "")
    if isinstance(exc, socket.gaierror):
        return "network", "net_dns", KIND_HINT.get("net_dns", "")
    if isinstance(exc, (ConnectionResetError, ConnectionAbortedError)):
        return "network", "net_reset", KIND_HINT.get("net_reset", "")
    if isinstance(exc, ConnectionRefusedError):
        return "network", "net_connect", KIND_HINT.get("net_connect", "")

    # 4) 编码
    if isinstance(exc, (UnicodeDecodeError, UnicodeEncodeError)):
        return "encoding", "encoding", "文件名或页面编码异常：请把系统区域设置与 UTF-8 选项的情况附在报告里。"

    # 5) 界面（tkinter 不导入也能判断）
    if name == "TclError":
        return "ui", "ui", "界面层报错：多为字体缺失或窗口已销毁，重开一次程序即可。"

    # 6) 文件系统（按 errno 判，最准）
    if isinstance(exc, OSError):
        errno_value = getattr(exc, "errno", None)
        if errno_value in (28,):                       # ENOSPC
            return "filesystem", "disk_full", KIND_HINT.get("disk_full", "")
        if errno_value in (13, 1):                     # EACCES / EPERM
            return "filesystem", "disk_perm", KIND_HINT.get("disk_perm", "")
        if errno_value in (36, 63, 206):               # ENAMETOOLONG 各平台
            return "filesystem", "path_too_long", KIND_HINT.get("path_too_long", "")
        if errno_value in (2, 3):                      # ENOENT / ESRCH
            return "filesystem", "link_missing", "目标路径不存在：确认下载目录还在（U 盘是否拔了）。"
        if name in ("PermissionError",):
            return "filesystem", "disk_perm", KIND_HINT.get("disk_perm", "")

    # 7) 兜底
    return "unknown", name or "unknown", "未知错误：请把诊断报告与日志片段一起附上。"


def category_text(category):
    return CATEGORY_TEXT.get(category, category or "未知")


# ---------------------------------------------------------------------------
# 环境采集
# ---------------------------------------------------------------------------

class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def _bytes_to_mb(value):
    try:
        return round(float(value) / (1024.0 * 1024.0), 1)
    except (TypeError, ValueError):
        return None


def _windows_memory():
    """返回 {'total_mb':x,'avail_mb':y,'load_percent':z}；非 Windows 或失败返回 {}。"""
    if os.name != "nt":
        return {}
    try:
        status = _MEMORYSTATUSEX()
        status.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return {}
        return {
            "total_mb": _bytes_to_mb(status.ullTotalPhys),
            "avail_mb": _bytes_to_mb(status.ullAvailPhys),
            "load_percent": int(status.dwMemoryLoad),
        }
    except Exception:
        return {}


def _windows_proxy():
    """读当前用户的系统代理设置。只读那几个开关，不碰任何凭据。"""
    if os.name != "nt":
        result = {}
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"):
            value = os.environ.get(name) or os.environ.get(name.lower())
            if value:
                result[name.lower()] = mask_text(value)
        return result
    try:
        import winreg
    except ImportError:
        return {}
    path = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
    result = {}
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
            for name in ("ProxyEnable", "ProxyServer", "ProxyOverride", "AutoConfigURL"):
                try:
                    value, _kind = winreg.QueryValueEx(key, name)
                except OSError:
                    continue
                result[name] = mask_text(str(value)) if isinstance(value, str) else value
    except OSError:
        return {}
    # ProxyOverride 常常很长，报告里只留条数与是否含媒体火域名
    override = str(result.get("ProxyOverride") or "")
    if override:
        parts = [part for part in override.split(";") if part]
        result["ProxyOverride_count"] = len(parts)
        result["ProxyOverride_mediafire_bypass"] = any(
            "mediafire" in part.lower() for part in parts)
        result.pop("ProxyOverride", None)
    return result


def _dpi():
    if os.name != "nt":
        return None
    try:
        user32 = ctypes.windll.user32
        try:
            return int(user32.GetDpiForSystem())
        except AttributeError:
            # Win7/8.1 没有 GetDpiForSystem，用桌面 DC 的水平分辨率代替
            gdi32 = ctypes.windll.gdi32
            dc = user32.GetDC(0)
            try:
                return int(gdi32.GetDeviceCaps(dc, 88))   # LOGPIXELSX
            finally:
                user32.ReleaseDC(0, dc)
    except Exception:
        return None


def _fonts():
    """只判断"关键中文字体在不在"，不去枚举全部字体（快且不触发 Tk）。"""
    if os.name != "nt":
        return {}
    root = os.path.join(os.environ.get("WINDIR") or r"C:\Windows", "Fonts")
    wanted = {
        "msyh": "微软雅黑",
        "msyhbd": "微软雅黑 Bold",
        "simsun": "宋体",
        "simhei": "黑体",
        "consola": "Consolas",
        "cascadiamono": "Cascadia Mono",
    }
    result = {}
    try:
        files = {name.lower() for name in os.listdir(root)}
    except OSError:
        return {}
    for stem, label in wanted.items():
        result[label] = any(name.startswith(stem) for name in files)
    return result


def _tk_version():
    try:
        import tkinter
        return {"tk": str(tkinter.TkVersion), "tcl": str(tkinter.TclVersion)}
    except Exception as exc:
        return {"tk": None, "tcl": None, "error": str(exc)}


def _ssl_info():
    info = {}
    try:
        info["openssl"] = ssl.OPENSSL_VERSION
    except Exception:
        info["openssl"] = None
    try:
        paths = ssl.get_default_verify_paths()
        info["cafile"] = mask_text(paths.cafile or paths.openssl_cafile or "")
        info["capath"] = mask_text(paths.capath or paths.openssl_capath or "")
    except Exception:
        pass
    try:
        # 有证书库 ≠ 证书能用：数一数默认上下文里到底装了几张
        context = ssl.create_default_context()
        stats = context.cert_store_stats()
        info["certificates"] = stats.get("x509", 0)
    except Exception as exc:
        info["certificates_error"] = str(exc)[:120]
    return info


def collect_environment():
    """采集排故障需要的环境信息（纯本地、只读、已掩码）。"""
    uname = platform.uname()
    env = {
        "platform": platform_label(),
        "system": uname.system,
        "release": uname.release,
        "version": uname.version,
        "machine": uname.machine,
        "processor": uname.processor,
        "python": sys.version.split()[0],
        "python_bits": struct.calcsize("P") * 8,
        "frozen": bool(getattr(sys, "frozen", False)),
        "executable": mask_text(sys.executable or ""),
        "filesystem_encoding": sys.getfilesystemencoding(),
        "stdio_encoding": (getattr(sys.stdout, "encoding", None) or ""),
    }
    try:
        env["preferred_encoding"] = locale.getpreferredencoding(False)
    except Exception:
        env["preferred_encoding"] = None
    try:
        env["locale"] = ".".join(str(part) for part in locale.getdefaultlocale() if part)
    except Exception:
        env["locale"] = None

    env.update(_tk_version())
    env["ssl"] = _ssl_info()
    env["proxy"] = _windows_proxy()
    env["memory"] = _windows_memory()
    env["dpi"] = _dpi()
    env["fonts"] = _fonts()

    try:
        usage = shutil.disk_usage(os.path.expanduser("~"))
        env["disk"] = {
            "total_gb": round(usage.total / 1024.0 ** 3, 1),
            "free_gb": round(usage.free / 1024.0 ** 3, 1),
        }
    except Exception:
        env["disk"] = {}

    try:
        env["hostname_masked"] = True
        env["cwd_exists"] = os.path.isdir(os.getcwd())
        env["cwd"] = mask_text(os.getcwd())
    except Exception:
        pass
    return env


# ---------------------------------------------------------------------------
# 诊断器
# ---------------------------------------------------------------------------

class Diagnostics(object):
    """收集诊断事件并按两维归集。

    线程安全：下载线程会调 ``note()``，界面线程会调 ``build_report()``，
    所以内部用一把锁护住事件表。
    """

    def __init__(self, platform_name=None):
        self._lock = threading.RLock()
        self._events = {}                      # key -> event dict
        self._order = []                       # 保持首次出现顺序
        self.platform = platform_name or platform_label()
        self._environment = None

    # -- 记录 ---------------------------------------------------------------
    def note(self, category, code, message="", context=""):
        """记一条诊断事件；同 (category, code, message) 只累加次数。"""
        category = str(category or "unknown")
        code = str(code or "unknown")
        message = str(message or "")[:400]
        context = str(context or "")[:600]
        key = (category, code, message)
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            event = self._events.get(key)
            if event is None:
                if len(self._order) >= _EVENT_LIMIT:
                    return None
                event = {
                    "category": category,
                    "category_text": category_text(category),
                    "code": code,
                    "code_text": KIND_TEXT.get(code, code),
                    "message": message,
                    "context": mask_text(context),
                    "platform": self.platform,
                    "count": 0,
                    "first_at": now,
                    "last_at": now,
                }
                self._events[key] = event
                self._order.append(key)
            event["count"] += 1
            event["last_at"] = now
            if context:
                event["context"] = mask_text(context)
            return dict(event)

    def note_failure(self, record):
        """把一条 FailureRecord 记成诊断事件（两维直接来自记录本身）。"""
        if record is None:
            return None
        kind = getattr(record, "kind", "unknown")
        category = KIND_CATEGORY.get(kind, "unknown")
        return self.note(category, kind,
                         message=getattr(record, "message", "") or "",
                         context="%s | %s" % (getattr(record, "name", ""),
                                              getattr(record, "detail", "") or ""))

    def note_exception(self, exc, context=""):
        category, code, _hint = classify(exc)
        return self.note(category, code, message=str(exc)[:400], context=context)

    # -- 读取 ---------------------------------------------------------------
    @property
    def events(self):
        with self._lock:
            return [dict(self._events[key]) for key in self._order]

    def __len__(self):
        with self._lock:
            return len(self._order)

    def clear(self):
        with self._lock:
            self._events.clear()
            del self._order[:]

    def collect_environment(self, refresh=False):
        """采集（并缓存）环境信息。"""
        with self._lock:
            if self._environment is None or refresh:
                self._environment = collect_environment()
            return dict(self._environment)

    # -- 两维交叉 -----------------------------------------------------------
    def cross_tab(self):
        """平台 × 错误类型 的二维计数：``{平台: {错误代码: 次数}}``。"""
        table = {}
        with self._lock:
            for key in self._order:
                event = self._events[key]
                row = table.setdefault(event["platform"], {})
                row[event["code"]] = row.get(event["code"], 0) + event["count"]
        return table

    def category_tab(self):
        """大类 → 次数（给界面画一个"主要矛盾"排序）。"""
        table = {}
        with self._lock:
            for key in self._order:
                event = self._events[key]
                table[event["category"]] = table.get(event["category"], 0) + event["count"]
        return table

    def headline(self):
        """一句话结论：出现最多的大类是哪一类。"""
        table = self.category_tab()
        if not table:
            return "暂无诊断事件"
        category = max(table, key=lambda key: table[key])
        return "主要问题集中在「%s」（%d 次）" % (category_text(category), table[category])

    # -- 报告 ---------------------------------------------------------------
    def build_report(self, failures=None):
        return build_report(self, failures=failures)

    def save_report(self, failures=None, directory=None):
        """把报告写成 JSON，返回文件路径（失败返回空串，绝不抛）。"""
        report = self.build_report(failures=failures)
        text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=False)
        target_dir = directory or _default_report_dir()
        if not target_dir:
            return ""
        try:
            os.makedirs(target_dir, exist_ok=True)
            name = "report-%s.json" % time.strftime("%Y%m%d-%H%M%S")
            path = os.path.join(target_dir, name)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(text)
            return path
        except OSError:
            return ""


def _default_report_dir():
    from . import paths
    return paths.data_file("diagnostics")


def build_report(diag, failures=None):
    """把诊断器与失败清单合成一份可序列化的报告 dict。"""
    environment = {}
    events = []
    cross = {}
    categories = {}
    headline = ""

    if diag is not None:
        try:
            environment = diag.collect_environment()
        except Exception as exc:
            environment = {"error": str(exc)}
        try:
            events = diag.events
        except Exception:
            events = []
        try:
            cross = diag.cross_tab()
        except Exception:
            cross = {}
        try:
            categories = diag.category_tab()
        except Exception:
            categories = {}
        try:
            headline = diag.headline()
        except Exception:
            headline = ""

    failures_summary = {}
    failure_rows = []
    if failures is not None:
        try:
            failures_summary = failures.summary()
        except Exception:
            failures_summary = {}
        try:
            failure_rows = [record.to_dict() for record in failures.records]
        except Exception:
            failure_rows = []
        # 失败清单自带两维表；合并进报告，界面缺事件时也能看到交叉统计
        try:
            for name, row in (failures.cross_tab() or {}).items():
                merged = cross.setdefault(name, {})
                for code, count in row.items():
                    merged[code] = merged.get(code, 0) + int(count)
        except Exception:
            pass

    report = {
        "schema": SCHEMA,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "app": {
            "name": _app_name(),
            "version": _app_version(),
            # 用诊断器自己的平台标签：事件交叉表的行名与这里保持一致，
            # 报告里才不会出现「两处写着不一样的系统」这种自相矛盾。
            "platform": getattr(diag, "platform", None) or platform_label(),
        },
        "headline": headline,
        "category_totals": categories,
        "category_text": CATEGORY_TEXT,
        "cross_tab": cross,
        "events": events,
        "environment": environment,
        "failures_summary": failures_summary,
        "failures": failure_rows,
        "privacy": "所有含用户名的路径已掩码为 ***/<用户主目录>；本报告不含账号、Cookie、浏览器数据。",
    }
    return report


def _app_name():
    try:
        from . import APP_NAME
        return APP_NAME
    except Exception:
        return "MediaFire 批量下载器"


def _app_version():
    try:
        from . import __version__
        return __version__
    except Exception:
        return ""


def quick_check():
    """一次性自检：给界面「诊断」页和命令行都用得上。"""
    diag = Diagnostics()
    problems = []
    env = diag.collect_environment()
    if env.get("ssl", {}).get("certificates") in (0, None):
        problems.append("证书库为空或读不到：https 请求可能全部失败")
    if env.get("proxy", {}).get("ProxyEnable") and \
            not env.get("proxy", {}).get("ProxyOverride_mediafire_bypass"):
        problems.append("系统代理已开启，且未把 mediafire.com 排除在代理之外")
    if env.get("disk", {}).get("free_gb") is not None and env["disk"]["free_gb"] < 0.5:
        problems.append("磁盘剩余不足 0.5 GB")
    if env.get("fonts") and not env["fonts"].get("微软雅黑"):
        problems.append("缺少微软雅黑：界面会退化成宋体，显示发虚")
    return {"ok": not problems, "problems": problems, "environment": env}
