#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""媒体火（MediaFire）批量下载器 —— 通用工具层。

设计约束（与旧版 ``auto_mediafire_v4.py`` 的差异，均对应实际缺陷）：
  * 只用标准库，不依赖任何 C 扩展（``os`` / ``re`` / ``html`` / ``shutil`` / ``unicodedata``）。
    旧版依赖 ``bs4`` + ``chardet`` + ``requests``；其中 ``chardet`` 从未被真正调用，
    ``bs4`` 只用来取一个 ``href``，属于纯粹的体积与兼容性负担。
  * Windows 保留名与非法字符一律净化，避免下载到一半才因非法文件名失败（旧版没有这一步）。
  * 目录/文件路径长度做上限保护，避免绕过 MAX_PATH 后出现难以定位的写入失败。

本模块不发起任何网络请求，纯函数，便于单元测试。
"""

import os
import re

try:  # Python 3.8+
    import unicodedata
except ImportError:  # pragma: no cover - 理论上不可能
    unicodedata = None


# Windows 文件名中明确非法的字符（保留 '/' 与 '\\' 由调用方分开处理）
INVALID_FILENAME_CHARS = '<>:"/\\|?*'

# Windows 设备名：即使带扩展名也不能用作文件名
RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
}

# 单个文件名长度上限。Windows 单文件名为 255 字符（UTF-16 码元），
# 这里留出余量，因为还要拼接目录和 ".part" 临时后缀。
MAX_FILENAME_LENGTH = 180

# 完整路径长度上限。理论上 Win10 1607+ 可用长路径，但需要注册表开关，
# 而 Win7 完全没有；保守按 259 字符（MAX_PATH-1）控制。
MAX_FULL_PATH_LENGTH = 255


def sanitize_filename(name, fallback="unknown_file"):
    """把任意来源的文件名净化成当前系统可安全落盘的名字。

    旧版直接把 URL 末段解码后当文件名，遇到 ``:``、``?`` 或超长日文名就会
    在写入阶段抛 OSError，而且报错信息与"文件名非法"之间没有提示。
    """
    if not name:
        return fallback

    # 1) 去掉路径分隔与控制字符
    cleaned = []
    for ch in name:
        if ch in INVALID_FILENAME_CHARS or ch == "\x00":
            cleaned.append("_")
        elif ord(ch) < 32:
            cleaned.append("_")
        else:
            cleaned.append(ch)
    name = "".join(cleaned)

    # 2) 去掉首尾的空白与点（Windows 不允许文件名以点或空格结尾）
    name = name.strip().strip(".").strip()

    # 3) 折叠连续空白，避免出现怪异的多个空格
    name = re.sub(r"\s+", " ", name)

    if not name:
        return fallback

    # 4) 拆分主名与扩展名，分别做长度与保留名校验
    stem, ext = os.path.splitext(name)
    if not stem:
        stem, ext = name, ""

    if stem.upper() in RESERVED_NAMES:
        stem = stem + "_"

    # 5) 超长时截断主名（保留扩展名，扩展名本身过长则一并截断）
    budget = MAX_FILENAME_LENGTH - len(ext)
    if budget < 16:  # 扩展名异常长，整体截断
        stem, ext = name[: MAX_FILENAME_LENGTH - 8], ""
        budget = MAX_FILENAME_LENGTH
    if len(stem) > budget:
        stem = stem[:budget].rstrip() or "file"

    result = stem + ext
    return result or fallback


def fit_in_directory(directory, filename, suffix=""):
    """在 ``directory`` 下为 ``filename``（可带 ``suffix``）找一个总长度合规的完整路径。

    返回 ``(full_path, final_filename)``。若超长则逐步截断主名。
    """
    limit = MAX_FULL_PATH_LENGTH - len(os.path.abspath(directory)) - 1 - len(suffix)
    if limit < 16:
        # 目录本身已经太深，交给系统报错，比静默改名更好排查
        return os.path.join(directory, filename + suffix), filename + suffix

    stem, ext = os.path.splitext(filename)
    if len(stem) + len(ext) <= limit:
        final = filename
    else:
        keep = max(8, limit - len(ext))
        stem = stem[:keep].rstrip() or "file"
        final = stem + ext
    return os.path.join(directory, final + suffix), final + suffix


def human_size(num_bytes):
    """把字节数转成人类可读字符串，例如 ``153.3 MB``。"""
    try:
        num_bytes = float(num_bytes)
    except (TypeError, ValueError):
        return "未知"
    if num_bytes < 0:
        return "未知"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num_bytes < 1024.0 or unit == "TB":
            if unit == "B":
                return "%d %s" % (int(num_bytes), unit)
            return "%.1f %s" % (num_bytes, unit)
        num_bytes /= 1024.0
    return "未知"


def human_time(seconds):
    """把秒数转成 ``1小时02分03秒`` 这类中文短串。"""
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return "--"
    if seconds < 0:
        return "--"
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return "%d小时%02d分%02d秒" % (h, m, s)
    if m:
        return "%d分%02d秒" % (m, s)
    return "%d秒" % s


def decode_filename_from_url(url):
    """从 MediaFire 直链中取文件名并解码。

    注意：这里必须用 ``unquote`` 而不是 ``unquote_plus``。MediaFire 的直链把
    文件名里的空格编码成 ``%20``，而 ``+`` 是文件名本身的字面字符；
    ``unquote_plus`` 会把合法的 ``+`` 误换成空格。
    """
    from urllib.parse import unquote

    tail = url.split("?")[0].rstrip("/")
    tail = tail.split("/")[-1]
    return sanitize_filename(unquote(tail))


def normalize_spaces(text):
    """全角空格与不换行空格统一成普通空格（日文资源名里很常见）。"""
    if not text:
        return text
    if unicodedata is not None:
        # U+3000 全角空格、U+00A0 不换行空格
        text = text.replace("\u3000", " ").replace("\u00a0", " ")
    return text.strip()
