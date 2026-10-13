# 0.4.32 / player.1 云端源码候选

本候选基于 main `fc505375ea04128a79f5efd9be3a9ea383dc6186`，保留 Windows 正式 0.4.31 构建源 `8d758c325136bb447f6fb63701fed80b7ea5986a` 之后的所有上游改动。没有发布、推送、安装或修改系统关联。此文件描述可供独立审查的源码；没有 Windows 安装包。

## 快速打开

Windows 的文件打开参数与实例收件箱现在将 MP4/M4V/MOV/WebM/MKV/AVI 路由至独立快速窗口。这是容器路由表，不是编码兼容保证。文档仍保留原来的只读快速阅览、文字缩放和“查看源码”。从主工作台文件选择器打开视频仍使用工作台。

文档和视频窗口共用一个自定义标题栏，替代原生 WS_CAPTION，保留 WS_THICKFRAME 的原生缩放边框。“在映序中打开 ↗”在最小化、最大化、关闭的左侧，随标题栏一起隐藏，不再占用文档第二行。标题空白支持拖动/双击最大化；按钮拥有独立点击区域和辅助名称，尺寸按 DPI 计算。

视频快速窗口只包含播放暂停、时间拖动、±5 秒、音量静音、倍速、循环与全屏。快捷键：空格/K、左右（Shift 为 30 秒）、上下音量、M、L、F、Home/End、Esc；输入框、选择框、按钮和输入法组合事件避让。播放时控件自动隐藏，移动指针或操作时唤出；暂停保持可见。加载失败显示明确提示。打开尝试有声播放；浏览器拒绝时保留首帧并提示点击播放。

快速窗口仅拦截本窗口随机 URL 的页面和当前视频两个资源。没有任意路径 HTTP 接口、目录映射或外网资源。一个只读句柄固定当前文件，Range 包括首尾、后缀及 416；响应流不将整部视频读进内存，窗口关闭撤销所有读取。文件播放期间该句柄禁止写入，关闭窗口释放。

WebView2 的 `ContainsFullScreenElementChanged` 同步切换 WinForms 窗口：去除边框与标题栏/状态栏，使用当前屏幕 Bounds；退出恢复原正常大小/位置和最大化状态。工作台与快速窗口都接入。原生实现尚未在 Windows 实机运行。浏览器全屏证据仅证明网页元素层行为，不能证明 Windows 边框、焦点或 DPI。

快速窗口进入主工作台时先暂停并传递时间、是否播放、音量、静音、速度和循环。工作台同标签、同路径及同 URL 的重绘保留播放器节点；切换或关闭释放旧节点。视频最小化/页面隐藏暂停；既有音频后台播放约定保留。

## 工作台审片与粗剪

“审片与粗剪”默认折叠。提供毫秒时间码、备注标记、AB、入出点与 I/O 快捷键。记录保存到应用数据中的独立 JSON，使用原子替换和版本冲突检查；不会写视频。项目内记录使用稳定条目 ID，外部视频记录按规范路径绑定。外部视频改名后不会自动迁移记录；文件元信息变化会提示重新核对时间标记。关闭有未保存记录的标签需保存、放弃或取消。

粗剪目前为单一入出点区间，不含多片段时间线。导出只接收已登记的视频 ID、范围与方式，使用本机 FFmpeg，限制输入协议为 file、限制到已支持的容器解复用器；不访问网络、不上传视频。一个工作线程，最多四个在途任务，一小时上限，并检查磁盘空间、输出和日志大小。

- 快速复制：MKV，第一条视频和第一条可选音轨，`-ss` + `-c copy`。受关键帧与时间戳约束，可能包含入点前内容，时长可能大于选择区间。
- 精确时间裁切：解码丢弃入点前数据并重编码 H.264/AAC MP4，保留 VFR 时间关系。输出 8-bit YUV420，适合 SDR 预览；不是 HDR/高位深母版保存方案，也不宣称任意入出点对应严格源帧。

其他音轨、字幕、附件不导出。输出和临时文件只在本任务随机目录中，源视频始终保留。只有成功才发布输出；取消/失败只清理明确的本任务临时文件，清理失败不会假报成功。完成记录可在后台重启后恢复。桌面端使用“在文件夹中查看片段”，浏览器端提供下载；没有放开工作台原有的通用网页下载权限。完成输出不会自动清理，最多在内存恢复 100 条历史，单文件展示最近 20 条。

## 帧准确边界与引擎决定

±40 ms 明确叫“近似时间步进”，不是逐帧。真实合成 CFR 有 192 帧（24 fps）；VFR 有 128 帧，后半段帧间隔是 125 ms。固定 `currentTime ± 1/fps` 无法对后者实现逐帧。`requestVideoFrameCallback` 可以报告提交显示的媒体 PTS，但不是源帧索引，也不保证浏览器 seek 选中任意指定帧。[HTML 标准接口说明](https://developer.mozilla.org/en-US/docs/Web/API/HTMLVideoElement/requestVideoFrameCallback)

严格前后帧步进、任意 VFR 帧号定位、PTS 索引持久化、按源帧边界的导出尚未实现。下一阶段需锁定源文件身份、构造实际 PTS/关键帧索引，并验证解码帧和显示帧一致；不能仅换 UI 名称。现有运行时打包主动移除 ffprobe，本候选没有把云端测试用 ffprobe 变为产品依赖，也没有扩大运行时许可证/包范围。

当前保留 WebView2：常见 H.264/AAC、VP9/Opus 在云端 Chromium 实测可播；AVI 中的 MPEG-4 明确失败，故尚不能声称替代所有格式播放器。若后续目标包括更广泛的专业格式、逐帧或 HDR，需评估 libav/专用解码显示路径或媒体引擎，并用真实 PTS/帧图像验证。mpv 自身也说明部分 VFR 的 seek 型帧步进存在边界，换引擎本身不能作为帧准确证明。[mpv frame-step](https://mpv.io/manual/stable/#command-interface-frame-step)

FFmpeg 官方区分输入 seek 在转码时的精确丢弃与流复制时保留片段，本候选据此区分两种导出方式。[FFmpeg seek 文档](https://ffmpeg.org/ffmpeg.html#Main-options)

WebView2 官方文档要求宿主响应网页全屏变化；仅 HTML 全屏不会自动隐藏宿主窗口装饰。本候选在原生宿主处理该事件。[WebView2 全屏状态](https://learn.microsoft.com/en-us/dotnet/api/microsoft.web.webview2.core.corewebview2.containsfullscreenelement)

## 可复现运行与测试

使用隔离绝对目录启动源码服务：

```sh
python3 -B server.py --port 18791 --data /tmp/yingxu-player-data --projects-root /tmp/yingxu-player-projects
```

本候选的云端验证使用临时目录、合成视频和已有浏览器，没有用户视频或付费服务。生成器必须写入新目录，拒绝覆盖已有视频：

```sh
python3 tools/make_video_fixtures.py /tmp/yingxu-video-fixtures
python3 tools/video_smoke.py --media /tmp/yingxu-video-fixtures --evidence /tmp/yingxu-video-evidence --browser /usr/bin/chromium
python3 -B -m unittest discover -s tests -p test_video_review.py -v
```

浏览器烟测需已安装的 Playwright 和 Chromium，生成器需 FFmpeg；ffprobe 仅用于测试 PTS 证据。Windows 构建仍按 `desktop/build.py` 使用校验过的固定官方 SDK 和 .NET Framework 编译器，新增资源和 C# 文件已加入所有构建分支。新增原生 Range/窗口恢复测试代码，但未在此 Linux 云环境执行。

保留未测：Windows 真实标题栏命中、拖动、最大化还原、Esc/Alt-F4、不同屏幕 DPI、焦点与多屏切换、硬解和物理音画同步；Windows 原生编译和完整包；macOS 原生快速视频窗口与原生全屏。本候选只验证共享网页在 Linux Chromium 的行为，不把它等同双端原生验收。截图均为合成素材的浏览器画面，不包含原生自定义标题栏。

## 功能图片展示

`tools/video_showcase.py` 使用隔离数据和真实合成视频捕获独立 PNG：快速控制条、控件隐藏的浏览器全屏、主工作台审片、导出成功、导出中/取消完成、未保存记录保护、AVI 格式错误。标题栏位置另为醒目标注的布局示意，不能作为原生运行截图；不使用微型拼图或 PDF 代替直接看图。

导出取消截图使用 CFR 素材的 4K 放大副本，可先用 FFmpeg 的 scale=3840:2160、libx264 ultrafast、threads=2、原音轨 copy 生成 `showcase-4k.mp4`，再执行 `python3 tools/video_showcase.py --media <合成素材目录> --output <新输出目录>`。未上传私人视频或调用付费服务。

补拍发现窄工作台可能挤掉控制按钮，现支持换行并有 520 像素检查。连续确认也暴露了原有复用 dialog 的旧 close 事件影响新提示的问题；选择完成即解除旧监听，并拒绝当前打开提示收到的旧关闭事件。42 项浏览器检查包括强制重现此异步事件顺序。[HTML dialog 关闭事件任务](https://html.spec.whatwg.org/multipage/interactive-elements.html#close-the-dialog)
