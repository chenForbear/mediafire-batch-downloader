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


def process_integrity_level():
    """返回当前进程的 Windows 完整性级别：``'low'`` / ``'medium'`` / ``'high'`` / ``'system'``。

    为什么需要这一步（2026-11-02 实机取证）：
      一个 exe 如果被放在带 ``Mandatory Label\\Low Mandatory Level`` 的目录里
      （例如某些沙箱、受限工作区），Windows 会让它以**低完整性进程**启动。
      低完整性进程写不进普通的中完整性目录，也不能通过 ShellExecute 拉起
      资源管理器——于是表现成「明明有权限却报 Permission denied」「双击报错、
      右键以管理员身份运行反而正常」。程序自己认得出这件事，就能直接把原因
      写进报错里，而不是让用户去猜杀毒软件。

    非 Windows、或系统调用取不到时返回 ``None``（调用方按"未知"处理）。
    """
    if os.name != "nt":
        return None
    rid = _integrity_rid()
    if rid is None:
        return None
    if rid < 0x2000:
        return "low"
    if rid < 0x3000:
        return "medium"
    if rid < 0x4000:
        return "high"
    return "system"


def _integrity_rid():
    """取当前进程完整性级别的 RID（4096=低 / 8192=中 / 12288=高 / 16384=系统）。"""
    try:
        import ctypes
        from ctypes import wintypes

        advapi = ctypes.windll.advapi32
        kernel = ctypes.windll.kernel32

        # 必须显式声明签名：64 位下 getcurrentprocess 返回 (HANDLE)-1，
        # 不声明 restype 会被截成 32 位整数，传进 OpenProcessToken 就变成
        # 0x00000000FFFFFFFF —— 那不是伪句柄，于是整个调用静默失败。
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                            ctypes.POINTER(wintypes.HANDLE)]
        advapi.OpenProcessToken.restype = wintypes.BOOL
        advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                               ctypes.c_void_p, wintypes.DWORD,
                                               ctypes.POINTER(wintypes.DWORD)]
        advapi.GetTokenInformation.restype = wintypes.BOOL

        token = wintypes.HANDLE()
        # TOKEN_QUERY = 0x0008
        if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008,
                                       ctypes.byref(token)):
            return None
        try:
            size = wintypes.DWORD(0)
            # TokenIntegrityLevel = 25；第一次只为问大小，必然失败，属预期
            advapi.GetTokenInformation(token, 25, None, 0, ctypes.byref(size))
            if not size.value:
                return None
            buffer = ctypes.create_string_buffer(size.value)
            if not advapi.GetTokenInformation(token, 25, buffer, size,
                                              ctypes.byref(size)):
                return None
            # TOKEN_MANDATORY_LABEL { SID_AND_ATTRIBUTES Label; }，首字段是 PSID
            sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
            if not sid:
                return None
            # SID 内存布局：Revision(1) / SubAuthorityCount(1) / Authority(6) / ...
            count = ctypes.cast(sid, ctypes.POINTER(ctypes.c_ubyte))[1]
            if not count:
                return None
            advapi.GetSidSubAuthority.restype = ctypes.POINTER(wintypes.DWORD)
            advapi.GetSidSubAuthority.argtypes = [ctypes.c_void_p, wintypes.DWORD]
            pointer = advapi.GetSidSubAuthority(ctypes.c_void_p(sid), count - 1)
            if not pointer:
                return None
            return int(pointer.contents.value)
        finally:
            kernel.CloseHandle(token)
    except Exception:
        return None


def _permission_reason(errno_value=None):
    """把 PermissionError 翻译成一句能照着做的话（低完整性优先解释）。"""
    if process_integrity_level() == "low":
        return ("没有写入权限——程序正以「低完整性」受限模式运行"
                "（多半是被解压进了带限制标签的文件夹，比如沙箱工作区）。"
                "把整个程序文件夹复制到桌面、或 D:\\ 下的普通目录再启动，不用管理员")
    if errno_value is None:
        return "没有写入权限（可能被杀毒软件锁定，或文件夹是只读的）"
    return "没有写入权限（可能被杀毒软件锁定，或文件夹是只读的，errno %s）" % errno_value


def probe_writable_dir(directory):
    """检查一个目录是否真的能写文件，返回 ``(ok, reason)``。

    ``reason`` 是给人看的中文句子，可直接显示在失败清单里。

    为什么需要这一步（实测取证，2026-11-02）：
      ``os.makedirs(..., exist_ok=True)`` 在"目录已存在但没有写入权限"
      时**不会报错**，它会安静地返回——于是程序以为目录可用，直到真正
      ``open(part_path, "wb")`` 才抛 ``[Errno 13] Permission denied``。
      那时错误信息里只有一长串路径，用户看不出到底是"没权限"还是
      "文件被占用"，只能干瞪眼。写一个探测文件就能把这两种情况区分开。
    """
    if not directory:
        return False, "保存目录为空"

    try:
        if not os.path.isdir(directory):
            try:
                os.makedirs(directory, exist_ok=True)
            except PermissionError as exc:
                if process_integrity_level() == "low":
                    return False, ("目录不存在且创建不了——程序正以「低完整性」受限模式运行，"
                                   "把整个程序文件夹复制到桌面或 D:\\ 下再试（errno %s）"
                                   % getattr(exc, "errno", "?"))
                return False, ("目录不存在且没有权限创建（检查上级文件夹是否只读，errno %s）"
                               % getattr(exc, "errno", "?"))
            except OSError as exc:
                return False, "目录不存在且创建失败：%s" % exc
        if not os.path.isdir(directory):
            return False, "路径已存在但不是文件夹"

        probe = os.path.join(directory, ".mfd_write_probe_%d.tmp" % os.getpid())
        try:
            handle = open(probe, "wb")
            try:
                handle.write(b"mfd")
            finally:
                handle.close()
        except PermissionError as exc:
            return False, _permission_reason(getattr(exc, "errno", None))
        except OSError as exc:
            return False, "写入测试失败：%s" % exc
        finally:
            try:
                os.remove(probe)
            except OSError:
                pass

        return True, ""
    except Exception as exc:  # 兜底：探测本身不能把程序搞崩
        return False, "目录检查异常：%s" % exc


def describe_write_error(path, exc):
    """把写入失败的异常翻译成一句能直接照做的话。"""
    directory = os.path.dirname(path) or "."
    name = os.path.basename(path)

    if isinstance(exc, PermissionError):
        ok, reason = probe_writable_dir(directory)
        if not ok:
            return "写不进%s：%s" % (directory, reason)
        if os.path.isdir(path):
            return "同名文件夹已存在，无法写入同名文件：%s" % name
        return ("文件夹可写，但这个名字被系统拒绝（多半是同名文件正被其他程序占用）：%s"
                % name)

    if isinstance(exc, FileNotFoundError):
        return "保存目录不存在：%s" % directory

    if isinstance(exc, OSError):
        code = getattr(exc, "errno", None)
        if code == 28:                      # ENOSPC，两平台一致
            return "磁盘空间不足"
        if code in (36, 38):               # ENAMETOOLONG：Linux 36 / Windows 38
            return "文件名太长，超出系统限制"
        return "写入失败：%s" % exc

    return "写入失败：%s" % exc


def pick_writable_fallback(preferred=""):
    """目录写不进去时，按顺序找一个真能写的位置，返回路径（找不到返回空串）。

    为什么需要这个（2026-11-02 用户实测）：他填的目录明明在资源管理器里能
    建文件，程序却报「没有写入权限」。这类"看起来正常却写不进去"的目录太多
    ——ACL 只读、OneDrive 占位、杀软锁定、移动硬盘掉线、企业策略限制。与其
    让整个下载停摆，不如换到确定能写的地方，并把"换了哪个、为什么换"明确
    告诉用户；人自己会判断要不要再挪回去。

    候选顺序：用户填的目录（再试一次）→ 家目录下 Downloads/Desktop/Documents
    下的子目录 → 数据目录下的 Downloads。最后那个一定可写（程序本来就往那
    里写失败清单）。
    """
    candidates = []
    if preferred:
        candidates.append(preferred)
    home = os.path.expanduser("~")
    if home and home != "~":
        for name in ("Downloads", "Desktop", "Documents"):
            candidates.append(os.path.join(home, name, "MediaFireDownloads"))
    try:
        from . import paths
        candidates.append(paths.data_file("Downloads"))
    except Exception:
        pass
    try:
        import tempfile
        candidates.append(os.path.join(tempfile.gettempdir(), "MediaFireDownloads"))
    except Exception:
        pass

    seen = set()
    for candidate in candidates:
        try:
            key = os.path.normcase(os.path.abspath(candidate))
        except Exception:
            continue
        if key in seen:
            continue
        seen.add(key)
        ok, _ = probe_writable_dir(candidate)
        if ok:
            return candidate
    return ""


