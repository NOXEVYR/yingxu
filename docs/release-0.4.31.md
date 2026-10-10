# 0.4.31 / xmind.3 思维导图交互与标注

左键空白拖动框选主题，Ctrl / ⌘ 点击切换选中，Shift 点击追加；中键拖动画布。Delete 一次删除全部选中分支及后代，Ctrl / ⌘ Z 一次恢复整批操作。父子同时选中时去重处理，中心主题保留。单选、框选、撤销及标签切换都保留文稿草稿；中文组词和文本输入控件不被结构快捷键抢占。

节点显示标签、优先级、任务进度、备注和链接。属性面板支持批量追加、替换和移除标签，统一标记、备注、链接与样式。批量修改先检查所有目标，再一次提交；富文本备注和未知标记、字段仍保留。可以批量移动分支，折叠和复制选中大纲。

链接保存到原生 `href`：HTTP(S) 在外部浏览器打开；`xmind:#<ID>` 跳转同一文件内的主题（含其他画布）；`yingxu://item/<ID>` 打开映序已登记文件。选择文件时可搜索不同项目；点击后沿用工作台跨项目导航，保持原导图标签和草稿。文件改名仍使用原身份，移除后的目标显示错误。映序文件链接依赖当前资料库，拷贝到另一台电脑不自动迁移资料库身份。原始 `file:`、附件等未实现的链接保留显示，不作为任意程序执行入口。

字段依据官方 [XMind 模型](https://github.com/xmindltd/xmind-model/blob/c448b0fcfc8bee51f68124aae87a57e3947c6b44/src/models/topic.ts)、[序列化器](https://github.com/xmindltd/xmind-generator/blob/af8e66ab57f480854671a45bccaa029151977534/src/internal/serializer.ts)与 [标记定义](https://github.com/xmindltd/xmind-sdk-js/blob/d35e820a2d09995c69bbb452d4edc42b09a13946/src/common/constants/marker.ts)。标签为字符串数组，标记为包含 `markerId` 的对象，纯备注为 `notes.plain.content`；已有对象的额外字段继续保留。

关系线可在属性面板定位关联主题；范围边界、概要、关系线编辑、精确排版和富文本备注编辑仍保留原有边界。包含复杂范围或被关系线引用的删除请求整批拒绝，防止改变关联含义。

## Windows 0.4.31 / xmind.3

[下载 Windows x64 完整包](https://github.com/NOXEVYR/yingxu/releases/download/yingxu-v0.4.31/YingXu-v0.4.31-Windows-x64.zip) · [发布与附件校验](https://github.com/NOXEVYR/yingxu/releases/tag/yingxu-v0.4.31) · [精确源码 8d758c3](https://github.com/NOXEVYR/yingxu/tree/8d758c325136bb447f6fb63701fed80b7ea5986a)

- ZIP：448724178 字节；SHA-256：`9505c932c336c96a8a61793bff5bd0f2ec69114a8c05a61537fa2e91548b9dde`。
- 完整包 38 项检查通过，81 个前端脚本通过。真实 WebView 验收覆盖 Ctrl 滚轮、双击输入、可信框选、Delete 与撤销。
- 后端运行 1120 项，其中 10 项跳过；另有 2 项因 C 盘测试夹具 ACL 拒绝而报错。涉及相同文件的 26 项相关套件在 D 盘隔离目录通过。

macOS 0.4.31-mac.1 云端回归未通过，暂不发布。[GitHub Actions CI 运行结果](https://github.com/NOXEVYR/yingxu/actions/runs/38039196355)：1120 项中 7 项失败、27 项错误、33 项跳过；结果包含旧测试的 Windows 平台假设及跨项目/迁移残留核验问题。当前 Mac 可下载版本仍为 [0.4.20-mac.1 历史试用版](https://github.com/NOXEVYR/yingxu/releases/tag/yingxu-v0.4.20-mac.1)。

所有写入验收使用合成项目，真实用户资料不纳入程序包。
