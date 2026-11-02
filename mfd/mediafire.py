#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MediaFire 交互层：解析文件夹、拿直链、取文件信息。

只用标准库（``urllib`` / ``json`` / ``re`` / ``html`` / ``concurrent.futures``）。
旧版用 ``requests`` + ``BeautifulSoup`` + ``chardet``，其中 ``chardet`` 从未被调用，
``bs4`` 只为了取一个属性；换成标准库后包体大幅缩小，也少三个可能出兼容问题的
C 扩展。子文件夹的并发解析用的是标准库的 ``concurrent.futures``，没有引入任何
第三方依赖。

所有对外函数在失败时抛 ``MediaFireError``，并同时携带：
  * ``user_message``：给普通用户看的中文说明（GUI 直接显示这一条）
  * ``detail``：技术细节（日志里记录，便于排查）
旧版把异常整个吞掉只 print 一行，用户只能看到"请确保链接是 /folder/ 类型"，
这是最影响可用性的缺陷。
"""

import concurrent.futures
import json
import re
import socket
import ssl
import threading
import time
from html import unescape
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlencode, urljoin, urlparse
from urllib.request import (HTTPRedirectHandler, HTTPSHandler, Request,
                            build_opener)

from . import __version__
from .util import human_size, sanitize_filename

# 默认 UA。刻意用较新的 Chrome 版本号：MediaFire 会针对过旧的 UA 返回不同页面。
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

CONNECT_TIMEOUT = 15
READ_TIMEOUT = 30
# 读响应体失败时的重试次数（实测走代理访问 mediafire 时偶发读超时）
READ_RETRIES = 3
API_BASE = "https://www.mediafire.com/api/1.4"

# 同一层子文件夹并发拉取的线程数。4 是折中值：1 路太慢（每个子目录都要一次
# 完整 HTTP 往返，200 个子目录实测 5 分钟以上），8 路以上 MediaFire 会开始回 429。
FOLDER_WORKERS = 4
# 单个请求最多自己跟几跳重定向（MediaFire 的 CDN 直链经常给 302/308）
MAX_REDIRECTS = 5
# 需要我们自己跟的 3xx 状态码（urllib 原生只认 301/302/303/307，不认 308）
REDIRECT_CODES = (301, 302, 303, 307, 308)

# 文件夹 API 返回的状态码含义（error 字段）
API_ERROR_MEANING = {
    129: "文件夹链接里的 key 无效或不是文件夹（常见原因：把单个文件的链接当成文件夹链接）",
    127: "文件夹不存在或已被删除",
    128: "文件夹是私密的，需要登录才能访问",
}


class MediaFireError(Exception):
    """统一的对外错误类型。"""

    def __init__(self, user_message, detail=""):
        super().__init__(user_message)
        self.user_message = user_message
        self.detail = detail or ""

    def __str__(self):
        return self.user_message


# --------------------------------------------------------------------------
# SSL / 网络基础
# --------------------------------------------------------------------------

def _build_ssl_context():
    """构造 SSL 上下文。

    Win7 的 OpenSSL 与部分老服务器只谈 TLS 1.2，某些站点在 TLS 1.3 握手上会失败；
    这里显式允许 1.2 与 1.3，并加载系统证书（``load_default_certs`` 在 3.8 可用）。
    """
    context = ssl.create_default_context()
    try:
        context.minimum_version = ssl.TLSVersion.TLSv1_2
    except (AttributeError, ValueError):
        pass
    try:
        context.load_default_certs()
    except Exception:
        pass
    return context


_SSL_CONTEXT = None
# 子文件夹改成并发解析后，多个工作线程会同时走到懒加载这里。不加锁也不会崩，
# 但会各自构造一个 SSL 上下文、把系统证书库重复加载一遍（Win7 上实测几百毫秒）；
# 加一把锁只需一次。
#
# 用**可重入**锁是防呆：这把锁有两处使用者（``get_ssl_context`` 与
# ``_get_opener``），一旦有人在持锁期间又调了另一个函数，普通 ``Lock`` 会当场
# 自锁——2026-11-02 实测现象是进程卡死在第一跳重定向上、连 socket 超时都不成立，
# 只能杀进程。``_get_opener`` 那边已经改成"先取上下文再上锁"，不依赖重入；
# 这里再用 ``RLock``，等于同一个坑上放两道保险。
_SSL_LOCK = threading.RLock()


def get_ssl_context():
    global _SSL_CONTEXT
    if _SSL_CONTEXT is None:
        with _SSL_LOCK:
            if _SSL_CONTEXT is None:
                _SSL_CONTEXT = _build_ssl_context()
    return _SSL_CONTEXT


class _NoAutoRedirect(HTTPRedirectHandler):
    """把 urllib 自带的"自动跟重定向"关掉。

    ``urlopen`` 默认会自己跟 301/302/303/307（最多 10 跳），但**不认 308**，
    而且它跟到第 11 跳时抛的 ``HTTPError`` 里带着 ``Location``，外面再套一层跟随
    就会变成"每层跟 10 跳、外层再来 5 轮"，实际跳数远超我们承诺的上限
    （实测：一个自指的重定向能把请求打到 30 次）。

    所以这里干脆让 urllib 一跳都不跟、一律抛 ``HTTPError``，转由 :func:`_open_url`
    逐跳推进——这样"最多 5 跳"才是真的 5 跳，308 也就自然被覆盖到了。
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        """返回 ``None`` = 不构造新的请求，让 urllib 按错误抛上来。"""
        return None


_OPENER = None


def _get_opener():
    """懒加载一个"不自动跟重定向"的 opener（SSL 上下文复用 ``get_ssl_context``）。

    写法上坚持**先取上下文、再上锁**：虽然 ``_SSL_LOCK`` 现在已经是 ``RLock``，
    但让锁里只包一句赋值，就不用指望"锁是可重入的"这个隐含前提——2026-11-02
    吃过一次亏，持锁再调 ``get_ssl_context()`` 时进程直接卡死在第一跳重定向上，
    连 socket 超时都不成立，只能杀进程。
    """
    global _OPENER
    if _OPENER is None:
        context = get_ssl_context()
        with _SSL_LOCK:
            if _OPENER is None:
                _OPENER = build_opener(
                    _NoAutoRedirect(),
                    HTTPSHandler(context=context),
                )
    return _OPENER


def _open_url(url, headers, timeout_value):
    """发 GET 并返回响应对象；重定向由本函数自己一跳一跳跟着走。

    为什么要自己跟：``urllib`` 自带的处理会跟 301/302/303/307，但 **不认 308**
    （Python 3.8 的 ``HTTPRedirectHandler`` 里没有 ``http_error_308``），而 MediaFire
    的 CDN 直链实测会用 302/308 把人往别的域上带。遇到 308 时旧代码会把它翻译成
    "直链已过期或被拦截"，用户看到的现象就是"解析得好好的，下载却莫名失败"。

    规则：3xx 且带 ``Location`` 就继续走（相对地址用 ``urllib.parse.urljoin`` 补齐，
    实测见过 ``Location: /ab12/x.zip`` 这种写法），最多 ``MAX_REDIRECTS`` 跳；
    超过就把最后一个 ``HTTPError`` 原样抛出，交给调用方按普通 HTTP 错误处理。
    """
    current = url
    hop = 0
    while True:
        request = Request(current, headers=headers, method="GET")
        try:
            return _get_opener().open(request, timeout=timeout_value)
        except HTTPError as exc:
            location = ""
            if exc.headers is not None:
                location = exc.headers.get("Location") or ""
            if exc.code in REDIRECT_CODES and location.strip() and hop < MAX_REDIRECTS:
                # 这一跳不打算交给调用方，先把它自己的响应体关掉，免得泄漏连接
                try:
                    exc.close()
                except Exception:
                    pass
                hop += 1
                current = urljoin(current, location.strip())
                continue
            raise


def http_get(url, headers=None, timeout=None, stream=False, read_retries=READ_RETRIES):
    """发起 GET 请求，返回 ``(response, raw)``。

    ``stream=True`` 时只返回响应对象，由调用方读取；否则把正文读出来。
    ``response`` 是 ``http.client.HTTPResponse``，调用方负责关闭。

    ``read_retries``：把**响应体读出来**这一步的重试次数。为什么要单独重试
    这一步——实测遇到过：连接和响应头都正常，卡在 ``resp.read()`` 上
    ``socket.timeout``（走代理访问 MediaFire 时接口响应慢、分块读取中途停住）。
    旧写法只对"建立连接"做了 try，读正文这一步失败就直接抛给用户，表现是
    "解析着解析着报网络超时"。现在读失败会自动重连重读，最多 3 次。

    重定向由 :func:`_open_url` 负责跟着走（含 urllib 原生不支持的 308，
    以及相对 ``Location``），最多 ``MAX_REDIRECTS`` 跳。
    """
    all_headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept": "*/*",
    }
    if headers:
        all_headers.update(headers)

    timeout_value = timeout or READ_TIMEOUT
    try:
        resp = _open_url(url, all_headers, timeout_value)
    except HTTPError as exc:
        # MediaFire 的接口在参数不合法时返回 HTTP 400，但正文里带着 error 码
        # （实测：error=129）。这个正文比"HTTP 400"有用得多，所以先读出来。
        body_text = ""
        try:
            body_text = decode_body(exc.read(), exc.headers.get("Content-Type", ""))
        except Exception:
            body_text = ""
        api_note = _api_error_note(body_text)
        reason = _http_error_message(exc.code, url)
        if api_note:
            reason += "（%s）" % api_note
        raise MediaFireError(
            reason,
            "HTTP %s %s; body=%r" % (exc.code, exc.reason, body_text[:300]),
        )
    except socket.timeout as exc:
        raise MediaFireError(
            "网络超时：连接 MediaFire 超过 %d 秒没有响应。请检查网络或代理。" % timeout_value,
            "socket.timeout: %s" % exc,
        )
    except URLError as exc:
        reason = getattr(exc, "reason", exc)
        raise MediaFireError(
            "无法连接 MediaFire：%s。请检查网络连通性或代理设置。" % reason,
            "URLError: %r" % (reason,),
        )
    except ssl.SSLError as exc:
        raise MediaFireError(
            "SSL 握手失败（可能是系统缺少根证书或补丁不全）。",
            "ssl.SSLError: %s" % exc,
        )
    except OSError as exc:
        raise MediaFireError("网络错误：%s" % exc, "OSError: %r" % (exc,))

    if stream:
        return resp, None
    # 读正文这一步单独重试：见函数文档里说明的实测场景。
    attempt = 0
    while True:
        try:
            raw = resp.read()
            break
        except HTTPError as exc:
            resp.close()
            body_text = ""
            try:
                body_text = decode_body(exc.read(), exc.headers.get("Content-Type", ""))
            except Exception:
                body_text = ""
            api_note = _api_error_note(body_text)
            reason = _http_error_message(exc.code, url)
            if api_note:
                reason += "（%s）" % api_note
            raise MediaFireError(
                reason,
                "HTTP %s %s; body=%r" % (exc.code, exc.reason, body_text[:300]),
            )
        except socket.timeout as exc:
            resp.close()
            attempt += 1
            if attempt > read_retries:
                raise MediaFireError(
                    "网络超时：MediaFire 在传数据的过程中停住了（已重试 %d 次）。"
                    "请检查网络或代理，稍后再试。" % read_retries,
                    "socket.timeout during read: %s" % exc,
                )
            time.sleep(min(2 ** attempt, 8))
            resp, _ = http_get(url, headers=headers, timeout=timeout, stream=True,
                               read_retries=read_retries)
        except (URLError, ssl.SSLError, OSError) as exc:
            resp.close()
            attempt += 1
            if attempt > read_retries:
                raise MediaFireError(
                    "读取数据时网络中断（已重试 %d 次）：%s" % (read_retries, exc),
                    "read error: %r" % (exc,),
                )
            time.sleep(min(2 ** attempt, 8))
            resp, _ = http_get(url, headers=headers, timeout=timeout, stream=True,
                               read_retries=read_retries)
    resp.close()
    return resp, raw


def _api_error_note(body_text):
    """从 MediaFire 的错误正文里取出可读说明；取不到返回空串。

    解决的是实测遇到的一个问题：非法 key 时接口返回 HTTP 400，如果只看状态码
    就只会告诉用户"HTTP 400"，而正文里其实写着 error=129（key 无效）。
    """
    if not body_text:
        return ""
    try:
        payload = json.loads(body_text)
    except (ValueError, TypeError):
        return ""
    response = (payload or {}).get("response") or {}
    message = response.get("message")
    if not message:
        return ""
    note = str(message)
    hint = API_ERROR_MEANING.get(response.get("error"))
    if hint:
        note += "，" + hint
    return note


def _http_error_message(code, url):
    if code == 404:
        return "MediaFire 返回 404：文件或页面不存在（可能已被删除）。"
    if code == 400:
        return "MediaFire 返回 400：请求被拒绝，通常说明这一串 ID 无效或不是文件夹。"
    if code == 403:
        return (
            "MediaFire 返回 403：拒绝访问。可能是直链已过期、"
            "或该文件被设为私密，也可能是运营商/代理被拦截。"
        )
    if code == 429:
        return "MediaFire 返回 429：请求过于频繁，请把「下载间隔」调大后重试。"
    if 500 <= code < 600:
        return "MediaFire 服务器返回 %d：对方服务异常，请稍后重试。" % code
    return "MediaFire 返回 HTTP %d（%s）。" % (code, urlparse(url).netloc)


def decode_body(raw, content_type=""):
    """把响应体解码成文本。

    旧版固定 ``res.text``（requests 猜测编码）。MediaFire 页面可能是 UTF-8，
    也可能在 charset 里声明其他编码；这里按 charset → UTF-8 → GBK 的顺序尝试，
    避免文件名乱码。
    """
    if raw is None:
        return ""
    charset = None
    match = re.search(r"charset=([\w\-]+)", content_type or "", re.I)
    if match:
        charset = match.group(1)
    candidates = [charset, "utf-8", "gbk", "latin-1"]
    for enc in candidates:
        if not enc:
            continue
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


# --------------------------------------------------------------------------
# 链接识别
# --------------------------------------------------------------------------

_FOLDER_KEY_RE = re.compile(r"mediafire\.com/(?:folder|file)/([A-Za-z0-9]+)", re.I)
_FOLDER_KEY_ALT_RE = re.compile(r"[?&]folder_key=([A-Za-z0-9]+)", re.I)
# 形如 https://www.mediafire.com/?xxxxxxxx 的"裸 key"链接。
# 这是 MediaFire 分享出来的短格式，用户原文说明里要求手工改写成 /folder/xxx/ 才能解析，
# 新版自动识别，不再要求用户自己改。
_BARE_KEY_RE = re.compile(r"mediafire\.com/\??[?#]?([A-Za-z0-9]{11,})", re.I)


def extract_folder_key(url):
    """从用户粘贴的链接里取出 folder key。

    支持三种形态：
      * ``https://www.mediafire.com/folder/<key>/<名称>``
      * ``https://www.mediafire.com/?<key>``（旧文档要求手工改写的短格式，这里自动支持）
      * 带 ``folder_key=`` 参数的 API 链接
    取不到时抛 ``MediaFireError`` 并给出可操作的提示。
    """
    text = (url or "").strip()
    if not text:
        raise MediaFireError("请先粘贴 MediaFire 文件夹链接。", "empty url")

    match = _FOLDER_KEY_RE.search(text)
    if match:
        return match.group(1)
    match = _FOLDER_KEY_ALT_RE.search(text)
    if match:
        return match.group(1)

    if "mediafire.com" not in text.lower():
        raise MediaFireError(
            "这看起来不是 MediaFire 链接。请粘贴形如 "
            "https://www.mediafire.com/folder/xxxxxxxx/名称 的文件夹链接。",
            "not mediafire: %s" % text[:200],
        )

    # 兜底：/folder/ 与 file/ 都没有，但整条链接里有一串 11 位以上的 key
    match = _BARE_KEY_RE.search(text)
    if match:
        return match.group(1)

    raise MediaFireError(
        "无法从链接里识别出文件夹 ID。请打开该文件夹页面，"
        "从地址栏复制 /folder/ 后面那一串字符所在网址。",
        "no key in: %s" % text[:200],
    )


def extract_query_key(url):
    """取出 ``https://www.mediafire.com/?<key>`` 里的裸 key；没有则返回空串。"""
    text = (url or "").strip()
    match = re.search(r"mediafire\.com/\??[?#]([A-Za-z0-9]{11,})", text, re.I)
    if match:
        return match.group(1)
    match = re.search(r"mediafire\.com/\?([A-Za-z0-9]{11,})", text, re.I)
    return match.group(1) if match else ""


# --------------------------------------------------------------------------
# 文件夹 API
# --------------------------------------------------------------------------

def _api_get(action, params):
    """调用 MediaFire 公开 API 并返回 ``response`` 节点。"""
    query = dict(params)
    query["response_format"] = "json"
    url = "%s/%s.php?%s" % (API_BASE, action, urlencode(query))
    resp, raw = http_get(url, headers={"Accept": "application/json"})
    text = decode_body(raw, resp.headers.get("Content-Type", ""))
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise MediaFireError(
            "MediaFire 返回的内容不是有效 JSON（可能被运营商或代理拦截篡改）。",
            "json decode failed: %s; head=%r" % (exc, text[:200]),
        )
    response = payload.get("response") or {}
    result = response.get("result")
    if result != "Success":
        code = response.get("error")
        message = response.get("message") or "未知错误"
        hint = API_ERROR_MEANING.get(code)
        user_msg = "MediaFire 接口返回错误：%s" % message
        if hint:
            user_msg += "（%s）" % hint
        raise MediaFireError(user_msg, "action=%s error=%s message=%s" % (action, code, message))
    return response


def looks_like_file(key):
    """判断一个 key 是不是"单个文件"（而不是文件夹）。

    背景：MediaFire 的短链接 ``https://www.mediafire.com/?<key>`` 既可能是文件夹
    也可能是单个文件，光看链接无法分辨。旧文档的做法是"用户自己去把链接改成
    ``/folder/<key>/``，能解析就说明是文件夹"，改不了就下不了。
    新版改为：先按文件夹试，接口回 error 129（key 类型不符）时自动改按文件处理，
    把单个文件也加进队列，用户不必手改链接。
    """
    if not key:
        return False
    try:
        info = get_file_info(key)
    except MediaFireError:
        return False
    return bool(info.get("name"))


def single_file_item(key):
    """把一个文件 key 包装成与 ``list_folder_files()`` 同构的条目。"""
    info = get_file_info(key)
    name = sanitize_filename(info.get("name") or "", "unknown_file")
    return {
        "name": name,
        "url": file_page_from_quickkey(key),
        "size": info.get("size", 0),
        "quickkey": key,
        "folder": "",
        "hash": info.get("hash", ""),
    }


def list_folder_files(folder_key, include_subfolders=True, on_progress=None):
    """列出一个文件夹下的全部文件（自动翻页，可选递归子文件夹）。

    返回 ``[{'name','url','size','quickkey','folder'}, ...]``。
    旧版只取第一页、且完全不看子文件夹（``more_chunks`` 与 ``folders`` 两个
    字段根本没被读取），这是旧工具最实质的功能缺陷。

    **遍历顺序**：逐层广度优先。结果顺序 == "根目录文件 → 第一层各子文件夹
    （按接口给出顺序）的文件 → 第二层 ……"，与本地文件夹树的阅读顺序一致，
    也是旧串行版本的行为，界面的排序、断点续传都对得上。

    **并发**：同一层的子文件夹交给 ``FOLDER_WORKERS`` 个线程同时拉（每个子文件夹
    一次 HTTP 往返，串行时层数一多就成倍变慢）。回收时**按提交顺序取 result**，
    所以哪怕 B 文件夹先拉完，结果里 B 也不会跑到 A 前面——并发不改变输出顺序。
    只有一个文件夹时不开线程池，与旧版完全同码。

    **进度回调的语义变了**：``on_progress`` 现在拿到的是**累计已解析的文件数**
    （以前是"当前这个文件夹内已翻到多少"）。并发时不可能让每个线程各自回调一次，
    否则界面上的数字会来回跳；改成每层收完在主线程回调一次，数字只增不减。

    若该 key 实际指向单个文件，会抛 ``MediaFireError``（detail 里含 ``error=129``）；
    调用方应改用 :func:`single_file_item`。
    """
    collected = []
    visited = set()
    level = [(folder_key, "")]
    while level:
        # 先按出现顺序去重再发请求：同一层里两个文件夹指向同一个子文件夹时只拉一次
        batch = []
        for key, prefix in level:
            if key in visited:
                continue
            visited.add(key)
            batch.append((key, prefix))
        if not batch:
            break

        if len(batch) == 1:
            # 最常见的一条路径（只有一个根目录 / 只有一个子文件夹）：不建线程池
            key, prefix = batch[0]
            progress = _offset_progress(on_progress, len(collected))
            results = [_fetch_folder_page(key, prefix, progress, include_subfolders)]
        else:
            results = _fetch_level(batch, include_subfolders)

        next_level = []
        for files, subfolders in results:
            collected.extend(files)
            if include_subfolders:
                next_level.extend(subfolders)
        if on_progress:
            on_progress(len(collected))
        level = next_level
    return collected


def _offset_progress(callback, base):
    """把"这个文件夹里已取多少"的回调，包成"整体已取多少"的回调。

    ``_fetch_folder_page`` 只知道自己翻了几个 chunk；界面要的是全局进度，
    所以在这里把基数加上去。没有回调时返回 ``None``（也让下游少一层判断）。
    """
    if not callback:
        return None

    def report(count):
        callback(base + count)

    return report


def _fetch_level(batch, want_subfolders=True):
    """并发拉取同一层的多个文件夹，返回与 ``batch`` **同序**的 (files, subfolders)。

    顺序稳定的关键在于：``as_completed`` 拿到的是"谁先好"，不是"谁排前面"，
    所以用 ``future -> 下标`` 的映射把结果写回原来的坑位，最后按数组顺序返回。
    失败的那个不能当作空结果——串行版本会直接抛出去，并发版也必须抛，
    否则"少了几个文件夹的文件"这种错会被静默吞掉。多个同时失败时抛下标最小的
    那个，跟串行时先走到谁就抛谁一致。
    """
    results = [None] * len(batch)
    failure = None
    with concurrent.futures.ThreadPoolExecutor(max_workers=FOLDER_WORKERS) as pool:
        futures = {}
        for index, (key, prefix) in enumerate(batch):
            # 各线程内的翻页进度不单独上报：并发回调会让界面数字乱跳，
            # 由 list_folder_files 在每层收完后统一报一次
            futures[pool.submit(_fetch_folder_page, key, prefix, None,
                                want_subfolders)] = index
        for future in concurrent.futures.as_completed(futures):
            index = futures[future]
            try:
                results[index] = future.result()
            except Exception as exc:  # noqa: BLE001 —— 原样重抛，这里只是记下
                if failure is None or index < failure[0]:
                    failure = (index, exc)
    if failure is not None:
        raise failure[1]
    return results


def _fetch_folder_page(folder_key, prefix, on_progress=None, want_subfolders=True):
    """翻页取完一个文件夹的全部文件；同时返回其中的子文件夹。"""
    files = []
    subfolder_count = 0
    chunk = 1
    while True:
        params = {
            "content_type": "files",
            "folder_key": folder_key,
            "chunk": chunk,
            "chunk_size": 100,
        }
        response = _api_get("folder/get_content", params)
        content = response.get("folder_content") or {}
        subfolder_count = max(subfolder_count, _to_int(content.get("folder_count")))
        for item in content.get("files") or []:
            link = ((item.get("links") or {}).get("normal_download")) or ""
            quickkey = item.get("quickkey") or ""
            if not link and quickkey:
                link = "https://www.mediafire.com/file/%s/file" % quickkey
            name = prefix + sanitize_filename(item.get("filename") or "", "unknown_file")
            files.append({
                "name": name,
                "url": link,
                "size": _to_int(item.get("size")),
                "quickkey": quickkey,
                "folder": prefix.rstrip("/"),
                # 接口给的官方 SHA-256（实测字段名就是 hash）。下载完会拿它校验，
                # 比只看大小可靠：大小是四舍五入显示的，哈希不会骗人。
                "hash": (item.get("hash") or "").strip().upper(),
            })
        if on_progress:
            on_progress(len(files))

        # more_chunks 是字符串 "yes"/"no"，旧版从未读取
        more = str(content.get("more_chunks", "no")).lower()
        if more == "yes":
            chunk += 1
            if chunk > 200:  # 防御：避免极端情况下无限翻页
                break
        else:
            break

    subfolders = []
    # 调用方明确说"不要子文件夹"时，连这一趟都别发——不然白花一个 HTTP 往返，
    # 结果拿回来还要被上层丢掉（深目录树上这能省掉成百次无用请求）。
    if subfolder_count <= 0 or not want_subfolders:
        return files, subfolders
    params = {"content_type": "folders", "folder_key": folder_key, "chunk": 1, "chunk_size": 100}
    try:
        response = _api_get("folder/get_content", params)
        content = response.get("folder_content") or {}
        for item in content.get("folders") or []:
            sub_key = item.get("folderkey")
            sub_name = sanitize_filename(item.get("name") or "", "subfolder")
            if sub_key:
                subfolders.append((sub_key, prefix + sub_name + "/"))
    except MediaFireError:
        # 子文件夹列举失败不该让整次解析失败：主目录文件已经拿到了
        pass

    return files, subfolders


def _to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def get_file_info(quick_key):
    """按 quickkey 取单个文件信息（匿名可用）。

    用于"只给了一个文件链接"的场景：先确认它是不是文件，再给出文件大小。
    """
    response = _api_get("file/get_info", {"quick_key": quick_key})
    info = response.get("file_info") or {}
    return {
        "name": info.get("filename") or "",
        "size": _to_int(info.get("size")),
        "quickkey": quick_key,
        # 实测 file/get_info 也返回官方 SHA-256（字段名 hash）
        "hash": (info.get("hash") or "").strip().upper(),
    }


# --------------------------------------------------------------------------
# 文件夹显示名（"自动以文件夹名建子目录"要靠它）
# --------------------------------------------------------------------------

def _url_folder_name(url):
    """从 URL 路径里取出 ``/folder/<key>/<名称>`` 的名称原文；没有就返回空串。

    只做切分与 ``unquote``，**不清洗**——是否可用交给 ``sanitize_filename`` 判。
    先 ``urlparse`` 再按 ``/`` 切，这样"带查询参数""带末尾斜杠""大小写混写"
    （``/Folder/``）、以及没写协议头的 ``www.mediafire.com/folder/...``
    都能走通，不用写五六个正则。
    """
    text = (url or "").strip()
    if not text:
        return ""
    path = urlparse(text).path or ""
    segments = [part for part in path.split("/") if part]
    for index, part in enumerate(segments):
        if part.lower() == "folder" and index + 1 < len(segments):
            # 第三段才是名字；只有两段说明这条链接只带了 key
            if index + 2 < len(segments):
                return unquote(segments[index + 2])
            return ""
    return ""


def folder_display_name(url, folder_key=""):
    """尽力拿到这个 MediaFire 文件夹的人类可读名字，取不到就返回空串。

    优先级：
      1. 链接自带的 ``/folder/<key>/<名称>``。分享链接通常会把文件夹名拼在末尾
         （``%5B...%5D`` 这类转义解码后就是原样），这一步不发请求、不会失败；
      2. 链接里没有名字时问 ``folder/get_info``，读 ``response.folder_info.name``。
         多一次 HTTP 往返，换来的是"只有 key 的短链接也能按名字建目录"；
      3. 都不行返回空串，调用方据此退回"不建子目录"。

    名字会先过 :func:`mfd.util.sanitize_filename`：下一步经常直接拿它当目录名，
    而 Windows 不允许 ``:`` ``?`` 这些字符。

    **本函数不抛异常**——"拿不到名字"是正常分支（链接里没有、接口抽风、key 失效），
    不该让整个下载流程中断。
    """
    raw = _url_folder_name(url)
    if raw:
        name = sanitize_filename(raw, "")
        if name:
            return name

    key = (folder_key or "").strip()
    if not key:
        try:
            key = extract_folder_key(url)
        except MediaFireError:
            key = ""
    if not key:
        return ""

    try:
        response = _api_get("folder/get_info", {"folder_key": key})
        info = (response or {}).get("folder_info") or {}
        name = info.get("name") or ""
        return sanitize_filename(str(name), "") if name else ""
    except Exception:
        # 网络抖动、接口改版、key 无效……都只是"拿不到名字"，吞掉即可
        return ""


def resolve_folder_meta(url):
    """界面层的便捷入口：一次拿到 ``{'name': 显示名, 'key': folder key}``。

    ``resolve_link`` 的返回值（``(items, description)``）被下载器、界面和历史
    记录多处依赖，不能为了带出一个文件夹名去改它的签名；想要"以文件夹名建子目录"
    的调用方走这里，一次拿两样。

    链接不是 MediaFire、或识别不出 key 时两个字段都返回空串，**不抛异常**。
    """
    key = ""
    try:
        key = extract_folder_key(url)
    except MediaFireError:
        key = ""
    return {"name": folder_display_name(url, key), "key": key}


# --------------------------------------------------------------------------
# 下载页 → 直链
# --------------------------------------------------------------------------

# MediaFire 的下载按钮属性顺序不固定，实测出现过 href 在前、id 在后，
# 因此两种顺序都要匹配（旧版用 BeautifulSoup 所以不受影响，换成正则后必须注意）。
_BUTTON_PATTERNS = (
    re.compile(r'id=["\']downloadButton["\'][^>]*?href=["\']([^"\']+)["\']', re.I),
    re.compile(r'href=["\']([^"\']+)["\'][^>]*?id=["\']downloadButton["\']', re.I),
)

_SIZE_IN_BUTTON_RE = re.compile(
    r"download\s*\(\s*([\d.,]+)\s*(bytes|kb|kib|mb|mib|gb|gib|tb)\s*\)", re.I
)

_UNIT_FACTORS = {
    "bytes": 1, "b": 1,
    "kb": 1000, "kib": 1024,
    "mb": 1000 ** 2, "mib": 1024 ** 2,
    "gb": 1000 ** 3, "gib": 1024 ** 3,
    "tb": 1000 ** 4,
}


def parse_download_button(html_text):
    """从下载页 HTML 里取出 ``(直链, 页面显示大小)``。

    取不到直链时抛 ``MediaFireError``，并区分三种常见情况：
      * 文件已被删除/取消分享 → 页面里通常有 ``File Removed`` / ``dangerous`` 字样
      * 需要密码 → ``password`` 表单
      * 其它 → 把页面标题一起报出来，便于判断
    """
    if not html_text:
        raise MediaFireError("MediaFire 返回了空页面。", "empty html")

    # 页面里可能出现多个 downloadButton（例如"下载"与"直接下载"两处），
    # 逐个取、优先返回 download*.mediafire.com 域名的那个。
    candidates = []
    for pattern in _BUTTON_PATTERNS:
        for match in pattern.finditer(html_text):
            href = unescape(match.group(1)).strip()
            if href and href not in candidates:
                candidates.append(href)

    direct = [c for c in candidates if "download" in urlparse(c).netloc.lower()]
    link = (direct or candidates or [None])[0]

    size = None
    size_match = _SIZE_IN_BUTTON_RE.search(html_text)
    if size_match:
        try:
            value = float(size_match.group(1).replace(",", ""))
            size = int(value * _UNIT_FACTORS.get(size_match.group(2).lower(), 1))
        except (ValueError, KeyError):
            size = None

    if not link:
        lowered = html_text.lower()
        title_match = re.search(r"<title>(.*?)</title>", html_text, re.I | re.S)
        title = unescape(title_match.group(1)).strip() if title_match else ""
        removed_marks = (
            "file removed", "file has been removed", "has been removed",
            "been deleted", "no longer available", "removed or deleted",
            "file you are trying to download",  # "…is no longer available"
            "this file is not available",
        )
        if any(mark in lowered for mark in removed_marks):
            raise MediaFireError(
                "该文件已被删除或取消分享，无法下载。", "page says file removed; title=%s" % title
            )
        if "password" in lowered and "downloadbutton" not in lowered:
            raise MediaFireError(
                "该文件需要密码才能下载，本工具暂不支持带密码的分享。",
                "page contains password form; title=%s" % title,
            )
        raise MediaFireError(
            "页面里找不到下载按钮（MediaFire 可能改了页面结构）。页面标题：%s" % (title or "无"),
            "no downloadButton in %d bytes of html" % len(html_text),
        )

    return link, size


def resolve_download_url(file_page_url):
    """把 ``/file/<key>/...`` 页面地址解析成真实直链。

    直链是带会话校验的临时地址，必须即时解析、即时使用，不能缓存进历史文件。
    """
    resp, raw = http_get(file_page_url)
    return parse_download_button(decode_body(raw, resp.headers.get("Content-Type", "")))


def probe_remote_size(file_page_url):
    """取一个文件的真实大小：先看下载页按钮文本，取不到再问直链的 Content-Length。"""
    link, size = resolve_download_url(file_page_url)
    if size:
        return link, size
    resp, _ = http_get(link, headers={"Range": "bytes=0-0"}, timeout=CONNECT_TIMEOUT, stream=True)
    try:
        content_range = resp.headers.get("Content-Range") or ""
        total = _to_int(resp.headers.get("Content-Length"))
        match = re.search(r"/(\d+)$", content_range)
        if match:
            total = int(match.group(1))
    finally:
        resp.close()
    return link, total


def describe_key_kind(url):
    """判断链接指向文件还是文件夹，供 GUI 给出更准确的提示。"""
    parsed = urlparse((url or "").strip())
    path = parsed.path or ""
    if "/folder/" in path:
        return "folder"
    if "/file/" in path or "/download/" in path:
        return "file"
    return "unknown"


def resolve_link(url, include_subfolders=True, on_progress=None):
    """统一的"给一条链接，我告诉你里面有什么"入口。

    返回 ``(items, description)``。会自动区分三种情况，用户不需要先判断链接类型、
    也不需要像旧文档要求的那样手工把 ``?key`` 改写成 ``/folder/key/``：

      * ``/folder/<key>`` → 列出文件夹全部文件（含翻页与子文件夹）
      * ``/file/<key>`` 或 ``/download/...`` → 单个文件
      * ``?<key>``（裸 key）→ 先当文件夹试，接口报 key 类型不符时自动当单个文件
    """
    kind = describe_key_kind(url)
    if kind == "file":
        key = _FOLDER_KEY_RE.search(url)
        if not key:
            raise MediaFireError("无法从这个文件链接里识别出文件 ID。", "no key in %s" % url[:200])
        item = single_file_item(key.group(1))
        return [item], "单个文件"

    folder_key = extract_folder_key(url)
    try:
        items = list_folder_files(folder_key, include_subfolders=include_subfolders,
                                  on_progress=on_progress)
    except MediaFireError as exc:
        # error=129 = key 类型不是文件夹 → 很有可能这其实是个单文件短链接
        if "error=129" not in (exc.detail or ""):
            raise
        if kind == "folder":
            # 链接明确写了 /folder/ 却解析不了：确实是链接有问题，原样报错
            raise
        if not looks_like_file(folder_key):
            raise
        item = single_file_item(folder_key)
        return [item], "单个文件（由 ?key 短链接自动识别）"

    if not items:
        raise MediaFireError(
            "这个文件夹里没有可下载的文件（可能是空文件夹，或文件都是私密的）。",
            "folder_key=%s returned 0 files" % folder_key,
        )
    folders = len({i.get("folder", "") for i in items if i.get("folder")})
    description = "%d 个文件" % len(items)
    if folders:
        description += "，分布在 %d 个子文件夹" % folders
    return items, description


def file_page_from_quickkey(quick_key):
    return "https://www.mediafire.com/file/%s/file" % quote(quick_key)


def debug_cert_hint():
    """给 Win7 用户的排错提示（GUI「帮助」里显示）。"""
    return (
        "Windows 7 无法连接时的检查顺序：\n"
        "1) 确认已安装 SP1 与 KB3140245，并启用 TLS 1.2；\n"
        "2) 系统时间正确（证书校验依赖时间）；\n"
        "3) 若使用代理，确认代理本身可用；\n"
        "4) 若提示缺少 api-ms-win-crt-*.dll，安装 KB2999226（通用 C 运行库）。"
    )


def version_banner():
    return "MediaFire 批量下载器 v%s（Python %s）" % (
        __version__,
        getattr(__import__("sys"), "version", "").split()[0],
    )
