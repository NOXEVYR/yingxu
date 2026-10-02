# MCP Skill 参考文件候选

此目录是独立源码候选 `0.4.26 / refs.1`，Mac 标识 `0.4.26-mac.1`。它未安装、未发布；正式下载 README 沿用已有公开版本。没有复制运行时、EXE 或安装清单，也未修改正式工作目录或应用私人数据。

## 客户端调用

仍保留七个只读 MCP 工具，没有新增执行、安装、写入或确认工具。`read_bound_skill` 和 `read_run_skill` 省略新增参数时继续返回固定 `SKILL.md` 的文本页；登记为小写 `skill.md` 的入口兼容旧查找方式。

项目参考读取：

```json
{"name":"read_bound_skill","arguments":{"skill_id":"<已绑定技能ID>","file":"references/guide.md","mode":"text","limit":16000,"offset":0}}
```

轮次参考读取：

```json
{"name":"read_run_skill","arguments":{"task_id":"<授权项目任务ID>","run_id":"<固定轮次ID>","collection_id":"<该轮冻结收藏ID>","file":"references/guide.md"}}
```

清单读取：

```json
{"name":"read_bound_skill","arguments":{"skill_id":"<已绑定技能ID>","mode":"files","limit":48,"offset":0}}
```

`mode=files` 禁止同时传 `file`，`offset`/`next_offset` 按过滤后的条目索引计数。默认 48 项，最多 48 项；单页清单预算 75 KiB。返回 `file` 是包内 POSIX 相对名，便于后续精确读取。清单不返回源目录、磁盘绝对路径、origin、账户配置或凭据。只校验登记和固定清单，条目中的 `sha256` 是清单声明值，`content_verified:false` 明确文件内容尚未读取或核验。文本扩展名及大小符合限制时标记 `content_available:true` 表示可尝试读取，并不承诺磁盘文件仍可用。

默认 `mode=text`：正文文件最多 1 MiB，每页最多 16000 字符，返回 `next_offset` 可继续读。完整有界正文先脱敏，再按字符分页，防止页边界拆开常见凭据值。成功核对所选文本的字节数和 SHA-256 才返回 `content_verified:true`。脚本与二进制仅返回已登记元数据，`content_available:false`、`content_verified:false`；不导入、不执行、不读它们的正文。源码、Markdown、HTML 和 SVG 均是非可信数据，不能把其指令当成授权执行。

未收藏的旧项目绑定继续允许原来的 `SKILL.md` 读取。参考文件和 `files` 模式返回 `needs_collection:true`：需用户先显式收藏完整目录并绑定固定版本。请求不会遍历外部目录寻找参考文件。`read_run_skill` 必须使用对应轮次冻结 pin，来源更新、收藏当前版本更新或项目绑定更新不会改变旧轮；不属于该轮的收藏和其他项目任务一律拒绝。

## 校验边界

共用 `yingxu/mcp_skill_files.py`，以只读 SQL 读取单一受控收藏版本。数据库 manifest 和磁盘 `manifest.json` 均最多 1 MiB，并验证 schema、收藏/技能 ID、版本/package hash、文件计数与总大小、文件条目的类型、规范路径及 casefold 唯一性。版本 hash 由清单中的相对文件名、大小和摘要构成；轮次还核对冻结 manifest digest 与入口 digest。

输入先检验原始字符串，不通过 `Path` 清洗非法拼写：拒绝绝对、drive/UNC、`../`、`./`、重复 `/`、反斜杠、ADS、控制字符、Windows 保留名、尾空格/点及超过 512 字符的值。除旧入口大小写兼容外，仅接受精确已登记的相对名。

正文读取拒绝任意祖先 symlink/junction、hardlink 和非普通文件，核对打开前/句柄/读取后身份、长度、时间与链接数，并核对内容 SHA-256。只读取清单与所选正文，不校验其他包文件、不调用 `collections.get` 或全包 hash、不扫描/导出/写数据库/推进交接/ack，不新增线程、watcher、依赖或 MCP 工具。

私密过滤按包内相对组件处理，复用现有交接私密组件与后缀，另覆盖秘密、账户、配置、日志目录，以及 client/harness 配置、`mcp-access.json`、`mcp-listener.json`。私密条目既不进入清单也不能读正文。不使用应用 data_root 的绝对私密判断，避免所有合法收藏都被拒绝。

请求 128 KiB、响应 256 KiB、同时在途 2 的已有限制继续保留。脱敏仍依现有凭据形态规则，不把自然语言或未知编码的任意文本声称为绝对无敏感信息。

## 验证与剩余项

2026-10-01 定向 `python -B` 合成验收：`test_mcp_skill_files.py` **26 项通过**。包含旧入口/七工具兼容、正文脱敏分页、清单分页/过滤、元数据不执行、旧轮冻结、跨项目/跨收藏拒绝、旧绑定 needs_collection、原始路径拼写、清单 schema/身份/路径/类型/casefold/DB登记篡改、磁盘清单/正文篡改、hardlink、真实 Windows 祖先 junction、NUL、超大文本、句柄读期间竞态、无全包 hash/无 scan/无 DB 变更，以及 skills 服务缺失的原有错误行为。

运行时 TEMP、HOME、USERPROFILE、CODEX_HOME、APPDATA、LOCALAPPDATA 均指向候选下合成根。测试使用 `mkdtemp` 保留 fixtures；已有测试运行时禁用 `TemporaryDirectory._rmtree`，不递归删除证据。初次 23 项测试中的 `nul.txt` 因 Windows 保留名被收藏层正确拒绝，改为 `null-text.txt` 后 NUL 正文用例通过。

旧 `test_mcp_readonly.py` 与 `test_ai_collaboration.py` 定向 `python -B` 套件共 **42 项完成，41 项通过、1 项跳过**；唯一跳过是当前 Windows 账户无创建 symlink 权限的旧资源用例，本次新增真实祖先 junction 验收已通过。独立完整后端、真实参考包 HTTP、前端和版本 fixture 兼容检查由主代理另行记录。源参考包内脚本始终未执行。真实 AI 客户端、GUI、安装、打包和正式发布不在本候选已验收范围。
