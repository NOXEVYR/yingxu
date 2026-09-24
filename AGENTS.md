# 映序公开版开发约定

- Windows 增量更新只在用户点击检查、确认下载和确认退出安装后执行；固定官方仓库 `NOXEVYR/yingxu`，禁止启动联网、静默整包回退或终止用户进程。`tools/package_release.py` 的外部清单必须包含精确内部清单摘要 `release_manifest_sha256`。程序和数据目录必须分离；项目、草稿、数据库、配置、未登记文件不能纳入更新。按文件复用、摘要校验、进程退出握手、持久事务回退和恢复不得跳过。定向运行 `test_incremental*.py`、`test_update_service.py`、`frontend_incremental_update.cjs`，原生 `desktop/build.py --test` 包含安装退出桥接；独立运行时测试设置 `YINGXU_INSTALLER_TEST_RUNTIME` 指向已验证官方 runtime。详见 `docs/incremental-updates.md`。

- 中文本地视频创作项目工作台；独立仓库根目录。公开版不包含任何个人素材、数据库、缓存、日志或帐号配置。
- Python 3.11+ 标准库 HTTP + SQLite，原生 HTML/CSS/JS；源码运行可使用 Pillow 与 PATH 中的 FFmpeg；公开完整包必须自带锁定来源的运行环境。无 CDN、遥测或启动时自动下载。开发构建可以显式下载锁定的上游档案。
- 默认应用数据 `%LOCALAPPDATA%\YingXu`，项目在 Windows“文档”目录的 `YingXu\Projects`。`YINGXU_DATA_DIR` 和 `YINGXU_PROJECTS_DIR` 只接受绝对路径；测试必须覆盖到临时目录，禁止在真实用户数据上测试。
- 桌面仅监听 127.0.0.1:8791，开发后台可用 `--port`。写接口要求同源和会话令牌；保留版本备份、原子替换、冲突检查和有界后台工作队列。
- 界面默认黑白，雾白松绿和暖纸书卷为可选浅色配色（appearance_theme: swiss/pine/paper）。使用系统已有字体，工具区与正文独立排版，不下载字库、不加日夜模式；侧栏悬停只做短暂透明度过渡，尊重减少动态效果。删除是可恢复的应用回收站，不永久删除素材。
- 分类显示名为文本、素材、预演、记录；内部 scripts/shots/previs/references 标识保持不变。普通外观调整和项目库归类不改既有磁盘目录；0.4.10 明确确认的项目迁移可在目标副本中升级目录布局，必须保留源目录。侧栏按用途分隔，SKILL 库、AI 协作、回收站位于右上角工作空间菜单，收起菜单须恢复可见焦点。外观改动不得重写 Markdown 或覆盖 Word 原有字体与排版。
- 侧栏项目列表跟随项目库所选分类（直属项目），选择保存在本地界面配置；切分类不得打开或关闭项目、丢失文稿。项目列表按可用高度自适应：默认两行，850px 高三行，1000px 高四行，更多内部滚动。全局搜索使用独立图标位于范围搜索左侧；分类根目录不重复标题，子文件夹保留祖先导航。各弹窗遮罩必须跟随配色，黑白默认不得残留绿色。
- 回收站清理默认预览后确认（用户可在设置关闭弹窗，仍必须取得后台预览令牌）：项目内原文件移入 Windows 回收站，外部引用与外部 SKILL 保留源文件。严禁永久删除降级；共享、状态变化、未知目录内容和失败必须保留记录并解释。相关测试仅使用临时合成文件，禁止操作真实回收条目。
- 验证：`python -B -m unittest discover -s tests -v`、`node --check frontend/app.js`、`node tests/frontend_context_menu.cjs`、`node tests/frontend_drag_drop.cjs`、`node tests/frontend_selection.cjs`。
- Markdown 编辑器构建：在 `tools/markdown-editor` 执行 `npm ci --ignore-scripts --no-audit --no-fund`，然后 `npm run build`；每步成功后再运行前端测试。版本与完整性由 `package-lock.json` 锁定，输出本地 bundle、依赖清单和许可证，禁止从 CDN 加载。Node.js/npm 只在开发构建与测试时使用，完整包运行不需要 Node.js。
- Markdown 文本是唯一保存模型，不将排版后的 HTML 回写文稿。超过 500000 字符或混合换行降级源码；中文 composition 期间禁止重建/关闭编辑器，保存草稿需与当前文本一致，并保留原文件 BOM 和换行方式。
- Windows 桌面离线构建：`python desktop/build.py --sdk-package <已下载官方SDK.nupkg> --output YingXu.exe --test`。构建脚本校验固定 SDK 摘要，不下载依赖。
- 发布：`python tools/package_release.py`，仅白名单打包；`python tools/verify_release.py releases/YingXu-v0.4.15-Windows-x64.zip` 使用隔离临时目录验证。
- 不提交构建日志、本机配置、个人目录、素材和数据库；打包文件不能包含个人绝对路径。

- 完整包先运行 `python tools/prepare_runtime.py --cache <构建缓存> --download`；运行时清单逐项校验，只能来自 `tools/runtime-lock.json`，禁止复制本机安装环境。大体积 ZIP 放 GitHub Release，不提交 Git 历史。

- 新增定向回归：`node tests/frontend_marquee.cjs`、`node tests/frontend_capture.cjs`、`node tests/frontend_resource_groups.cjs`、`node tests/frontend_live_markdown.cjs`、`node tests/frontend_markdown_integration.cjs`；后端 `python -B -m unittest discover -s tests -p test_markdown_assets.py -v`、`python -B -m unittest discover -s tests -p test_resource_groups.py -v`。完整前端测试应逐个运行 `tests/frontend_*.cjs` 并检查退出码。
- 所有合成测试根目录使用 `Path(temporary).resolve()` 后再构建来源路径，避免 Windows TEMP 短路径或大小写不同导致索引漏项。
- 截图仅在用户按快捷键或点击截图时触发，无持续屏幕/剪贴板轮询；一次只截请求时鼠标所在的屏幕，最大 40000000 像素。原生 `desktop/build.py --test` 包含 CaptureTests，使用合成位图、模拟剪贴板和临时 HTTP 服务；不得把通过合成测试写成已验收真实屏幕、多显示器或真实剪贴板。
- 截图快捷键录入使用聚焦按钮的按键事件，不安装全局键盘钩子；先等待桌面暂停本应用截图热键的确认，再接受组合。Esc/Tab、焦点离开、关闭设置或页面导航必须恢复已保存热键。保存与系统注册分别显示，只有原生回报匹配当前已保存组合并注册成功时显示“已生效”；旧宿主或超时不得假报成功。检查 `tests/frontend_hotkey_recorder.cjs` 及原生生命周期回归。
- 文件名标题置于正文 contenteditable 外，既有 Markdown 第一行不删除；新建普通笔记显式空正文。截图保存到项目参考资料后，仅在原笔记仍处于可编辑模式、项目/文稿/输入法状态未改变时插入草稿。
- 素材组是本项目内的逻辑集合，不搬动文件或改变其分类；成员与修订号需由后台验证。Markdown 图片仅预览已登记的项目内独立光栅文件，拒绝外部 URL、越界路径与链接；无 imageResolver 不加载图片。

- 框选只从资源滚动区空白启动；默认本页 48 项，排除隐藏组成员/控件/正文。缓存卡片几何、合并帧更新，空闲不得持续 RAF 或轮询；Ctrl 切换、Shift 追加、Esc/取消恢复，减少动态效果设置下不自动滚动。
- 独立文稿标题仅显示名称、不含扩展名，真实文件路径与正文不得因显示标题改变。截图插入只在选区末尾追加，不替换已选正文；异步结果处理结束前保持忙碌，防止退出漏掉新草稿。
- 0.3.7 点击项目文件标题复用确认式重命名弹窗；名称输入不含原扩展名，确认时保留扩展名和未保存正文，不逐字自动重命名文件。不可改名来源维持原权限边界。

- 0.4.0 Word 编辑只挂载最多 40 段，跨页草稿必须保留；不要重引入全量 textarea 与逐项布局读写。检查 `node tests/frontend_docx_editor.cjs`。
- 0.4.4 Word 预览同样按 40 段挂载；文内查找须覆盖当前草稿和跨页定位。画板 iframe 必须固定宿主保留撤销，隐藏时停用，关闭时销毁；本地字体构建不得放宽 CSP。画板构建在 `tools/canvas-editor` 执行 `pnpm install --frozen-lockfile --ignore-scripts` 后 `node build.mjs`，开发依赖不打入运行环境。容量清理只允许预览 token 中的受控缓存/旧版本，选项变更必须使确认失效。
- 画布退出/整理标签前先同步 iframe 草稿再判断 dirty；明确放弃后销毁旧实例，防止退出前再次读回。保存中/输入法状态须整轮预检；后续标签取消时恢复已销毁的活动画布。退出成功前不重新创建已放弃的 iframe。回归 `node tests/frontend_canvas_exit.cjs`。
- SVG/HTML 是独立只读类型，无缩略图任务。SVG 只能净化后作为图片提供，所有媒体直链必须经过同一净化器；HTML 静态片段必须在无 allow-* 的 sandbox iframe 与限制性 CSP 中，原始媒体响应为文本附件。检查 `python -B -m unittest discover -s tests -p test_static_formats_http.py -v` 和 `node tests/frontend_static_formats.cjs`。

- macOS 14+ Apple Silicon 试用版由 `macos_app.py` 使用系统 WebKit。平台分支保留 Finder、废纸篓、安全排他重命名和退出草稿保护；共同界面仍提供文内搜索、版本与容量管理。不得把 Windows 截图、托盘和打开方式注册宣称为 Mac 已实现。
- macOS 在独立 Python 3.13 环境执行 `python -B macos/prepare_dependencies.py`，只下载 `macos/dependencies-lock.json` 中的已锁定档案并校验大小/SHA-256；开发依赖和安装清单不等于模型。Node 仅测试和构建 Markdown，不随应用运行。
- macOS 构建 `python -B macos/build.py` 使用每次新建的暂存目录；验证 `python -B macos/verify_release.py releases/macos-preview/YingXu-v0.4.15-mac.1-macOS-arm64.zip` 必须对最终 ZIP 解压、验签并运行原生及 WKWebView 合成检查。用户目录、输入法、权限对话框或长期稳定性未经实测时必须说明。

- README 下载链接必须按实际已发布资产更新，不因源码合并提前切换版本，保留 0.4.4 和更早更新记录。
- 原生应用图标须与 frontend/index.html 的取景框/播放标志一致，不能仅给旧图案换色。Windows ICO 使用 32 位 DIB 帧兼容 .NET Framework，并通过生命周期检查核对 WM_GETICON、窗口和托盘实际图标。0.4.6 图标修复由清单 icon_revision=viewfinder-v1 区分；明确授权同版本覆盖时，保留原标签，发布说明记录附件的新来源提交及摘要。保留旧远程附件直到新附件上传回检成功，再按资产 ID 改名切换；切换失败恢复旧名称及说明，成功后的旧附件清理失败仅报告残留。已有本地备份应复用，不为备份重复下载旧安装包。
- 来源登记仅管理扫描配置；外部 SKILL 始终只读，关闭/移除位置保留源文件与项目绑定。映序本地不可关闭，内置位置不可移除；自定义最多 16 个具体本地目录，拒绝网络共享、链接、磁盘根和整个用户目录。同物理文件多来源去重，身份复用必须重新检查当前路径，禁止凭历史 inode 猜测。
- SKILL 扫描仅初始化、显式刷新或登记变更触发，查询只读索引。保持单文件 1 MiB、2000 技能、20000 条目、3 层深度和协作式 3 秒预算；无常驻扫描、无新增依赖或模型。只在某位置扫描完整时替换该位置旧成员，读取失败与预算中断保留旧成员并向界面报告；列表组装需持有来源锁。缓存按 mtime/size 复用，不宣称硬性延迟上限。
- WorkBuddy 只使用受校验安装清单中的技能目录，ZCode 只识别约定缓存层级，不将缓存存在推断为插件当前启用。不得为自动发现读取真实账号、凭据或任意历史日志。默认目录以 `yingxu/skill_sources.py` 为准，自定义补充未知布局。
- 来源回归：`python -B -m unittest discover -s tests -p test_skill_sources.py -v`、`python -B -m unittest discover -s tests -p test_skill_sources_http.py -v`、`node tests/frontend_skill_sources.cjs`；关联检查覆盖原技能、回收、搜索、交接和定位文件。测试使用临时合成目录，浏览器验证不得充当 macOS 原生验收。

- 新项目使用 layout_version=1 的中文分类目录（文本、素材、音乐、角色、场景、道具、预演、生成素材、成片交付、记录、未分类），按创建时选中的项目库分类层级落盘；已登记的物理分类目录可以复用。未迁移的旧项目 layout_version=0 保持原路径。只有经预览、确认的版本化迁移可将目标副本升级为布局 1，保留原件并受控更新内部 Markdown 链接。所有分类路径须经过 project_layout.category_paths(project)，不得硬编码旧目录；逻辑归类或重命名不自动搬动已有文件。

- 音频在最小化和关闭到托盘时继续播放；视频仍暂停。页面退出、标签关闭或切换文件须释放媒体。播放器重绘同一音频时保留节点与播放进度；仅当前文件解码，不为切歌预加载整页。检查 `node tests/frontend_media_lifecycle.cjs`、`python -B -m unittest discover -s tests -p test_music_category.py -v`，原生生命周期测试另覆盖 Windows 最小化，合成静音内容不得操作真实音乐。
- 设置更改项目存放位置时，有活动项目必须预览全部活动项目的源/目标及容量，再“确认保存并迁移”；没有活动项目才直接保存。旧 POST /api/project-storage 仍只配置后续新项目，不能先调用它再迁移。全部复制及 SHA-256 校验成功后，才提交项目路径与新根目录；旧目录和外部引用原件保留。迁移前拒绝未保存文稿、画板/属性草稿和在途写操作，不自动保存或放弃；迁移中写接口返回 409，允许读取与同 token 启动请求重试。前端不提供假取消，不把断连当作失败，保留任务 ID/令牌以重试查询。
- 迁移在应用数据 project-migrations 中保留数据库备份与恢复记录。启动恢复只核对并修复被中断的数据库/配置提交，不自动续传复制；状态混杂时保留两边文件并明确报错，禁止猜测清理。配置磁盘离线时仍须允许打开设置和可用的已有项目，新建明确报错，不能悄悄回退默认目录。
- 导入界面默认复制进项目分类，可显式选择引用原位置；旧 API 省略 mode 时保留 reference 契约，ZIP 始终受控解压。原件保留，测试仅使用临时合成目录。
- 存放位置与迁移定向验收：`node tests/frontend_project_storage.cjs`、`node tests/frontend_project_migration.cjs`、`node tests/frontend_import_mode.cjs`；后端 `python -B -m unittest discover -s tests -p "test_project_storage.py" -v`、`python -B -m unittest discover -s tests -p "test_project_migration*.py" -v`、`python -B -m unittest discover -s tests -p "test_migration*.py" -v`。覆盖全活动项目一致性、失败/中断恢复、幂等请求、写保护、外部引用、链接、长路径与过期弹窗回调；不得以真实用户数据执行迁移验收。

- Windows 资源区外部拖拽优先经受信任的 WebView 附件消息读取磁盘路径，再走 mode=copy 的既有导入队列；路径解析仅读取，不能执行导入。按锁定 SDK 和真实 WebView 测试确认附件类型，不假定在线旧示例的类型仍适用。无磁盘路径的合成 File 保留字节上传；写入结果不明时不自动重传。
- 跨项目移动复用 POST /api/move 的 target_project_id；保留条目身份、历史和批次内关系，部分素材组或未一同移动的本地文稿链接必须明确拒绝。先复制校验再提交，提交结果不明须核对持久记录，不能删除可能已经提交的目标。目标验证后才清理源文件，失败保留副本并提示。UI 的忙碌状态在首次 await 前建立，已提交后的列表刷新失败不能重做移动。
- Windows 文件夹前台打开仅在新宿主能力 yingxuDesktopOpenFolder 下请求 native_open，由后台验证登记路径、宿主发起 Explorer；旧宿主和浏览器沿用后台打开。匹配和激活使用单一 STA 工作线程，有界重试，用户切换应用后不抢焦点，不永久置顶。
- 新定向检查：tests/frontend_native_drop_import.cjs、frontend_upload_connection.cjs、frontend_cross_project_move.cjs、frontend_folder_focus.cjs、test_cross_project.py、test_cross_project_http.py。原生 build.py --test 包含文件夹前台合成测试；有锁定 WebView 运行时时还验证真实文件对象桥接，不能将其等同于对方电脑上的 Explorer OLE 手势验收。

- Windows 显式打开文件使用 Hub.ResolveOpenedFilePath，以文件句柄解析本机兼容目录联接，再校验真实路径；启动参数、IPC、快速阅览和转工作台统一使用实际路径。素材原生拖出仍用严格 ValidateNativeFilePath，不得将显式打开规则扩散到受管素材权限。desktop/QuickReaderTests.cs 包含真实临时联接测试；仅非递归移除联接本身，禁止测试操作用户原文件。
