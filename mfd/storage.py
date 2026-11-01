#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""下载记录（去重表）——默认写进 Windows 注册表，不产生任何文件。

为什么不再写文件
----------------
旧版把 ``MediaHistory.txt`` 写在 exe 同目录，有三个问题：

1. **痕迹**：桌面/下载夹里凭空多出一个 txt，重装或换电脑后才发现，删了还会再生成。
   用户明确要求"不必在桌面生成 history 之类的文件，内置在程序里更好"。
2. **隐私**：它是明文，每行一条完整 URL（含网盘文件名）。放在 OneDrive 同步目录里
   等于把你的下载清单同步到云端和其它设备。
3. **权限**：exe 若放在 ``C:\\Program Files``，普通用户没有写权限，旧版会在
   下载完成后因写历史失败而报错。

新版的存储策略（按优先级自动选）
--------------------------------
    winreg  →  ``HKCU\\Software\\MediaFireDownloader2`` 下的一个值
    JSON 文件 → ``%LOCALAPPDATA%\\MediaFireDownloader2\\download_history.json``

* **首选注册表**：属于程序自身的配置区（和绝大多数 Windows 软件一样），不在用户的
  桌面/文档/下载目录里留任何可见文件，也天然跟着当前用户走，不需要管理员权限。
* **写入注册表失败时**（极少数被策略锁定的环境）自动退回 ``%LOCALAPPDATA%`` 下的
  JSON 文件，并在界面上如实告知落到了哪里——用户要的是"知道东西在哪"，不是
  "看不见就算了"。
* **无痕模式**：界面提供开关，打开后本次运行完全不做任何记录、也不写注册表。

记录内容也只留去重所需的最小字段：文件名、大小、quickkey、时间。
**不记录完整直链**（直链本身带会话校验信息，属敏感数据），
**不记录本机完整路径**。
"""

import json
import os
import sys
import tempfile
import time

from .util import human_size

APP_NAME = "MediaFireDownloader2"
REGISTRY_PATH = r"Software\%s" % APP_NAME
REGISTRY_VALUE = "DownloadRecords"
HISTORY_FILE_NAME = "download_history.json"
HISTORY_VERSION = 3
MAX_ENTRIES = 5000


# ----------------------------------------------------------------------
# 后端：注册表
# ----------------------------------------------------------------------

class RegistryStore(object):
    """把记录存进 ``HKCU`` 注册表的一个字符串值里。

    为什么用"一个值装整段 JSON"而不是"每条记录一个子键"：整段替换是单次原子操作，
    不会出现"删了两条、还剩一半"的中间状态；条数在几千级时性能完全够用。
    """

    name = "registry"

    def load(self):
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGISTRY_PATH, 0,
                                winreg.KEY_READ) as key:
                raw, _kind = winreg.QueryValueEx(key, REGISTRY_VALUE)
        except FileNotFoundError:
            return None
        except OSError:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        try:
            return json.loads(raw)
        except ValueError:
            # 注册表里的内容被外部改坏了：当作空表，不阻塞下载
            return None

    def save(self, payload):
        import winreg
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, REGISTRY_PATH, 0,
                                winreg.KEY_WRITE | winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, REGISTRY_VALUE, 0, winreg.REG_SZ, data)

    def describe(self):
        return "Windows 注册表（不产生文件）"


# ----------------------------------------------------------------------
# 后端：JSON 文件（兜底）
# ----------------------------------------------------------------------

class JsonFileStore(object):
    """``%LOCALAPPDATA%\\MediaFireDownloader2\\download_history.json``。

    ``LOCALAPPDATA`` 不参与漫游、不会被 OneDrive 同步，是 Windows 上放这类
    用户级缓存的规范位置；相比旧版的"exe 同目录"既不会污染用户目录，
    也不受安装位置写权限影响。
    """

    name = "json"

    def __init__(self, path=None):
        self.path = path or self.default_path()

    @staticmethod
    def default_path():
        # 与失败清单、诊断报告共用 mfd.paths 算出来的目录，别再各拼一份
        from . import paths
        return paths.data_file(HISTORY_FILE_NAME)

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except (IOError, OSError, ValueError):
            return None

    def save(self, payload):
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        # 先写临时文件再原子替换，避免断电/崩溃留下半截 JSON
        fd, tmp_path = tempfile.mkstemp(prefix=".mfd_hist_", dir=directory or None)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=1)
            os.replace(tmp_path, self.path)
        except OSError:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            raise

    def describe(self):
        return "本地文件 %s" % self.path


def pick_store(preferred="auto"):
    """挑一个可用的后端，并实测"能写"再定。

    只判断"能不能导入 winreg"是不够的：某些企业策略会允许读、拒绝写。
    这里直接试写一次（写入当前已有的内容，等于无副作用），失败才退到文件后端。
    """
    if preferred == "file":
        return JsonFileStore(), ""
    if sys.platform != "win32":
        return JsonFileStore(), "当前不是 Windows，记录改用本地文件保存。"

    store = RegistryStore()
    try:
        payload = store.load()
        store.save(payload if isinstance(payload, dict) else _empty_payload())
        return store, ""
    except Exception as exc:
        fallback = JsonFileStore()
        return fallback, ("注册表不可写（%s），记录已改用 %s。"
                          % (exc, fallback.describe()))


def _empty_payload():
    return {"version": HISTORY_VERSION, "updated": 0, "entries": []}


# ----------------------------------------------------------------------
# 记录表
# ----------------------------------------------------------------------

class History(object):
    """去重表。以「文件名 + 大小」为键，quickkey 命中时优先按 quickkey 判定。"""

    def __init__(self, store=None, backend="auto", enabled=True, path=None):
        self.enabled = bool(enabled)
        self._note = ""
        if store is not None:
            self.store = store
        elif path is not None:
            self.store = JsonFileStore(path)
        else:
            self.store, self._note = pick_store(backend)
        self._data = _empty_payload()
        self._keys = set()
        self._quickkeys = set()
        if self.enabled:
            self.load()

    # -- 元信息 ---------------------------------------------------------
    def describe(self):
        if not self.enabled:
            return "无痕模式（本次运行不记录）"
        return self.store.describe()

    @property
    def note(self):
        return self._note

    @property
    def path(self):
        """仅文件后端有意义的路径（界面显示用）。"""
        return getattr(self.store, "path", "")

    # -- 持久化 ---------------------------------------------------------
    def load(self):
        raw = self.store.load()
        entries = raw.get("entries") if isinstance(raw, dict) else None
        if not isinstance(entries, list):
            return
        self._data["entries"] = [e for e in entries if isinstance(e, dict)]
        self._data["updated"] = raw.get("updated", 0) if isinstance(raw, dict) else 0
        self._rebuild_index()

    def save(self):
        if not self.enabled:
            return
        self._data["version"] = HISTORY_VERSION
        self._data["updated"] = int(time.time())
        self.store.save(self._data)

    def _rebuild_index(self):
        self._keys = set()
        self._quickkeys = set()
        for entry in self._data["entries"]:
            self._keys.add(self._make_key(entry.get("name", ""), entry.get("size", 0)))
            key = entry.get("quickkey")
            if key:
                self._quickkeys.add(key)

    # -- 查询与更新 -----------------------------------------------------
    @staticmethod
    def _make_key(name, size):
        return "%s|%d" % ((name or "").strip().lower(), int(size or 0))

    def has(self, name, size, quickkey=""):
        """该文件是否已经下载成功过。

        判定顺序：quickkey（MediaFire 内部 ID，最稳）→ 文件名 + 大小。
        旧版按整份历史做 URL 子串匹配：直链每次都会变，按 URL 去重等于没去重。
        只按文件名也不够——同名不同内容会被误判为已下载；所以必须带上大小。
        """
        if not self.enabled:
            return False
        if quickkey and quickkey in self._quickkeys:
            return True
        return self._make_key(name, size) in self._keys

    def add(self, name, size, quickkey="", folder_key="", saved_path=""):
        if not self.enabled:
            return False
        key = self._make_key(name, size)
        if quickkey and quickkey in self._quickkeys:
            return False
        if key in self._keys:
            return False
        self._data["entries"].append({
            "name": name,
            "size": int(size or 0),
            "size_text": human_size(size),
            "quickkey": quickkey or "",
            "folder": folder_key or "",
            # 只留目录名，不留本机完整路径
            "dir": os.path.basename(os.path.dirname(saved_path)) if saved_path else "",
            "time": int(time.time()),
        })
        self._keys.add(key)
        if quickkey:
            self._quickkeys.add(quickkey)
        if len(self._data["entries"]) > MAX_ENTRIES:
            del self._data["entries"][: len(self._data["entries"]) - MAX_ENTRIES]
            self._rebuild_index()
        return True

    def entries(self):
        return list(self._data["entries"])

    def remove_entry(self, index):
        """删除第 index 条记录（"下载记录"窗口里的"删除选中"）。

        删掉某条后该文件下次会重新下载——旧版要"把 history 文档整个删掉"才能重下，
        这里把粒度细化到单条。
        """
        if not self.enabled:
            return False
        if 0 <= index < len(self._data["entries"]):
            del self._data["entries"][index]
            self._rebuild_index()
            self.save()
            return True
        return False

    def clear(self):
        """清空记录，返回清掉的条数。"""
        count = len(self._data["entries"])
        self._data["entries"] = []
        self._keys = set()
        self._quickkeys = set()
        self.save()
        return count

    def __len__(self):
        return len(self._data["entries"])

    def summary(self):
        total = sum(int(e.get("size", 0) or 0) for e in self._data["entries"])
        return "记录 %d 条，累计 %s" % (len(self._data["entries"]), human_size(total))


# ----------------------------------------------------------------------
# 默认下载目录
# ----------------------------------------------------------------------

def default_download_dir():
    """默认下载目录：``<下载>\\MediaFireDownloads``。

    旧版写死 ``os.path.expanduser('~') + '\\Desktop\\MediaFireDownloads'``
    （英文 Desktop）。两个问题：
      * ``~`` 取的是 ``USERPROFILE`` 环境变量，不一定是用户真正的桌面位置；
        桌面/下载夹被 OneDrive 重定向或用户改过位置时，这个路径并不存在；
      * 中文系统上目录名是"桌面"而不是 "Desktop"。
    这里改用 Shell 的已知文件夹 API（``SHGetFolderPathW``，Win7 行为一致），
    失败才退回常见路径猜测。
    """
    candidates = []
    if sys.platform == "win32":
        try:
            import ctypes

            buf = ctypes.create_unicode_buffer(260)
            shell32 = ctypes.windll.shell32
            for csidl in (16, 5):  # C：CSIDL_DESKTOPDIRECTORY=桌面，CSIDL_PERSONAL=我的文档
                if shell32.SHGetFolderPathW(None, csidl, None, 0, buf) == 0 and buf.value:
                    candidates.append(buf.value)
        except Exception:
            pass

    home = os.path.expanduser("~")
    candidates.append(os.path.join(home, "Downloads"))
    candidates.append(os.path.join(home, "下载"))
    candidates.append(os.path.join(home, "Desktop", "MediaFireDownloads"))
    candidates.append(os.path.join(home, "桌面", "MediaFireDownloads"))

    for candidate in candidates:
        try:
            if candidate and os.path.isdir(candidate):
                target = os.path.join(candidate, "MediaFireDownloads")
                os.makedirs(target, exist_ok=True)
                return target
        except OSError:
            continue

    fallback = os.path.join(tempfile.gettempdir(), "MediaFireDownloads")
    try:
        os.makedirs(fallback, exist_ok=True)
    except OSError:
        pass
    return fallback
