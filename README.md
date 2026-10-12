# 视频下载工具 (VideoDownloader)

[![Release](https://img.shields.io/badge/下载-v1.0.0-FB7299?logo=github)](https://github.com/alllml/bilibili_dl/releases)
[![Platform](https://img.shields.io/badge/平台-Windows%20x64-blue)](https://github.com/alllml/bilibili_dl/releases)
[![Python](https://img.shields.io/badge/Python-3.13-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

**粘贴链接，下载视频。** 一个 Windows 免安装的桌面视频下载器，支持 **B站 / 抖音 / Twitter(X)**，
不用装任何依赖——解压、双击、用。

> 🖼️ *（此处放一张主界面截图，效果翻倍——见下方"待办"）*

## ✨ 为什么用它

- 🎯 **什么都不用配就能下**：分享文案、短链 `b23.tv`、裸 ID、弹窗页链接，粘进来的都能识别
- ⚡ **下载快**：多线程分片 + 自动测速选最快的 CDN 节点，并发数按网速自适应调节（详见[原理](#下载提速原理)）
- 🧲 **抖音去水印**，视频无声时自动换源重下，不产出坏文件
- 🎵 **只要音频？** 一键下载 MP3；B站还能带弹幕和字幕
- 🍪 **Cookie 好心**：扫码登录全自动，过期提前提醒，不用研究"SESSDATA 是什么"
- 📦 **免安装绿色版**：59MB 解压即用，[点这里下载](https://github.com/alllml/bilibili_dl/releases)

## 🚀 30 秒上手

1. 从 [Releases](https://github.com/alllml/bilibili_dl/releases) 下载 `VideoDownloader-win64.zip`
2. 解压后双击 `VideoDownloader.exe`（首次运行若被 SmartScreen 拦截，点"仍要运行"）
3. 粘贴视频链接 → 解析 → 选清晰度 → 下载，完成

<details>
<summary>📎 想下 1080P / 去水印？需要配一下 Cookie（点开看怎么做）</summary>

打开菜单「B站 Cookie」/「抖音 Cookie」：
- **B站**：直接扫码登录，全自动；或手动粘贴 Cookie（必需 `SESSDATA`）
- **抖音**：粘贴 Cookie（必需 `sessionid`），App 里复制分享文案也行
- 程序会自动识别字段、检查有效期，快过期会提醒你
- 详细图文步骤见下方 [Cookie 配置](#cookie-配置)

</details>

---

## 目录

- [功能特性](#功能特性)
- [环境要求](#环境要求)
- [安装与运行](#安装与运行)
- [使用说明](#使用说明)
- [Cookie 配置](#cookie-配置)
- [配置项](#配置项)
- [项目结构](#项目结构)
- [架构设计](#架构设计)
- [下载提速原理](#下载提速原理)
- [打包发布](#打包发布)
- [常见问题](#常见问题)
- [日志与排查](#日志与排查)
- [已知限制](#已知限制)

---

## 功能特性

### 解析

| 能力 | 说明 |
|---|---|
| 多种输入形式 | 完整链接、**分享文案**（抖音 App 复制的一整段文字）、短链、**裸 ID** |
| 短链还原 | `b23.tv/xxx`、`v.douyin.com/xxx` 自动跟随跳转 |
| 裸 ID 补全 | `BV1xx...`、`av123`、抖音作品 ID（如 `7674115208758988042`）、`modal_id=xxx` 都能直接解析 |
| 抖音精选页 | 支持 `https://www.douyin.com/jingxuan?modal_id=xxx` 这类**弹窗播放**页 |
| 多重兜底 | RENDER_DATA → `_ROUTER_DATA` → DOM → 分享页，逐级降级 |

### 下载

| 能力 | 说明 |
|---|---|
| **多线程分片下载** | 基于 HTTP Range，绕过 CDN 单连接限速 |
| **并发自适应** | 不是固定线程数：按实测吞吐自动升降，见 [下载提速原理](#下载提速原理) |
| **CDN 测速择优选址** | 下载前对多个 CDN 采样测速，按实测速度从快到慢排序，躲开"不报错但很慢"的节点 |
| **断连韧性** | 单片失败自动重试 → 仍失败则记录缺口继续下别的 → 最后低并发补下；连接中断时主动降并发 |
| **完整性校验** | 以"分片集合"判断是否下完，杜绝静默截断产出坏文件 |
| 可取消 | 随时中断，自动清理半成品文件 |
| 实时进度 | 进度条 + 已下载/总大小 + 实时速率 + 剩余时间 |
| 重复下载提示 | 已下载过的视频会先询问 |

### 平台支持

| 平台 | 域名 | Cookie | 独有能力 |
|---|---|---|---|
| **B站** | `bilibili.com` / `b23.tv` / `bili2233.cn` | 需要（未配置只能 480P 以下） | 多P选择、清晰度选择、仅音频 MP3、弹幕 XML、字幕 SRT、DASH 音视频合并 |
| **抖音** | `douyin.com` / `v.douyin.com` / `iesdouyin.com` | 需要 | 去水印、**无声自动换源**、不开浏览器的轻量解析 |
| **Twitter/X** | `twitter.com` / `x.com` | 需要 | 失败自动重试 3 次 |

### 界面与体验

- **粘贴即解析**：复制链接后自动填入输入框（可关闭）
- **拖拽入窗**：把链接或分享文案直接拖进窗口
- **快捷键**：`Ctrl+Enter` 解析、`Ctrl+L` 清空输入
- **下载队列**：最大并发可配、失败一键重试、任务置顶、取消、打开文件
- **历史记录**：自动记录已下载内容，可按标题搜索
- **托盘通知**：下载完成时提示（窗口非激活时）
- **关闭确认**：有任务在跑时关闭会询问
- **文件名模板**：`{title}{owner}{platform}{quality}{date}` 自由组合
- **日志落盘**：滚动日志 + 菜单一键打开日志目录

### Cookie 管理

专门为"完全不懂的人"设计，解决最常见的劝退点：

| 能力 | 说明 |
|---|---|
| **一键扫码登录** | 后台拉起浏览器并**自动弹出登录框、自动切到扫码页**，扫一下即可 |
| **持久化浏览器 profile** | 登录态保存在本地，**下次通常无需再扫码** |
| **整段粘贴即可** | 几十段 Cookie 直接粘贴，自动只挑出关键字段（抖音只保留 `sessionid` 等 4 项） |
| 多种格式兼容 | `a=1; b=2` 标准串、浏览器插件导出的 JSON、带 `Cookie:` 前缀的请求头、**甚至只贴一个裸值** |
| 实时校验提示 | 边粘贴边显示已识别/缺失的关键字段 |
| **有效期离线检测** | 从 Cookie 内部解析真实到期时间（无需联网）：B站 `SESSDATA` 中段、抖音 `sid_guard` |
| 状态分级提醒 | `未配置` / `已过期` / `即将过期`（≤3 天）/ `有效（N 天后过期）`；启动时弹窗提示 |
| 在线校验 | B站可真实校验登录态并回显用户名 |

---

## 环境要求

| 项目 | 要求 |
|---|---|
| 操作系统 | Windows（其他平台理论上可用，未测试） |
| Python | **3.10+**（代码使用了 `X \| None` 类型标注） |
| 浏览器 | 扫码登录需要 Chrome 或 Edge（Selenium 驱动） |
| FFmpeg | **可选但强烈建议**。B站高清（DASH 音视频分离）必须用它合并；缺失时只能下到低画质无声视频 |

Python 依赖见 `requirements.txt`：

```
PySide6>=6.11.0
requests>=2.31.0
selenium>=4.20.0
pyinstaller>=6.0.0
```

---

## 安装与运行

### 方式一：源码 + 虚拟环境（推荐）

```bash
cd bilibili-dl

# 1. 创建虚拟环境
python -m venv venv

# 2. 安装依赖
venv\Scripts\pip install -r requirements.txt

# 3. 启动
venv\Scripts\python.exe main.py
```

也可以直接双击项目根目录的 **`run.bat`**（内部就是执行上面的第 3 步，并在退出时提示日志位置）。

### 方式二：打包后的可执行文件

见 [打包发布](#打包发布)。

### 首次使用建议顺序

1. 启动后进入 **设置 → 指定 FFmpeg 路径**（指向 `ffmpeg.exe`）
2. **设置 → B站 Cookie / 抖音 Cookie** → 点「打开浏览器登录」扫码
3. **设置 → 指定存储目录**
4. 粘贴链接 → 解析 → 下载

---

## 使用说明

### 输入链接的三种方式

```
https://www.bilibili.com/video/BV1xx411c7XX        ← 完整链接
BV1xx411c7XX                                        ← 裸 BV 号
https://www.douyin.com/jingxuan?modal_id=7674115208758988042
modal_id=7674115208758988042                        ← 直接贴参数也行
7674115208758988042                                 ← 裸作品 ID 也行
```

抖音 App 里「分享 → 复制链接」得到的一整段带文字的内容，**直接粘贴即可**，会自动提取出链接。

### B站多P视频

解析后会出现「分P」下拉框，切换即重新解析对应分P；多P标题会自动带上 `- P2` 之类的后缀。

### 仅下载音频（MP3）

「输出格式」选 **仅音频 MP3**。B站直接取 DASH 音频流转码（快且无损音质），抖音/推特则下载完整流后抽取音轨。**需要 FFmpeg**。

### 弹幕 / 字幕

B站视频可勾选「弹幕」「字幕」，会额外产出：

- `<文件名>.danmaku.xml` — 弹幕
- `<文件名>.srt` — 字幕（从接口 JSON 转成 SRT）

---

## Cookie 配置

### 推荐做法：扫码登录

**设置 → `<平台>` Cookie → 打开浏览器登录** → 在浏览器中扫码。

程序会自动帮你点开登录框并切到扫码页，登录成功后自动回填、保存，并关闭浏览器。

> 浏览器 profile 持久化在 `%LOCALAPPDATA%\VideoDownloader\browser_profiles\<平台>`，
> 所以**第二次登录通常直接就是已登录状态**，不用再扫。

### 手动粘贴

从浏览器 F12 → Network → 任意请求 → Request Headers → `Cookie`，**整段复制**粘贴进输入框即可，无需自己挑字段。

各平台真正会被保留的关键字段：

| 平台 | 必需 | 可选（有则保留） |
|---|---|---|
| B站 | `SESSDATA` | `bili_jct`、`DedeUserID`、`buvid3` |
| 抖音 | `sessionid` | `ttwid`、`sid_guard`、`odin_tt` |
| Twitter | `auth_token` | `ct0` |

### 有效期检测

程序会**离线**解析 Cookie 内部编码的到期时间：

- **B站** `SESSDATA`：形如 `<uid>%2C<到期时间戳>%2C<校验>`，中段即到期时间
- **抖音** `sid_guard`：形如 `<sid>%7C<登录时间>%7C<有效期秒数>%7C<过期日期>`
  （注意第 2 段是**登录时间**而非到期时间，需用「登录时间 + 有效期」计算）

状态栏会直接显示真实状态，快到期时启动会弹窗提醒。

---

## 配置项

配置文件为项目根目录的 **`config.ini`**（已在 `.gitignore` 中忽略，**不会**被提交，因为里面含 Cookie）。

```ini
[settings]
ffmpeg_path =                 ; ffmpeg.exe 路径，留空则尝试从 PATH 查找
save_dir =                    ; 下载保存目录
bilibili_cookie =             ; 由界面写入，不建议手改
douyin_cookie =
twitter_cookie =
max_concurrent = 2            ; 同时下载的任务数
download_connections = auto   ; 单文件并发上限：auto（自适应，上限 16）或 1-16 的固定值
auto_paste_clipboard = 1      ; 复制链接后自动填入输入框
filename_template =           ; 留空则用默认 {title}
```

对应菜单项：**设置** 菜单下均可图形化修改。

### 文件名模板占位符

| 占位符 | 说明 |
|---|---|
| `{title}` | 视频标题 |
| `{owner}` | UP主 / 作者 |
| `{platform}` | 平台名（B站 / 抖音 / Twitter） |
| `{quality}` | 清晰度 |
| `{date}` | 下载日期 |

示例：`{platform}/{owner}/{date}_{title}` → `B站/某UP主/2026-10-10_视频标题.mp4`

> 模板写错或含非法字符时会自动兜底为默认命名，不会导致下载失败。

---

## 项目结构

```
bilibili-dl/
├── main.py                      # GUI 入口（菜单、解析、队列、历史、状态栏）
├── run.bat                      # 一键启动（使用 venv）
├── requirements.txt
├── build.py                     # PyInstaller 打包脚本（自动收集插件）
├── icon.ico
├── core/
│   ├── base_extractor.py        # 抽象基类 BaseExtractor + VideoInfo 定义
│   ├── extractor_factory.py     # 工厂：扫描插件目录、URL 规范化、域名路由
│   ├── download_manager.py      # 协调层：串联工厂 / 插件 / 环境检测，命名与落盘
│   ├── downloader.py            # 公共下载工具：分片 + 并发自适应 + CDN 测速 + 取消
│   ├── environment_checker.py   # FFmpeg 检测、配置读写、capabilities
│   ├── cookie_utils.py          # Cookie 解析规整 + 有效期离线检测
│   ├── cookie_manager.py        # Selenium 扫码登录（B站 / 抖音共用骨架）
│   ├── media.py                 # FFmpeg 封装（抽音轨、转码）
│   ├── task_queue.py            # 任务队列（状态、置顶、重试、取消标记）
│   ├── database.py              # 下载历史（SQLite）
│   └── logger.py                # 滚动日志，接管 stdout
├── gui/
│   ├── cookie_dialog.py         # Cookie 设置对话框
│   ├── task_list_widget.py      # 下载队列列表
│   ├── task_worker.py           # 下载线程（进度 / 速率 / 取消）
│   └── history_widget.py        # 历史记录页
├── plugins/                     # 插件目录（工厂自动扫描注册）
│   ├── bilibili_plugin.py
│   ├── douyin_plugin.py
│   └── twitter_plugin.py
└── docs/                        # 设计文档与工作记录
```

---

## 架构设计

```
                    main.py（GUI）
                         │
                         ▼
              DownloadManager（协调层）
                │        │        │
                ▼        ▼        ▼
     ExtractorFactory  EnvironmentChecker  DownloadHistoryDB
        （工厂/路由）      （FFmpeg/配置）
                │
                ▼
        BaseExtractor（抽象策略）
                │
   ┌────────────┼────────────┐
   ▼            ▼            ▼
Bilibili     Douyin       Twitter     ← plugins/ 下自动注册
```

**要点**

1. **工厂 + 策略**：`plugins/` 下的模块被自动扫描，`BaseExtractor` 的子类按 `SUPPORTED_DOMAINS` 自动注册。**新增平台 = 新增一个插件文件**，无需改动其他代码。
2. **协调层隔离**：GUI 的 QThread 只调用 `DownloadManager`，不直接碰工厂或插件，保证界面层与业务逻辑解耦。
3. **能力降级**：`EnvironmentChecker` 汇总 `capabilities`（FFmpeg 是否可用等）传给插件，插件据此在运行时选择不同下载子策略。
4. **公共下载器**：所有插件共用 `core/downloader.py` 完成实际 HTTP 下载，插件只负责"选流 + 编排 + 拼接"。

### 如何新增一个平台

```python
# plugins/example_plugin.py
from core.base_extractor import BaseExtractor, VideoInfo

class ExampleExtractor(BaseExtractor):
    SUPPORTED_DOMAINS = ["example.com"]
    PLATFORM_ID = "example"
    PLATFORM_NAME = "示例站"
    REQUIRES_COOKIE = False

    def extract_info(self, url: str) -> VideoInfo:
        ...   # 返回标准化的 VideoInfo（含流地址与下载所需 headers）

    def download(self, video_info, save_path, capabilities, progress_callback=None):
        ...
```

保存后重启即可生效——工厂会自动发现它，GUI 的 Cookie 菜单也会自动出现对应条目（若 `REQUIRES_COOKIE = True`）。

---

## 下载提速原理

### 为什么要分片

B站等 CDN 对**单条连接**有明显限速。把文件切成小片、用多条连接并行下载，就能绕过单连接限速。
同时，共享分片队列（谁空了谁取下一片）避免了"3 条下完干等 1 条"的负载不均。

### 自适应并发怎么工作

不是拍脑袋开固定线程数——**固定线程数在网差时只会互相抢带宽、更容易被风控**。实际策略：

1. 从 **2 条连接**起步（从低起步：少开才能避免被批量掐断，有提升几秒内就会加上去）
2. 每 **2 秒**测一次整体吞吐
3. 每次改变并发后先空一个**预热窗口**不计入判断——新连接刚建立时吞吐天然偏低，不隔开就会把"TCP 慢启动变快"误判成"加并发有效"
4. 只有实测吞吐提升 **≥10%** 才保留更高并发；没有提升或变差就**回退一档**并停止向上试探
5. 之后**持续监测**：吞吐掉到峰值 70% 以下连续两个窗口，自动降并发

日志会完整打印每次决策：

```
并发 2 档吞吐 2.05 MB/s，试着加到 4 条看看
并发 4 未带来提升（2.10 MB/s），回退到 2 条连接（链路总带宽可能是瓶颈）
分片下载完成（并发 2 条，平均 2.19 MB/s）
```

### 重要：什么时候多线程没用

**分片加速只在「CDN 对单连接限速、但链路总带宽有富余」时有效。**

| 场景 | 瓶颈 | 多线程效果 |
|---|---|---|
| 家庭宽带直连 | CDN 单连接限速 | **成倍提速**（16 条可跑满带宽） |
| 走 VPN / 代理隧道 | 隧道总带宽（如 ~2 MB/s） | **基本无效果**——总量固定，多开只是分蛋糕，还更容易被掐断 |

如果你正走 VPN，日志里通常会出现 `未带来提升，回退到 N 条连接（链路总带宽可能是瓶颈）` ——
这是**正常且预期**的行为，说明程序已识别出瓶颈在链路而非 CDN。
这种情况下固定用「单线程」也一样快，但**多线程版的断连重试与补下机制更不容易整个失败**，仍建议保持「自动」。

### 相关参数

| 参数 | 值 | 说明 |
|---|---|---|
| 分片大小 | 1–8 MB（按文件大小与并发数自适应） | 每个线程至少分到 6 片以保证均衡 |
| 单文件并发上限 | `auto` → 16 | 可在设置中改为固定 1/2/4/8/16 |
| 单片重试 | 3 次（同区间重连重下） | 连接被掐断后重连通常即可恢复 |
| 缺口补下 | 最多 3 轮，低并发（≤4） | 只补缺失分片，不重下整个文件 |
| CDN 测速 | 每候选最多 0.8 秒、最多 3 个 | 从收到首字节开始计时，排除握手干扰 |
| 超时 | 连接 15s / 读取 90s | 卡死的连接会较快放弃 |

---

## 打包发布

```bash
venv\Scripts\python.exe build.py
```

`build.py` 会自动：

- 收集 `plugins/` 下所有插件文件
- 生成/更新 PyInstaller spec
- 以 `--onedir` 模式打包

产物位于 `dist/VideoDownloader/`，包含 `VideoDownloader.exe` 与 `_internal/` 依赖目录，
可直接分发给没有 Python 环境的用户。

> 打包为**无控制台**模式。运行日志仍会写入
> `%LOCALAPPDATA%\VideoDownloader\logs\app.log`，排查问题时看这里。

---

## 常见问题

**Q：B站只能下到 480P，或提示需要 Cookie？**
未配置 Cookie 或 Cookie 已过期。菜单 **设置 → B站 Cookie** 查看状态；界面会显示真实过期时间。

**Q：抖音下载的视频没有声音？**
抖音同一清晰度会混入「被剥离音轨的纯视频流」，长视频还会用 DASH 音视频分离。程序已做两层防护：
解析阶段按体积优选完整流、下载后校验音轨，**无声会自动换下一个候选地址重下**。若最终仍无声音，日志会给出 `[警告] 所有候选地址均无音轨`。

**Q：解析很慢 / 一直卡住？**
抖音页面是无限流，永远不会触发 `load` 事件。程序使用 `page_load_strategy=none` 并自行轮询数据，
正常情况下**轻量路径（不开浏览器）1~2 秒**即可出结果。若长时间无响应，检查日志确认走的哪条路径。

**Q：下载速度很慢，是程序的问题吗？**
先看日志结尾的 `分片下载完成（并发 N 条，平均 X MB/s）`。若出现"未带来提升，回退"，
说明瓶颈在你的网络链路。可用同一视频分别试「自动」与「固定 1 线程」对比。

**Q：校验 Cookie 时提示 SSL / 网络错误？**
这不代表 Cookie 失效。通常是网络、代理或防火墙问题（程序会明确区分并提示）。

**Q：下载的文件损坏 / 打不开？**
程序会校验分片完整性，理论上不会产出截断文件。若发生，请把 `app.log` 提供出来。

---

## 日志与排查

日志位置：

```
%LOCALAPPDATA%\VideoDownloader\logs\app.log
```

菜单 **设置 → 打开日志目录** 可直接打开。

日志采用滚动策略不会无限增长。排查问题时，**从本次启动的那行 `日志系统已启动` 往下看**即可。

关键的排查锚点：

| 日志内容 | 含义 |
|---|---|
| `CDN 测速：xxx 5.20MB/s` | 各 CDN 实测速度与选中顺序 |
| `并发 N 档吞吐 X MB/s，试着加到 M 条看看` | 自适应正在试探 |
| `并发 M 未带来提升，回退到 N 条连接（链路总带宽可能是瓶颈）` | 已识别链路瓶颈 |
| `检测到连接中断，并发自动降到 N 条（链路不稳）` | 链路不稳，主动降并发 |
| `有 N 个分片未下完，第 k 轮低并发补下...` | 缺口补下（正常兜底） |
| `分片下载完成（并发 N 条，平均 X MB/s）` | 完成，含最终并发与平均速率 |
| `[诊断] <video> 数量 / video src / 疑似媒体` | 抖音页面渲染诊断 |

---

## 已知限制

- **VPN / 隧道链路**：总带宽是硬上限，多线程无法提速（见上文说明）
- **抖音合集 / 分P**：目前按单个作品解析，一个链接对应一个视频，**尚未支持整个合集批量下载**
- **抖音部分视频**：若服务端只提供 DASH 分离流，仍可能出现无音轨（日志会明确警告）
- **Twitter**：接口不稳定时依赖重试；未做多种清晰度选择
- **Cookie**：各平台机制随时可能变更，失效后需重新获取
- 仅 `*.mp4` / `*.mp3` 输出，未提供其他封装格式

---

## 文档

| 文件 | 内容 |
|---|---|
| [docs/architecture.md](docs/architecture.md) | 架构说明 |
| [docs/design-review.md](docs/design-review.md) | 设计模式与验收说明 |
| [docs/plan.md](docs/plan.md) | 早期设计思路 |
| [docs/daily-2026-04-10.md](docs/daily-2026-04-10.md) | 开发记录 |
