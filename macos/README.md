# 映序 macOS 试用版

面向 **macOS 14 或更新版本、Apple Silicon（M 系列芯片）**。
这是独立的 macOS 试用包，Windows 版和既有 Windows 发布包保持不变。

当前已发布 **0.4.6-mac.1**，最终安装包已通过原生、WebKit 及上传后下载回检。
2026-09-12 同版本更新四角取景框应用图标（`viewfinder-v1`），新附件已完成 20 项验证及上传后回检；版本号和下载入口不变。
下载与校验见 [0.4.6-mac.1 发布页](https://github.com/turnsolesama/yingxu/releases/tag/yingxu-v0.4.6-mac.1)。
请以[发布列表](https://github.com/turnsolesama/yingxu/releases)中实际可下载的附件和对应校验文件为准。

## 安装

完整解压所选版本的 `YingXu-v版本号-macOS-arm64.zip`，将 `YingXu.app` 拖入“应用程序”，双击打开。
自带 Python、Pillow、FFmpeg 和本地编辑器；窗口使用 macOS 系统 WebKit。
正常使用无需安装 Python、Node.js 或 Homebrew，也不在启动时下载组件。

试用包没有 Apple Developer ID 签名和公证。如果 macOS 阻止打开，请核对来源和 SHA-256，
然后在“系统设置 → 隐私与安全”中使用系统提供的“仍要打开”；不需要关闭系统安全保护。

## 已提供

- 项目分类、素材与素材组、搜索、标签、状态、SKILL 与 AI 交接。
- SKILL 可按 DSH、WorkBuddy、ZCode、共享技能等来源及具体目录筛选，也可登记自定义目录；外部原文件只读，不增加模型或运行依赖。
- Markdown 实时编辑与源码编辑、Word 普通正文分页编辑（每页最多 40 段，保留跨页草稿）、图片及音视频预览。
- HTML 只读源码与安全静态预览；SVG 安全静态预览，轻量阴影和虚线可简化显示，原文件不改写。
- 外部 Markdown、文本和 Word 文稿编辑，保存前检查冲突并保留版本备份。
- 本机文件/文件夹选择、Finder 定位、调用默认应用。
- 文件和文件夹安全改名，重名时拒绝覆盖。
- 应用回收站与恢复；清理原文件时送入 macOS 废纸篓，失败保留记录，无永久删除后备。
- Command+S 保存，Command+F 按焦点查文档正文或当前资源，Command+K 全局搜索；Word 查找可跨分页定位。
- 同步 0.4.5 的 Markdown 项目文件链接、画板切换与撤销、本地字体、隐藏刷新和重复序列化修复；设置提供版本与缓存/历史占用预览。
- 关闭窗口前检查未保存文稿；退出会结束本实例的后台服务。

## 0.4.6 源码更新

- 默认黑白外观，雾白松绿、暖纸书卷可选；偏好保存到本机，不提供日夜自动切换。
- 侧栏按功能分组；“剧本与文档”显示为“文本”，“分镜”显示为“素材”，“参考资料”显示为“记录”。原分类 ID、文件路径和功能保持不变。
- 工具区与正文分别排版，只使用系统已有字体，无新增字库或运行依赖；菜单悬停使用短暂渐变，并遵循减少动态效果偏好。
- 黑白应用图标；SKILL 库、AI 协作与回收站集中在“工作空间”入口。既有编辑、搜索和整理功能保留。

## 第一版范围

暂不提供全局截图、标注、菜单栏常驻、原生拖出文件和 Finder“打开方式”注册。
需要拖出原文件时，先在 Finder 定位，再从 Finder 拖动。视频播放取决于系统 WebKit 支持的编码。
不自动迁移 Windows 项目路径；从 Mac 导入素材或创建项目后使用。

数据位于 `~/Library/Application Support/YingXu`，默认项目位于 `~/Documents/YingXu/Projects`。
删除或更新 `.app` 不删除这些数据。环境变量 `YINGXU_DATA_DIR`、`YINGXU_PROJECTS_DIR`
仍可指定绝对目录。应用仅监听本机 `127.0.0.1:8791`。

## 验证与反馈

构建流程在 GitHub macOS runner 上检查 Python/前端回归、应用包启动、文件改名防覆盖、
原生废纸篓往返、内置媒体组件及 WebKit 页面加载。所有检查使用临时合成数据。
自动检查不等于真实用户环境验收；目前没有人工验证中文输入法、系统权限提示和长期使用。
请反馈 macOS 版本、芯片型号、复现步骤；诊断日志在数据目录的 `macos.log`。

## 开发

在 macOS 的独立 Python 3.13 环境从应用目录运行 `python -B macos/prepare_dependencies.py`，
按 `macos/dependencies-lock.json` 下载并校验全部构建档案。然后运行 `python -B macos/build.py`，
再用 `python -B macos/verify_release.py releases/macos-preview/YingXu-v0.4.6-mac.1-macOS-arm64.zip`
验证最终解压包、签名和 WKWebView。使用 `python macos_app.py` 进行源码运行。
打包不读取个人项目、数据库、缓存、技能目录或本机配置。依赖版本记录随构建结果交付。

