# 这是一份思维导图，不涉及详细规划，目的是通过开发项目学习设计知识点。

# 项目名称：国内视频网站视频下载工具
## 项目详细说明：实现一个视频下载工具，输入视频网址，输出视频文件。要求支持国内部分视频网站（支持B站、抖音）
## 项目实现要求：1.使用python语言编写。2.实现最小mvp，即最小可行产品。3.包含UI界面（无要求，可使用即可），包含.exe可执行文件
## 项目设计思路：体现清晰的设计模式，工厂模式，策略模式等

# 设计模式在插件化架构中的应用描述
1.主程序仅负责界面交互与调度，具体下载能力由插件式解析器提供。
2.定义抽象基类 BaseExtractor（即策略接口），约定了所有视频平台插件必须实现的统一行为
（体现形式：BilibiliExtractor 和 DouyinExtractor 是实现该契约的具体策略类。它们在运行时如同可插拔的插件卡带，可以被随时替换。GUI 层调用 extractor.download() 时，完全不知道底层是 B 站还是抖音的逻辑。）
3.工厂模式的应用：实现插件的发现与注册
为了解耦对象创建过程，我们设计 ExtractorFactory 工厂类。它不仅仅是 if-else 判断，更承担了插件管理器的角色：
插件注册表：工厂内部维护一个 Dict[Platform, ExtractorClass]，通过反射机制自动扫描 plugins/ 目录下的 .py 文件并注册。
自动发现：新增一个“小红书”插件时，开发者只需将 XiaohongshuExtractor.py 放入 plugins 文件夹，主程序无需重新编译即可自动加载该策略。
运行时绑定：当用户输入 URL，工厂解析域名，从注册表中取出对应的策略对象并返回给主程序。

# FFmpeg工具需要项目优先进行检查，设置降级策略，如果有FFmpeg，启用高清Dash流下载，发挥最高性能，如果没有FFmpeg则使用720p低画质输出

# 补充描述
1.插件发现机制（Python 实现思路）：
利用 importlib 标准库遍历 plugins/ 目录下的 .py 文件，通过 import_module 动态加载模块，并利用 inspect.getmembers 查找 BaseExtractor 的子类，完成向工厂字典 Dict[Platform, ExtractorClass] 的注册。
注：此过程在程序 GUI 启动初始化时完成，对用户透明

2.工厂注册表设计优化：
由于同一平台可能有多域名（如 bilibili.com、b23.tv），工厂内部不直接存储域名，而是存储平台标识符。在 get_extractor(url) 方法中，增加一层 “URL 规范化处理” （如提取主域名或短链还原），再映射到具体策略类。

3.降级策略的职责归属：
为保证架构整洁，FFmpeg 可用性检测由独立的 EnvironmentChecker 工具类完成。BaseExtractor 的 download 方法接收一个 capabilities 参数（例如 ffmpeg_available: bool）。具体策略类（如 BilibiliExtractor）根据该参数运行时选择不同的下载子策略（高清 Dash 流 vs 720P 直链流）。这体现了策略模式嵌套策略模式的灵活运用。

4.UI 交互优化策略：
若启动时检测到缺失 FFmpeg，主界面右下角常驻提示：“未检测到 FFmpeg，高画质不可用。点击此处设置路径。” 点击后弹出文件选择框，允许用户手动指定 ffmpeg.exe 位置，路径保存至 config.ini。这体现了工厂模式对配置管理的扩展应用。

# 暂定的项目目录
VideoDownloader/
├── main.py                     # 入口源码（打包后为 main.exe）
├── core/                   
│   ├── extractor_factory.py    # 工厂 + 插件扫描与注册
│   ├── base_extractor.py       # 抽象策略接口
│   └── environment_checker.py  # 环境检测与降级决策
├── plugins/                    # 物理插件目录
│   ├── bilibili_plugin.py      
│   └── douyin_plugin.py        
├── tools/                      # 外部依赖存放
│   └── ffmpeg                  
├── gui/                        # UI 相关代码
│   ├── main_window.py          
│   └── download_thread.py      # 防止 UI 卡顿的 QThread
└── config.ini                  # 保存用户自定义工具路径