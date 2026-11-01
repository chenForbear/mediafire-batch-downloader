# 参与开发

先说三句话，省下后面很多来回：

1. **只写实现了的功能**。文档、注释、提交信息里不出现「计划中」「即将支持」当卖点——没做就是没做。
2. **不引第三方库**。这个项目从 1.0 到 2.0 一直是「除界面用 Tkinter 外全部标准库」，这是它能被打包成单文件 exe 并塞进 Windows 7 的前提。要引库的 PR，请先在 issue 里论证标准库为什么做不了。
3. **兼容 Python 3.8**。不是"建议"，是硬线：3.8 是能在 Win7 上跑起来的最后一个 Python 版本。

---

## 一、动手前先读

| 文件 | 为什么读 |
|---|---|
| [docs/UI-CONTRACT.md](docs/UI-CONTRACT.md) | 内核与界面之间的接口契约。字段名、事件名、状态枚举都在这里，改接口先改它 |
| [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md) | 兼容性矩阵与「报错类型 → 现象 → 原因 → 处置」全表。改错误分类要看它 |
| [CHANGELOG.md](CHANGELOG.md) | 2.0.0 干了什么、为什么这么分模块 |

## 二、环境

```powershell
git clone https://github.com/CHEN-CHEN/mediafire-batch-downloader.git
cd mediafire-batch-downloader
python --version          # 需要 3.8 或更高
python run.pyw            # 直接起界面
```

- 没有任何依赖要装，`pip install` 不需要；
- Windows 官方 Python 安装包自带 Tkinter；Linux / macOS 上若缺 Tk，装系统包（`python3-tk` / `tcl-tk`）；
- 想验证环境是否齐，跑一遍离线测试即可（见第四节）。

## 三、代码结构与边界

模块是按「谁跟谁说话」切的，不是按文件大小切的。改动请落在正确的边界里：

| 模块 | 职责 | 不许干的事 |
|---|---|---|
| `mfd/mediafire.py` | 和 MediaFire 说话：解析链接、翻页、取直链、把接口错误码翻成人话 | 不碰磁盘、不碰界面 |
| `mfd/downloader.py` | 和磁盘说话：任务队列、线程池、续传、校验、原子落盘 | 不发解析请求（直链通过 `mediafire` 拿） |
| `mfd/failures.py` | 记住失败：错误分类、失败清单、导出 | 不做重试决策（那是 downloader 的事） |
| `mfd/diagnostics.py` | 记住环境：环境采集、「平台 × 错误类型」交叉统计、报告导出 | 不改动失败清单本身 |
| `mfd/storage.py` | 记住历史：注册表 / JSON 双后端、去重、上限 | 不存完整直链与本机全路径 |
| `mfd/ui/` | 和用户说话：主题、控件、主窗口 | **不写业务逻辑**，不直接发网络请求 |

几条铁律：

- **`tkinter` 只允许出现在 `mfd/ui/` 里**。别的模块一旦 `import tkinter`，就再也没法在无界面环境里被测；
- **后台线程不碰控件**。worker 只发事件，界面在 `_pump` 里回到主线程再动控件；
- **落盘一律原子写**：`tempfile` + `os.replace`，或注册表整值替换。任何时刻断电，都不能留下写了一半的状态文件；
- **正式文件名只在全部校验通过后才出现**（先 `.part`，再改名）。

## 四、跑测试

```powershell
# 离线单元测试：不联网、不写用户目录、不弹窗
python tests\test_offline.py

# 界面冒烟：不显示窗口、不进 mainloop，需要能建 Tk 根窗口的环境
python tests\test_gui_smoke.py

# 两个一起
python -m unittest discover -s tests -v
```

- `tests/test_offline.py`：42 个用例，覆盖文件名净化、大小/时间格式化、URL 解码、链接解析、错误分类、脱敏、失败记录与失败清单、任务模型，以及一个用标准库 `http.server` 起本地假直链的下载测试（含 `Range` 续传与模拟 403）；
- `tests/test_gui_smoke.py`：5 个用例，验证色板完整、主题可逆、初始尺寸不小于最小值、`App` 能构建并在 3 秒内完成、四个页签存在、切换主题不崩；全程 `root.withdraw()` + 有限轮 `update()`，**绝不 `mainloop()`**，所以在无人值守的机器上跑也不会闪窗口。

测试纪律：

- 测试里**不许联网**。要测网络行为就起本地 `http.server`（现有写法照抄即可）；
- 测试里**不许读写真实用户目录**，用临时目录；
- 测试里**不许弹窗、不许开主循环**；
- 新增解析逻辑 → 在 `tests/test_offline.py` 加用例；新增错误判据 → 在 `TestClassifyFailure` 里补一条。

## 五、新增一种错误类型

错误类型是**一套口径、三处落地**，漏一处就会出现「日志说 A、清单说 B、报告里没有」：

1. **`mfd/failures.py`**：`KIND_TEXT` 加中文短名（给人看的标签）、`KIND_HINT` 加一句处置建议、`classify_failure()` 加判据并想清楚**优先级**——现有顺序是：MediaFire 语义（校验/大小/127/128/129）→ 代理与 TLS → 网络（DNS / 重置 / 拒绝 / 超时）→ HTTP 4xx/5xx → 直链状态 → 本地文件系统 → 已取消 → 兜底；
2. **`mfd/diagnostics.py`**：确认新类型能被正常归集——诊断报告的交叉统计是按错误类型 × 平台做计数的，新类型必须能在报告里出现，不能落进「其他」；
3. **`docs/COMPATIBILITY.md`**：在报错表里加一行「现象 / 原因 / 处置」，措辞与 `KIND_HINT` 保持一致；
4. **`tests/test_offline.py`**：`TestClassifyFailure` 加一条判据用例，把 `exc` 或 `message` 的真实形态钉住；
5. **README**：若这是用户会撞上的高频错误，在[常见问题](README.zh-CN.md#常见问题)里补一段。

## 六、提交信息规范

采用 [Conventional Commits](https://www.conventionalcommits.org/zh-hans/v1.0.0/)（中文描述）。格式：

```
<type>(<scope>): <一句话说清做了什么>

<可选正文：为什么这么改、权衡了什么>

<可选 footer：Closes #12>
```

**type**：

| type | 用途 |
|---|---|
| `feat` | 新功能 |
| `fix` | 修 bug |
| `docs` | 只动文档 |
| `refactor` | 重构（行为不变） |
| `perf` | 性能优化（如减少一次 API 往返） |
| `test` | 只动测试 |
| `build` | 打包配置（`packaging/`、`pyproject.toml`） |
| `ci` | 流水线 |
| `style` | 格式、缩进、空行（不改逻辑） |
| `chore` | 杂项（版本号、ignore 规则） |
| `revert` | 回滚 |

**scope** 用模块名：`mediafire` / `downloader` / `failures` / `diagnostics` / `storage` / `util` / `ui` / `packaging` / `docs`。跨模块就挑最主要的一个。

**subject** 规则：中文、祈使句、不加句号、不超过 50 字；描述「做了什么」，不要写「修复了一些问题」。

示例：

```
feat(failures): 为 429 限流单独归类并提示调大间隔
fix(downloader): 服务端返回整包时丢弃断点重下，避免拼出坏文件
perf(mediafire): 先取文件列表再决定是否请求子文件夹
docs(compatibility): 补 Win7 缺 api-ms-win-crt-* 的处置
refactor(ui): 把主题令牌的派生逻辑收进 theme.py
```

**破坏性变更**：type 后加 `!`，并在 footer 写 `BREAKING CHANGE:` 说明影响面与迁移方式。改 `UI-CONTRACT.md` 里的字段名、事件名、状态枚举，一律算破坏性变更。

一个提交只做一件事。「顺手改了格式」请单独一个 `style` 提交，否则 review 时看不出真正的改动。

## 七、PR 检查清单

提交前逐条过一遍，PR 描述里直接贴这份清单打勾：

- [ ] `python tests\test_offline.py` 全绿
- [ ] 有界面改动时 `python tests\test_gui_smoke.py` 也全绿
- [ ] 没有新增第三方依赖；没有用到 Python 3.9+ 才有的语法或标准库 API（`list[int]` 泛型、`dict |` 合并、`str.removeprefix`、`functools.cache` 等一律不用）
- [ ] 除 `mfd/ui/` 外没有 `import tkinter`
- [ ] 用户可见文案是中文，且不把原始异常/错误码直接甩给用户（一律过 `classify_failure` 翻成人话）
- [ ] 新增或修改错误分类时，三处口径已同步（`failures.py` / `diagnostics.py` / `COMPATIBILITY.md`）
- [ ] 写文件仍是原子的；正式文件名仍然只在全部校验通过后才生成
- [ ] 后台线程没有直接操作控件
- [ ] 重试/续传相关改动，说明了对「已完成的文件、已有的同名文件」的影响
- [ ] 文档已同步：README 相关段落、`CHANGELOG.md` 的「未发布」段
- [ ] 没有提交 `build/`、`dist/`、`build-temp/`、`*.part`、日志、下载产物
- [ ] 没有把本机绝对路径、用户名、真实下载链接写进代码或测试
- [ ] 版本号若改动，`mfd/__init__.py` 的 `__version__` 与 `pyproject.toml` 的 `version` 保持一致

## 八、报 issue 时

- 先搜一遍已有 issue，很多报错在 [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md) 里已经有处置；
- **必须附诊断报告 JSON**：程序内「诊断」页 →「导出诊断报告」。里面有系统版本、环境项和「平台 × 错误类型」交叉统计，比口述有用得多；
- 附一段相关日志（日志页可直接选中复制）；
- 文件名可以脱敏，但请说清是「文件名问题」还是「内容问题」——错误类型标签请保留。

细节看 [issue 模板](.github/ISSUE_TEMPLATE/bug_report.md)。
