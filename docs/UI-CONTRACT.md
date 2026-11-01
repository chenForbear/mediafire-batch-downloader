# UI 契约（2.0）—— 界面层与内核层之间唯一的接口

界面代码一律放在 `mfd/ui/`，禁止在那里写解析/下载逻辑；内核一律不 import tkinter。

## 1. 内核对外 API（界面只能调这些）

```python
from mfd import APP_NAME, __version__
from mfd.mediafire import MediaFireError, resolve_link, describe_key_kind
from mfd.downloader import Downloader, DownloadTask, STATUS_TEXT
from mfd.failures import FailureBook, FailureRecord, KIND_TEXT
from mfd.storage import History, default_download_dir
from mfd.diagnostics import Diagnostics, build_report
```

### 1.1 解析

```python
items, description = resolve_link(url, include_subfolders=True, on_progress=None)
# items: list[dict]，键固定为 name / url / size / quickkey / folder / hash
# description: str，例如 "128 个文件，分布在 3 个子文件夹"
# 失败抛 MediaFireError：.user_message（给用户看）、.detail（技术细节）
```

### 1.2 下载引擎

```python
dl = Downloader(
    download_dir="D:/Downloads",
    history=History(),                 # 可 None
    max_threads=2,                     # 1..8
    gap_seconds=1.0,
    on_event=cb,                       # cb(event: str, payload: Any)
    max_retries=3,
    failures=FailureBook(),            # 失败清单（可 None）
)
dl.load_tasks(items)      # -> list[DownloadTask]，顺带标出历史命中（STATUS_SKIPPED）
dl.start()                # -> bool
dl.stop(); dl.stop_and_wait(5.0)
dl.pause(); dl.resume()
dl.stats()                # dict，见下
dl.retry_failed()         # -> int 被重新排队的失败任务数（只跑失败项；内部会自己 start()）
dl.tasks                  # list[DownloadTask]
```

`dl.stats()` 返回：

```python
{
  "counts": {"waiting":0,"running":0,"done":0,"skipped":0,"failed":0,"canceled":0},
  "total": 0, "finished": 0, "bytes": 0, "elapsed": 0.0,
  "speed": 0.0,          # KB/s
  "paused": False,
  "failed_items": 0,     # = counts["failed"]
}
```

### 1.3 事件（`on_event(event, payload)`，可能来自工作线程！）

| event      | payload                                   | 界面该做什么                     |
|------------|-------------------------------------------|----------------------------------|
| `log`      | `str`                                     | 追加到日志区，按前缀分色         |
| `task`     | `DownloadTask`                            | 刷新该行（要求线程安全）         |
| `progress` | `dict`（同 stats）                        | 刷新顶部概览/进度条              |
| `failure`  | `FailureRecord`                           | 失败清单加一行、角标 +1          |
| `finished` | `dict`（同 stats）                        | 结束态：按钮复位、弹出摘要       |

**线程规则**：事件来自下载线程。界面层必须 `queue.Queue` 转主线程（`root.after(80, pump)`），
不得在回调里直接碰 tkinter 控件。

### 1.4 任务对象字段

`name, url, size, quickkey, folder, hash_value, status, error, saved_path,
downloaded, speed, started_at, finished_at, attempts, percent(property), eta_seconds()`

> 单位约定（照真身，别按直觉写）：`percent` 返回 **0~100**（不是 0~1）；
> `speed` 是 **KB/s**；`size` / `downloaded` 是字节；`eta_seconds()` 返回秒。

状态常量：`waiting / running / done / skipped / failed / canceled`，
文案用 `STATUS_TEXT[status]`。

### 1.5 失败清单

```python
book = FailureBook(path=None)      # 默认 %LOCALAPPDATA%\MediaFireDownloader2\failures.json
book.add(record)                   # 幂等：同 quickkey+name 覆盖
book.records                       # list[FailureRecord]
book.by_kind()                     # dict[kind] -> list[FailureRecord]
book.by_platform()                 # dict[platform_label] -> list[FailureRecord]
book.to_items()                    # -> list[dict]，可直接 load_tasks() 重试
book.export_json() / export_markdown() / export_links_txt()  # -> str
book.clear() ; book.save() ; book.load()
```

`FailureRecord` 字段：`name, url, size, quickkey, folder, hash_value, kind, message,
detail, attempts, platform, created_at, part_path`。

> 读可读文案用 **`kind_label`**（类属性/property），不是 `kind_text`；
> `created_at` 是 **字符串** `"%Y-%m-%d %H:%M:%S"`（本地时间），不是 datetime。

### 1.6 诊断

```python
diag = Diagnostics()
diag.collect_environment()         # dict
diag.note(category, code, message, context="")   # 记一条诊断事件
report = build_report(diag, failures=book)       # -> dict（json 可序列化）
diag.save_report(failures=book)    # -> 落盘路径 str
```

## 2. 视觉规范（2.0 美化基线）

令牌集中在 `mfd/ui/theme.py`，两套：`DARK`（默认）、`LIGHT`。界面里禁止硬编码颜色。

| 令牌 | 深色 | 浅色 | 用途 |
|------|------|------|------|
| `bg` | `#0E1116` | `#F4F6FA` | 窗口底 |
| `surface` | `#161B22` | `#FFFFFF` | 卡片 |
| `surface_alt` | `#1C2330` | `#EDF1F7` | 次级面/表头 |
| `border` | `#252D3A` | `#DCE3EC` | 分隔线 |
| `text` | `#E6EDF3` | `#17202B` | 主文字 |
| `text_dim` | `#8B98A9` | `#5C6B7F` | 次要文字 |
| `accent` | `#3B82F6` | `#2563EB` | 主按钮/进度 |
| `accent_hover` | `#2F6FE0` | `#1D4ED8` | hover |
| `success` | `#22C55E` | `#15803D` | 完成 |
| `warning` | `#F59E0B` | `#B45309` | 跳过/警告 |
| `danger` | `#EF4444` | `#B91C1C` | 失败 |
| `info` | `#38BDF8` | `#0369A1` | 解析/提示 |

字形：中文优先 `Microsoft YaHei UI` → `Microsoft YaHei` → `Segoe UI`；
标题 16/13px 半粗，正文 10px，等宽日志 `Consolas`/`Cascadia Mono`。

布局（自上而下）：

1. **Header**：应用名 + 版本 pill（`2.0`）+ 主题切换按钮 + 最小化式留白；高度 56。
2. **输入卡**：链接输入框（占位提示）+ 「解析并加入队列」主按钮 + 选项（目录/线程/间隔/含子文件夹）。
3. **概览条**：大号百分比 + 圆角进度条 + 四个统计胶囊（完成/失败/跳过/总计）+ 速度/剩余时间。
4. **主体 Notebook 四个页**：队列 / 失败清单 / 日志 / 诊断。
   - 队列：Treeview（列：文件名、大小、进度、速度、状态、保存位置），行内状态着色。
   - 失败清单：Treeview（列：文件名、错误类型、原因、尝试次数、时间）+ 工具条
     「重试全部失败」「复制失败链接」「导出 JSON / Markdown」「清空」。
   - 日志：只读文本区（深色块，等宽），按 INFO/WARN/ERROR/OK 分色。
   - 诊断：环境信息表 + 按系统/错误类型的交叉统计 + 「导出诊断报告」。
5. **底部状态栏**：连接状态点 + 当前操作 + 版本 + 右侧「打开下载目录」。

交互硬度要求：

* 所有耗时操作（解析、开始、重试、导出）都不得阻塞主线程；按钮在工作期间置灰。
* 「重试全部失败」在无失败项时禁用，鼠标悬停给出原因提示。
* 关闭窗口：若正在下载，弹三选一（继续后台/停止并退出/取消），停止时 `stop_and_wait`。
* 键盘：`Ctrl+V` 粘贴链接、`Ctrl+Enter` 开始解析、`F5` 刷新失败清单、`Ctrl+D` 打开下载目录。
* 窗口最小 980×680，默认 1180×780，支持最大化；高 DPI 下不得模糊（`SetProcessDpiAwareness`）。

## 3. 文件划分

```
mfd/ui/__init__.py   只导出 run()
mfd/ui/theme.py      令牌、字体探测、ttk.Style 构建、DPI
mfd/ui/widgets.py    自绘组件：RoundedButton / Card / Pill / ProgressBar / ThemeSwitch
mfd/ui/app.py        App 类、四个页签、事件泵、所有交互
run.pyw              入口（无控制台）
```
