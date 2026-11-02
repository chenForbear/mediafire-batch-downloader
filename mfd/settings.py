"""用户设置持久化（界面层专用，轻量、线程安全、永不抛异常）。

为什么单独一个模块：设置项是"界面偏好"，不是"下载任务状态"，不该混进
:mod:`mfd.storage` 的历史记录里；而且早期版本把配置直接写进界面代码，改一个默认值
要翻三个文件。

三条硬约束：

1. **只依赖标准库与** :mod:`mfd.paths` **。** 不 import ``mfd.downloader``、
   不 import ``mfd.ui``——界面 import 设置、下载器 import 界面就会成环，
   Python 的循环 import 会在启动时炸得莫名其妙。
2. **写盘不能影响使用。** 磁盘满、目录只读、杀软锁文件……设置存不下来顶多是
   "下次打开还是老样子"，绝不该让下载流程中断，所以 :meth:`Settings.save`
   把所有异常吞掉、只返回真假。
3. **读盘必须容错。** 用户手动编辑过、进程被杀时写了半截、两台机器同步撞车，
   都会留下坏 JSON。这时退回默认值，并把坏文件改名成 ``settings.json.bad``
   留作证据，而不是让程序起不来。

用法::

    from mfd.settings import Settings

    settings = Settings()                 # 默认落在 mfd.paths.data_file("settings.json")
    settings.get("max_threads")           # 2
    settings.set("theme", "light")        # 改完立刻存盘
    settings.push_dir_history("D:\\\\下载")  # 最近用过的保存位置，最新在前
"""

import copy
import json
import os
import tempfile
import threading

from . import paths

__all__ = ["DEFAULTS", "HISTORY_LIMIT", "Settings", "default_path"]

#: 保存位置历史最多留几条（再多界面上的下拉框就看不清了）
HISTORY_LIMIT = 8

#: 全部设置项的出厂值。**新增设置项一律加在这里**，界面上读不到的键就用这里的值，
#: 这样"升级后老配置文件缺字段"永远不需要写迁移代码。
DEFAULTS = {
    "download_dir_history": [],      # 最近用过的保存位置，最多 8 条，最新在前
    "last_url": "",
    "window_geometry": "",
    "theme": "dark",
    "autostart": True,
    "max_threads": 2,
    "gap_seconds": 1.0,
    "speed_limit_kbps": 0,           # 0 = 不限速
    "on_complete_action": "none",    # none / open_dir / shutdown
    "conflict_policy": "rename",     # rename / overwrite / skip
    "keep_subfolders": True,
    "language": "zh",                # zh / en
    "verify_after_download": False,
    "auto_retry_times": 3,
    "auto_folder": True,             # 是否自动建以 MediaFire 文件夹名命名的子目录
}

#: 写盘互斥锁。**故意做成模块级**：同一个进程里界面可能同时持有两个 Settings 实例
#: （主窗口一个、设置对话框一个），实例级锁挡不住它们同时写同一个文件。
#: 用 RLock 而不是 Lock，是因为 :meth:`Settings.push_dir_history` 会在持锁时调
#: :meth:`Settings.save`，而 save 自己也要拿锁。
_IO_LOCK = threading.RLock()


def default_path():
    """设置文件的默认位置。

    跟着 :func:`mfd.paths.data_file` 走（实测受 ``MFD_DATA_DIR`` 环境变量控制），
    测试只要把该变量指到临时目录，就不会碰到真实用户的配置。
    """
    return paths.data_file("settings.json")


def _copy_defaults():
    """出厂值的一份深拷贝。

    ``DEFAULTS["download_dir_history"]`` 是列表，**不能直接返回它本身**：
    任何一个实例改了历史记录，所有实例（包括还没创建的）都会跟着变。
    """
    return copy.deepcopy(DEFAULTS)


def _clean_history(value):
    """把"保存位置历史"整成干净的字符串列表（去空白、丢空串、丢非字符串）。

    配置是用户能手改的文本文件，里面出现 ``[null, 123, "  "]`` 完全可能，
    界面拿它去填下拉框就会崩。这里一次性把脏数据挡在门外。
    """
    if not isinstance(value, (list, tuple)):
        return []
    result = []
    for item in value:
        if not isinstance(item, str):
            continue
        text = item.strip()
        if text:
            result.append(text)
    return result


def _history_key(path_text):
    """历史记录去重用的比较键：Windows 下 ``D:\\A`` 与 ``d:\\a\\`` 视为同一个。"""
    return os.path.normcase(os.path.normpath(path_text))


def _atomic_save(target, payload):
    """原子写：先写同目录的临时文件，再 ``os.replace`` 顶上去。

    临时文件名里带 pid（``.settings_<pid>_xxxx.tmp``）：两个进程同时写同一份配置
    （用户开了两个窗口）时，各自的临时文件不会互相覆盖，最后 ``os.replace``
    是原子操作，读到的要么是完整的旧文件、要么是完整的新文件，不会读到半截。

    失败时尽量删掉临时文件再**原样抛出**——要不要吞由上层 :meth:`Settings.save` 决定。

    刻意**不用** ``tempfile.mkstemp``：它内部按 ``O_CREAT|O_EXCL`` 打开，在受限
    令牌或重定向过的目录下会长时间挂起（不是快速报错），一旦落在退出路径上就
    表现为「程序关不掉」。改成自己拼临时名 + 普通 open，失败立刻抛。
    """
    directory = os.path.dirname(os.path.abspath(target))
    if directory and not os.path.isdir(directory):
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError:
            pass
    tmp_path = "%s.%d.tmp" % (target, os.getpid())
    try:
        with open(tmp_path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.flush()
            try:
                # 尽量把内容刷到盘上；某些文件系统（网络盘）不支持就跳过
                os.fsync(handle.fileno())
            except OSError:
                pass
        os.replace(tmp_path, target)
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


class Settings(object):
    """一份用户设置。所有方法都线程安全，且**不会抛异常**。

    多线程怎么用：界面线程改设置、下载线程读设置（比如 :meth:`get` 取并发数）
    是常态，所以读写都在 ``RLock`` 里。读操作返回的是副本，调用方拿到列表后
    随便改都不会影响这里的真身。
    """

    def __init__(self, path=None):
        """读入设置。``path`` 缺省走 :func:`default_path`。

        文件不存在（第一次运行）、读不动、JSON 坏了、根节点不是对象——一律退回
        默认值并且**绝不抛异常**；坏文件会被改名成 ``<文件名>.bad`` 留档。
        """
        self._lock = threading.RLock()
        self._path = path or default_path()
        self._data = _copy_defaults()
        self._load()

    # ---------------------------------------------------------------- 读盘
    def _load(self):
        """把磁盘上的设置并进出厂值。缺的键自动用默认值补齐。"""
        try:
            with open(self._path, "r", encoding="utf-8") as handle:
                raw = handle.read()
        except (IOError, OSError):
            # 第一次运行，或者路径不可读：就用默认值，不写盘（等用户真的改了再说）
            return
        try:
            payload = json.loads(raw)
        except ValueError:
            self._quarantine()
            return
        if not isinstance(payload, dict):
            self._quarantine()
            return

        merged = _copy_defaults()
        for key, value in payload.items():
            if key == "download_dir_history":
                # 这一项一定是列表，脏数据在界面里是直接崩的，单独清洗
                merged[key] = _clean_history(value)
            else:
                # 认不出的键也留着：将来版本加的设置项，被旧版本打开一次不该丢
                merged[key] = value
        self._data = merged

    def _quarantine(self):
        """把坏掉的配置文件改名成 ``*.bad``，然后退回默认值。

        "尽力而为"：改名失败（权限、被占用）就算了，反正内存里已经是默认值，
        下次保存会直接覆盖掉那个坏文件。
        """
        try:
            os.replace(self._path, self._path + ".bad")
        except Exception:
            pass
        self._data = _copy_defaults()

    # ---------------------------------------------------------------- 读
    def get(self, key, default=None):
        """取一项设置；没有这个键就返回 ``default``。

        列表／字典返回的是副本——调用方 ``append`` 一下不该悄悄改掉这里的真身，
        要改请走 :meth:`set` 或 :meth:`push_dir_history`。
        """
        with self._lock:
            if key not in self._data:
                return default
            value = self._data[key]
            if isinstance(value, (list, dict)):
                return copy.deepcopy(value)
            return value

    def to_dict(self):
        """整份设置的副本（给"导出配置""调试打印"用）。"""
        with self._lock:
            return copy.deepcopy(self._data)

    def dir_history(self):
        """保存位置历史（最新在前）的副本。"""
        with self._lock:
            return _clean_history(self._data.get("download_dir_history"))

    # ---------------------------------------------------------------- 写
    def set(self, key, value, save=True):
        """改一项设置。``save=True`` 时立刻落盘。

        返回布尔：``True`` 表示（需要落盘时）落盘成功。**落盘失败不算操作失败**，
        内存里的值照样生效——本次运行期间用户看到的仍是新值。
        """
        with self._lock:
            self._data[key] = value
            if save:
                return self.save()
            return True

    def update(self, mapping, save=True):
        """批量改多项，只落盘一次（界面"确定"按钮就该走这里）。"""
        with self._lock:
            for key, value in dict(mapping).items():
                self._data[key] = value
            if save:
                return self.save()
            return True

    def reset(self):
        """恢复出厂值并落盘。"""
        with self._lock:
            self._data = _copy_defaults()
            return self.save()

    def push_dir_history(self, directory):
        """记一条"用过的保存位置"：去重、插到最前、只留 :data:`HISTORY_LIMIT` 条。

        去重按"规范化后的路径"比：同一目录选了十次，历史里也只有一条。
        返回更新后的历史列表（副本），方便调用方直接刷新下拉框。
        """
        text = (directory or "").strip()
        if not text:
            return self.dir_history()
        key = _history_key(text)
        with self._lock:
            kept = [item for item in self.dir_history() if _history_key(item) != key]
            kept.insert(0, text)
            self._data["download_dir_history"] = kept[:HISTORY_LIMIT]
            self.save()
            return list(self._data["download_dir_history"])

    # ---------------------------------------------------------------- 落盘
    def save(self):
        """把当前设置写回磁盘，成功返回 ``True``。

        任何失败（目录只读、磁盘满、被杀软锁住）都只返回 ``False``，
        **绝不抛异常**：设置存不下来不该影响使用。
        """
        with self._lock:
            payload = copy.deepcopy(self._data)
        try:
            with _IO_LOCK:
                _atomic_save(self._path, payload)
            return True
        except Exception:
            return False

    def __repr__(self):
        """只报路径，不打印内容——设置里将来可能有敏感项，日志里不该出现。"""
        return "<Settings path=%r>" % (self._path,)
