#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线自检：不联网、不访问真实 MediaFire，只验证纯逻辑与本地下载链路。

直接运行：

    python tests\\test_offline.py

覆盖范围
--------
1. 文件名净化 / 大小与时间格式化 / URL 文件名解码（沿用 5.0 的既有断言）
2. 失败分类 ``classify_failure``：403、404、429、5xx、TLS、超时、重置、
   磁盘满、权限、路径过长、校验不符、大小不符、MediaFire 错误码
3. 失败清单 ``FailureBook``：去重、两维归集、交叉表、三种导出、落盘往返
4. 下载引擎（本地 HTTP 服务器模拟）：正常下载 + SHA-256 校验、校验失败、
   403 失败记录、断点续传、已存在跳过、**只重试失败项**
"""

import hashlib
import http.server
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.dirname(_HERE)
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from mfd import mediafire, util                                     # noqa: E402
from mfd.downloader import (STATUS_CANCELED, STATUS_DONE, STATUS_FAILED,  # noqa: E402
                            STATUS_SKIPPED, STATUS_WAITING, DownloadTask,
                            Downloader)
from mfd.failures import (KIND_TEXT, FailureBook, FailureRecord,    # noqa: E402
                          classify_failure, kind_hint, kind_text,
                          mask_text, platform_label)
from mfd.storage import History                                     # noqa: E402


# ======================================================================
# 本地 HTTP 服务器：模拟 MediaFire 直链（支持 Range，可模拟 403）
# ======================================================================

class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    files = {}       # path -> bytes
    deny_paths = set()
    hits = {}        # path -> 命中次数
    ranges = []      # 收到的 Range 头

    def log_message(self, *args):  # 静默，别往测试输出里喷
        pass

    def do_GET(self):
        path = self.path.split("?")[0]
        type(self).hits[path] = type(self).hits.get(path, 0) + 1

        if path in type(self).deny_paths:
            body = b"forbidden"
            self.send_response(403)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        data = type(self).files.get(path)
        if data is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        range_header = self.headers.get("Range")
        type(self).ranges.append(range_header)
        start = 0
        if range_header and range_header.startswith("bytes="):
            try:
                start = int(range_header[6:].split("-")[0])
            except (ValueError, IndexError):
                start = 0
        chunk = data[start:]
        if start:
            self.send_response(206)
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, len(data) - 1, len(data)))
        else:
            self.send_response(200)
        self.send_header("Content-Length", str(len(chunk)))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        self.wfile.write(chunk)


class _LocalServer(object):
    def __init__(self):
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def base(self):
        return "http://127.0.0.1:%d" % self.port

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        try:
            self.httpd.shutdown()
        except Exception:
            pass
        try:
            self.httpd.server_close()
        except Exception:
            pass


_SERVER = None
_ENV_BACKUP = {}


def setUpModule():
    global _SERVER
    # 本机可能配了系统代理：测试走 127.0.0.1 时必须直连，否则会被代理拦掉
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
                 "ALL_PROXY", "all_proxy"):
        if name in os.environ:
            _ENV_BACKUP[name] = os.environ.pop(name)
    _SERVER = _LocalServer().start()


def tearDownModule():
    if _SERVER is not None:
        _SERVER.stop()
    os.environ.update(_ENV_BACKUP)


# ======================================================================
# 1. 纯函数层（沿用 5.0 断言）
# ======================================================================

class TestSanitizeFilename(unittest.TestCase):
    def test_illegal_chars_replaced(self):
        self.assertEqual(util.sanitize_filename('a<b>c:d"e/f\\g|h?i*j'),
                         'a_b_c_d_e_f_g_h_i_j')

    def test_control_chars_removed(self):
        self.assertEqual(util.sanitize_filename("a\x01b\x1fc"), "a_b_c")

    def test_trailing_dot_and_space_stripped(self):
        self.assertEqual(util.sanitize_filename("name. "), "name")
        self.assertEqual(util.sanitize_filename("  name  "), "name")

    def test_reserved_names(self):
        for reserved in ("CON", "PRN", "AUX", "NUL", "COM1", "LPT9", "con"):
            got = util.sanitize_filename(reserved + ".txt")
            self.assertTrue(got.upper().startswith(reserved.upper() + "_"),
                            "%s 未被净化：%s" % (reserved, got))

    def test_long_name_truncated(self):
        got = util.sanitize_filename("あ" * 400 + ".rar")
        self.assertLessEqual(len(got), util.MAX_FILENAME_LENGTH)
        self.assertTrue(got.endswith(".rar"))

    def test_japanese_brackets_preserved(self):
        name = "【一般コミック】山本直樹「堀田 第01巻」.rar"
        self.assertEqual(util.sanitize_filename(name), name)

    def test_empty_falls_back(self):
        self.assertEqual(util.sanitize_filename(""), "unknown_file")
        self.assertEqual(util.sanitize_filename("..."), "unknown_file")


class TestHumanSizeTime(unittest.TestCase):
    def test_size(self):
        self.assertEqual(util.human_size(0), "0 B")
        self.assertEqual(util.human_size(1533), "1.5 KB")
        self.assertEqual(util.human_size(160789949), "153.3 MB")
        self.assertEqual(util.human_size(None), "未知")

    def test_time(self):
        self.assertEqual(util.human_time(45), "45秒")
        self.assertEqual(util.human_time(125), "2分05秒")
        self.assertEqual(util.human_time(3723), "1小时02分03秒")
        self.assertEqual(util.human_time(None), "--")


class TestDecodeFilenameFromUrl(unittest.TestCase):
    def test_percent20_becomes_space(self):
        got = util.decode_filename_from_url(
            "https://download856.mediafire.com/a/b/My%20File%20Name.zip")
        self.assertEqual(got, "My File Name.zip")

    def test_query_string_ignored(self):
        got = util.decode_filename_from_url("https://d.mediafire.com/a/b/file.rar?token=1")
        self.assertEqual(got, "file.rar")


class TestExtractFolderKey(unittest.TestCase):
    def test_variants(self):
        cases = [
            ("https://www.mediafire.com/folder/atlz3pkzfxos5", "atlz3pkzfxos5"),
            ("https://www.mediafire.com/folder/atlz3pkzfxos5/Some_Name/", "atlz3pkzfxos5"),
            ("https://www.mediafire.com/file/fszkql28gawcu6s/CZII.cbr/file", "fszkql28gawcu6s"),
            ("https://www.mediafire.com/?atlz3pkzfxos5", "atlz3pkzfxos5"),
            ("http://mediafire.com/folder/abcdefghijkl", "abcdefghijkl"),
        ]
        for url, want in cases:
            self.assertEqual(mediafire.extract_folder_key(url), want, url)

    def test_garbage_raises(self):
        with self.assertRaises(mediafire.MediaFireError):
            mediafire.extract_folder_key("https://example.com/hello")


# ======================================================================
# 2. 失败分类
# ======================================================================

class TestClassifyFailure(unittest.TestCase):
    def test_hash_and_size(self):
        self.assertEqual(classify_failure(None, "文件校验不通过，已删除并会重新下载", "")[0],
                         "hash_mismatch")
        self.assertEqual(classify_failure(None, "下载不完整：已收到 1 MB", "")[0], "size_mismatch")
        self.assertEqual(classify_failure(None, "", "sha256 mismatch: got A want B")[0],
                         "hash_mismatch")

    def test_http_codes(self):
        self.assertEqual(classify_failure(None, "HTTP 403 拒绝访问", "")[0], "http_403")
        self.assertEqual(classify_failure(None, "HTTP 404 Not Found", "")[0], "http_404")
        self.assertEqual(classify_failure(None, "HTTP 429 Too Many Requests", "")[0], "http_429")
        self.assertEqual(classify_failure(None, "HTTP 503 Service Unavailable", "")[0], "http_5xx")

    def test_network_exceptions(self):
        import socket
        import ssl

        self.assertEqual(classify_failure(socket.timeout(), "", "")[0], "net_timeout")
        self.assertEqual(classify_failure(ConnectionResetError(), "", "")[0], "net_reset")
        self.assertEqual(classify_failure(ConnectionRefusedError(), "", "")[0], "net_connect")
        self.assertEqual(classify_failure(ssl.SSLError("bad"), "", "")[0], "tls")
        self.assertEqual(
            classify_failure(None, "SSL 握手失败（可能是系统缺少根证书或补丁不全）", "")[0], "tls")

    def test_mediafire_api_codes(self):
        self.assertEqual(classify_failure(None, "接口错误", "error=129")[0], "api_error")
        self.assertEqual(classify_failure(None, "该文件夹被设为私密", "error=128")[0], "api_error")

    def test_filesystem(self):
        exc = OSError(28, "No space left on device")
        self.assertEqual(classify_failure(exc, "写入文件失败", "")[0], "disk_full")
        exc2 = OSError(13, "Permission denied")
        self.assertEqual(classify_failure(exc2, "写入文件失败", "")[0], "disk_perm")

    def test_cancel_and_unknown(self):
        self.assertEqual(classify_failure(None, "用户停止，已保留断点文件", "")[0], "canceled")
        self.assertEqual(classify_failure(None, "某种没人见过的错误", "")[0], "unknown")

    def test_every_kind_has_text_and_hint(self):
        for kind in ("hash_mismatch", "http_403", "net_timeout", "disk_full", "unknown"):
            self.assertIn(kind, KIND_TEXT)
            self.assertTrue(kind_text(kind))
            self.assertTrue(kind_hint(kind))


class TestMaskText(unittest.TestCase):
    def test_user_path_masked(self):
        masked = mask_text(r"C:\Users\user\Downloads\a.zip")
        self.assertIn("***", masked)
        self.assertNotIn("user", masked)

    def test_home_replaced(self):
        home = os.path.expanduser("~")
        masked = mask_text(os.path.join(home, "x.txt"))
        self.assertNotIn(home, masked)

    def test_platform_label_available(self):
        label = platform_label()
        self.assertTrue(label)
        self.assertNotIn("\n", label)


# ======================================================================
# 3. 失败清单
# ======================================================================

class TestFailureRecord(unittest.TestCase):
    def test_key_prefers_quickkey(self):
        a = FailureRecord(name="a.zip", size=10, quickkey="abc")
        b = FailureRecord(name="a.zip", size=10)
        self.assertEqual(a.key, "qk:abc")
        self.assertEqual(b.key, "nm:a.zip|10")

    def test_roundtrip_dict(self):
        record = FailureRecord(name="a.zip", url="http://x/1", size=10, quickkey="abc",
                               folder="sub", hash_value="ff", kind="http_403",
                               message="拒绝访问", attempts=2)
        again = FailureRecord.from_dict(record.to_dict())
        self.assertEqual(again.name, record.name)
        self.assertEqual(again.kind, record.kind)
        self.assertEqual(again.size, record.size)
        self.assertEqual(again.hash_value, "FF")

    def test_to_item_is_load_tasks_compatible(self):
        record = FailureRecord(name="a.zip", url="http://x/1", size=3, quickkey="k",
                               folder="sub", hash_value="ab")
        item = record.to_item()
        self.assertEqual(set(item), {"name", "url", "size", "quickkey", "folder", "hash"})
        self.assertEqual(item["hash"], "AB")


class TestFailureBook(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mfd_fail_")
        self.path = os.path.join(self.tmp, "failures.json")
        self.book = FailureBook(path=self.path, autosave=False)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _add(self, name="a.zip", size=10, quickkey="q1", kind="http_403",
             platform="Windows 10 (19045)", message="拒绝访问", attempts=1):
        return self.book.add(FailureRecord(name=name, size=size, quickkey=quickkey,
                                           kind=kind, platform=platform, message=message,
                                           attempts=attempts,
                                           url="http://mediafire.com/file/%s" % quickkey))

    def test_add_and_dedupe_by_quickkey(self):
        self._add(attempts=1)
        first_time = self.book.records[0].created_at
        self._add(attempts=5)
        self.assertEqual(len(self.book), 1, "同一 quickkey 应覆盖而不是新增")
        record = self.book.records[0]
        self.assertEqual(record.attempts, 5)
        self.assertEqual(record.created_at, first_time, "首次发现时间应保留")

    def test_two_dimension_grouping(self):
        self._add(name="a.zip", quickkey="q1", kind="http_403", platform="Windows 10 (19045)")
        self._add(name="b.zip", quickkey="q2", kind="net_timeout", platform="Windows 10 (19045)")
        self._add(name="c.zip", quickkey="q3", kind="http_403", platform="Windows 7 SP1 (7601)")

        by_kind = self.book.by_kind()
        self.assertEqual(sorted(by_kind), ["http_403", "net_timeout"])
        self.assertEqual(len(by_kind["http_403"]), 2)

        by_platform = self.book.by_platform()
        self.assertEqual(len(by_platform["Windows 10 (19045)"]), 2)
        self.assertEqual(len(by_platform["Windows 7 SP1 (7601)"]), 1)

        cross = self.book.cross_tab()
        self.assertEqual(cross["Windows 10 (19045)"]["http_403"], 1)
        self.assertEqual(cross["Windows 7 SP1 (7601)"]["http_403"], 1)

    def test_discard_on_success(self):
        self._add(quickkey="q1")
        self.assertTrue(self.book.discard("q1", "a.zip", 10))
        self.assertEqual(len(self.book), 0)
        self.assertFalse(self.book.discard("q1", "a.zip", 10))

    def test_exports(self):
        self._add(name="剧集 01.mkv", quickkey="q1", kind="http_403")
        self._add(name="剧集 02.mkv", quickkey="q2", kind="net_timeout")

        payload = self.book.export_json()
        self.assertIn('"cross_tab"', payload)
        self.assertIn("http_403", payload)

        markdown = self.book.export_markdown()
        self.assertIn("按错误类型分组", markdown)
        self.assertIn("按操作系统分组", markdown)
        self.assertIn("失败链接", markdown)
        self.assertIn("HTTP 403 拒绝访问", markdown)

        links = self.book.export_links_txt()
        self.assertEqual(len([line for line in links.splitlines() if line.strip()]), 2)

    def test_persistence_roundtrip(self):
        self._add(quickkey="q1", kind="http_403")
        self.book.save()
        self.assertTrue(os.path.isfile(self.path))
        again = FailureBook(path=self.path, autosave=False)
        self.assertEqual(len(again), 1)
        self.assertEqual(again.records[0].kind, "http_403")

    def test_to_items_feeds_retry(self):
        self._add(quickkey="q1")
        items = self.book.to_items()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["quickkey"], "q1")

    def test_summary_text(self):
        self._add(quickkey="q1")
        self.assertIn("失败 1 个", self.book.summary())
        self.assertIn("没有失败", FailureBook(path=os.path.join(self.tmp, "empty.json"),
                                            autosave=False).summary())


# ======================================================================
# 4. 下载引擎（本地服务器）
# ======================================================================

class TestDownloadTask(unittest.TestCase):
    def test_percent_and_eta(self):
        task = DownloadTask(name="a.bin", url="u", size=1000)
        task.downloaded = 250
        self.assertEqual(task.percent, 25.0)
        task.status = "running"
        task.speed = 1024.0  # 单位是 KB/s（=1 MB/s）
        self.assertAlmostEqual(task.eta_seconds(), 750 / (1024.0 * 1024.0), places=6)

    def test_reset_for_retry_keeps_bytes(self):
        task = DownloadTask(name="a.bin", url="u", size=1000)
        task.status = STATUS_FAILED
        task.error = "boom"
        task.fail_kind = "http_403"
        task.downloaded = 400
        task.attempts = 3
        task.reset_for_retry()
        self.assertEqual(task.status, STATUS_WAITING)
        self.assertEqual(task.error, "")
        self.assertEqual(task.fail_kind, "")
        self.assertEqual(task.downloaded, 400, "已下载字节必须保留，续传才有意义")
        self.assertEqual(task.attempts, 0)


class TestDownloaderLocal(unittest.TestCase):
    """用本地 HTTP 服务器把整条下载链路跑通（解析直链这一步被打桩）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mfd_dl_")
        self.dl_dir = os.path.join(self.tmp, "downloads")
        self.fail_path = os.path.join(self.tmp, "fail.json")
        _Handler.files = {}
        _Handler.deny_paths = set()
        _Handler.hits = {}
        _Handler.ranges = []
        self.payload = (b"0123456789abcdef" * 4096)  # 64 KB
        _Handler.files["/ok.bin"] = self.payload
        _Handler.files["/bad.bin"] = b"other content" * 100

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- 辅助 -----------------------------------------------------------
    def _make(self, max_threads=1, max_retries=0):
        self.events = []
        self.book = FailureBook(path=self.fail_path, autosave=False)
        return Downloader(
            download_dir=self.dl_dir,
            history=History(enabled=False),
            max_threads=max_threads,
            gap_seconds=0.0,
            on_event=lambda event, payload: self.events.append((event, payload)),
            max_retries=max_retries,
            timeout=(5, 10),
            failures=self.book,
        )

    def _run(self, downloader, url_path="/ok.bin", name="ok.bin", size=None,
             hash_value="", quickkey="q1"):
        real = _SERVER.base + url_path
        size = len(_Handler.files[url_path]) if size is None else size
        with mock.patch.object(mediafire, "resolve_download_url",
                               return_value=(real, size)):
            downloader.load_tasks([{
                "name": name, "url": "http://page/%s" % name, "size": size,
                "quickkey": quickkey, "folder": "", "hash": hash_value,
            }])
            started = downloader.start()
            self.assertTrue(started, "任务未启动")
            self._wait(downloader)
        return downloader.tasks[0]

    def _wait(self, downloader, timeout=30, min_finished=1):
        """等到 monitor 发出第 ``min_finished`` 个 finished 事件（而不是只看线程是否退出）。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            count = sum(1 for event, _ in self.events if event == "finished")
            if count >= min_finished:
                return
            time.sleep(0.05)
        raise AssertionError("下载超时未结束（已完成事件 %d 个）"
                             % sum(1 for event, _ in self.events if event == "finished"))

    # -- 用例 -----------------------------------------------------------
    def test_download_ok_with_sha256(self):
        digest = hashlib.sha256(self.payload).hexdigest().upper()
        dl = self._make()
        task = self._run(dl, hash_value=digest)
        self.assertEqual(task.status, STATUS_DONE, task.error)
        saved = os.path.join(self.dl_dir, "ok.bin")
        self.assertTrue(os.path.isfile(saved))
        with open(saved, "rb") as handle:
            self.assertEqual(handle.read(), self.payload)
        self.assertFalse(os.path.exists(saved + ".part"))
        self.assertEqual(len(self.book), 0, "成功后不应留下失败记录")
        self.assertTrue(any(event == "finished" for event, _ in self.events))

    def test_sha256_mismatch_marks_failed(self):
        dl = self._make()
        task = self._run(dl, hash_value="DEADBEEF" * 8)
        self.assertEqual(task.status, STATUS_FAILED)
        self.assertEqual(task.fail_kind, "hash_mismatch")
        self.assertEqual(len(self.book), 1)
        record = self.book.records[0]
        self.assertEqual(record.kind, "hash_mismatch")
        self.assertFalse(os.path.exists(os.path.join(self.dl_dir, "ok.bin")))

    def test_http_403_recorded(self):
        _Handler.deny_paths.add("/deny.bin")
        _Handler.files["/deny.bin"] = b"x"
        dl = self._make()
        task = self._run(dl, url_path="/deny.bin", name="deny.bin", quickkey="qd")
        self.assertEqual(task.status, STATUS_FAILED)
        self.assertIn(task.fail_kind, ("http_403", "unknown"))
        self.assertEqual(len(self.book), 1)
        self.assertTrue(self.book.records[0].message)

    def test_resume_from_part_file(self):
        _Handler.files["/big.bin"] = self.payload
        half = len(self.payload) // 2
        os.makedirs(self.dl_dir, exist_ok=True)
        part_path = os.path.join(self.dl_dir, "big.bin" + ".part")
        with open(part_path, "wb") as handle:
            handle.write(self.payload[:half])

        dl = self._make()
        task = self._run(dl, url_path="/big.bin", name="big.bin", quickkey="qb")
        self.assertEqual(task.status, STATUS_DONE, task.error)
        with open(os.path.join(self.dl_dir, "big.bin"), "rb") as handle:
            self.assertEqual(handle.read(), self.payload, "续传拼出来的文件必须完整")
        self.assertIn("bytes=%d-" % half, [r for r in _Handler.ranges if r])

    def test_existing_same_size_is_skipped(self):
        os.makedirs(self.dl_dir, exist_ok=True)
        with open(os.path.join(self.dl_dir, "ok.bin"), "wb") as handle:
            handle.write(self.payload)
        dl = self._make()
        hits_before = _Handler.hits.get("/ok.bin", 0)
        task = self._run(dl, name="ok.bin")
        self.assertEqual(task.status, STATUS_SKIPPED)
        self.assertEqual(_Handler.hits.get("/ok.bin", 0), hits_before,
                         "同大小文件已存在时不该再发请求")

    def test_retry_only_failed_items(self):
        """先让 A 失败、B 成功，修好 A 后只重试 A，B 不能被重下。"""
        _Handler.files["/a.bin"] = b"A" * 2048
        _Handler.files["/b.bin"] = b"B" * 2048
        _Handler.deny_paths.add("/a.bin")

        dl = self._make(max_threads=1, max_retries=0)
        real_a = _SERVER.base + "/a.bin"
        real_b = _SERVER.base + "/b.bin"

        def fake_resolve(file_page_url):
            if "a.bin" in file_page_url:
                return real_a, 2048
            return real_b, 2048

        with mock.patch.object(mediafire, "resolve_download_url", side_effect=fake_resolve):
            dl.load_tasks([
                {"name": "a.bin", "url": "http://page/a.bin", "size": 2048,
                 "quickkey": "qa", "folder": "", "hash": ""},
                {"name": "b.bin", "url": "http://page/b.bin", "size": 2048,
                 "quickkey": "qb", "folder": "", "hash": ""},
            ])
            dl.start()
            self._wait(dl)

            statuses = {t.name: t.status for t in dl.tasks}
            self.assertEqual(statuses["a.bin"], STATUS_FAILED)
            self.assertEqual(statuses["b.bin"], STATUS_DONE)
            self.assertEqual(len(self.book), 1)
            b_hits_before = _Handler.hits.get("/b.bin", 0)

            # 修复后只重试失败项
            _Handler.deny_paths.discard("/a.bin")
            retried = dl.retry_failed()
            self.assertEqual(retried, 1, "只应重试 1 个失败项")
            self._wait(dl, min_finished=2)

        statuses = {t.name: t.status for t in dl.tasks}
        self.assertEqual(statuses["a.bin"], STATUS_DONE, "重试后应成功")
        self.assertEqual(statuses["b.bin"], STATUS_DONE)
        self.assertEqual(_Handler.hits.get("/b.bin", 0), b_hits_before,
                         "已完成的任务不能被重新下载")
        self.assertEqual(len(self.book), 0, "重试成功后失败记录应被清掉")

    def test_load_tasks_dedupes(self):
        dl = self._make()
        items = [
            {"name": "x.bin", "url": "u", "size": 10, "quickkey": "k", "folder": "f", "hash": ""},
            {"name": "x.bin", "url": "u2", "size": 10, "quickkey": "k", "folder": "f", "hash": ""},
        ]
        tasks = dl.load_tasks(items)
        self.assertEqual(len(tasks), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
