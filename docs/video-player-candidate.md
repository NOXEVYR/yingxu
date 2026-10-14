# 0.4.34 / frames.2 云端源码候选

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

主工作台增加独立“暂停帧检查”。它按需顺序解码全片，建立展示序号 → 原始整数 PTS / 时间基 / 关键帧的 SQLite 索引，重复 PTS 保留为不同序号；PTS 缺失会明确显示 best-effort 值，绝不按平均 FPS 编造。前后帧、输入帧号定位显示独立 PNG；普通视频保持暂停，返回时明确提示近似时间定位。进入检查从第 0 帧开始，不假定浏览器当前时间等于某个精确帧号。快速窗口没有加入这些工具。

每个索引固定已登记源 ID、规范路径、文件身份和 SHA-256。Linux 会话用 inode、大小、mtime、ctime 签名复用已校验的源摘要；签名变化立即使索引失效，含“修改内容后恢复 mtime”的情况。缓存命中也核对签名。这是受支持 Linux 本地文件系统的变更检查，不等于每次重新读取全片或防御拥有任意文件系统元数据控制权的外部进程。Windows/Mac 不启用这条优化路径。

首次索引顺序解码两遍：ffprobe 保存真实 ordinal / 整数 PTS / duration / timebase / 关键帧，FFmpeg 再生成每张 RGB24 画面的 SHA-256。后续定位在 PTS 严格递增且有可用关键帧时，从真实关键帧 seek、按原 PTS trim，再按展示序号抽取；每张 PNG 的 RGB 像素必须匹配完整顺序解码摘要。任何对齐或解码问题回退起点，回退仍不匹配则拒绝展示。重复/缺失/倒退 PTS 保留起点 ordinal 解码。只在选出 PNG 后重置其输出时间戳；源 PTS 索引不变。相邻预取最多向后 12 帧，并按画面像素预算缩小，缓存命中不再全片散列或启动解码器。

使用与 ffprobe 同版本的 FFmpeg，检查期间工具变化使索引失效。[FFmpeg 输入 seek 与 copyts](https://ffmpeg.org/ffmpeg.html#Main-options)、[trim 的 start_pts 与 select](https://ffmpeg.org/ffmpeg-filters.html#trim)、[framehash](https://ffmpeg.org/ffmpeg-formats.html#framehash)。这些选项只提供定位手段，最终可信依据是逐张像素核验，不以 seek 成功冒充正确。

索引显示真实累计帧数、阶段、校验字节和经过时间，可取消。单工作线程，最多四个在途任务、八个会话、64 条任务状态；索引最多两百万帧/256 MiB，PNG 缓存全局 64 MiB / 128 张，缓存磁盘 512 MiB，保留 128 MiB 可用空间。索引与严格导出一小时、抽帧三分钟上限。终态与清理在锁内发布，取消/源变化/关闭不能让未清理的失败任务冒充完成。崩溃恢复仅删除自有清单登记文件，未知文件保留。缓存不是永久项目文件。HDR、非方形像素或超 1677 万像素仍明确拒绝。

实际启用范围是带已有同版本 FFmpeg/ffprobe 与 Pillow 的 Linux 云端源码环境。Windows 原有 runtime-lock 官方 FFmpeg 档案下载被代理 Tunnel 403 阻断，未核验 ffprobe；没有改变运行时锁文件、绕过下载或用任意本机工具替代。Windows 暂停帧检查和严格帧导出明确关闭；Mac 同样关闭。普通快速播放与审片保留。持久帧索引和严格音频帧边界裁切尚未实现。

## 严格源展示帧范围导出

工作台检查面板以当前静帧设置首帧/尾帧，范围为 **[first, last] 两端包含**，单次最多 10000 帧。每次从源起点按 ordinal 选择，解码后无损编码 FFV1，而非 stream copy。输出是显示方向下的 8-bit RGB 像素保真，不是原压缩码流、高位深或所有色彩元数据无损归档。

本阶段按钮显式标注 **不含音频**，也不包含字幕、附件、其他视频轨。上方原有 H.264/AAC 按时间裁切仍可保留第一音轨，但不宣称按展示帧准确。没有实现首音轨 PCM 严格时间边界裁切，不能把静音导出称作音画精确剪辑。

输出相对时间 = 源帧 PTS − 选择范围首帧 PTS。若全部相对时间可精确表示为毫秒，则输出 MKV，可保留重复 PTS；否则输出 NUT，避免 Matroska 默认毫秒量化。重复 PTS 与非毫秒时间基的组合、缺失 PTS、倒退 PTS 会明确拒绝。编码后重新读取每帧 PTS，以有理数逐项对比源时间，并再次全片解码验证每张 RGB 摘要和帧数。任一不一致即丢弃本任务产物，不发布“近似成功”。末帧 duration 由容器处理，源 duration 保留在清单；严格保证的是选中展示帧的身份、顺序、数量与相对 PTS，不承诺输出结束时刻或音频边界。

视频与 `frames.json` 清单保存在受控导出目录；清单含源 SHA-256、原始 ordinal/PTS/duration/timebase、输出 PTS/timebase、每帧 RGB SHA-256、端点与无声语义。成功后检查会话关闭也保留产物，现有审片导出历史可恢复视频下载。单次最多 32 GiB、一小时，取消或失败只清理自有文件。MKV/NUT + FFV1 需要支持该格式的编辑工具，独立快速播放器不保证可播；不会新增快速播放器负担。

## 本阶段真实性能证据

5 分钟、640×360、24 fps、7200 帧合成片（连续帧号/时间码与节奏音）在同一云环境对比旧 `08f60ca`：旧索引 4.28 秒，新索引 19.63 秒；尾部第 7100 帧应用冷缓存定位从 1.920 秒降为 0.244 秒；热缓存从 0.031 秒降为 0.011 秒；第 6500 帧随机冷定位从 1.714 秒降为 0.244 秒。代价是首次完整 RGB 校验增加索引时间，不能宣称所有操作都更快。

这里的冷/热仅指应用 PNG 缓存，未清理 OS 页缓存；测试期间可能存在同机浏览器验证负载，不是硬件通用 SLA。另以独立顺序 rawvideo 管道逐帧读取全部 7200 张 RGB，与索引每帧摘要核对通过，并测了连续 15 次步进。复现：`python tools/video_frames_benchmark.py --media <合成长片> --output <证据JSON>`。进度取消回归中的人工闸门只验证生命周期，不作为性能证据。

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
python3 -B -m unittest discover -s tests -p "test_video_*.py" -v
python3 tools/video_frames_smoke.py --media /tmp/yingxu-video-fixtures --evidence /tmp/yingxu-frame-evidence
```

浏览器烟测需已安装的 Playwright 和 Chromium，生成器需 FFmpeg；ffprobe 在源码开发环境用于真实暂停帧索引，并用于测试 PTS 证据；打包平台能力受上述限制。Windows 构建仍按 `desktop/build.py` 使用校验过的固定官方 SDK 和 .NET Framework 编译器，新增资源和 C# 文件已加入所有构建分支。新增原生 Range/窗口恢复测试代码，但未在此 Linux 云环境执行。

保留未测：Windows 真实标题栏命中、拖动、最大化还原、Esc/Alt-F4、不同屏幕 DPI、焦点与多屏切换、硬解和物理音画同步；Windows 原生编译和完整包；macOS 原生快速视频窗口与原生全屏。本候选只验证共享网页在 Linux Chromium 的行为，不把它等同双端原生验收。截图均为合成素材的浏览器画面，不包含原生自定义标题栏。

## 功能图片展示

`tools/video_showcase.py` 使用隔离数据和真实合成视频捕获独立 PNG：快速控制条、控件隐藏的浏览器全屏、主工作台审片、导出成功、导出中/取消完成、未保存记录保护、AVI 格式错误。标题栏位置另为醒目标注的布局示意，不能作为原生运行截图；不使用微型拼图或 PDF 代替直接看图。

导出取消截图使用 CFR 素材的 4K 放大副本，可先用 FFmpeg 的 scale=3840:2160、libx264 ultrafast、threads=2、原音轨 copy 生成 `showcase-4k.mp4`，再执行 `python3 tools/video_showcase.py --media <合成素材目录> --output <新输出目录>`。未上传私人视频或调用付费服务。

补拍发现窄工作台可能挤掉控制按钮，现支持换行并有 520 像素检查。连续确认也暴露了原有复用 dialog 的旧 close 事件影响新提示的问题；选择完成即解除旧监听，并拒绝当前打开提示收到的旧关闭事件。42 项浏览器检查包括强制重现此异步事件顺序。[HTML dialog 关闭事件任务](https://html.spec.whatwg.org/multipage/interactive-elements.html#close-the-dialog)
