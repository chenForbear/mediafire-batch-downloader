#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""``mfd.util`` 里"路径与文件名硬骨头"的离线自检。

覆盖范围
--------
1. ``is_reserved_name``：Windows 保留设备名（大小写、带扩展名、多级扩展名）
2. ``sanitize_filename``：命中保留名加下划线、非法字符与控制字符、结尾空格与点，
   以及**原有行为一个字都没变**（几个典型输入逐字对比）
3. ``fit_full_path``：短路径不动、超长时截断并保住扩展名、目录本身超长时给提示、
   同名结果稳定、不同尾部不撞名
4. ``disk_free_bytes`` / ``warn_if_not_enough_space``：剩余空间与告警
5. ``human_speed``：0 / 负数 / 1024 / 1536*1024 等边界

只跑本地函数，不联网、不写真磁盘（``fit_full_path`` 只算名字，目录不真建）。

直接运行::

    python -m unittest tests.test_util_paths -v
"""

import os
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.dirname(_HERE)
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from mfd import util                                                  # noqa: E402


# ======================================================================
# 1. 保留设备名
# ======================================================================

class TestIsReservedName(unittest.TestCase):
    """``is_reserved_name``：大小写不敏感，带不带扩展名都算。"""

    def test_bare_names_any_case(self):
        for name in ("CON", "con", "Con", "PRN", "prn", "AUX", "aux",
                     "NUL", "nul", "COM1", "com5", "Com9", "LPT1", "lpt4", "Lpt9"):
            self.assertTrue(util.is_reserved_name(name), "%s 应当算保留名" % name)

    def test_with_extension(self):
        for name in ("CON.txt", "aux.zip", "Com1.rar", "nul.log",
                     "CON.tar.gz", "LPT9.a.b.c", "con."):
            self.assertTrue(util.is_reserved_name(name), "%s 应当算保留名" % name)

    def test_trailing_space_variant(self):
        # Windows 会把 "CON .txt" 也当成 CON 设备：设备名后的空格不算数
        self.assertTrue(util.is_reserved_name("CON .txt"))

    def test_not_reserved(self):
        for name in ("", None, "CONSOLE", "CON1", "COM0", "COM10", "LPT0",
                     "LPT10", "con_file", "x.CON", "我的文件.txt", "auxiliary.zip"):
            self.assertFalse(util.is_reserved_name(name), "%r 不该算保留名" % (name,))

    def test_reserved_set_shape(self):
        self.assertIsInstance(util.RESERVED_NAMES, frozenset)
        self.assertEqual(len(util.RESERVED_NAMES), 22)      # 4 + 9 + 9
        self.assertIn("COM9", util.RESERVED_NAMES)
        self.assertIn("LPT9", util.RESERVED_NAMES)
        self.assertNotIn("COM0", util.RESERVED_NAMES)


# ======================================================================
# 2. 文件名净化
# ======================================================================

class TestSanitizeReserved(unittest.TestCase):
    """命中保留名要在主干后面加下划线，扩展名原样保留。"""

    def test_reserved_gets_underscore(self):
        self.assertEqual(util.sanitize_filename("CON.zip"), "CON_.zip")
        self.assertEqual(util.sanitize_filename("aux.zip"), "aux_.zip")
        self.assertEqual(util.sanitize_filename("Com1"), "Com1_")
        self.assertEqual(util.sanitize_filename("CON"), "CON_")
        self.assertEqual(util.sanitize_filename("nul"), "nul_")

    def test_multi_dot_reserved(self):
        # Windows 认第一个点之前那段：CON.tar.gz 依然是设备
        self.assertEqual(util.sanitize_filename("CON.tar.gz"), "CON_.tar.gz")

    def test_result_is_no_longer_reserved(self):
        for raw in ("CON.zip", "aux.zip", "Com1", "CON", "LPT9.7z"):
            got = util.sanitize_filename(raw)
            self.assertFalse(util.is_reserved_name(got), "%s 净化后仍保留：%s" % (raw, got))


class TestSanitizeUnchangedBehaviour(unittest.TestCase):
    """原有行为不能动——这些用例是 42 个老用例的平行副本。"""

    def test_illegal_chars_replaced(self):
        self.assertEqual(util.sanitize_filename('a<b>c:d"e/f\\g|h?i*j'),
                         "a_b_c_d_e_f_g_h_i_j")

    def test_control_chars_replaced(self):
        self.assertEqual(util.sanitize_filename("a\x01b\x1fc"), "a_b_c")
        self.assertEqual(util.sanitize_filename("a\x00b"), "a_b")

    def test_trailing_dot_and_space_stripped(self):
        self.assertEqual(util.sanitize_filename("name. "), "name")
        self.assertEqual(util.sanitize_filename("  name  "), "name")
        self.assertEqual(util.sanitize_filename("name."), "name")
        self.assertEqual(util.sanitize_filename(".hidden"), "hidden")

    def test_japanese_brackets_preserved(self):
        name = "【一般コミック】山本直樹「堀田 第01巻」.rar"
        self.assertEqual(util.sanitize_filename(name), name)

    def test_long_name_truncated_keeps_extension(self):
        got = util.sanitize_filename("あ" * 400 + ".rar")
        self.assertLessEqual(len(got), util.MAX_FILENAME_LENGTH)
        self.assertTrue(got.endswith(".rar"))

    def test_empty_falls_back(self):
        self.assertEqual(util.sanitize_filename(""), "unknown_file")
        self.assertEqual(util.sanitize_filename("..."), "unknown_file")
        self.assertEqual(util.sanitize_filename(None), "unknown_file")


class TestSanitizeHardCases(unittest.TestCase):
    """这次补上的硬骨头：交替结尾、字符集不残留。"""

    def test_alternating_trailing_space_and_dot(self):
        # 旧写法"先 strip 空格、再 strip 点、再 strip 空格"会在这里留下结尾的点
        self.assertEqual(util.sanitize_filename("name. ."), "name")
        self.assertEqual(util.sanitize_filename("name . . "), "name")
        self.assertEqual(util.sanitize_filename("a..."), "a")

    def test_never_ends_with_space_or_dot(self):
        cases = ["a. ", "a .", "a...", "a. . .", " a . ", "x ", "x.", " . ",
                 "a\u3000", "a\u00a0"]
        for raw in cases:
            got = util.sanitize_filename(raw)
            self.assertFalse(got.endswith(" ") or got.endswith("."),
                             "%r 清洗后仍以空格或点结尾：%r" % (raw, got))

    def test_no_illegal_or_control_char_survives(self):
        raw = 'ab<>:"/\\|?*\x00\x01\x1f'
        got = util.sanitize_filename(raw)
        for ch in got:
            self.assertNotIn(ch, util.INVALID_FILENAME_CHARS, "%r 残留非法字符" % got)
            self.assertGreaterEqual(ord(ch), 32, "%r 残留控制字符" % got)

    def test_all_nine_illegal_chars_covered(self):
        # 任务书列的九个字符一个都不能漏
        for ch in '\\/:*?"<>|':
            self.assertIn(ch, util.INVALID_FILENAME_CHARS)
            self.assertNotIn(ch, util.sanitize_filename("a%sb.txt" % ch))


# ======================================================================
# 3. 路径长度
# ======================================================================

class TestFitFullPath(unittest.TestCase):
    """``fit_full_path``：短路径不动，超长压到上限内且保住扩展名。"""

    def _long_dir(self):
        # 不真建目录：fit_full_path 只算名字
        return os.path.join(tempfile.gettempdir(), "mfd" * 40)

    def test_short_path_untouched(self):
        directory = tempfile.gettempdir()
        name, note = util.fit_full_path(directory, "hello.txt")
        self.assertEqual(name, "hello.txt")
        self.assertEqual(note, "")

    def test_long_name_compressed_within_limit(self):
        directory = self._long_dir()
        name, note = util.fit_full_path(directory, "あ" * 200 + ".iso")
        full = os.path.abspath(os.path.join(directory, name))
        self.assertLessEqual(len(full), util.MAX_PATH)
        self.assertLessEqual(len(full), 260)
        self.assertTrue(name.endswith(".iso"), "扩展名必须保住：%s" % name)
        self.assertTrue(note, "截断时必须给日志一句话")
        self.assertIn(".iso", note)

    def test_hash_keeps_same_prefix_apart(self):
        # 前缀相同、尾部不同的两个长文件名不能落到同一个名字上
        directory = self._long_dir()
        first, _ = util.fit_full_path(directory, "あ" * 200 + ".iso")
        second, _ = util.fit_full_path(directory, "あ" * 200 + "x.iso")
        self.assertNotEqual(first, second)

    def test_same_input_same_output(self):
        # 续传要靠这个：同一个名字每次算出来必须一致
        directory = self._long_dir()
        first, _ = util.fit_full_path(directory, "あ" * 200 + ".iso")
        second, _ = util.fit_full_path(directory, "あ" * 200 + ".iso")
        self.assertEqual(first, second)

    def test_directory_itself_too_long(self):
        directory = os.path.join(tempfile.gettempdir(), "x" * 300)
        name, note = util.fit_full_path(directory, "abc.iso")
        self.assertTrue(name.endswith(".iso"))
        self.assertLessEqual(len(name), 12)
        self.assertTrue(note)
        self.assertIn("目录本身", note)
        self.assertIn("更浅的目录", note)

    def test_empty_filename_falls_back(self):
        name, note = util.fit_full_path(tempfile.gettempdir(), "")
        self.assertEqual(name, "unknown_file")
        self.assertEqual(note, "")


# ======================================================================
# 4. 磁盘剩余空间
# ======================================================================

class TestDiskSpace(unittest.TestCase):

    def test_existing_path_positive(self):
        self.assertGreater(util.disk_free_bytes(tempfile.gettempdir()), 0)
        self.assertGreater(util.disk_free_bytes(os.getcwd()), 0)

    def test_missing_path_returns_minus_one(self):
        missing = os.path.join(tempfile.gettempdir(),
                               "mfd_no_such_dir_9f8a7b", "x.iso")
        self.assertFalse(os.path.exists(missing))
        self.assertEqual(util.disk_free_bytes(missing), -1)

    def test_huge_requirement_warns(self):
        directory = tempfile.gettempdir()
        if util.disk_free_bytes(directory) < 0:
            self.skipTest("当前临时目录取不到剩余空间，跳过")
        message = util.warn_if_not_enough_space(directory, 1 << 50)   # 1 PB
        self.assertTrue(message)
        self.assertIn("空间", message)
        self.assertIn("建议", message)

    def test_small_requirement_silent(self):
        directory = tempfile.gettempdir()
        free = util.disk_free_bytes(directory)
        if free < 0:
            self.skipTest("当前临时目录取不到剩余空间，跳过")
        if free > 400 * 1024 * 1024:
            self.assertEqual(util.warn_if_not_enough_space(directory, 1024), "")

    def test_degenerate_inputs(self):
        directory = tempfile.gettempdir()
        self.assertEqual(util.warn_if_not_enough_space(directory, 0), "")
        self.assertEqual(util.warn_if_not_enough_space(directory, -5), "")
        self.assertEqual(util.warn_if_not_enough_space(directory, None), "")
        # 目录取不到剩余空间时宁可不提示，也不凭猜的数字劝人清盘
        self.assertEqual(util.warn_if_not_enough_space(
            os.path.join(directory, "mfd_no_such_dir_9f8a7b"), 1 << 50), "")


# ======================================================================
# 5. 速度格式化
# ======================================================================

class TestHumanSpeed(unittest.TestCase):

    def test_placeholders(self):
        self.assertEqual(util.human_speed(0), "-- KB/s")
        self.assertEqual(util.human_speed(-1), "-- KB/s")
        self.assertEqual(util.human_speed(-1024), "-- KB/s")
        self.assertEqual(util.human_speed(None), "-- KB/s")
        self.assertEqual(util.human_speed("abc"), "-- KB/s")
        self.assertEqual(util.human_speed(float("nan")), "-- KB/s")
        self.assertEqual(util.human_speed(float("inf")), "-- KB/s")

    def test_units(self):
        self.assertEqual(util.human_speed(512), "512 B/s")
        self.assertEqual(util.human_speed(1023), "1023 B/s")
        self.assertEqual(util.human_speed(1024), "1.0 KB/s")
        self.assertEqual(util.human_speed(1536 * 1024), "1.5 MB/s")
        self.assertEqual(util.human_speed(3.5 * 1024 * 1024 * 1024), "3.5 GB/s")

    def test_accepts_string_number(self):
        self.assertEqual(util.human_speed("2048"), "2.0 KB/s")


if __name__ == "__main__":
    unittest.main(verbosity=2)
