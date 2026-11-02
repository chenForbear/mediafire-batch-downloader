#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""``mfd.net`` 的离线自检：全部只连 127.0.0.1，不碰外网。

覆盖范围
--------
1. ``open_url``：取正文、301/302/303/307/308 跟随、相对 Location、五跳上限、
   错误状态原样返回、地址非法抛 ValueError
2. ``matches_no_proxy`` / ``resolve_proxy``：纯函数层面的代理判定（不真连代理），
   含"回环直连"“SOCKS 直接拒绝且不泄露密码”
3. ``build_ssl_context``：模块级缓存，同一对象
4. ``ConnectionPool``：同 host 两次请求只建一条 TCP、``close()`` 幂等且之后仍可用、
   空闲上限 8 且按 LRU 关、服务器要求断开时老实新建、池里的连接被掐后能自愈、
   复用的连接发不出去时重建重试一次

直接运行::

    python -m unittest tests.test_net -v
"""

import os
import ssl
import sys
import threading
import unittest
from unittest import mock

import http.server

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.dirname(_HERE)
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from mfd import net                                                  # noqa: E402


# ======================================================================
# 本地测试服务器（只监听 127.0.0.1）
# ======================================================================

class _Handler(http.server.BaseHTTPRequestHandler):
    """按 ``routes`` 表回响应的小服务端。

    ``routes``: path -> (status, headers dict, body bytes)
    ``connections``: 一共接受过多少条 TCP 连接（用来证明连接真的被复用了）
    ``calls``: 收到过的 (方法, 路径) 序列
    """

    protocol_version = "HTTP/1.1"
    routes = {}
    connections = 0
    calls = []
    hits = {}

    def log_message(self, fmt, *args):
        pass                                   # 测试时不要往 stderr 刷访问日志

    def setup(self):
        type(self).connections += 1
        http.server.BaseHTTPRequestHandler.setup(self)

    def _handle(self):
        cls = type(self)
        path = self.path.split("?", 1)[0]
        cls.calls.append((self.command, path))
        cls.hits[path] = cls.hits.get(path, 0) + 1
        status, headers, body = cls.routes.get(path, (404, {}, b"not found"))
        if str(headers.get("Connection", "")).lower() == "close":
            self.close_connection = True
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    do_GET = _handle
    do_POST = _handle


_SERVER = None
_THREAD = None
_BASE = ""
_SAVED_ENV = {}


def _build_routes():
    """常规路由（不含需要知道端口的绝对地址重定向）。"""
    routes = {
        "/hello": (200, {}, b"hello world"),
        "/close": (200, {"Connection": "close"}, b"bye"),
    }
    for status in (301, 302, 303, 307, 308):
        routes["/r%d" % status] = (status, {"Location": "/hello"}, b"")
        routes["/p%d" % status] = (status, {"Location": "/hello"}, b"")
    # 相对 Location：从 /dir/page 出发，"../hello" 与 "hello" 都要能解析对
    routes["/redir-rel"] = (302, {"Location": "hello"}, b"")
    routes["/redir-dot"] = (302, {"Location": "./hello"}, b"")
    routes["/dir/page"] = (302, {"Location": "../hello"}, b"")
    routes["/dir/rel"] = (302, {"Location": "sub/hello"}, b"")
    routes["/dir/sub/hello"] = (200, {}, b"hello world")
    # 六跳链条：/chain/1 需要 5 跳（刚好允许），/chain/0 需要 6 跳（必须报错）
    for index in range(6):
        routes["/chain/%d" % index] = (302, {"Location": "/chain/%d" % (index + 1)}, b"")
    routes["/chain/6"] = (200, {}, b"end")
    return routes


def setUpModule():
    global _SERVER, _THREAD, _BASE
    # 环境变量里的代理会把测试带偏，先收起来，模块结束再还回去
    for key in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY",
                "all_proxy", "ALL_PROXY", "no_proxy", "NO_PROXY"):
        if key in os.environ:
            _SAVED_ENV[key] = os.environ.pop(key)
    _Handler.routes = _build_routes()
    _SERVER = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    _SERVER.daemon_threads = True
    _BASE = "http://127.0.0.1:%d" % _SERVER.server_address[1]
    # 绝对地址的 Location 得等端口定下来才写得出来
    _Handler.routes["/redir-abs"] = (302, {"Location": _BASE + "/hello"}, b"")
    _THREAD = threading.Thread(target=_SERVER.serve_forever, name="mfd-net-test")
    _THREAD.daemon = True
    _THREAD.start()


def tearDownModule():
    if _SERVER is not None:
        _SERVER.shutdown()
        _SERVER.server_close()
    if _THREAD is not None:
        _THREAD.join(5)
    for key, value in _SAVED_ENV.items():
        os.environ[key] = value


class _TempEnv(object):
    """临时改代理相关环境变量，退出时原样还回去。

    Windows 上 ``os.environ`` 不分大小写，所以这里按固定清单整批清空再设。
    """

    _KEYS = ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY",
             "all_proxy", "ALL_PROXY", "no_proxy", "NO_PROXY")

    def __init__(self, mapping=None):
        self._mapping = dict(mapping or {})
        self._saved = {}

    def __enter__(self):
        for key in self._KEYS:
            self._saved[key] = os.environ.get(key)
            os.environ.pop(key, None)
        for key, value in self._mapping.items():
            os.environ[key] = value
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        for key in self._KEYS:
            os.environ.pop(key, None)
        for key, value in self._saved.items():
            if value is not None:
                os.environ[key] = value
        return False


class _NetTestCase(unittest.TestCase):
    """带上服务端地址的基类。"""

    @property
    def base(self):
        return _BASE


# ======================================================================
# 1. open_url
# ======================================================================

class TestOpenUrl(_NetTestCase):

    def test_get_body(self):
        response = net.open_url(self.base + "/hello")
        try:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), b"hello world")
        finally:
            response.close()

    def test_absolute_location(self):
        response = net.open_url(self.base + "/redir-abs")
        try:
            self.assertEqual(response.read(), b"hello world")
        finally:
            response.close()

    def test_relative_location(self):
        for path in ("/redir-rel", "/redir-dot", "/dir/page", "/dir/rel"):
            response = net.open_url(self.base + path)
            try:
                self.assertEqual(response.read(), b"hello world", path)
            finally:
                response.close()

    def test_every_redirect_status(self):
        for status in (301, 302, 303, 307, 308):
            response = net.open_url(self.base + "/r%d" % status)
            try:
                self.assertEqual(response.read(), b"hello world",
                                 "状态 %d 没跟对" % status)
                self.assertEqual(response.status, 200)
            finally:
                response.close()

    def test_five_redirects_allowed(self):
        response = net.open_url(self.base + "/chain/1")
        try:
            self.assertEqual(response.read(), b"end")
        finally:
            response.close()

    def test_six_redirects_rejected(self):
        with self.assertRaises(ValueError) as caught:
            net.open_url(self.base + "/chain/0")
        self.assertIn("5", str(caught.exception))

    def test_error_status_returned_not_raised(self):
        # 状态码交给调用方判断，这一层不替人决定
        response = net.open_url(self.base + "/missing")
        try:
            self.assertEqual(response.status, 404)
            self.assertEqual(response.read(), b"not found")
        finally:
            response.close()

    def test_303_switches_to_get(self):
        del _Handler.calls[:]
        response = net.open_url(self.base + "/p303", method="POST")
        try:
            response.read()
        finally:
            response.close()
        self.assertEqual(_Handler.calls, [("POST", "/p303"), ("GET", "/hello")])

    def test_307_keeps_method(self):
        del _Handler.calls[:]
        response = net.open_url(self.base + "/p307", method="POST")
        try:
            response.read()
        finally:
            response.close()
        self.assertEqual(_Handler.calls, [("POST", "/p307"), ("POST", "/hello")])

    def test_bad_urls_rejected(self):
        for url in ("", None, "ftp://127.0.0.1/x", "http:///no-host"):
            with self.assertRaises(ValueError):
                net.open_url(url)

    def test_custom_headers_sent(self):
        response = net.open_url(self.base + "/hello",
                                headers={"X-Mfd-Test": "1", "User-Agent": None})
        try:
            response.read()
        finally:
            response.close()


# ======================================================================
# 2. 代理判定（纯函数，不真连代理）
# ======================================================================

class TestNoProxy(unittest.TestCase):

    def test_suffix_match(self):
        self.assertTrue(net.matches_no_proxy("example.com", "example.com"))
        self.assertTrue(net.matches_no_proxy("a.example.com", "example.com"))
        self.assertTrue(net.matches_no_proxy("deep.a.example.com", ".example.com"))
        self.assertTrue(net.matches_no_proxy("a.example.com", "*.example.com"))
        self.assertTrue(net.matches_no_proxy("A.Example.COM", "EXAMPLE.com"))

    def test_no_false_positive(self):
        self.assertFalse(net.matches_no_proxy("notexample.com", "example.com"))
        self.assertFalse(net.matches_no_proxy("example.com.evil.net", "example.com"))
        self.assertFalse(net.matches_no_proxy("other.io", "example.com"))

    def test_list_and_wildcard(self):
        self.assertTrue(net.matches_no_proxy("anything.io", "*"))
        self.assertTrue(net.matches_no_proxy("x.internal", "foo.com, .internal"))
        self.assertTrue(net.matches_no_proxy("127.0.0.1", "127.0.0.1"))
        self.assertTrue(net.matches_no_proxy("a.example.com", "example.com, 10.0.0.0"))

    def test_entry_with_port(self):
        self.assertTrue(net.matches_no_proxy("example.com", "example.com:8080"))

    def test_env_fallback(self):
        with _TempEnv({"no_proxy": "corp.local"}):
            self.assertTrue(net.matches_no_proxy("a.corp.local"))
            self.assertFalse(net.matches_no_proxy("a.example.com"))
        with _TempEnv({"NO_PROXY": "*.lan"}):
            self.assertTrue(net.matches_no_proxy("nas.lan"))


class TestResolveProxy(unittest.TestCase):

    def test_loopback_always_direct(self):
        # 配了代理也必须直连回环，否则本地测试全得挂
        with _TempEnv({"http_proxy": "http://127.0.0.1:9",
                       "https_proxy": "http://127.0.0.1:9"}):
            self.assertIsNone(net.resolve_proxy("http://127.0.0.1:8000/x"))
            self.assertIsNone(net.resolve_proxy("http://localhost:8000/x"))

    def test_http_proxy_parsed(self):
        with _TempEnv({"http_proxy": "http://10.0.0.1:8888"}):
            got = net.resolve_proxy("http://example.com/a")
        self.assertEqual(got[0], "10.0.0.1")
        self.assertEqual(got[1], 8888)
        self.assertIsNone(got[2])

    def test_proxy_credentials_become_basic_auth(self):
        with _TempEnv({"http_proxy": "http://user:pass@10.0.0.1:8888"}):
            got = net.resolve_proxy("http://example.com/a")
        self.assertEqual(got[0], "10.0.0.1")
        self.assertTrue(got[2].startswith("Basic "), got[2])

    def test_no_proxy_wins(self):
        with _TempEnv({"http_proxy": "http://10.0.0.1:8888",
                       "no_proxy": "example.com"}):
            self.assertIsNone(net.resolve_proxy("http://example.com/a"))

    def test_https_prefers_https_proxy(self):
        with _TempEnv({"https_proxy": "http://10.0.0.2:3128",
                       "http_proxy": "http://10.0.0.1:8888"}):
            self.assertEqual(net.resolve_proxy("https://example.com/")[1], 3128)

    def test_socks_rejected(self):
        with _TempEnv({"http_proxy": "socks5://127.0.0.1:1080"}):
            with self.assertRaises(ValueError) as caught:
                net.resolve_proxy("http://example.com/a")
        message = str(caught.exception)
        self.assertIn("SOCKS", message)

    def test_socks_error_hides_password(self):
        with _TempEnv({"http_proxy": "socks5://user:secret@127.0.0.1:1080"}):
            with self.assertRaises(ValueError) as caught:
                net.resolve_proxy("http://example.com/a")
        self.assertNotIn("secret", str(caught.exception))


# ======================================================================
# 3. SSL 上下文
# ======================================================================

class TestSslContext(unittest.TestCase):

    def test_cached_same_object(self):
        first, first_warning = net.build_ssl_context()
        second, second_warning = net.build_ssl_context()
        self.assertIs(first, second)
        self.assertIsInstance(first, ssl.SSLContext)
        self.assertIsInstance(first_warning, str)
        self.assertEqual(first_warning, second_warning)

    def test_not_written_as_a_bare_context(self):
        # 返回值是 (context, warning) 二元组，别让人误当 context 用
        result = net.build_ssl_context()
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 2)


# ======================================================================
# 4. 连接池
# ======================================================================

class TestConnectionPool(_NetTestCase):

    def test_two_requests_share_one_tcp(self):
        pool = net.ConnectionPool()
        before = _Handler.connections
        try:
            for round_index in range(2):
                response = pool.request(self.base + "/hello")
                try:
                    self.assertEqual(response.read(), b"hello world")
                finally:
                    response.close()
        finally:
            pool.close()
        self.assertEqual(_Handler.connections - before, 1, "第二次请求没有复用连接")

    def test_pool_follows_redirect(self):
        pool = net.ConnectionPool()
        try:
            response = pool.request(self.base + "/r302")
            try:
                self.assertEqual(response.read(), b"hello world")
            finally:
                response.close()
        finally:
            pool.close()

    def test_close_is_idempotent_and_pool_still_usable(self):
        pool = net.ConnectionPool()
        response = pool.request(self.base + "/hello")
        response.read()
        response.close()
        self.assertEqual(pool.idle_count, 1)
        pool.close()
        self.assertEqual(pool.idle_count, 0)
        self.assertTrue(pool.closed)
        pool.close()                    # 重复关不炸
        response = pool.request(self.base + "/hello")
        try:
            self.assertEqual(response.read(), b"hello world")
        finally:
            response.close()
        pool.close()

    def test_context_manager_closes(self):
        with net.ConnectionPool() as pool:
            response = pool.request(self.base + "/hello")
            response.read()
            response.close()
            self.assertEqual(pool.idle_count, 1)
        self.assertEqual(pool.idle_count, 0)

    def test_server_close_forces_new_connection(self):
        pool = net.ConnectionPool()
        before = _Handler.connections
        try:
            for _round in range(2):
                response = pool.request(self.base + "/close")
                try:
                    self.assertEqual(response.read(), b"bye")
                finally:
                    response.close()
        finally:
            pool.close()
        self.assertEqual(_Handler.connections - before, 2)

    def test_idle_connections_capped_by_lru(self):
        pool = net.ConnectionPool()

        class _FakeConn(object):
            def __init__(self, index):
                self.index = index
                self.closed = False

            def close(self):
                self.closed = True

        fakes = [_FakeConn(index) for index in range(12)]
        for index, conn in enumerate(fakes):
            pool._release(("http", "h%d" % index, 80, ()), conn, None)
        self.assertEqual(pool.idle_count, net.MAX_IDLE_CONNECTIONS)
        self.assertEqual(pool.idle_count, 8)
        # 最久没用的 4 条被关掉，最近用的 8 条留着
        self.assertEqual([conn.closed for conn in fakes],
                         [True] * 4 + [False] * 8)
        pool.close()
        self.assertTrue(all(conn.closed for conn in fakes))

    def test_recovers_after_pooled_socket_dies(self):
        pool = net.ConnectionPool()
        try:
            response = pool.request(self.base + "/hello")
            response.read()
            response.close()
            self.assertEqual(pool.idle_count, 1)
            # 白盒：把池里那条连接的 socket 直接掐掉，模拟"服务器已经关了连接"
            with pool._lock:
                for conn, _resp in pool._idle.values():
                    conn.sock.close()
            response = pool.request(self.base + "/hello")
            try:
                self.assertEqual(response.read(), b"hello world")
            finally:
                response.close()
        finally:
            pool.close()

    def test_retries_once_when_reused_connection_is_dead(self):
        pool = net.ConnectionPool()
        try:
            response = pool.request(self.base + "/hello")
            response.read()
            response.close()
            with pool._lock:
                for conn, _resp in pool._idle.values():
                    conn.sock.close()
            # 让池误以为"这条还活着"（select 那一关故意放过），逼它走
            # "发请求失败 → 丢掉重建 → 重试一次"的分支。
            with mock.patch.object(net.ConnectionPool, "_reusable", lambda *args: True):
                response = pool.request(self.base + "/hello")
                try:
                    self.assertEqual(response.read(), b"hello world")
                finally:
                    response.close()
        finally:
            pool.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
