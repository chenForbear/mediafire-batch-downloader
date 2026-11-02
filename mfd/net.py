#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""媒体火（MediaFire）批量下载器 —— HTTP 复用层：SSL 上下文缓存 + 代理 + 连接池。

为什么单独开一个模块
--------------------
``mfd.mediafire`` 里的 ``http_get`` 每次请求都新建一条 TCP + TLS 连接（它基于
``urllib``，不保留连接）。解析一个文件夹要发好几次接口请求，下载一个文件还要
再发一次直链探测——每一跳都重新握手，在移动网络和代理环境里就是肉眼可见的
等待。这里单独开一层"能复用连接"的 HTTP 实现，供后续逐步接入；**不改动**
``mediafire.http_get``（那是既有的、已经被 42 个离线用例盯住的行为）。

设计边界
--------
* 只用标准库：``http.client`` / ``ssl`` / ``socket`` / ``select`` / ``urllib.parse``。
* 不自定义异常：超时抛 ``socket.timeout``，证书问题抛 ``ssl.SSLError``，
  网络/协议问题抛 ``OSError`` 或 ``http.client.HTTPException``，参数与被支持的
  代理类型不对抛 ``ValueError``。调用方按标准库类型分类即可，不必认识本模块。
* 只做 HTTP 语义（重定向、代理、keep-alive），不做重试策略、不做正文解码、
  不判断 HTTP 状态码是否"成功"——那些属于调用方的业务判断。

代理规则
--------
* 读环境变量：``https_proxy`` / ``HTTPS_PROXY``（https 目标）、
  ``http_proxy`` / ``HTTP_PROXY``（http 目标），以及 ``no_proxy`` / ``NO_PROXY``。
* 代理协议只支持 http（https 目标时走 ``CONNECT`` 隧道）。遇到 ``socks://``、
  ``socks5://`` 一律抛 ``ValueError`` 并说明原因——**不静默绕过**：悄悄直连
  会让"以为走了代理"的用户暴露真实 IP，比报错严重得多。
* 回环地址（``localhost`` / ``127.0.0.0/8`` / ``::1``）永远直连，不走代理。
"""

import base64
import collections
import http.client
import os
import select
import socket
import ssl
import threading

try:  # Python 3.8 一定有，这里只是让老版本解释器也能导入本模块
    from urllib.parse import unquote, urljoin, urlsplit
except ImportError:  # pragma: no cover
    from urllib import unquote                              # type: ignore
    from urlparse import urljoin, urlsplit                  # type: ignore

__all__ = [
    "DEFAULT_TIMEOUT",
    "MAX_IDLE_CONNECTIONS",
    "MAX_REDIRECTS",
    "ConnectionPool",
    "build_ssl_context",
    "matches_no_proxy",
    "open_url",
    "resolve_proxy",
]

# 默认超时：(连接, 读取)。与 mediafire.CONNECT_TIMEOUT / READ_TIMEOUT 保持一致。
DEFAULT_TIMEOUT = (15, 30)

# 跟随重定向的最大跳数（第 6 跳直接报错，不无限跟）
MAX_REDIRECTS = 5

# 连接池里最多保留多少个空闲连接，超出按"最久未用"关闭
MAX_IDLE_CONNECTIONS = 8

# 会触发跟随的状态码
REDIRECT_STATUSES = frozenset((301, 302, 303, 307, 308))

# 默认请求头。UA 与 mediafire.DEFAULT_USER_AGENT 保持一致（MediaFire 会对过旧的
# UA 返回不同页面）；Accept-Encoding 显式要 identity，因为本模块把原始响应交给
# 调用方去 read()，要是被 gzip 了，调用方读到的就是压缩字节。
DEFAULT_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "identity",
    "Connection": "keep-alive",
}

_DEFAULT_PORTS = {"http": 80, "https": 443}


# ==========================================================================
# SSL 上下文
# ==========================================================================

_SSL_LOCK = threading.Lock()
_SSL_CONTEXT = None
_SSL_WARNING = ""


def build_ssl_context():
    """返回一个可复用的 SSLContext（缓存在模块级）。

    返回 ``(context, warning)`` 二元组：
      ``context``  已缓存的 ``ssl.SSLContext``，多次调用返回同一个对象；
      ``warning``  一句中文提示，证书库正常加载时为空串；退回"不校验证书"时
                   写明原因，调用方应当把它显示给用户（这属于安全降级，不能吞）。

    为什么缓存：``ssl.create_default_context()`` 每次都要读一遍系统证书库，
    在 Windows 上要解密 ``cert:\LocalMachine\Root``，单次十几毫秒；一次批量下载
    要发几十上百个请求，这个开销纯属白烧。
    """
    global _SSL_CONTEXT, _SSL_WARNING
    with _SSL_LOCK:
        if _SSL_CONTEXT is None:
            _SSL_CONTEXT, _SSL_WARNING = _create_ssl_context()
        return _SSL_CONTEXT, _SSL_WARNING


def _create_ssl_context():
    """真正干活的那一次：优先用系统证书，失败才降级。"""
    try:
        context = ssl.create_default_context()
        # Win7 的 OpenSSL 与部分老服务器只谈 TLS 1.2，显式放开 1.2/1.3
        try:
            context.minimum_version = ssl.TLSVersion.TLSv1_2
        except (AttributeError, ValueError):
            pass
        try:
            context.load_default_certs()
        except Exception as exc:            # 证书库读不出来但上下文能建：仍可用
            return context, "系统证书加载不完整（%s），HTTPS 可能有个别站点校验失败" % exc
        return context, ""
    except Exception as exc:
        context = _unverified_context()
        return context, ("SSL 上下文创建失败（%s），已退回不校验证书的模式："
                         "当前连接无法确认服务器身份，先保证能下载" % exc)


def _unverified_context():              # pragma: no cover - 只在系统 SSL 坏掉时走
    """造一个不校验证书的上下文（标准库公开接口，不用私有函数）。"""
    protocol = getattr(ssl, "PROTOCOL_TLS_CLIENT", ssl.PROTOCOL_SSLv23)
    context = ssl.SSLContext(protocol)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


# ==========================================================================
# 代理：环境变量 → (host, port, Proxy-Authorization)
# ==========================================================================

def _read_env(*names):
    """按顺序取第一个非空的环境变量值（环境变量名大小写不敏感，但这里照写）。"""
    for name in names:
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()
    return ""


def _is_loopback(host):
    """是不是本机回环地址。回环永远直连——走代理只会把本地测试搞坏。"""
    if not host:
        return False
    host = host.strip().lower()
    if host.startswith("["):
        host = host.strip("[]")
    if host == "localhost" or host == "::1" or host == "0.0.0.0":
        return True
    return host.startswith("127.")


def _strip_entry_port(entry):
    """去掉 no_proxy 条目里可能带的端口：``host:8080`` → ``host``。"""
    if entry.startswith("["):                      # [::1]:8080
        end = entry.find("]")
        if end != -1:
            return entry[1:end]
        return entry
    if entry.count(":") == 1:                      # host:port
        return entry.split(":", 1)[0]
    return entry


def matches_no_proxy(host, no_proxy=None):
    """判断 ``host`` 是否命中 ``no_proxy`` 规则（逗号分隔，域名后缀匹配）。

    ``no_proxy=None`` 时读环境变量 ``no_proxy`` / ``NO_PROXY``；显式传字符串
    便于单元测试，不必去改全局环境。

    匹配规则（对齐 curl / urllib 的常见做法）：
      ``*``              全部直连；
      ``example.com``    自身与其所有子域都直连；
      ``.example.com``   同上（前导点在两种写法里等价）；
      ``*.example.com``  同上（通配符只认这种写法）；
      ``127.0.0.1``      精确匹配；带端口（``host:8080``）时端口被忽略。
    """
    if not host:
        return False
    host = host.strip().lower().rstrip(".")
    if host.startswith("["):
        host = host.strip("[]")

    if no_proxy is None:
        no_proxy = _read_env("no_proxy", "NO_PROXY")
    if not no_proxy:
        return False

    for raw in no_proxy.split(","):
        entry = raw.strip().lower()
        if not entry:
            continue
        if entry == "*":
            return True
        entry = _strip_entry_port(entry).rstrip(".")
        if entry.startswith("*"):
            entry = entry[1:]
        entry = entry.lstrip(".")
        if not entry:
            continue
        if host == entry or host.endswith("." + entry):
            return True
    return False


def _mask_proxy(raw):
    """把代理地址里的账号密码抹掉，好写进报错信息里。"""
    if "@" not in raw:
        return raw
    head, _, tail = raw.rpartition("@")
    scheme = ""
    if "://" in head:
        scheme = head.split("://", 1)[0] + "://"
    return "%s***@%s" % (scheme, tail)


def _parse_proxy(raw):
    """把代理地址解析成 ``(host, port, auth_header)``；不支持的类型抛 ValueError。

    地址里没带账号时 ``auth_header`` 是 ``None``（不是空串），调用方可以
    直接用它判断"要不要发 Proxy-Authorization"。
    """
    text = raw.strip()
    if "://" not in text:
        text = "http://" + text
    parts = urlsplit(text)
    scheme = (parts.scheme or "http").lower()

    if scheme.startswith("socks"):
        raise ValueError(
            "不支持 SOCKS 代理（%s）：本模块只会走 http 代理。"
            "请把环境变量换成 http://主机:端口 的形式，或使用支持 socks 的客户端"
            % _mask_proxy(raw))
    if scheme not in ("http", ""):
        raise ValueError("不认识的代理协议「%s」（%s）：只支持 http 代理"
                         % (scheme, _mask_proxy(raw)))

    host = parts.hostname
    if not host:
        raise ValueError("代理地址缺少主机名：%s" % _mask_proxy(raw))
    try:
        port = parts.port or 8080
    except ValueError:
        raise ValueError("代理端口不合法：%s" % _mask_proxy(raw))

    auth = None
    if parts.username is not None:
        token = "%s:%s" % (unquote(parts.username), unquote(parts.password or ""))
        auth = "Basic " + base64.b64encode(token.encode("utf-8")).decode("ascii")
    return (host, int(port), auth)


def resolve_proxy(url):
    """按环境变量决定这次请求要不要走代理。

    返回 ``(host, port, auth_header)``；直连返回 ``None``。代理地址里没写
    账号时 ``auth_header`` 是 ``None``。``socks://`` 之类的代理会抛
    ``ValueError``（不静默直连）。
    """
    parts = urlsplit(url)
    host = parts.hostname or ""
    if _is_loopback(host):
        return None
    if matches_no_proxy(host):
        return None

    scheme = (parts.scheme or "http").lower()
    if scheme == "https":
        raw = _read_env("https_proxy", "HTTPS_PROXY")
        if not raw:                                  # 只配了 http_proxy 的环境
            raw = _read_env("http_proxy", "HTTP_PROXY")
    else:
        raw = _read_env("http_proxy", "HTTP_PROXY")
    if not raw:
        return None
    return _parse_proxy(raw)


# ==========================================================================
# 建连接 / 发请求
# ==========================================================================

def _split_timeout(timeout):
    """把超时参数统一成 ``(连接, 读取)`` 两段。"""
    if timeout is None:
        return float(DEFAULT_TIMEOUT[0]), float(DEFAULT_TIMEOUT[1])
    if isinstance(timeout, (tuple, list)):
        try:
            return float(timeout[0]), float(timeout[1])
        except (IndexError, TypeError, ValueError):
            return float(DEFAULT_TIMEOUT[0]), float(DEFAULT_TIMEOUT[1])
    try:
        value = float(timeout)
    except (TypeError, ValueError):
        value = float(DEFAULT_TIMEOUT[1])
    return value, value


def _tune_socket(sock, read_timeout):
    """连上之后把 socket 的超时切成"读取超时"。"""
    try:
        sock.settimeout(read_timeout)
    except (AttributeError, OSError):
        pass


def _check_url(url):
    """校验地址并拆出各部分；不合法就抛 ValueError。"""
    parts = urlsplit(url or "")
    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise ValueError("只支持 http/https 地址：%r" % (url,))
    if not parts.hostname:
        raise ValueError("地址里没有主机名：%r" % (url,))
    return parts


def _target_for(parts, proxy):
    """算出请求行里的目标字符串，以及需要额外带的头。

    直连（或 https 隧道内）用 ``/path?query``；走 http 代理访问 http 目标时
    必须用绝对 URI（``http://host:port/path``），这是 RFC 7230 对代理的要求。
    """
    path = parts.path or "/"
    target = path + ("?" + parts.query if parts.query else "")
    headers = {}
    if proxy is None:
        return target, headers
    if parts.scheme.lower() == "https":
        return target, headers          # CONNECT 隧道内仍是普通 origin-form
    host = parts.hostname
    default_port = _DEFAULT_PORTS.get(parts.scheme.lower(), 80)
    try:
        port = parts.port
    except ValueError:
        port = None
    if port and port != default_port:
        host = "%s:%d" % (host, port)
    return "%s://%s%s" % (parts.scheme.lower(), host, target), headers


def _tunnel_through_proxy(sock, host, port, auth, connect_timeout):
    """在 http 代理上开 CONNECT 隧道（https 目标必须走这条）。"""
    authority = "%s:%d" % (host, port)
    lines = ["CONNECT %s HTTP/1.1" % authority, "Host: %s" % authority]
    if auth:
        lines.append("Proxy-Authorization: %s" % auth)
    request = "\r\n".join(lines) + "\r\n\r\n"
    sock.sendall(request.encode("ascii"))

    sock.settimeout(connect_timeout)
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            raise OSError("代理在 CONNECT 阶段直接断开连接（%s）" % authority)
        data += chunk
        if len(data) > 65536:
            raise OSError("代理返回的 CONNECT 响应异常（超过 64KB 仍未结束）")

    status_line = data.split(b"\r\n", 1)[0].decode("latin-1", "replace").strip()
    pieces = status_line.split(" ", 2)
    if len(pieces) < 2 or pieces[1] != "200":
        raise OSError("代理拒绝建立隧道：%s" % status_line)
    # CONNECT 响应之后若有残留字节，那已经属于隧道内的数据；正常代理不会有，
    # 真有的话留给 TLS 握手会直接失败，比在这里瞎猜更安全。


def _new_connection(parts, timeout_pair, context, proxy):
    """新建一条连接，返回 ``(conn, target, extra_headers)``。"""
    connect_timeout, read_timeout = timeout_pair
    scheme = parts.scheme.lower()
    host = parts.hostname
    try:
        port = parts.port
    except ValueError:
        raise ValueError("端口不合法：%r" % (parts.netloc,))
    if port is None:
        port = _DEFAULT_PORTS[scheme]

    target, extra = _target_for(parts, proxy)

    if proxy is None:
        if scheme == "https":
            conn = http.client.HTTPSConnection(host, port, timeout=connect_timeout,
                                               context=context)
        else:
            conn = http.client.HTTPConnection(host, port, timeout=connect_timeout)
        conn.connect()
        _tune_socket(conn.sock, read_timeout)
        return conn, target, extra

    proxy_host, proxy_port, auth = proxy
    if auth:
        extra["Proxy-Authorization"] = auth

    if scheme == "https":
        sock = socket.create_connection((proxy_host, proxy_port), connect_timeout)
        try:
            _tunnel_through_proxy(sock, host, port, auth, connect_timeout)
            tls_sock = context.wrap_socket(sock, server_hostname=host)
        except Exception:
            try:
                sock.close()
            except OSError:
                pass
            raise
        _tune_socket(tls_sock, read_timeout)
        conn = http.client.HTTPSConnection(host, port, timeout=read_timeout,
                                           context=context)
        # 手动塞进已连接好的 TLS socket：HTTPSConnection.send() 见 sock 非空
        # 就不会再去 connect()，省掉第二次握手
        conn.sock = tls_sock
        return conn, target, extra

    conn = http.client.HTTPConnection(proxy_host, proxy_port, timeout=connect_timeout)
    conn.connect()
    _tune_socket(conn.sock, read_timeout)
    return conn, target, extra


def _merge_headers(headers):
    """默认头 + 调用方头（调用方的优先，允许用 None 屏蔽某个默认头）。"""
    merged = dict(DEFAULT_HEADERS)
    if headers:
        for key, value in headers.items():
            if value is None:
                merged.pop(key, None)
            else:
                merged[key] = value
    return merged


def _close_quietly(target):
    """关掉连接/响应，出错不吭声（清理路径不该抛异常）。"""
    if target is None:
        return
    try:
        close = getattr(target, "close", None)
        if close is not None:
            close()
    except Exception:
        pass


def _discard_response(response):
    """把响应体读掉再关，让连接可以干净地进入下一轮。"""
    if response is None:
        return
    try:
        response.read()
    except Exception:
        pass
    _close_quietly(response)


def _bind_connection(response, conn):
    """让 ``response.close()`` 顺带把连接也关掉。

    ``http.client`` 的响应不持有连接——谁建的谁负责关。``open_url`` 是一次性
    请求，把关闭动作挂到响应的 ``close`` 上，调用方按常规写
    ``response.read()`` 再 ``response.close()`` 就不会漏 socket：否则 fd 要等
    垃圾回收才释放，Windows 上还会刷 ``ResourceWarning``。

    连接池不走这条路：池里的连接要留着复用，由 ``pool.close()`` 统一收摊。
    """
    original_close = response.close

    def _close_with_connection():
        try:
            original_close()
        finally:
            _close_quietly(conn)

    try:
        response.close = _close_with_connection
    except (AttributeError, TypeError):          # pragma: no cover - 兜底分支
        _close_quietly(conn)


def open_url(url, headers=None, timeout=DEFAULT_TIMEOUT, method="GET"):
    """发一次请求，返回 ``http.client.HTTPResponse``（已 send 完，可直接 read）。

    参数
    ----
    url      要请求的地址（http / https）。
    headers  额外请求头；值为 ``None`` 表示**去掉**同名默认头。
    timeout  ``(连接超时, 读取超时)``，也接受单个数字；默认 ``(15, 30)``。
    method   HTTP 方法，默认 ``GET``（本模块不发送请求体）。

    行为
    ----
    * 跟随 301/302/303/307/308，最多 5 跳，``Location`` 用 ``urljoin`` 解析
      （相对地址、协议相对地址都能正确处理）。
    * 303 以及"301/302 + 非 GET/HEAD"会改成 GET 再跟（浏览器就是这么干的）。
    * https 用模块级缓存的 SSLContext。
    * 走环境变量代理，http 代理访问 https 目标时用 CONNECT 隧道。

    异常
    ----
    只用标准库类型：``socket.timeout``（超时）、``ssl.SSLError``（证书/握手）、
    ``OSError``（连接被拒、代理拒绝隧道等）、``http.client.HTTPException``
    （协议错乱）、``ValueError``（地址非法 / 不支持 SOCKS / 重定向超过 5 跳）。

    调用方负责读取正文并关闭返回的响应；``close()`` 会顺带把底层连接关掉
    （这一层不复用连接，要复用请用 :class:`ConnectionPool`）。
    """
    context, _warning = build_ssl_context()
    timeout_pair = _split_timeout(timeout)
    current_url = url
    current_method = method
    redirects = 0

    while True:
        parts = _check_url(current_url)
        proxy = resolve_proxy(current_url)
        conn, target, extra = _new_connection(parts, timeout_pair, context, proxy)
        try:
            all_headers = _merge_headers(headers)
            all_headers.update(extra)
            conn.request(current_method, target, headers=all_headers)
            response = conn.getresponse()
        except Exception:
            _close_quietly(conn)
            raise

        location = response.getheader("Location")
        if response.status not in REDIRECT_STATUSES or not location:
            # 交给调用方之前把连接的寿命挂到响应上：读到 read()/close() 就一起收
            _bind_connection(response, conn)
            return response

        # 这一跳只是个中转：把正文读掉、连接关掉，再跟下一跳
        _discard_response(response)
        _close_quietly(conn)

        if redirects >= MAX_REDIRECTS:
            raise ValueError("重定向超过 %d 跳，已停止跟随（起始地址：%s）"
                             % (MAX_REDIRECTS, url))
        redirects += 1
        if response.status == 303 or (response.status in (301, 302)
                                      and current_method.upper() not in ("GET", "HEAD")):
            current_method = "GET"
        current_url = urljoin(current_url, location)


# ==========================================================================
# 连接池
# ==========================================================================

class ConnectionPool(object):
    """极简 keep-alive 连接池：按 (scheme, host, port) 复用 http.client 连接。

    用法::

        pool = ConnectionPool()
        response = pool.request(url, headers=..., timeout=...)
        body = response.read()          # 读完，连接才能干净地被复用
        pool.close()

    要点
    ----
    * 线程安全：所有对池的操作都在 ``RLock`` 里。
    * 复用前检查连接是否已被服务器关闭（``select`` 探 sock + 上一次响应是否
      读完）；send 失败时丢掉旧连接、重建一次再试。
    * 最多保留 8 个空闲连接，超出按"最久未用"关闭。
    * ``close()`` 关闭全部连接，可重复调用；关掉之后照样能继续 ``request``
      （会重新建连），不会因为状态位炸掉。

    走代理时，池键里额外带上代理身份 ``(host, port)``——同一个目标经不同代理
    是两条完全不同的 TCP 连接，混用会串数据。
    """

    def __init__(self, max_idle=MAX_IDLE_CONNECTIONS):
        self._lock = threading.RLock()
        # 有序字典当 LRU：末尾是最近用的，开头是最久没用的
        self._idle = collections.OrderedDict()
        self._max_idle = max(1, int(max_idle))
        self._closed = False

    # -- 对外接口 ------------------------------------------------------
    def request(self, url, headers=None, timeout=DEFAULT_TIMEOUT, method="GET"):
        """同 ``open_url``，但尽量复用连接。返回的响应由调用方读取并关闭。"""
        context, _warning = build_ssl_context()
        timeout_pair = _split_timeout(timeout)
        current_url = url
        current_method = method
        redirects = 0

        while True:
            parts = _check_url(current_url)
            proxy = resolve_proxy(current_url)
            key = self._key_for(parts, proxy)

            conn, target, extra, reused = self._acquire(key, parts, timeout_pair,
                                                        context, proxy)
            all_headers = _merge_headers(headers)
            all_headers.update(extra)
            try:
                conn.request(current_method, target, headers=all_headers)
                response = conn.getresponse()
            except (OSError, http.client.HTTPException):
                # 复用的连接多半已经被服务器悄悄关掉了：丢掉重建一次再试。
                # 新连接还失败就是真失败，交给调用方。
                self._drop(key, conn)
                if not reused:
                    raise
                conn, target, extra = _new_connection(parts, timeout_pair,
                                                      context, proxy)
                try:
                    conn.request(current_method, target, headers=all_headers)
                    response = conn.getresponse()
                except Exception:
                    _close_quietly(conn)
                    raise

            location = response.getheader("Location")
            is_redirect = (response.status in REDIRECT_STATUSES and bool(location))
            if not is_redirect:
                self._release(key, conn, response)
                return response

            _discard_response(response)
            if getattr(response, "will_close", False):
                _close_quietly(conn)
            else:
                self._release(key, conn, response)

            if redirects >= MAX_REDIRECTS:
                raise ValueError("重定向超过 %d 跳，已停止跟随（起始地址：%s）"
                                 % (MAX_REDIRECTS, url))
            redirects += 1
            if response.status == 303 or (response.status in (301, 302)
                                          and current_method.upper()
                                          not in ("GET", "HEAD")):
                current_method = "GET"
            current_url = urljoin(current_url, location)

    def close(self):
        """关闭所有空闲连接。可重复调用，关完之后还能继续用这个池。"""
        with self._lock:
            entries = list(self._idle.values())
            self._idle.clear()
            self._closed = True
        for conn, _response in entries:
            _close_quietly(conn)

    @property
    def idle_count(self):
        """当前空闲连接数（诊断与测试用）。"""
        with self._lock:
            return len(self._idle)

    @property
    def closed(self):
        """是否已经被 ``close()`` 过。注意：关过之后仍可继续 ``request``。"""
        with self._lock:
            return self._closed

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
        return False

    # -- 内部：池的存取 ------------------------------------------------
    @staticmethod
    def _key_for(parts, proxy):
        """池键：(scheme, host, port, 代理身份)。"""
        scheme = parts.scheme.lower()
        try:
            port = parts.port or _DEFAULT_PORTS[scheme]
        except ValueError:
            port = _DEFAULT_PORTS[scheme]
        proxy_part = (proxy[0], proxy[1]) if proxy else ()
        return (scheme, (parts.hostname or "").lower(), int(port), proxy_part)

    def _acquire(self, key, parts, timeout_pair, context, proxy):
        """取一条可用连接；没有可用连接就新建。返回 ``(conn, target, extra, reused)``。"""
        entry = None
        with self._lock:
            entry = self._idle.pop(key, None)        # 取出即视为"最近使用"
        if entry is not None:
            conn, response = entry
            if self._reusable(conn, response):
                target, extra = _target_for(parts, proxy)
                _tune_socket(getattr(conn, "sock", None), timeout_pair[1])
                if proxy and proxy[2]:
                    extra["Proxy-Authorization"] = proxy[2]
                return conn, target, extra, True
            _close_quietly(conn)
        conn, target, extra = _new_connection(parts, timeout_pair, context, proxy)
        return conn, target, extra, False

    @staticmethod
    def _reusable(conn, response):
        """判断一条空闲连接还能不能接着用。"""
        if conn is None:
            return False
        if response is not None and not getattr(response, "isclosed", lambda: True)():
            return False        # 上一次的正文没读完，连接里还有残留数据
        sock = getattr(conn, "sock", None)
        if sock is None:
            return False        # 服务器回了 Connection: close，连接已自行关闭
        try:
            readable = select.select([sock], [], [], 0)[0]
        except (OSError, ValueError):
            return False        # socket 已被关闭（select 会直接报错）
        # 可读 = 要么服务器已发 FIN（读到 b""），要么上一次没读完还有残留字节；
        # 两种情况都不该复用，一律重建。
        return not readable

    def _release(self, key, conn, response):
        """把用完的连接放回池里（超出上限就按 LRU 关掉最久未用的）。"""
        if conn is None or getattr(response, "will_close", False):
            _close_quietly(conn)
            return
        evicted = []
        with self._lock:
            old = self._idle.pop(key, None)
            if old is not None:
                evicted.append(old[0])
            self._idle[key] = (conn, response)
            while len(self._idle) > self._max_idle:
                _old_key, (dead_conn, _dead_resp) = self._idle.popitem(last=False)
                evicted.append(dead_conn)
        for dead in evicted:
            _close_quietly(dead)

    def _drop(self, key, conn):
        """把一条坏掉的连接从池里彻底移除并关闭。"""
        with self._lock:
            entry = self._idle.pop(key, None)
            if entry is not None and entry[0] is not conn:
                # 池里躺着的是另一条好连接，别误伤：塞回去
                self._idle[key] = entry
        _close_quietly(conn)
