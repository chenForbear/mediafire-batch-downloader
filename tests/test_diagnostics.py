#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""诊断模块的单元测试：classify 映射、两维归集、报告结构与落盘。

跑法：
    python tests/test_diagnostics.py
"""

import json
import os
import socket
import ssl
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from mfd import diagnostics as diagmod                        # noqa: E402
from mfd.diagnostics import (CATEGORY_TEXT, Diagnostics, build_report,  # noqa: E402
                             category_text, classify, collect_environment,
                             quick_check)
from mfd.failures import (FailureBook, FailureRecord, KIND_TEXT,  # noqa: E402
                          classify_failure)


class _FakeMediaFireError(Exception):
    """冒充 MediaFireError：只靠 user_message/detail 两个属性识别。"""

    def __init__(self, user_message, detail=""):
        Exception.__init__(self, user_message)
        self.user_message = user_message
        self.detail = detail


class TestClassify(unittest.TestCase):
    def test_every_kind_has_a_category(self):
        """21 类失败必须都能落到一个大类，且大类在 CATEGORY_TEXT 里。"""
        for kind in KIND_TEXT:
            category, code, _hint = classify(kind)
            self.assertEqual(code, kind)
            self.assertIn(category, CATEGORY_TEXT,
                          "失败类型 %s 没归到大类" % kind)

    def test_none_is_unknown(self):
        self.assertEqual(classify(None)[0], "unknown")

    def test_exception_types(self):
        cases = [
            (socket.timeout("timed out"), "network"),
            (ConnectionResetError("reset"), "network"),
            (ConnectionRefusedError("refused"), "network"),
            (socket.gaierror("nope"), "network"),
            (ssl.SSLError("bad cert"), "tls"),
            (UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad"), "encoding"),
            (PermissionError(13, "denied"), "filesystem"),
        ]
        for exc, expect in cases:
            category, code, hint = classify(exc)
            self.assertEqual(category, expect, "%r 归类错误（得到 %s/%s）"
                             % (exc, category, code))
            self.assertIsInstance(hint, str)

    def test_errno_mapping(self):
        self.assertEqual(classify(OSError(28, "No space"))[1], "disk_full")
        self.assertEqual(classify(OSError(13, "Denied"))[1], "disk_perm")
        self.assertEqual(classify(OSError(36, "Too long"))[1], "path_too_long")
        self.assertEqual(classify(OSError(2, "Missing"))[1], "link_missing")

    def test_mediafire_error_uses_failure_vocabulary(self):
        exc = _FakeMediaFireError(
            "MediaFire 返回 403 拒绝访问。", "HTTP 403 on https://x/y")
        category, code, hint = classify(exc)
        self.assertEqual(category, "http")
        self.assertEqual(code, "http_403")
        self.assertTrue(hint)

    def test_category_text_fallback(self):
        self.assertEqual(category_text("network"), "网络")
        self.assertEqual(category_text("no_such"), "no_such")
        self.assertEqual(category_text(None), "未知")


class TestNoteAndTabs(unittest.TestCase):
    def setUp(self):
        self.diag = Diagnostics(platform_name="Windows 10 22H2 (19045)")

    def test_note_dedupes_and_counts(self):
        for _ in range(3):
            self.diag.note("network", "net_timeout", "读取超时")
        self.assertEqual(len(self.diag), 1)
        event = self.diag.events[0]
        self.assertEqual(event["count"], 3)
        self.assertEqual(event["category_text"], "网络")
        self.assertEqual(event["code_text"], KIND_TEXT["net_timeout"])

    def test_different_message_is_a_new_event(self):
        self.diag.note("network", "net_timeout", "读取超时")
        self.diag.note("network", "net_timeout", "连接超时")
        self.assertEqual(len(self.diag), 2)

    def test_cross_tab_two_dimensions(self):
        self.diag.note("http", "http_403", "拒绝访问")
        self.diag.note("http", "http_403", "拒绝访问")
        self.diag.note("network", "net_timeout", "超时")
        other = Diagnostics(platform_name="Windows 7 SP1 (7601)")
        other.note("http", "http_403", "拒绝访问")

        table = self.diag.cross_tab()
        self.assertEqual(table["Windows 10 22H2 (19045)"]["http_403"], 2)
        self.assertEqual(table["Windows 10 22H2 (19045)"]["net_timeout"], 1)

        self.assertEqual(other.cross_tab()["Windows 7 SP1 (7601)"]["http_403"], 1)

    def test_headline_picks_majority(self):
        self.diag.note("http", "http_403", "拒绝访问")
        self.diag.note("http", "http_404", "不存在")
        self.diag.note("network", "net_timeout", "超时")
        self.assertIn("HTTP 协议", self.diag.headline())

    def test_note_masks_user_paths(self):
        self.diag.note("filesystem", "disk_perm", "写入失败",
                       context=r"C:\Users\user\Downloads\a.zip")
        self.assertNotIn("user", json.dumps(self.diag.events, ensure_ascii=False))

    def test_note_exception(self):
        self.diag.note_exception(socket.timeout("timed out"), context="下载第 3 个文件")
        event = self.diag.events[0]
        self.assertEqual(event["category"], "network")
        self.assertEqual(event["code"], "net_timeout")
        self.assertIn("第 3 个文件", event["context"])

    def test_note_failure_record(self):
        record = FailureRecord(name="a.zip", size=10, quickkey="q1",
                               kind="http_429", message="太频繁")
        self.diag.note_failure(record)
        event = self.diag.events[0]
        self.assertEqual(event["code"], "http_429")
        self.assertEqual(event["category"], "http")

    def test_clear(self):
        self.diag.note("network", "net_timeout", "x")
        self.diag.clear()
        self.assertEqual(len(self.diag), 0)
        self.assertEqual(self.diag.headline(), "暂无诊断事件")

    def test_event_limit(self):
        limit = diagmod._EVENT_LIMIT
        for index in range(limit + 5):
            self.diag.note("unknown", "unknown", "第 %d 条" % index)
        self.assertEqual(len(self.diag), limit)


class TestEnvironment(unittest.TestCase):
    def test_collect_returns_core_fields(self):
        env = collect_environment()
        for key in ("platform", "system", "python", "python_bits",
                    "frozen", "ssl", "proxy", "memory", "disk", "fonts"):
            self.assertIn(key, env, "环境信息缺字段 %s" % key)
        self.assertIsInstance(env["python"], str)
        self.assertIn(env["python_bits"], (32, 64))
        self.assertIsInstance(env["ssl"], dict)

    def test_no_user_name_leaks(self):
        env = collect_environment()
        blob = json.dumps(env, ensure_ascii=False, default=str)
        home = os.path.expanduser("~")
        user = os.path.basename(home.rstrip("\\/"))
        if user and len(user) > 2:
            self.assertNotIn(home, blob, "环境信息里出现了完整用户主目录")
            # 用户名可能作为普通词出现在别处，所以只堵"路径形态"
            self.assertNotIn("Users\\%s" % user, blob)
            self.assertNotIn("Users/%s" % user, blob)

    def test_non_windows_branches_do_not_crash(self):
        with mock.patch.object(diagmod.os, "name", "posix"):
            self.assertEqual(diagmod._windows_memory(), {})
            self.assertIsNone(diagmod._dpi())
            self.assertEqual(diagmod._fonts(), {})
            proxy = diagmod._windows_proxy()
            self.assertIsInstance(proxy, dict)

    def test_tk_version_shape(self):
        info = diagmod._tk_version()
        self.assertIn("tk", info)
        self.assertIn("tcl", info)

    def test_quick_check_is_serializable(self):
        result = quick_check()
        self.assertIn("ok", result)
        self.assertIn("problems", result)
        self.assertIsInstance(result["problems"], list)
        json.dumps(result, ensure_ascii=False, default=str)


class TestReport(unittest.TestCase):
    def setUp(self):
        self.diag = Diagnostics(platform_name="Windows 11 24H2 (26100)")
        self.diag.note("http", "http_403", "拒绝访问")
        self.book = FailureBook(path=None, autosave=False)

    def test_structure(self):
        report = build_report(self.diag, failures=self.book)
        for key in ("schema", "generated_at", "app", "headline",
                    "category_totals", "cross_tab", "events",
                    "environment", "failures_summary", "failures", "privacy"):
            self.assertIn(key, report, "报告缺字段 %s" % key)
        self.assertEqual(report["schema"], diagmod.SCHEMA)
        self.assertEqual(report["app"]["platform"], "Windows 11 24H2 (26100)")

    def test_json_serializable(self):
        report = build_report(self.diag, failures=self.book)
        text = json.dumps(report, ensure_ascii=False, default=str)
        self.assertIn("cross_tab", text)
        json.loads(text)

    def test_merges_failure_cross_tab(self):
        record = FailureRecord(name="a.zip", size=5, quickkey="q9",
                               kind="http_403", message="拒绝访问",
                               platform="Windows 7 SP1 (7601)")
        self.book.add(record)
        report = build_report(self.diag, failures=self.book)
        self.assertEqual(report["cross_tab"]["Windows 7 SP1 (7601)"]["http_403"], 1)
        self.assertEqual(len(report["failures"]), 1)

    def test_save_report_writes_file(self):
        workdir = tempfile.mkdtemp(prefix="mfd-diag-")
        self.addCleanup(lambda: __import__("shutil").rmtree(workdir, ignore_errors=True))
        path = self.diag.save_report(failures=self.book, directory=workdir)
        self.assertTrue(path and os.path.isfile(path), "报告没写出来：%r" % (path,))
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        self.assertEqual(data["schema"], diagmod.SCHEMA)

    def test_empty_diagnostics_still_reports(self):
        empty = Diagnostics()
        report = build_report(empty, failures=None)
        self.assertEqual(report["events"], [])
        self.assertEqual(report["headline"], "暂无诊断事件")

    def test_build_report_without_failures_book(self):
        report = build_report(None, failures=None)
        self.assertEqual(report["events"], [])
        self.assertIn("environment", report)


if __name__ == "__main__":
    unittest.main(verbosity=2)
