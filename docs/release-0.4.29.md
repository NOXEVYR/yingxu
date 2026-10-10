# 映序 0.4.29 双端候选交付

Windows 版本为 `0.4.29+xmind.1`；macOS 为 `0.4.29-mac.1`。本轮仅准备构建与验收，不公开新 Release、不切换下载入口、不替换正在使用的安装。

新增 XMind 导入、右键新建和主题编辑，详见 [XMind 使用说明](xmind.md)。基线为官方仓库提交 `cde7883`，候选源码在 `codex/xmind-dual-platform` 分支；最终构建必须绑定该分支完成审核后的完整提交 SHA。

## 构建和平台范围

Windows 10 22H2 / Windows 11 x64 沿用便携 ZIP、锁定离线运行环境和 .NET Framework / WebView2 宿主。macOS 沿用 macOS 14+、Apple Silicon arm64 的 `.app` ZIP；仅 ad-hoc 签名，尚未进行 Developer ID 签名与公证，不提供 Intel Mac 包。

新增 `YingXu Windows and macOS release candidates` 手动工作流。选中候选分支，以该分支当前完整提交 SHA 填写 `source_commit`；两个任务必须使用同一 SHA，只构建、验证和保存 Actions 候选附件。工作流仅有读取仓库权限，不上传 Release。未审核前不要把版本文件推送 main，旧 `yingxu-portable.yml` 的 main 推送触发器会走历史发布流程。

Windows 本地按以下顺序执行；SDK 和 runtime 必须通过现有来源锁与逐项摘要校验：

```powershell
python -B desktop/build.py --sdk-package <官方锁定SDK.nupkg> --output YingXu.exe --test
python -B tools/package_release.py --runtime-dir <已验证runtime>
python -B tools/verify_release.py releases/YingXu-v0.4.29-Windows-x64.zip
```

Mac 构建机使用独立 Python 3.13 环境，先运行 `macos/prepare_dependencies.py`，再运行 `macos/build.py` 和 `macos/verify_release.py`。验包会解压最终 ZIP、校验 arm64、应用版本、签名，并执行原生及真实 WKWebView 检查；XMind 的创建、编辑和保存已加入 WebKit 合成检查。

## 正式发布前剩余项

- 两个平台候选包都须完成最终 ZIP 验证，记录来源提交、大小、SHA-256 和报告。
- 使用 XMind 桌面客户端人工验证新建文件与典型导入文件，尤其包含关系线、概要、附件、多画布的真实样本。
- Mac 还需实际中文输入法、权限弹窗、不同显示缩放、退出与长期使用验收；Windows 需实际输入法及 Explorer 文件打开/拖放验收。合成测试不能代替这些检查。
- 正式发布获授权后，上传各平台资产，完整下载回检，再切换 README / 官网链接；保留旧版附件。Mac 是否继续作为未公证试用版需在发布说明写明。

运行测试时只用临时合成项目与隔离数据目录。具体本机检查结果另见候选附件和本轮验收记录；未执行的 Mac 构建不能写成已完成。

## 2026-10-10 本机验证

- 79 个前端回归脚本全部退出成功，包括真实 Chromium 的 XMind 18 项编辑/布局检查和右键创建落点、创建后刷新失败重试检查。
- 后端完整套件运行 1114 项。原开发目录有 5 个环境相关错误：2 个合成 SKILL 目录替换被拒、3 个 FFmpeg 子测试启动失败。相同源码在独立 D 盘候选目录以官方运行环境复测，对应 SKILL 26 项和缩略图 3 项全部通过。首轮另有 12 项平台、符号链接权限或可选运行环境跳过，保留这些边界。
- Windows 原生构建和完整 `--test` 在独立候选目录通过，包含固定 WebView2、启动、关闭、原生附件和窗口生命周期。修正了原有桌面测试夹具没有初始化程序根目录的问题。原开发目录固定 WebView2 初始化失败，未据此修改产品安全策略；独立暂存目录复测通过。
- 使用官方运行环境补跑增量安装 29 项、安装器子进程 2 项，全部通过。
- 新的双端候选工作流 YAML 已解析核对。macOS 实际 `.app` 构建、验签及 WKWebView 执行仍待 Mac 构建机完成。
