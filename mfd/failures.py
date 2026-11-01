#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""失败清单：把每一次下载失败记录下来，供「只看失败项、只重试失败项」使用。

为什么单独一层
--------------
批量下载真正让人头疼的不是下载本身，而是「下了一夜，早上发现少了十几个文件，
但不知道少的是哪几个」。旧版（5.0 及更早）失败后只在界面上打一行红字，滚动
几屏就找不到了，也没法复制出去。2.0 把失败变成一等公民：

* 每条失败都留成结构化记录（文件名 / 链接 / 大小 / 所属文件夹 / 错误类型 /
  原始报错 / 尝试次数 / 系统平台 / 时间 / 断点文件路径）；
* 记录按 **错误类型** 与 **操作系统平台** 两维归集，正好对上"兼容性问题按系统
  和报错分别收集"这个要求；
* 可导出 JSON（给程序用）、Markdown（给人看、贴 issue）、纯链接 txt
  （粘回输入框只重试失败的这几个）；
* 下载成功后再自动把对应失败记录删掉，清单里始终只剩"现在还没下成的"。

记录只存去重与排查必需的信息，不存 Cookie、不存请求头，路径只留文件名一级。
"""

import datetime
import json
import os
import re
import sys
import tempfile
import threading

# ----------------------------------------------------------------------
# 错误分类表
# ----------------------------------------------------------------------
# 键是稳定的英文短标识（写进 JSON、可以做统计），值是给用户看的中文。
# 新增类型时请同时补 KIND_HINT（处置建议），诊断报告和 Markdown 导出都用它。

KIND_TEXT = {
    "net_connect": "无法连接服务器",
    "net_timeout": "连接或读取超时",
    "net_reset": "连接被对方重置",
    "net_dns": "域名解析失败",
    "proxy": "代理服务器故障",
    "tls": "TLS/证书校验失败",
    "http_403": "HTTP 403 拒绝访问",
    "http_404": "HTTP 404 文件不存在",
    "http_429": "HTTP 429 请求过于频繁",
    "http_5xx": "服务器内部错误（5xx）",
    "link_expired": "直链已失效",
    "link_missing": "解析不到下载直链",
    "api_error": "MediaFire 接口报错",
    "disk_full": "磁盘空间不足",
    "disk_perm": "没有写入权限",
    "path_too_long": "路径过长",
    "filename": "文件名非法",
    "hash_mismatch": "SHA-256 校验不符",
    "size_mismatch": "文件大小不符",
    "canceled": "已取消",
    "unknown": "未知错误",
}

KIND_HINT = {
    "net_connect": "先确认能打开 mediafire.com；检查代理/VPN 是否拦截，换直连再试。",
    "net_timeout": "降低线程数（1~2）与并发间隔后重试；移动网络或弱网更容易触发。",
    "net_reset": "多为运营商或防火墙中途切断，把线程数降到 1 再试，或换网络环境。",
    "net_dns": "检查 DNS 设置；把 hosts 或 DNS 换成 8.8.8.8 / 223.5.5.5 后重试。",
    "proxy": "检查系统代理设置与代理认证（407）；必要时关闭代理直连。",
    "tls": "系统缺少根证书或 TLS 版本过旧：Win7 需装 SP1 与 SHA-2/根证书更新。",
    "http_403": "直链已过期或文件被设为私密；重新解析链接后再试，仍失败请向分享者确认权限。",
    "http_404": "文件已被上传者删除或移动；重新解析文件夹获取最新列表。",
    "http_429": "请求太密：把线程数降到 1、间隔调到 3 秒以上，等 10 分钟再试。",
    "http_5xx": "MediaFire 服务端临时故障，隔一段时间重试即可。",
    "link_expired": "重新解析链接获取新直链（直链有时效，长时间挂机后必然过期）。",
    "link_missing": "文件页结构变化或需要密码；用浏览器打开该链接确认是否要求密码。",
    "api_error": "按报错码处理：127 文件不存在、128 私密文件、129 链接不是文件夹。",
    "disk_full": "清理目标磁盘或更换下载目录后重试。",
    "disk_perm": "换一个可写目录（不要放 C:\\Program Files 或受控文件夹）。",
    "path_too_long": "缩短下载目录层级或文件名；Windows 默认单路径上限 260 字符。",
    "filename": "文件名含非法字符或命中系统保留名，已在保存时自动净化，通常重试即可。",
    "hash_mismatch": "文件内容与官方校验值不符，已自动删除重下；反复出现请检查代理是否改写了流量。",
    "size_mismatch": "多半是下载被中断，重试会自动从断点续传。",
    "canceled": "用户主动停止，不算失败；重新开始即可继续。",
    "unknown": "请导出诊断报告并附到 issue 里。",
}

# 平台标签缓存（一次进程内不变）
_PLATFORM_CACHE = []

# 允许在一句话里泄露用户名的路径模式，导出前统一掩码
_USER_PATH_PATTERNS = (
    re.compile(r"([A-Za-z]:\\Users\\)([^\\/:*?\"<>|\r\n]+)", re.IGNORECASE),
    re.compile(r"(/home/)([^/\s]+)"),
    re.compile(r"(/Users/)([^/\s]+)"),
)


def platform_label():
    """当前系统的可读标签，例如 ``Windows 10 22H2 (19045)``。

    为什么不用 ``platform.platform()``：它给出的串又长又不稳定（同一台机器
    不同 Python 版本格式还不一样），没法拿来归类统计。这里自己拼一个短标签。
    """
    if _PLATFORM_CACHE:
        return _PLATFORM_CACHE[0]

    label = "未知系统"
    try:
        if sys.platform.startswith("win"):
            ver = sys.getwindowsversion()
            major, minor, build = ver.major, ver.minor, ver.build
            name = "Windows"
            release = ""
            if (major, minor) == (6, 1):
                name = "Windows 7"
            elif (major, minor) == (6, 2):
                name = "Windows 8"
            elif (major, minor) == (6, 3):
                name = "Windows 8.1"
            elif (major, minor) == (10, 0):
                name = "Windows 11" if build >= 22000 else "Windows 10"
                release = _WINDOWS_RELEASE.get(build, "")
            else:
                name = "Windows %d.%d" % (major, minor)
            label = ("%s %s (%d)" % (name, release, build)).replace("  ", " ")
        elif sys.platform == "darwin":
            label = "macOS"
        else:
            label = "Linux"
    except Exception:
        label = "未知系统"

    _PLATFORM_CACHE.append(label)
    return label


# 常见 build 号 → 发行代号（拿不到就只显示 build 号，不影响统计）
_WINDOWS_RELEASE = {
    7601: "SP1",
    10240: "1507", 10586: "1511", 14393: "1607", 15063: "1703",
    16299: "1709", 17134: "1803", 17763: "1809", 18362: "1903",
    18363: "1909", 19041: "2004", 19042: "20H2", 19043: "21H1",
    19044: "21H2", 19045: "22H2",
    22000: "21H2", 22621: "22H2", 22631: "23H2", 26100: "24H2", 26200: "25H2",
}


def mask_text(text):
    """把文本里可能出现的用户名掩掉，避免诊断报告泄露个人信息。"""
    if not text:
        return text
    out = str(text)
    for pattern in _USER_PATH_PATTERNS:
        out = pattern.sub(lambda m: m.group(1) + "***", out)
    home = os.path.expanduser("~")
    if home and home in out:
        out = out.replace(home, "<用户主目录>")
    return out


# ----------------------------------------------------------------------
# 分类
# ----------------------------------------------------------------------

def classify_failure(exc=None, message="", detail=""):
    """把一次失败映射到 ``(kind, hint)``。

    判据优先级：异常类型 → 报错文本里的特征串 → 兜底 unknown。
    为什么还看文本：MediaFire 的错误大多以 ``MediaFireError`` 抛出来，
    真正的区分信息在 ``user_message`` 里（"403"、"校验不通过"、"下载不完整"），
    光看类型分不出来。
    """
    exc_name = type(exc).__name__ if exc is not None else ""
    raw = " ".join([str(message or ""), str(detail or ""), str(exc or "")])
    text = raw.lower()

    # --- 1) 媒体火自身语义（先判，避免被 "http" 之类的字眼抢走） ---
    if "sha256" in text or "校验不通过" in text or "校验不符" in text:
        return "hash_mismatch", KIND_HINT["hash_mismatch"]
    if "下载不完整" in text or "size mismatch" in text or "大小不符" in text:
        return "size_mismatch", KIND_HINT["size_mismatch"]
    if "129" in text and ("api" in text or "接口" in text):
        return "api_error", "链接不是文件夹，也不是有效文件；请确认粘贴的是分享链接。"
    if "128" in text and ("api" in text or "接口" in text or "私密" in text):
        return "api_error", "该文件夹/文件被设为私密：请向分享者索取提取码或公开权限。"
    if "127" in text and ("api" in text or "接口" in text or "不存在" in text):
        return "api_error", "资源不存在或已被删除：重新打开分享页确认。"

    # --- 2) 用户主动停止（不是故障，但也要能归类） ---
    if "cancel" in text or "取消" in text or "用户停止" in text:
        return "canceled", KIND_HINT["canceled"]

    # --- 3) HTTP 状态码 ---
    # 顺序很关键：必须排在"代理/TLS"之前。403 的默认提示语里常带
    # "请检查网络或代理设置"，若先匹配"代理"就会把 403 误判成代理故障。
    if "407" in text:
        return "proxy", "代理要求身份验证（407）：在系统代理里填好账号密码，或改成直连。"
    if " 403" in text or "403 " in text or "拒绝访问" in text:
        return "http_403", KIND_HINT["http_403"]
    if " 404" in text or "404 " in text or "removed" in text or "文件不存在" in text:
        return "http_404", KIND_HINT["http_404"]
    if " 429" in text or "too many requests" in text or "频繁" in text:
        return "http_429", KIND_HINT["http_429"]
    if re.search(r"\b5\d\d\b", text) and ("http" in text or "server" in text or "状态" in text):
        return "http_5xx", KIND_HINT["http_5xx"]

    # --- 4) TLS / 证书 ---
    if (exc_name in ("SSLError", "SSLCertVerificationError", "CertificateError")
            or "certificate" in text or "ssl" in text or "证书" in text):
        return "tls", KIND_HINT["tls"]

    # --- 5) 代理（非 407 的其它代理故障） ---
    if "proxy" in text or "代理" in text:
        return "proxy", KIND_HINT["proxy"]

    # --- 6) 网络异常 ---
    if exc_name in ("gaierror",) or "name or service not known" in text \
            or "getaddrinfo" in text or "解析失败" in text:
        return "net_dns", KIND_HINT["net_dns"]
    if exc_name in ("ConnectionResetError", "ConnectionAbortedError") \
            or "connection reset" in text or "重置" in text or "10054" in text:
        return "net_reset", KIND_HINT["net_reset"]
    if exc_name in ("ConnectionRefusedError",) or "refused" in text or "10061" in text:
        return "net_connect", KIND_HINT["net_connect"]
    if (exc_name in ("timeout", "TimeoutError", "socket.timeout")
            or "timed out" in text or "超时" in text or "10060" in text):
        return "net_timeout", KIND_HINT["net_timeout"]

    # --- 5) 直链解析 ---
    if "直链" in text and ("过期" in text or "失效" in text):
        return "link_expired", KIND_HINT["link_expired"]
    if "直链" in text or "download button" in text or "下载按钮" in text:
        return "link_missing", KIND_HINT["link_missing"]

    # --- 6) 本地文件系统 ---
    if (exc_name in ("FileNotFoundError", "NotADirectoryError")
            and "no such file" in text and "part" not in text):
        return "disk_perm", KIND_HINT["disk_perm"]
    if "no space" in text or "112" == (_errno_of(exc) or "") or "空间不足" in text:
        return "disk_full", KIND_HINT["disk_full"]
    if "permission" in text or "access is denied" in text or "拒绝" in text \
            or _errno_of(exc) in ("13", "5"):
        return "disk_perm", KIND_HINT["disk_perm"]
    if "too long" in text or "206" == (_errno_of(exc) or "") or "路径过长" in text:
        return "path_too_long", KIND_HINT["path_too_long"]
    if exc_name in ("OSError", "IOError", "PermissionError", "FileNotFoundError"):
        return "disk_perm", KIND_HINT["disk_perm"]

    # --- 7) 兜底 ---
    if "cancel" in text or "取消" in text:
        return "canceled", KIND_HINT["canceled"]
    return "unknown", KIND_HINT["unknown"]


def _errno_of(exc):
    """取异常里的 errno / winerror 字符串形式（取不到返回 None）。"""
    if exc is None:
        return None
    for attr in ("errno", "winerror"):
        value = getattr(exc, attr, None)
        if value:
            return str(value)
    return None


def kind_text(kind):
    """``kind`` 的中文说明；未知类型原样返回。"""
    return KIND_TEXT.get(kind, kind or KIND_TEXT["unknown"])


def kind_hint(kind):
    return KIND_HINT.get(kind, KIND_HINT["unknown"])


# ----------------------------------------------------------------------
# 单条失败记录
# ----------------------------------------------------------------------

class FailureRecord(object):
    """一次下载失败的最小可复现信息。"""

    __slots__ = ("name", "url", "size", "quickkey", "folder", "hash_value",
                 "kind", "message", "detail", "attempts", "platform",
                 "created_at", "part_path")

    def __init__(self, name="", url="", size=0, quickkey="", folder="",
                 hash_value="", kind="unknown", message="", detail="",
                 attempts=0, platform="", created_at="", part_path=""):
        self.name = name or ""
        self.url = url or ""
        try:
            self.size = int(size or 0)
        except (TypeError, ValueError):
            self.size = 0
        self.quickkey = quickkey or ""
        self.folder = folder or ""
        self.hash_value = (hash_value or "").strip().upper()
        self.kind = kind or "unknown"
        self.message = mask_text(message or "")
        self.detail = mask_text(detail or "")
        self.attempts = int(attempts or 0)
        self.platform = platform or platform_label()
        self.created_at = created_at or datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.part_path = mask_text(part_path or "")

    # -- 派生信息 -------------------------------------------------------
    @property
    def kind_label(self):
        return kind_text(self.kind)

    @property
    def hint(self):
        return kind_hint(self.kind)

    @property
    def key(self):
        """去重键：优先 quickkey（MediaFire 内部 ID），退回「文件名 + 大小」。"""
        if self.quickkey:
            return "qk:" + self.quickkey
        return "nm:%s|%d" % (self.name.strip().lower(), self.size)

    def to_dict(self):
        return {
            "name": self.name,
            "url": self.url,
            "size": self.size,
            "quickkey": self.quickkey,
            "folder": self.folder,
            "hash": self.hash_value,
            "kind": self.kind,
            "kind_text": self.kind_label,
            "message": self.message,
            "detail": self.detail,
            "attempts": self.attempts,
            "platform": self.platform,
            "created_at": self.created_at,
            "part_path": self.part_path,
        }

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict):
            return None
        return cls(
            name=data.get("name", ""), url=data.get("url", ""),
            size=data.get("size", 0), quickkey=data.get("quickkey", ""),
            folder=data.get("folder", ""), hash_value=data.get("hash", ""),
            kind=data.get("kind", "unknown"), message=data.get("message", ""),
            detail=data.get("detail", ""), attempts=data.get("attempts", 0),
            platform=data.get("platform", ""), created_at=data.get("created_at", ""),
            part_path=data.get("part_path", ""),
        )

    def to_item(self):
        """转成 ``Downloader.load_tasks()`` 能吃的条目，实现"只重试失败项"。"""
        return {
            "name": self.name, "url": self.url, "size": self.size,
            "quickkey": self.quickkey, "folder": self.folder,
            "hash": self.hash_value,
        }

    def __repr__(self):
        return "<FailureRecord %s %s>" % (self.name, self.kind)


# ----------------------------------------------------------------------
# 失败清单
# ----------------------------------------------------------------------

class FailureBook(object):
    """失败清单容器：线程安全、可持久化、可导出。

    为什么落盘而不是只放内存：批量任务常常跨小时，中途关一次程序（或电脑
    休眠出错）就全丢了，而"昨天失败的那 12 个"恰恰是第二天最需要的东西。
    默认路径：``%LOCALAPPDATA%\\MediaFireDownloader2\\failures.json``。
    """

    FILE_NAME = "failures.json"

    def __init__(self, path=None, autosave=True):
        self.path = path or self.default_path()
        self.autosave = bool(autosave)
        self._lock = threading.RLock()
        self._records = {}   # key -> FailureRecord（保持插入顺序）
        self.load()

    # -- 路径 -----------------------------------------------------------
    @staticmethod
    def default_path():
        # 路径只在 mfd.paths 里算一次，避免各处拼出"差一个字符"的目录
        from . import paths
        return paths.data_file(FailureBook.FILE_NAME)

    # -- 增删改查 -------------------------------------------------------
    def add(self, record):
        """加入（或覆盖）一条失败记录，返回该记录。"""
        if record is None:
            return None
        with self._lock:
            old = self._records.get(record.key)
            if old is not None:
                # 同一文件再次失败：保留首次发现时间，累加尝试次数，刷新原因
                record.created_at = old.created_at
                record.attempts = max(record.attempts, old.attempts)
            self._records[record.key] = record
            self._maybe_save()
            return record

    def add_task(self, task, exc=None, kind=None, message="", detail="", part_path=""):
        """从 ``DownloadTask`` + 异常造一条记录（下载引擎内部用）。"""
        if kind is None:
            kind, _hint = classify_failure(exc, message or getattr(task, "error", ""), detail)
        record = FailureRecord(
            name=getattr(task, "name", ""), url=getattr(task, "url", ""),
            size=getattr(task, "size", 0), quickkey=getattr(task, "quickkey", ""),
            folder=getattr(task, "folder", ""), hash_value=getattr(task, "hash_value", ""),
            kind=kind, message=message or getattr(task, "error", ""), detail=detail,
            attempts=getattr(task, "attempts", 0), part_path=part_path,
        )
        return self.add(record)

    def discard(self, quickkey="", name="", size=0):
        """下载成功后移除对应失败记录（清单里只留当下还没下成的）。"""
        key = ("qk:" + quickkey) if quickkey else ("nm:%s|%d" % ((name or "").strip().lower(), int(size or 0)))
        with self._lock:
            removed = self._records.pop(key, None)
            if removed is not None:
                self._maybe_save()
            return removed is not None

    def remove(self, key):
        with self._lock:
            if key in self._records:
                del self._records[key]
                self._maybe_save()
                return True
        return False

    def clear(self):
        with self._lock:
            count = len(self._records)
            self._records = {}
            self._maybe_save()
            return count

    def get(self, key):
        with self._lock:
            return self._records.get(key)

    @property
    def records(self):
        with self._lock:
            return list(self._records.values())

    def __len__(self):
        with self._lock:
            return len(self._records)

    def __contains__(self, key):
        with self._lock:
            return key in self._records

    # -- 两维归集 -------------------------------------------------------
    def by_kind(self):
        """按错误类型分组（第一维）。"""
        groups = {}
        for record in self.records:
            groups.setdefault(record.kind, []).append(record)
        return groups

    def by_platform(self):
        """按操作系统分组（第二维）。"""
        groups = {}
        for record in self.records:
            groups.setdefault(record.platform, []).append(record)
        return groups

    def cross_tab(self):
        """平台 × 错误类型 的二维计数表，诊断报告里的 ``cross_tab`` 就是它。"""
        table = {}
        with self._lock:
            for record in self._records.values():
                row = table.setdefault(record.platform, {})
                row[record.kind] = row.get(record.kind, 0) + 1
        return table

    def summary(self):
        """一句话摘要，界面状态栏用。"""
        total = len(self)
        if not total:
            return "没有失败的下载。"
        kinds = self.by_kind()
        top = sorted(kinds.items(), key=lambda kv: -len(kv[1]))[:3]
        detail = "、".join("%s %d 个" % (kind_text(k), len(v)) for k, v in top)
        return "失败 %d 个（%s）" % (total, detail)

    def to_items(self):
        """把所有失败项转成可再次排队的条目列表。"""
        return [record.to_item() for record in self.records]

    # -- 导出 -----------------------------------------------------------
    def export_json(self, path=None):
        """导出为 JSON。``path`` 为空时只返回字符串。"""
        payload = {
            "schema": 1,
            "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "platform": platform_label(),
            "total": len(self),
            "records": [record.to_dict() for record in self.records],
            "cross_tab": self.cross_tab(),
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        if path:
            _atomic_write(path, text)
        return text

    def export_markdown(self, path=None):
        """导出为 Markdown（贴 issue / 发给别人看用）。"""
        lines = ["# MediaFire 下载失败清单", ""]
        lines.append("- 生成时间：%s" % datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        lines.append("- 本机系统：%s" % platform_label())
        lines.append("- 失败条目：%d 个" % len(self))
        lines.append("")

        lines.append("## 按错误类型分组")
        lines.append("")
        if not self.by_kind():
            lines.append("（无）")
        for kind, records in sorted(self.by_kind().items(), key=lambda kv: -len(kv[1])):
            lines.append("### %s —— %d 个" % (kind_text(kind), len(records)))
            lines.append("")
            lines.append("> 处置建议：%s" % kind_hint(kind))
            lines.append("")
            lines.append("| 文件名 | 大小 | 所属文件夹 | 尝试 | 时间 | 原始报错 |")
            lines.append("| --- | --- | --- | --- | --- | --- |")
            for record in records:
                lines.append("| %s | %s | %s | %d | %s | %s |" % (
                    _md_cell(record.name), _size_text(record.size),
                    _md_cell(record.folder or "（根目录）"), record.attempts,
                    record.created_at, _md_cell(record.message),
                ))
            lines.append("")

        lines.append("## 按操作系统分组")
        lines.append("")
        for platform, records in sorted(self.by_platform().items()):
            counts = {}
            for record in records:
                counts[record.kind] = counts.get(record.kind, 0) + 1
            detail = "、".join("%s %d" % (kind_text(k), n) for k, n in sorted(counts.items()))
            lines.append("- **%s**：%d 个（%s）" % (platform, len(records), detail))
        lines.append("")

        lines.append("## 失败链接（粘回程序即可只重试这些）")
        lines.append("")
        for record in self.records:
            lines.append("```")
            lines.append("%s" % (record.url or record.quickkey or record.name))
            lines.append("```")
        lines.append("")
        text = "\n".join(lines)
        if path:
            _atomic_write(path, text)
        return text

    def export_links_txt(self, path=None):
        """只导出链接，每行一条——粘到输入框就是"只重试失败项"。"""
        lines = []
        for record in self.records:
            target = record.url or record.quickkey
            if target:
                lines.append(target)
        text = "\n".join(lines) + ("\n" if lines else "")
        if path:
            _atomic_write(path, text)
        return text

    # -- 持久化 ---------------------------------------------------------
    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (IOError, OSError, ValueError):
            return 0
        raw = payload.get("records") if isinstance(payload, dict) else payload
        if not isinstance(raw, list):
            return 0
        loaded = 0
        with self._lock:
            self._records = {}
            for item in raw:
                record = FailureRecord.from_dict(item)
                if record is not None:
                    self._records[record.key] = record
                    loaded += 1
        return loaded

    def save(self, path=None):
        target = path or self.path
        payload = {
            "schema": 1,
            "updated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "records": [record.to_dict() for record in self.records],
        }
        _atomic_write(target, json.dumps(payload, ensure_ascii=False, indent=1))
        return target

    def _maybe_save(self):
        if not self.autosave:
            return
        try:
            self.save()
        except OSError:
            # 清单写不进去不能反过来打断下载；本次运行仍在内存里有效
            pass


# ----------------------------------------------------------------------
# 小工具
# ----------------------------------------------------------------------

def _atomic_write(path, text):
    """先写临时文件再替换，避免断电留下半截文件。

    这里刻意**不用** ``tempfile.mkstemp``：它内部按 ``O_CREAT|O_EXCL`` 打开，
    在"目录可写但受限"的环境里既可能抛 PermissionError，也可能长时间卡住
    （实测在受限令牌下卡死，程序关窗时无法退出）。普通 ``open(..., "w")``
    失败会立刻报错，反而更好排查；临时文件名带 pid 与线程号，互不覆盖。
    """
    import threading

    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError:
            pass
    tmp_path = "%s.%d.%d.tmp" % (
        path, os.getpid(), threading.current_thread().ident or 0)
    try:
        with open(tmp_path, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp_path, path)
    except OSError:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def _md_cell(text):
    """Markdown 表格单元格转义：竖线与换行会让表格错位。"""
    return (str(text or "")).replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def _size_text(size):
    from .util import human_size
    return human_size(size) if size else "未知"
