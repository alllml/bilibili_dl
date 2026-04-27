# 项目说明
是一个“轻量、面向国内平台、GUI 优先”的下载器，采用插件化架构支持 B 站和抖音视频下载。
  项目架构概览
  GUI 层（main.py）
      ↓ 调用
  协调层（DownloadManager）
      ↓ 使用      
  工厂层（ExtractorFactory）
      ↓ 创建
  策略层（BilibiliExtractor / DouyinExtractor）
      ↓ 使用
  工具层（downloader.py, environment_checker.py）

- GUI 不直接依赖具体平台实现
- 平台差异被收束在插件层
- 公共能力被抽到 core 中复用
- 后续扩平台时，不需要推翻已有结构
这也是我认为这个项目比“直接在 main.py 里写一堆 if/else”更成熟的地方。

## 3. 设计模式与架构思想

### 3.1 策略模式

项目里最核心的设计之一是策略模式。

`core/base_extractor.py` 定义了统一抽象接口：

- `extract_info(url, cookie)`
- `download(video_info, save_path, capabilities, progress_callback)`

然后：

- `plugins/bilibili_plugin.py` 实现 B 站策略
- `plugins/douyin_plugin.py` 实现抖音策略

这意味着“平台不同”只是“策略不同”，而不是“主程序逻辑完全不同”。

### 我的理解

这类项目最容易失控的地方，就是每支持一个平台，就在主流程里加一层判断。久而久之，主界面会知道太多细节：

- B 站要不要 FFmpeg
- 抖音需不需要 Selenium
- 某个平台怎么拿 Cookie
- 某个平台有没有多清晰度

而策略模式的价值就在于：**主流程只关心接口，不关心实现。**

这对后续扩展非常关键。比如如果以后加小红书、微博、快手，理论上只需要新增一个 Extractor，不必重写整个 GUI 或协调层。
