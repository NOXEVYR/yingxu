# 按文件增量更新

Windows 桌面沿用 Python 标准库和现有运行环境，提供软件内增量更新，不增加独立常驻服务、模型或运行依赖。工作台就绪后延迟在后台检查，不阻塞启动。

## 使用

默认启用自动检查及小补丁下载，可在“设置 → 关于映序”分别关闭。自动检查跨重启按每日一次节流，失败逐步退避。Windows 检查到新版本后，只有差异压缩下载量不超过 50 MiB 才后台下载；超过上限停在确认步骤。不会自动关闭窗口或替换程序。

也可点击“立即检查更新”。程序读取固定官方仓库 `NOXEVYR/yingxu` 的发布信息，核对本机文件，显示变化、复用和移除的程序文件数量及变化文件的压缩下载量。手动下载或超过自动下载上限时，点击“确认下载更新”；下载校验完成后，点击“保存文稿并退出安装”。

退出前保留已有的保存、放弃、取消流程。正在导入或迁移时不能安装；独立阅读窗口也需先保存关闭。安装助手等待桌面和后台两个进程真正退出后，才替换程序文件。替换失败按持久记录恢复；安装不终止用户进程。

这是文件级增量：未变化的运行库不重新下载。某个大文件发生变化时，该文件需要重新下载，不承诺每次都是几 KB。检查本身会传输发布元数据、ZIP 文件目录和内部清单；界面展示的下载量是之后需要传输的变化文件压缩数据，不含这些检查数据和网络协议开销。

不会覆盖项目、数据库、设置、素材或未登记的本地文件。被修改过的程序文件、路径联接、占用、校验不符和不支持 Range 的网络会明确阻止增量安装；不会偷偷降级下载整个 ZIP。旧宿主和 Mac 能自动发现版本，安装仍使用完整包入口；此迭代未宣称 Mac 已具备自动安装。

## 发布约定

使用 `tools/package_release.py` 生成完整 ZIP 和外部清单。外部清单新增 `release_manifest_sha256`，绑定 ZIP 内 `YingXu/RELEASE_MANIFEST.json` 的精确 UTF-8 字节；GitHub 资产摘要再绑定外部清单。完整包、外部清单、版本、源提交和文件集合必须一致。上传正常完整包即可，不需要另建增量包或服务器。

旧发布没有这一字段时，更新器会提示使用完整包。首次获得这一能力需要先安装包含它的版本；之后只有带有该字段的新正式 Windows 发布才能走增量流程。`0.4.19` 首次将此功能纳入正式源码；各平台可下载版本以 GitHub Release 已公开的附件为准。

按需更新数据位于应用数据目录的 `updates/incremental/`，安装事务及变化文件备份位于 `updates/incremental-install/`。自动检查节流记录位于 `updates/automatic/state.json`。应用内调度线程按低频唤醒，退出时停止；网络检查遵守每日/退避间隔，不是持续扫描磁盘或下载。下载重试可复用已经完整下载并校验的文件，半截文件不使用。记录到达容量上限会明确停止，不能靠删除未知文件腾出空间。

## 验证入口

- `python -B -m unittest discover -s tests -p "test_incremental*.py" -v`
- `python -B -m unittest discover -s tests -p test_update_service.py -v`
- `python -B -m unittest discover -s tests -p test_automatic_updates.py -v`
- `node tests/frontend_automatic_updates.cjs`
- `node tests/frontend_incremental_update.cjs`
- `python desktop/build.py --sdk-package <已验证的本地 SDK> --output YingXu.exe --test`

合成 HTTP Range 测试须证明未读取未变化的大运行库，同时覆盖无 Range、摘要损坏、本地冲突和中途失败。安装测试须覆盖真实文件替换、回滚、恢复、进程等待和退出保护。浏览器与模拟请求通过不能替代 Windows 原生宿主测试，更不能替代 macOS 验收。
