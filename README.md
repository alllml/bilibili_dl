
# 国内视频网站视频下载工具

## 一、项目概述
实现一个基于 Python 的视频下载工具，输入视频网址，输出视频文件。要求支持 B 站和抖音，并体现工厂模式与策略模式的插件化设计。

### 核心约束
- 语言：Python 
- 界面：PySide6 图形界面
- 交付：最终输出 `--onedir` 模式的可执行程序目录（包含 `VideoDownloader.exe` 及 `_internal/` 依赖目录），可分发给无 Python 环境的用户使用
- 外部依赖：FFmpeg 作为独立工具，通过环境检测实现降级策略

## 二、设计模式与架构要点（必须遵循）
### 1. 插件化策略模式
- 定义抽象基类 `BaseExtractor`（位于 `core/base_extractor.py`），约定 `extract_info(url)` 和 `download(video_info, save_path, capabilities)` 两个抽象方法。
- `extract_info(url)` 返回标准化的 `VideoInfo` 字典（含标题、流 URL、所需 headers 等完整下载信息）。
- `download(video_info, save_path, capabilities)` 负责下载编排逻辑（选择流、决定是否调用 FFmpeg 合并），实际的 HTTP 分块下载委托给公共工具函数 `download_file`。
- `BilibiliExtractor` 和 `DouyinExtractor` 作为具体策略类，分别实现对应平台的解析与下载编排逻辑。

### 2. 工厂模式实现插件发现与注册
- 工厂类 `ExtractorFactory`（位于 `core/extractor_factory.py`）维护注册表 `Dict[Platform, ExtractorClass]`。
- 通过扫描 `plugins/` 目录动态加载 `.py` 文件，自动注册 `BaseExtractor` 的子类。
- 工厂内部实现 URL 规范化处理（短链还原、主域名提取），再映射到具体策略类。
- GUI 通过 `DownloadManager` 协调层间接使用工厂和 Extractor，不直接调用。

### 3. FFmpeg 环境检测与降级策略
- 独立工具类 `EnvironmentChecker` 检测 FFmpeg 是否可用（系统 PATH 或用户自定义路径）。
- 具体策略类根据检测结果在运行时选择下载子策略：
  - **FFmpeg 可用**：下载高清 Dash 流（音视频分离），调用 FFmpeg 合并为 MP4。
  - **FFmpeg 不可用**：下载低清晰度 Dash 视频流（仅视频，不含独立音轨），直接保存，画质和音质受限。

### 4. 协调层 DownloadManager
- 新增 `DownloadManager`（位于 `core/download_manager.py`），作为 GUI 与 Extractor 之间的中间协调层。
- 职责：接收 URL，调用工厂获取 Extractor，执行 `extract_info` → `download` 的完整流程，管理环境检测结果（capabilities），向上层报告进度与状态。
- GUI 的 `QThread` 仅负责"在子线程中调用 `DownloadManager`"，不直接操作工厂或 Extractor。
- 这样 GUI 层与业务逻辑彻底解耦，后续增加批量下载、队列管理等功能时只需扩展协调层。

### 5. 公共下载工具函数
- 新增 `core/downloader.py`，提供 `download_file(url, headers, save_path, progress_callback)` 函数。
- 职责：HTTP 分块下载 + 进度回报，所有 Extractor 的 `download()` 方法共用此函数完成实际的文件下载。
- `BilibiliExtractor.download()` 只负责编排下载逻辑（选择流、决定是否合并），实际的 HTTP 下载和进度上报委托给 `download_file`。
- 阶段五实现 B 站下载时一并完成此工具函数，阶段六抖音直接复用。

### 6. B 站 Cookie 管理
- B 站获取 1080P 及以上清晰度的视频流需要有效的登录 Cookie（SESSDATA）。
- 提供 Cookie 导入功能：用户可在"设置"中粘贴 Cookie 字符串，保存至 `config.ini`。
- 未配置 Cookie 时，仅能获取 480P 及以下清晰度，GUI 需明确提示用户。

## 三、目录结构（大致遵守）

```
VideoDownloader/
├── main.py                     # GUI 入口
├── core/
│   ├── extractor_factory.py    # 工厂 + 插件扫描 + URL 规范化
│   ├── base_extractor.py       # 抽象策略接口 + VideoInfo 定义
│   ├── download_manager.py     # 协调层：串联工厂、Extractor、环境检测
│   ├── downloader.py           # 公共下载工具：download_file()
│   └── environment_checker.py  # FFmpeg 检测与配置管理
├── plugins/                    # 物理插件目录
│   ├── bilibili_plugin.py
│   └── douyin_plugin.py
├── tools/
│   └── ffmpeg/                 # 外部依赖
├── gui/
│   ├── main_window.py          # 主窗口
│   └── download_thread.py      # 下载线程（QThread），仅调用 DownloadManager
└── config.ini                  # 保存用户自定义 FFmpeg 路径、Cookie 等配置
```

#  项目架构层次

  GUI 层（main.py）
      ↓ 调用
  协调层（DownloadManager）
      ↓ 使用
  工厂层（ExtractorFactory）
      ↓ 创建
  策略层（BilibiliExtractor / DouyinExtractor）
      ↓ 使用
  工具层（downloader.py, environment_checker.py）

## 四、分阶段开发任务（逐阶段执行，不可跨阶段）

### 阶段一：环境与 GUI 骨架
**目标**：跑通 Python 环境，显示空白窗口。
**产出**：
- 创建虚拟环境 `venv`，安装 `PySide6`。
- `main.py` 启动后显示 800x600 空白窗口，标题为"视频下载工具"。
- 窗口可正常关闭。

**验收**：双击 `main.py` 弹出窗口，无报错。

---

### 阶段二：策略接口与工厂骨架
**目标**：建立抽象基类与工厂，实现基于 URL 的策略选择，包含短链还原。
**产出**：
- `core/base_extractor.py`：定义 `BaseExtractor` 抽象类，包含 `extract_info(url)` 和 `download(video_info, save_path, capabilities)` 抽象方法，定义 `VideoInfo` 数据结构。
- `core/extractor_factory.py`：工厂类，含 `register` 方法和 `get_extractor(url)` 方法。内部实现 URL 规范化（短链如 `b23.tv`、`v.douyin.com` 的 302 跳转还原，主域名提取）。
- `plugins/` 下创建 `bilibili_plugin.py` 和 `douyin_plugin.py`，其中类继承 `BaseExtractor`，方法内仅打印"正在处理 xxx 链接"。
- GUI 增加输入框和按钮，点击后根据 URL 调用工厂返回策略实例并打印类名。

**验收**：
- 输入 B 站链接（含 `b23.tv` 短链），控制台打印 `正在使用 BilibiliExtractor 处理链接...`。
- 输入抖音链接，控制台打印对应提示。
- **注意**：此阶段不实现真实网络请求（短链还原除外）。

---

### 阶段三：真实解析 B 站视频信息
**目标**：让 `BilibiliExtractor` 真实请求 B 站 API，获取视频元数据。
**产出**：
- 使用 `requests` 库请求 B 站视频信息 API（携带 User-Agent、Referer，以及可选的 Cookie）。
- `extract_info` 返回 `VideoInfo` 字典，包含标题、封面、Dash 流 URL 及所需请求头。
- GUI 上新增标签，点击按钮后显示视频标题。
- 控制台打印可用的视频流格式列表（如 1080P 视频流、音频流 URL）。
- 未配置 Cookie 时，GUI 提示"未配置 Cookie，仅可获取低清晰度"。

**验收**：
- 输入有效 B 站 BV 号链接，GUI 正确显示视频标题。
- 控制台能打印出 Dash 流 URL。

---

### 阶段四：FFmpeg 环境检测与用户配置
**目标**：实现 FFmpeg 可用性检测，并在 GUI 显示状态，支持用户手动指定路径和配置 Cookie。
**产出**：
- `core/environment_checker.py`：检测系统 PATH 中是否有 FFmpeg（通过 `subprocess.run(['ffmpeg', '-version'])`），支持读取 `config.ini` 中的自定义路径。
- GUI 底部状态栏显示"FFmpeg 已找到"或"FFmpeg 不可用，仅支持低画质下载"。
- 菜单栏"设置"→"指定 FFmpeg 路径"，弹出文件选择对话框，保存路径至 `config.ini`。
- 菜单栏"设置"→"配置 B 站 Cookie"，弹出输入对话框，保存至 `config.ini`。

**验收**：
- 启动时状态栏正确反映 FFmpeg 是否存在。
- 手动指定路径后重启软件，状态栏更新为"已找到"。
- Cookie 配置后重新解析视频，可获取更高清晰度的流。

---

### 阶段五：B 站下载功能与进度条
**目标**：实现协调层、公共下载工具、B 站真实下载与 FFmpeg 合并，并在 GUI 显示进度。
**产出**：
- `core/downloader.py`：实现 `download_file(url, headers, save_path, progress_callback)` 公共下载工具函数，负责 HTTP 分块下载与进度回报。后续所有 Extractor 共用。
- `core/download_manager.py`：实现 `DownloadManager` 协调层，串联工厂 → Extractor → 环境检测 → 下载的完整流程，向上层（QThread）暴露统一的 `start_download(url, save_path, progress_callback)` 接口。
- `gui/download_thread.py`：继承 `QThread`，在 `run` 中仅调用 `DownloadManager`，通过信号更新进度条和状态。不直接操作工厂或 Extractor。
- `BilibiliExtractor.download` 实现下载编排逻辑，实际 HTTP 下载委托给 `download_file`：
  - 若 FFmpeg 可用：下载 Dash 视频流和音频流，调用 FFmpeg 合并为 MP4。
  - 若 FFmpeg 不可用：下载低清晰度 Dash 视频流（仅视频），直接保存。
- GUI 进度条与下载进度同步，完成后弹窗提示保存路径。

**验收**：
- 点击下载后进度条走动，界面不卡死。
- 下载完成后在本地生成可播放的视频文件。

---

### 阶段六：抖音插件框架（占位，暂不验证）
**目标**：搭建 `DouyinExtractor` 的代码框架，预留解析与下载接口，但不实现真实的网络请求逻辑。
**说明**：抖音反爬机制较复杂（签名验证、设备指纹等），优先保证 B 站全链路跑通后再扩展。
**产出**：
- `DouyinExtractor.extract_info`：框架方法，内部抛出 `NotImplementedError("抖音解析功能开发中")`。
- `DouyinExtractor.download`：框架方法，同上。
- GUI 调用抖音插件时弹出提示"抖音下载功能开发中，敬请期待"。

**验收**：
- 输入抖音链接，GUI 弹出"功能开发中"提示，不崩溃。
- 工厂能正确识别抖音 URL 并返回 `DouyinExtractor` 实例。

---

### 阶段七：打包为可分发程序
**目标**：使用 PyInstaller 生成可分发的程序目录。
**产出**：
- 使用 PyInstaller `--onedir` 模式打包，生成 `VideoDownloader.exe` 及 `_internal/` 依赖目录。
- 使用 `--add-binary` 将 `tools/ffmpeg.exe` 打包进 `_internal/tools/`。
- 修改 `EnvironmentChecker`，使 FFmpeg 检测逻辑兼容打包后的路径（优先检查 `_internal/tools/`，再检查 `config.ini` 自定义路径，最后检查系统 PATH）。
- 不显示控制台窗口（`--noconsole`）。

**验收**：
- 将生成的程序目录复制到无 Python 环境的 Windows 电脑上，双击 `VideoDownloader.exe` 可启动。
- B 站视频下载功能正常，FFmpeg 状态检测正确。
- 输入抖音链接弹出"功能开发中"提示，不崩溃。

## 五、给 Claude Code 的执行指令
1. 严格按照以上七个阶段的顺序执行，每个阶段完成后请等待我的确认再进入下一阶段。
2. 每个阶段仅实现该阶段要求的功能，不要提前编写后续阶段的代码。
3. 代码中需包含详细注释，注释语言为中文，解释关键设计模式的落地位置。
4. 所有文件路径和目录结构需与第三节保持一致。
5. 如遇技术选型疑问（如 B 站 API 的选取），请优先选择维护活跃、文档清晰的公开接口，并向我说明理由。
