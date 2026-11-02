#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""``mfd.settings`` 自检：不联网、不碰真实配置，全部在临时目录里来回折腾。

直接运行：

    python tests\\test_settings.py

覆盖范围
--------
1. 默认值齐全（每一项都跟 ``DEFAULTS`` 对齐）、``get`` 的默认值与副本语义
2. ``set`` / ``update`` / ``reset`` 的行为，以及 ``save=False`` 时"只改内存不落盘"
3. ``push_dir_history``：去重、最新在前、截断 8 条、返回副本
4. 存盘再读回来一致（含历史记录）
5. 坏 JSON / 根节点不是对象 → 用默认值、不抛异常、坏文件改名成 ``.bad``
6. 写盘失败（把 path 指到一个目录上）只返回 False，不抛异常
7. 20 个线程同时 ``set`` 不同 key，不炸、文件仍是合法 JSON
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.dirname(_HERE)
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from mfd.settings import DEFAULTS, HISTORY_LIMIT, Settings      # noqa: E402


class SettingsCase(unittest.TestCase):
    """所有用例的公共底座：每个用例一个干净的临时目录。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mfd_settings_")
        self.path = os.path.join(self.tmp, "settings.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -------------------------------------------------- 小工具
    def read_file(self):
        """把磁盘上的设置原文读出来（断言"到底有没有真的存下去"用）。"""
        with open(self.path, "r", encoding="utf-8") as handle:
            return handle.read()


class DefaultsTest(SettingsCase):
    """默认值与只读接口。"""

    def test_默认值齐全(self):
        settings = Settings(self.path)
        for key, expected in DEFAULTS.items():
            self.assertEqual(settings.get(key), expected, "默认值不对：%s" % key)

    def test_键集合与默认表一致(self):
        settings = Settings(self.path)
        self.assertEqual(sorted(settings.to_dict().keys()), sorted(DEFAULTS.keys()))

    def test_默认值类型符合界面预期(self):
        """界面会直接拿这些值去填控件，类型错了会在 set() 处炸。"""
        settings = Settings(self.path)
        self.assertIsInstance(settings.get("download_dir_history"), list)
        self.assertIsInstance(settings.get("theme"), str)
        self.assertIsInstance(settings.get("autostart"), bool)
        self.assertIsInstance(settings.get("max_threads"), int)
        self.assertIsInstance(settings.get("gap_seconds"), float)
        self.assertIsInstance(settings.get("speed_limit_kbps"), int)

    def test_取不存在的键返回默认(self):
        settings = Settings(self.path)
        self.assertIsNone(settings.get("根本没有这个键"))
        self.assertEqual(settings.get("根本没有这个键", 42), 42)

    def test_取列表拿到的是副本(self):
        """调用方 append 一下不该改掉设置里的真身。"""
        settings = Settings(self.path)
        history = settings.get("download_dir_history")
        history.append("D:\\乱塞的")
        self.assertEqual(settings.get("download_dir_history"), [])

    def test_配置文件不存在时不报错(self):
        settings = Settings(os.path.join(self.tmp, "不存在的子目录", "settings.json"))
        self.assertEqual(settings.get("theme"), "dark")


class WriteTest(SettingsCase):
    """改值、落盘、读回。"""

    def test_set之后立刻落盘(self):
        settings = Settings(self.path)
        self.assertTrue(settings.set("theme", "light"))
        self.assertTrue(os.path.isfile(self.path))
        self.assertEqual(Settings(self.path).get("theme"), "light")

    def test_set不落盘时只改内存(self):
        settings = Settings(self.path)
        settings.set("theme", "light", save=False)
        self.assertEqual(settings.get("theme"), "light")
        self.assertFalse(os.path.exists(self.path), "save=False 不该写盘")
        self.assertEqual(Settings(self.path).get("theme"), "dark")

    def test_update批量修改(self):
        settings = Settings(self.path)
        settings.update({"theme": "light", "max_threads": 8})
        self.assertEqual(settings.get("theme"), "light")
        self.assertEqual(settings.get("max_threads"), 8)
        reloaded = Settings(self.path)
        self.assertEqual(reloaded.get("theme"), "light")
        self.assertEqual(reloaded.get("max_threads"), 8)

    def test_update不落盘(self):
        settings = Settings(self.path)
        settings.update({"theme": "light"}, save=False)
        self.assertFalse(os.path.exists(self.path))

    def test_reset恢复出厂值并落盘(self):
        settings = Settings(self.path)
        settings.set("theme", "light")
        settings.set("max_threads", 9)
        settings.reset()
        self.assertEqual(settings.get("theme"), "dark")
        self.assertEqual(settings.get("max_threads"), 2)
        self.assertEqual(Settings(self.path).get("theme"), "dark")

    def test_存盘再读回来一致(self):
        settings = Settings(self.path)
        settings.set("last_url", "https://www.mediafire.com/folder/abc/名字")
        settings.set("speed_limit_kbps", 512)
        settings.set("conflict_policy", "skip")
        settings.push_dir_history("D:\\下载一")
        back = Settings(self.path)
        self.assertEqual(back.get("last_url"), settings.get("last_url"))
        self.assertEqual(back.get("speed_limit_kbps"), 512)
        self.assertEqual(back.get("conflict_policy"), "skip")
        self.assertEqual(back.dir_history(), ["D:\\下载一"])

    def test_落盘内容是合法json(self):
        settings = Settings(self.path)
        settings.set("theme", "light")
        payload = json.loads(self.read_file())
        self.assertEqual(payload["theme"], "light")


class DirHistoryTest(SettingsCase):
    """保存位置历史：去重、最新在前、只留 8 条。"""

    def test_最新在前且去重(self):
        settings = Settings(self.path)
        settings.push_dir_history("D:\\A")
        settings.push_dir_history("D:\\B")
        settings.push_dir_history("D:\\C")
        self.assertEqual(settings.dir_history(), ["D:\\C", "D:\\B", "D:\\A"])

        # 再选一次老位置：提到最前，不重复出现
        settings.push_dir_history("D:\\A")
        self.assertEqual(settings.dir_history(), ["D:\\A", "D:\\C", "D:\\B"])

    def test_截断到八条(self):
        settings = Settings(self.path)
        for index in range(12):
            settings.push_dir_history("D:\\目录%d" % index)
        history = settings.dir_history()
        self.assertEqual(len(history), HISTORY_LIMIT)
        self.assertEqual(history[0], "D:\\目录11")
        self.assertEqual(history[-1], "D:\\目录4")

    def test_空串忽略(self):
        settings = Settings(self.path)
        settings.push_dir_history("   ")
        settings.push_dir_history("")
        settings.push_dir_history(None)
        self.assertEqual(settings.dir_history(), [])

    def test_返回的是副本(self):
        settings = Settings(self.path)
        settings.push_dir_history("D:\\A")
        got = settings.push_dir_history("D:\\B")
        got.append("D:\\乱塞的")
        self.assertEqual(settings.dir_history(), ["D:\\B", "D:\\A"])

    def test_历史随存盘保留(self):
        settings = Settings(self.path)
        settings.push_dir_history("D:\\甲")
        settings.push_dir_history("D:\\乙")
        self.assertEqual(Settings(self.path).dir_history(), ["D:\\乙", "D:\\甲"])

    def test_目录末尾分隔符视为同一个(self):
        """``D:\\下载`` 与 ``D:\\下载\\`` 是同一条，不该占两格。"""
        settings = Settings(self.path)
        first = "D:\\下载"
        second = first + os.sep
        settings.push_dir_history(first)
        settings.push_dir_history(second)
        self.assertEqual(len(settings.dir_history()), 1)


class BrokenFileTest(SettingsCase):
    """坏文件：用默认值、不抛异常、留档成 .bad。"""

    def test_json解析失败(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("{ 这不是 JSON: [1, 2,, }")
        settings = Settings(self.path)          # 不该抛
        for key, expected in DEFAULTS.items():
            self.assertEqual(settings.get(key), expected)
        self.assertTrue(os.path.isfile(self.path + ".bad"), "坏文件应改名留档")
        with open(self.path + ".bad", "r", encoding="utf-8") as handle:
            self.assertIn("这不是 JSON", handle.read())

    def test_根节点不是对象(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("[1, 2, 3]")
        settings = Settings(self.path)
        self.assertEqual(settings.get("theme"), "dark")
        self.assertTrue(os.path.isfile(self.path + ".bad"))

    def test_空文件(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("")
        settings = Settings(self.path)
        self.assertEqual(settings.get("theme"), "dark")

    def test_历史记录里的脏数据被清洗(self):
        payload = {"download_dir_history": ["D:\\正常", None, 123, "   ", "  D:\\带空格  "]}
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
        settings = Settings(self.path)
        self.assertEqual(settings.dir_history(), ["D:\\正常", "D:\\带空格"])

    def test_缺字段的旧配置用默认值补齐(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump({"theme": "light"}, handle)
        settings = Settings(self.path)
        self.assertEqual(settings.get("theme"), "light")
        self.assertEqual(settings.get("max_threads"), 2)
        self.assertEqual(sorted(settings.to_dict().keys()), sorted(DEFAULTS.keys()))

    def test_坏文件之后仍能存盘(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("坏的")
        settings = Settings(self.path)
        self.assertTrue(settings.set("theme", "light"))
        self.assertEqual(Settings(self.path).get("theme"), "light")


class WriteFailureTest(SettingsCase):
    """写盘失败只吞不抛。"""

    def test_路径是个目录(self):
        """把 path 指到已存在的目录上：读不成、也写不进，但全程不抛。"""
        target = os.path.join(self.tmp, "其实是个目录")
        os.makedirs(target)
        settings = Settings(target)             # 读目录会抛 IsADirectoryError，应被吞掉
        self.assertEqual(settings.get("theme"), "dark")
        self.assertFalse(settings.set("theme", "light"))   # 落盘失败返回 False
        self.assertEqual(settings.get("theme"), "light")   # 但内存里已经生效


class ConcurrencyTest(SettingsCase):
    """多线程同时写：不炸、数据不丢、文件不烂。"""

    def test_二十个线程各写各的(self):
        settings = Settings(self.path)
        errors = []
        start = threading.Event()

        def worker(index):
            try:
                start.wait(2.0)                 # 尽量让 20 个线程同时冲进去
                settings.set("自定义键%d" % index, index)
            except Exception as exc:            # noqa: BLE001 —— 记下来给断言看
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for thread in threads:
            thread.start()
        start.set()
        for thread in threads:
            thread.join(10.0)
            self.assertFalse(thread.is_alive(), "线程卡住了")

        self.assertEqual(errors, [])
        for index in range(20):
            self.assertEqual(settings.get("自定义键%d" % index), index)

        # 落盘文件必须始终是能解析的完整 JSON（原子写的意义就在这儿）
        payload = json.loads(self.read_file())
        self.assertIsInstance(payload, dict)
        for key in DEFAULTS:
            self.assertIn(key, payload)

    def test_并发读同时改(self):
        """一边读一边改不该读到"改了一半"的结构。"""
        settings = Settings(self.path)
        settings.set("max_threads", 2)
        seen = []
        stop = threading.Event()

        def reader():
            while not stop.is_set():
                seen.append(settings.get("max_threads"))

        thread = threading.Thread(target=reader)
        thread.start()
        try:
            for value in range(1, 50):
                settings.set("max_threads", value, save=False)
        finally:
            stop.set()
            thread.join(10.0)
        self.assertTrue(all(isinstance(value, int) for value in seen))


if __name__ == "__main__":
    unittest.main(verbosity=2)
