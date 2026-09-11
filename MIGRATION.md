# 仓库迁移说明

映序现在使用独立仓库 `turnsolesama/yingxu`。源码、功能文档和后续开发在本仓库维护；[portfolio](https://github.com/turnsolesama/portfolio) 保留工具总入口与原有历史。

迁移来源是 `portfolio` 的 [`d3b47d7fc4320f627e6b5fc8f653fcbd35007670`](https://github.com/turnsolesama/portfolio/commit/d3b47d7fc4320f627e6b5fc8f653fcbd35007670) 提交中的 [`yingxu/` 目录](https://github.com/turnsolesama/portfolio/tree/d3b47d7fc4320f627e6b5fc8f653fcbd35007670/yingxu)。该目录内容提升为本仓库根目录；`docs/`、`desktop/`、`frontend/` 等路径及[开发说明](docs/开发说明.md)仍相对于这个根目录。

- 原仓库提交历史与既有下载资产保留。源码迁移本身不代表发布新的应用版本，也不把旧完整运行包加入新 Git 历史。
- Windows 0.4.4 完整包继续使用原 [`yingxu-v0.4.4` Release](https://github.com/turnsolesama/portfolio/releases/tag/yingxu-v0.4.4)；macOS M 系列 0.4.3-mac.1 试用包保留在 [`yingxu-v0.4.3` Release](https://github.com/turnsolesama/portfolio/releases/tag/yingxu-v0.4.3)。首页未改用不存在的新仓库下载资产。
- 本仓库已发布 [macOS **0.4.4-mac.1** 试用包](https://github.com/turnsolesama/yingxu/releases/tag/yingxu-v0.4.4-mac.1)，适用于 macOS 14+、Apple Silicon（arm64）。最终 ZIP 已在原生 macOS 与 WKWebView 验证，并在上传后重新下载复验。试用版未公证，尚未完成人工中文输入法与不同 DPI 显示环境验收，不将自动化构建通过表述为完整 Mac 使用验收。0.4.5 新增 SKILL 多软件来源，后续下载见[本仓库发布列表](https://github.com/turnsolesama/yingxu/releases)。
- [首页版本记录](README.md#044文档查找文件链接与画板性能修复)已按语义版本倒序整理并补齐 0.4.4；保留原有历史说明，未补写缺少依据的 0.4.2 内容。ZIP 导入仍作为独立使用说明保留在首页。
- 版本、大小与校验记录见[原发布说明](https://github.com/turnsolesama/portfolio/blob/d3b47d7fc4320f627e6b5fc8f653fcbd35007670/yingxu/releases/README.md)。首页示意图保留在本仓库 `docs/assets/`，无需其他工具目录。
- 本机项目、素材、数据库与缓存不属于此次源码迁移。已安装程序的数据位置和启动方式不变，运行包升级仍按[安装与运行](docs/安装与运行.md)操作。

[返回映序首页](README.md) · [工具总入口](https://github.com/turnsolesama/portfolio)


