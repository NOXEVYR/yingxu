# 映序 API v1

`POST /api/clipboard/paste {project_id,category,folder_id?}` 仅在用户发起粘贴时读取本机剪贴板，优先复制本地文件；没有文件列表时读取位图、转换为 PNG 并保留透明度，再通过既有上传流程写入指定分类/子目录。同名自动编号，不覆盖原文件。位图最多 4000 万像素、64 MiB；不解释文本路径或图片网址。返回 `{items,job_ids,error}`，沿用同源与会话令牌要求。

本文保留历次契约，并追加 0.4.10 项目存放位置与自动迁移接口；各平台实际已发布下载与边界见 [README](README.md)。

Base http://127.0.0.1:8791。JSON；错误 {error:"中文信息"} 配相应状态码。GET /api/bootstrap 返回 {app:"yingxu",version,token,project_root,data_root,categories:[{key,label}],statuses:[...],capabilities:{...}}。写请求头 X-YingXu-Token=token，Content-Type:application/json。

Categories: scripts 剧本与文档 / shots 分镜 / characters 角色 / scenes 场景 / props 道具 / previs 白模预演 / generated 生成素材 / delivery 成片交付 / references 参考资料。Statuses: 待开始, 进行中, 待审核, 已完成。

GET /api/projects -> {projects:[{id,name,description,color,root,created,updated,counts:{total,shots,completed,documents}}]}
POST /api/projects {name,description,folder_id?:null|ID} -> project object，创建标准目录。0.4.8 起可同时指定项目库分类；省略或 null 为未分类。项目与分类归属在同一事务保存，分类不存在返回 404，不降级为未分类。0.4.10 新项目按创建时分类层级落盘，采用 layout_version=1 中文目录；后续逻辑归类或改名不移动已有项目。
GET /api/items?project=ID&category=KEY&q=QUERY&status=STATUS&kind=KIND&limit=60&offset=0&sort=updated|name|order -> {items,total,limit,offset,categories:[{key,label,count}],elapsed_ms}。q 支持普通中文文本、tag:夜景、type:video、status:已完成、category:scenes，多个条件 AND。服务端优先分页；前端每页最大60，不无限累积DOM。
Item object: {id,project_id,name,category,kind,ext,path,size,mtime,status,tags:[...],notes,metadata:{...},sort_order,created,updated,thumbnail_url,media_url}. kinds markdown,text,docx,image,video,audio,pdf,model,file。新分镜本体是 category=shots 的 markdown。图片/视频 thumb 用 /api/thumbnail/ID（202 尚未就绪，稍后有限重试）。media_url=/api/media/ID。列表不含全文。
GET /api/items/ID -> item + {relations:[{id,source_id,target_id,relation,item:relatedItem}],versions:[{id,created,size}],content_preview}
POST /api/items {project_id,category,name,content?,status?,tags?,metadata?} -> item；创建 .md，本体是本地文件。metadata 支持 shot_number, duration, shot_size, camera, prompt, negative_prompt, seed, model, version 等。
PATCH /api/items/ID {name?,category?,status?,tags?,notes?,metadata?,sort_order?} -> item。name 只改显示名，不移动文件。
DELETE /api/items/ID -> {ok:true,batch_id,project_id,kind,count} 移入映序回收站（磁盘文件保留），可恢复。
GET /api/content/ID -> {format:"markdown"|"text"|"docx"|"binary",content,etag,editable,paragraphs?:[{id,text,editable}],notice?}。
PUT /api/content/ID {etag,content} 或 DOCX {etag,paragraphs:[{id,text}]} -> 同 GET，保存前备份；冲突409，前端保留编辑内容并提示刷新。
POST /api/import {project_id,category,paths:[absolutePath,...],mode?:"copy"|"reference"} -> {job_id}，异步扫描，忽略链接。0.4.10 界面默认传 copy，省略 mode 仍按旧 API 的 reference 行为；详见项目存放位置节。引用文件同样保存时备份且用户明确点击保存才写。GET /api/jobs/ID -> {id,state:"queued"|"running"|"done"|"error",done,skipped,errors:[...],message}。
POST /api/pick {kind:"files"|"folder"} -> {paths:[...]} 打开 Windows 选择对话框。取消为空。
POST /api/rescan {project_id} -> {job_id} 重扫已注册源和项目文件，保留分类/标签/状态。
POST /api/relations {source_id,target_id,relation} -> {id}，同项目；relation 自由中文，如角色/场景/道具/生成版本/白模参考。
DELETE /api/relations/ID -> {ok:true}
POST /api/open {id,action:"open"|"reveal"} -> {ok:true}，系统关联打开（仅允许已索引安全文档媒体），或定位文件。
POST /api/demo {} -> project object，创建明确标注“示例项目”的合成可编辑示例；默认全新应用先空项目状态，由用户按钮导入示例，安装验收可以创建示例供用户体验。
GET /api/health -> {app:"yingxu",ok:true,version}，供桌面验证服务身份。

前端：fetch 读请求使用 AbortController/序号避免过时响应覆盖；输入搜索250ms debounce；多标签内容在页内保存未提交稿，关闭脏标签确认；Ctrl+S 保存，Ctrl+F 聚焦当前页面搜索，Ctrl+K 打开独立全局搜索窗口。编辑 Markdown 为 textarea+安全预览（禁止原始HTML脚本）；DOCX显示段落编辑与格式说明。3D文件首版系统应用打开，白模视频用播放器。所有占位内容显式示例，不用伪按钮。

## 追加：SKILL 与项目交接

GET /api/skills?q=&project= -> {skills:[{id,name,description,path,source,source_label,editable,bound}],total}。
POST /api/skills/refresh {}。GET /api/skills/ID -> {...,content,etag,editable}；PUT /api/skills/ID {content,etag} 只允许映序自建技能，外部库只读。POST /api/skills {name,description,content} 新建本地 SKILL。POST /api/skills/bind {project_id,skill_id,bound:bool}。
GET /api/context?project=ID 与 POST /api/context/refresh {project_id} 返回 {markdown,path,json_path,index_path,updated,stale,pending,error,...}。项目导出是文本供AI读取，不会自动执行技能。

## 追加：拖放与文件名

POST /api/upload?project=ID&category=KEY&name=URLENCODED_FILENAME 原始File请求体，X-YingXu-Token；响应item。文件流分块写，复制进项目，不移动原文件。
POST /api/rename {id,name} 真实文件改名，保留后缀，遇同名409；更新工作台中全部指向原路径的引用。
GET /api/native-file/ID 返回 {path}，仍检查本地来源、索引及当前路径，仅供受限桌面拖出桥。
桌面拖出手柄 pointerdown 发送 window.chrome.webview.postMessage({action:'drag-file',id})，需真实左键仍按下。桌面提供 FileDrop+Copy；普通浏览器用定位文件。
PNG提取元数据为 source_prompt/source_parameters/source_workflow，width,height；用户手动 prompt 独立。列表不返回全文和完整元数据，完整属性在 GET /api/items/ID。

## 0.2 追加：文件夹、移动、回收站

- GET /api/folders?project=ID&category=KEY -> {folders:[{id,project_id,category,parent_id,name,path,relative_path,folder_path,count}],total}。
- POST /api/folders {project_id,category,name,parent_id?} -> folder，创建实际项目子目录。
- PATCH /api/folders/ID {name} -> folder；DELETE /api/folders/ID -> 删除批次。
- GET /api/items 增加 folder 参数：不传/空串为当前分类递归全部；root 为分类直属；ID 为子文件夹直属。列表与详情带 folder_id/folder_path。
- POST /api/items、POST /api/import 与 POST /api/upload query 均支持 folder_id；空/root 表示分类目录。
- POST /api/move {ids:[...],category,folder_id:null|ID} -> {ok,project_id,items,stats:{moved,referenced,unchanged,copied}}，最多200条同项目。项目内文件实际移动，外部引用只改组织归属；不覆盖同名文件。
  - 可选 `target_project_id`：省略或等于原项目时保持旧行为；指定其他活动项目时跨项目移动，返回额外 `source_project_id`、`warnings`、`content_changed`（已改写链接的文稿 ID），`project_id` 是目标项目。所有 ID、文稿历史、属性保留；整组选中时保留素材组及顺序，整批内关联保留。
  - 跨项目每批 1–200 个同项目活动文件、项目内文件合计最多 2 GiB。目标分类/文件夹必须属于目标项目；同名文件/已有路径记录整批拒绝，不覆盖。外部引用只迁移登记归属、保留原文件路径；项目内文件经临时复制、SHA-256 校验、排他发布后提交数据库，提交成功后才清理原文件。旧文件被占用或变化时返回成功及 `warnings`，保留副本；已提交后通知失败也只返回警告。
  - 部分素材组、与未选项关联、被其他项目引用的项目内原文件返回 409，提示一起选择/先解除关联/复制导入。Markdown 最多核对 1000 篇、合计 16 MiB、单篇 2 MiB；只改写随批次移动的文稿并备份原字节至历史。未选文稿仍引用被移动文件，或选中文稿含未随批次移动/无法核对的本地链接时拒绝，禁止静默破坏链接。
  - 与项目迁移共用写入预约，存在导入/扫描或其他在途写操作时拒绝；复制期间允许读取，其他写请求 409。调用方须先保护文稿/画板草稿，成功后刷新两项目与受影响标签。响应丢失后重发同目标同 ID 会按目标项目内整理处理，不重复生成条目。
  - 应用数据 `cross-project-moves` 保存源/目标路径及摘要操作记录；断电时原件或已校验目标仍可用，不自动猜测删除遗留副本。数据库提交响应异常会先重新核对持久状态，无法确认时保留两边文件并报告。仅处理所选文件，非 Markdown 格式内部的外链不自动重写。

- PATCH /api/projects/ID {name?,description?} -> project，仅改显示资料，项目根路径不动。
- DELETE /api/projects/ID、DELETE /api/folders/ID、POST /api/trash/items {ids} -> {ok,batch_id,project_id,kind,count}。
- DELETE /api/skills/ID -> {ok,kind:'skill',batch_id:ID,count:1}。自建技能可恢复删除；外部来源仅在映序隐藏，源文件不卸载。绑定关系保留但回收期间不参与项目交接。
- GET /api/trash?project=ID&q=关键词&limit=48&offset=0 -> {entries:[{id,batch_id,kind,target_id,project_id,name,created,count}],total,limit,offset,truncated}，名称搜索在数据库分页前执行；省略project时列所有项目，始终合并全局技能回收条目。
- POST /api/trash/ID/restore {kind?:'skill'} -> {ok,project_id?,kind,count}。技能必须传kind=skill，其他类型按组织批次恢复。恢复目录/项目仅恢复该删除批次带走的条目，不复活更早删除的文件。
- 进度快照增加folders树和folder_id；项目在回收站时 PROJECT_CONTEXT.md/progress.json 明确标记已回收，恢复后重新生成。

## 0.2.1 追加：本地目录与公开版启动隔离

- POST /api/open-folder {project_id,category?,folder_id?} 在 Windows 资源管理器打开已验证的项目根目录、分类目录或子目录；客户端不能传入任意路径。文件通过原有 POST /api/open {id,action:'reveal'} 定位。
- POST /api/open-folder {skill_id} 打开已登记且未移除的 SKILL.md 所在目录；本地与外部只读技能均可定位，不读取或改写正文。此目标与 project_id/category/folder_id 互斥，拒绝任意 path、未知 ID、已移除/缺失文件及联接或符号链接路径；同样需要当前会话令牌与同源校验。
- GET /api/health -> {app:'yingxu',ok:true,version,instance_id}。公开版启动器通过数据目录规范路径的 SHA-256 指纹确认后台；拒绝旧版缺少指纹或指纹不符的服务，避免错误复用其他数据目录。
- 默认数据路径与环境变量见 RUNNING.md；server.py 的 --data 与 --projects-root 参数优先于有效的环境变量默认值。

## 回收站清理（0.3.1）

- `POST /api/trash/delete-preview {entries:[{id,kind}]}` 预览所选批次；`{all:true}` 预览全部回收条目，不受列表搜索和分页影响。
- 返回 `{token,total,entries:[{id,kind,name,paths,warnings,error?}],paths,warnings,expires_in}`。存在 `error` 的条目不执行；默认展示实际文件位置与外部引用说明并确认；用户可在设置显式关闭确认。关闭确认不跳过后台预览校验，存在阻挡项仍展示原因。
- `POST /api/trash/delete {token}` 执行已确认快照，返回 `{deleted,failed:[{id,kind,name,error}],remaining}`。客户端不能自行提交磁盘路径。令牌有期限、仅使用一次；实际实体状态和磁盘内容变化时拒绝旧预览。
- 只有确认进入 Windows 回收站的文件才算成功，不提供永久删除后备。外部引用和外部 SKILL 保留原文件。失败批次保留回收记录；已清理批次不能通过旧恢复请求、刷新或同步自动复活。


## 0.3.1：设置、临时预览与项目库

- `GET /api/settings` 与 `PATCH /api/settings` 读取/更新六项设置：`confirm_delete`、`confirm_trash_delete`、`close_to_tray`、`autoplay_media`（布尔）；`default_view`（grid/list/board）、`default_sort`（updated/name/order）。默认确认删除、关闭到托盘、画廊/最近更新、不自动播放；未知键及类型错误拒绝，原子保存。bootstrap 附带 settings。
- `POST /api/external-open {paths:[绝对路径]}` 受会话令牌保护，返回 `{entries}`，仅注册会话临时 ID。`GET /api/external/ID` 返回元数据、content 和实际 editable 能力；`GET /api/external-media/ID` 通过已验证文件句柄流式读取并支持 Range。仅受支持文件可读，不增加项目或复制文件，0.3.8 起提供下文的受保护文稿保存接口。预览 ID 会在后台重启或过期后失效。
- `GET /api/project-library` 返回 `{folders,projects,recent_ids,total}`；项目带 folder_id 和 last_opened。`POST /api/project-folders {name,parent_id?}` 创建逻辑分类；`PATCH /api/project-folders/ID {name?,parent_id?}` 改名或移动，拒绝循环及同级重名；`DELETE /api/project-folders/ID` 仅删除可见项目与子分类均为空的分类，历史归属置为未分类。
- `PATCH /api/project-library/PROJECT_ID {folder_id}` 归类（null 为未分类）；`POST /api/project-library/PROJECT_ID/visit {}` 记录最近打开，不改写项目磁盘目录。以上写接口沿用同源及会话令牌验证。

## 0.3.4 全局搜索交互边界

- `GET /api/search?q=关键词&limit=30&offset=0` 返回下表字段。仅接受 `q`、`limit`、`offset`，不接收磁盘路径、项目或分类参数；沿用 Host、Origin 和跨站请求校验。bootstrap 的 `capabilities.global_search` 为 `true`。
- `q` 最多 200 字符，禁止 NUL；按空白分隔最多 12 个关键词。空查询返回空结果且不扫描。`limit` 默认为 30，必须为正整数，最大按 50 处理；`offset` 默认为 0，范围为 0–100000。无效参数返回 400，不可信来源返回 403。
- 各类结果统一使用 Unicode casefold 后的字面子串匹配；全部关键词都必须命中，可分布在不同可搜索字段中。`night` 可以命中 `midnight`，`café` 可以命中 `CAFÉ`；不把关键词解释为 FTS 表达式、SQL 或通配符。

| 返回字段 | 含义 |
| --- | --- |
| `q` | 去除首尾空白的查询 |
| `results` | 当前页结果数组；每项结构见下文 |
| `total`、`total_exact` | 本次已找到的结果数量、是否可视为完整数量；不完整时 `total` 只是已找到的数量 |
| `limit`、`offset`、`has_more` | 实际页大小、偏移、已找到的结果中是否还有下一页 |
| `truncated`、`warnings` | 是否未完成全部扫描及说明；正常非空查询也会提示索引时效，因此不能仅凭 `warnings` 非空判断失败 |
| `scope` | `projects`、`items`、`skills`、`content_source` 四项范围与索引时效说明 |
| `scanned` | `projects`、`items`、`skills` 候选检查计数，以及实际读取的 `skill_bytes`；不是底层数据库扫描行数 |
| `elapsed_ms` | 本次搜索耗时，单位毫秒 |

结果共有 `type`（`project` / `item` / `skill`）、`id`、`project_id`、`project_name`、`name`、`category`、`folder_id`、`snippet`。文件结果额外有 `kind`；技能结果额外有 `source`，且项目字段为 null；项目结果的 `project_id` 等于自身 `id`，分类与文件夹字段为 null。结果不返回磁盘路径或完整正文。排序优先名称精确匹配，其次名称包含全部关键词，随后其他命中；同级按名称、类型、ID 排序。分页针对本次结果排序后切片，跨请求不提供冻结快照。

- 项目与文件候选每类最多 5000 个，SKILL 候选最多 2000 个；SKILL 单文件正文最多 1 MiB、单次累计最多 32 MiB。另有时间、数据库工作量和锁等待预算；触限、路径失效或记录变更时返回部分结果与原因，`truncated=true`、`total_exact=false`。
- 全局搜索跨所有项目名称/简介、文件名称/标签/备注及已索引正文（含提取的 DOCX 正文），以及已注册 SKILL 名称/描述和限额正文；不继承当前项目或分类过滤。
- 返回结果需显示来源、命中摘要和分页信息；超过扫描限额必须明确标记部分结果。未保存草稿不纳入索引，外部项目文件变化需要同步索引。
- 独立搜索窗口支持上/下选择、Enter 打开及 Esc 关闭；当前页面 Ctrl+F 行为保留。顶栏另提供全局搜索按钮，帮助位于设置左侧、设置最右。

## 0.3.7：截图模式与项目、组成员拖动

`POST /api/resource-groups/{source_id}/transfer {ids:[id],revision,target_group_id,target_revision}`：按资源 ID 转移当前组成员；`revision` 是来源组修订号。转入另一组时必须给出 `target_revision`，同一事务校验来源和目标修订号。`target_group_id:null` 表示移出组，不传 `target_revision`。目标仅允许同项目素材组，失败保留原归属。

项目行拖动复用项目库逻辑归类，弹窗外的明确空白放置区域表示未分类。组成员拖动支持同项目换组和移出组；跨组转移需在后台一次事务中校验来源、目标及修订状态，失败保留原归属。客户端仅接受自己的内部拖动载荷，Esc、无效区域或过期请求不提交转移。不根据磁盘路径猜测项目或成员。

素材选择框的 Delete 例外仅限当前资源卡片或列表行内、ID 与所属行一致的选择框。其他输入控件与编辑器继续保护，删除仍走普通映序回收站及现有确认和草稿守卫。

`GET /api/settings`、`PATCH /api/settings` 及 `bootstrap.settings` 新增 `capture_mode`，只允许 `"annotate"`（默认）和 `"quick"`。旧设置缺少此项时使用默认值；非法值返回 400，不覆盖有效设置。`capture_enabled` 仍只控制后台快捷键，主动截图按钮可用。

桌面在一次截图请求开始时锁定模式。`annotate` 在框选后等待标注或确认，只有确认才把结果位图交给已有复制、上传与插入流程；取消返回 `cancelled:true`，不调用复制与上传。`quick` 保留松开鼠标即完成。桥接字段、二进制上传与原笔记匹配规则不变，不传输 base64 图片。

原生状态栏的界面百分比直接来自 WebView `ZoomFactor`，由 `ZoomFactorChanged` 更新；点击将其设为 `1.0`。此状态不新增 HTTP 接口或后台轮询，也不由屏幕 DPI 推算。

## 0.3.6：标题改名入口

- 项目文件的独立标题打开既有重命名弹窗，仍使用 `POST /api/rename {id,name}`；不新增逐字重命名接口。
- 输入名称不包含当前真实文件扩展名，确认改名时保留原扩展名。标题不属于正文输入区，取消与失败不修改正文；成功后保持原标签页、未保存草稿与编辑状态。
- 原有路径授权、同名冲突和来源限制保持生效；其他预览来源不因新增标题入口获得文件改名权限。

## 0.3.5：截图、Markdown 图片与逻辑素材组

- 设置新增 `capture_enabled`（默认 true）与 `capture_hotkey`（默认 `Ctrl+Alt+Shift+S`）。快捷键需要至少两个不同的 Ctrl/Alt/Shift 修饰键和大写字母、数字或 F1–F24，排除 F12；关闭后台快捷键不关闭主动截图按钮。
- 原生桥接 `capture-context-request {requestId}` → `capture-context {requestId,projectId,itemId}` 锁定目标；仅接受同源页面、匹配 ID 与严格字段。5 秒未取得上下文时只尝试剪贴板。截图只在主动触发时读取鼠标所在单屏，最大 40000000 像素，无持续截图或剪贴板轮询。
- 原生 PNG 通过原有 `POST /api/upload?project=ID&category=references&name=文件名` 上传二进制，带新取得的会话令牌与同源头。`capture-result` 返回 `requestId,item,clipboardCopied,cancelled,error,clipboardError,saveError`；复制与保存独立报告。前端只在原项目/笔记/草稿及可编辑模式仍匹配、且没有保存中或输入法组合时插入 Markdown 草稿，不自动保存笔记。
- `GET /api/markdown-assets/link?note=ID&image=ID` 返回 `{relative_path,markdown,preview_url}`；仅为同项目已登记的独立 Markdown 与光栅图片生成相对引用，不改文稿。`GET /api/markdown-assets/image?note=ID&path=编码相对路径` 按安全文件句柄流式读取、支持 Range；拒绝越界、网络协议、UNC、回收对象、硬/软链接及超过 32 MiB 的图片。
- 编辑器 `create({imageResolver})` 只对普通相对路径图片调用回调，默认不加载图片；返回值必须是同源相对路由。`getSelection()` 返回原文 UTF-16 的 `{from,to}`（CRLF 计两个字符）；`insertText(text,from?,to?)` 是一次可撤销文本事务，保留原文换行。输入法组合拒绝插入，非法范围或超过实时编辑限额抛出错误。
- `GET /api/resource-groups?project=ID` 返回 `{groups,total}`，组摘要含 `id,project_id,name,revision,count,member_ids,categories,preview` 等字段。`GET /api/resource-groups/ID` 追加完整 `members`。`POST /api/resource-groups {project_id,name?,item_ids}` 创建组，至少两个同项目成员。
- `PATCH /api/resource-groups/ID {name,revision}` 改名；`POST` / `DELETE /api/resource-groups/ID/members {item_ids,revision}` 加入/移出；`DELETE /api/resource-groups/ID {revision}` 解散。写接口要求同源与令牌，修订号冲突返回 409。一素材只属于一组，每组最多 200 成员、每项目最多 500 组；组变更不搬动/删除文件或修改原分类与制作信息。
# 批量制作信息（0.3.7）

`POST /api/items/batch-properties`，写接口沿用同源与会话令牌校验。

请求仅接受 `project_id`、`ids`、可选 `tags_add`、可选 `status`。项目与素材 ID 是 32 位小写十六进制；`ids` 为 1–200 个不重复的当前活跃项目素材，至少提供一项修改。`tags_add` 为 1–50 个字符串，去掉首尾空白后每项 1–80 字且不含控制字符；追加时有序去重，保留各素材原标签，合并后超出 50 个则整批拒绝。状态仅接受「待开始、进行中、待审核、已完成」；未提供的字段不修改。

所有素材验证通过后在同一数据库事务内修改标签、状态、更新时间和搜索索引。已删除条目、跨项目条目、标签合并溢出或执行错误不会留下部分更新，不读取或改动素材原文件。返回 `{items:[{id,project_id,tags,status,updated}]}`，按输入 ID 顺序排列；不返回正文或大体积制作参数。

## 0.3.8：外部文稿编辑与 Word 结构预览

POST /api/external/ID/content 接受 {etag,content}（Markdown/文本）或 {etag,paragraphs:[{id,text}]}（DOCX），要求既有同源与会话令牌，ID 必须来自显式打开的有效会话记录，不能传任意路径。返回完整 detail（content 为更新后的内容对象，成功备份有 backup_id）。原路径保存、原编码保留、etag/身份/链接重查、私有 external-versions 备份、原子替换；冲突 409，过期 404，不支持类型 415。GET /api/external/ID 的顶层 editable 与 content.editable 表明实际能力，不再一律只读。

DOCX content 保留 paragraphs/content/notice，并增加 blocks（paragraph/table/unsupported），段落带 heading_level/alignment/runs/images/readonly_reason。图片仅受限内部光栅 data URI，拒绝外链。导入索引使用 read_docx(...,structured=False)，不生成图片预览。

## 0.4.0 静态格式

资源 kind 增加 svg/html。项目 GET /api/content/ID 与外部 GET /api/external/ID.content 返回只读格式内容。SVG preview_url 为经过净化的 data:image/svg+xml;base64；HTML content 为原始解码源码，preview_html 为重建后的静态片段，必须使用无 allow-* 的 sandbox iframe + CSP。保存接口拒绝这两种格式。SVG 媒体直链也经过净化并返回隔离 CSP；HTML 媒体直链按 text/plain 附件提供，禁止同源网页执行。两者不创建缩略图任务。后台不解析超出各自有界限制的文件。

## 0.4.1 SVG兼容

SVG内容notice包含本次静态预览省略的装饰效果提示；内容与媒体均用相同净化结果。filter装饰省略，stroke-dasharray降级实线；不放宽脚本、外链、事件或动画边界。

## 0.4.4 文档搜索、文件链接与维护

- Ctrl+F 按焦点选择范围：编辑区搜索当前文档草稿，资源区搜索当前分类/文件夹；Ctrl+K 保持跨项目搜索。文内查找采用字面文本，不执行正则；最多扫描 200 万字符、显示 1 万个匹配，并提示限额。Word 预览和编辑每页最多 40 段，查找可跳到其他页且保留草稿。
- `GET /api/markdown-assets/file-link?note=ID&item=ID` 返回 `{markdown,relative_path,item_id}`。使用标准相对 Markdown 链接，附 `#yx-item=ID`；链接目标改名或移动后，映序优先按登记 ID 解析。
- `GET /api/markdown-assets/resolve-file?note=ID&path=RELATIVE` 返回 `{id,project_id,name,kind}`。校验笔记和目标均为同项目内登记存活的独立文件，并检查路径边界；非法/已删除/他项目 ID 不回退。首次版本不为外部独立文稿建立本地文件链接。
- `POST /api/maintenance/preview` 接收 `include_cache`、`include_versions`、`keep_versions`、`older_than_days`，返回容量分组、可清理数量、5 分钟一次性 token、truncated 与 warnings。默认只选缓存，历史不自动清理。
- `POST /api/maintenance/cleanup {token}` 只处理本次预览绑定的候选，执行前复验身份；原稿、数据库和数据库备份不进入候选，每篇至少保留最新历史。扫描超出 2 万入口或 3 秒时拒绝清理；返回 removed_files/removed_bytes/skipped_files/warnings。前端改变选项或离开弹窗时不得复用旧确认。
- 画板 iframe 挂载到固定宿主，标签切换隐藏而不重载；关闭标签销毁。只在场景内容版本变化后合并序列化，保存/关闭同步读取最终内容。字体仅使用本地来源。Markdown 编辑器首次打开需要时才载入本地 bundle。

## 0.4.5 SKILL 来源与扫描位置

读接口沿用本地来源校验；写接口沿用同源、会话令牌与 JSON 请求体要求。目录登记不赋予外部技能编辑权限。

| 接口 | 请求与返回 |
| --- | --- |
| `GET /api/skill-sources` | 不接受查询参数；返回 `{sources,groups,all_total,errors,truncated}`，不重新扫描磁盘 |
| `GET /api/skills` | 仅接受 `q`、`project`、`source`、`source_id`；返回以上来源字段及 `{skills,total}`，筛选只读取索引 |
| `POST /api/skills/refresh` | 手动有界刷新，返回技能列表和来源字段，追加 `scanned`、`skipped`、本次 `errors` 与 `truncated` |
| `POST /api/skill-sources` | 仅接受 `{path,label?}`，成功返回 201 与刷新结果 |
| `PATCH /api/skill-sources/ID` | 接受非空 `{enabled?,label?}`；`enabled` 必须是布尔值，成功返回刷新结果 |
| `DELETE /api/skill-sources/ID` | 空请求体，只移除自定义登记，返回刷新结果 |

`source` 为来源组：`yingxu`、`codex`、`claude`、`dsh`、`workbuddy`、`zcode`、`agents`、`custom`。`source_id` 是具体位置 ID，两者与 `q` 组合取交集。`project` 用于校验项目并标记 `bound`，**不将列表过滤为仅绑定技能**。`q` 去除首尾空白、Unicode casefold 后最多取 300 字符，按空白拆词，所有词都须在名称、描述或路径中命中；不是全文或正则搜索。未知来源返回 400，未知位置返回 404。接口不接受 `limit/offset`，前端对有界结果按页显示，默认每页 48 条。

每个 `sources` 条目含 `id`、`source`（组 ID）、`label`、`path`、`enabled`、`custom`、`removable`、`readonly`、`status`、`count`。状态包括 `ready`、`missing`、`disabled`、`error`、`truncated`；客户端必须检查各位置状态，不能只凭 `errors` 数组判断全部正常，因为普通列表不重放上次扫描错误。`groups` 为 `{id,label,count}`，各组按当前可用、未移除技能 ID 去重；`all_total` 为全库去重数，均不随当前关键词筛选变化。`total` 才是当前筛选命中数。技能摘要增加 `source_id`、`source_group`、`source_label` 与 `source_ids`；同一文件可有多个有效来源，不能把各组计数相加作为总量。

自定义位置最多 16 个，路径字符串最多 4096 字符，必须为存在的本地绝对目录，拒绝 NUL、UNC/网络共享、磁盘根、用户目录及其祖先、符号链接或目录联接。规范路径已登记返回 409。新增时 `label` 可省略或留空，默认目录名；非空名称去首尾空白后最多 80 字符且无控制字符，PATCH 名称不可为空。映序本地位置不可修改启用状态或移除；其他内置位置可关闭但不可移除，此类操作返回 403。不存在的 ID 返回 404，其余非法参数返回 400。

关闭或移除位置只修改映序登记，不删除源文件、技能记录或项目绑定。技能仍有其他启用来源时继续可用；否则暂时不可用，不参与交接。恢复登记/启用并成功扫描后可恢复使用。物理同文件去重保留多位置成员关系，独立副本不按内容合并；来源切换复用身份时重新核实原规范路径的当前文件身份，不能只信任历史 inode。

默认目录与识别边界见[功能指南](docs/功能指南.md)：WorkBuddy 插件只采用经过路径校验的安装清单位置；ZCode 只识别已知缓存层级，不代表插件当前启用。扫描不执行技能，不加载账号配置，也不自动安装依赖。单文件最多 1 MiB、索引最多 2000 技能、单轮最多 20000 条目、技能根目录向下最多 3 层，约 3 秒协作式时间预算；插件目录发现另有限额。限时不是慢 I/O 的硬超时。

初始化、手动刷新及来源登记变更触发扫描，查询和翻页不触发扫描，无后台轮询。未变化文件按 mtime/size 复用元数据。只有完整扫描的位置才替换其旧成员关系；读取失败或预算中断保留相应旧索引并标记部分结果，旧索引不保证文件当前仍可打开。来源列表与技能列表组装保持同一锁保护，避免并发移除导致不一致。


## 外观设置（开发版）

`GET /api/settings`、`PATCH /api/settings` 和 `bootstrap.settings` 支持 `appearance_theme`，仅允许 `swiss`（默认黑白）、`pine`、`paper`。旧设置缺省时回退 `swiss`，读取不改写旧文件；未知主题或错误类型拒绝，仍使用原子保存。分类显示名调整只在界面层映射，scripts/shots/previs/references 及目录路径不变。


## 0.4.7 来源标签

标签仅管理已登记来源的筛选，不变更扫描位置、原技能文件或项目绑定；读取和标签增删不触发扫描。`GET /api/skill-sources` 的 `groups` 增加 `builtin`、`hidden`、`source_ids`。`GET /api/skills?source=ID` 支持自定义标签，与目录、项目和搜索筛选组合。

| 接口 | 请求与行为 |
| --- | --- |
| `POST /api/skill-source-labels` | `{label,source_ids}`；名称 1–40 字符、同名拒绝；关联 1–24 个已登记位置，自定义标签最多 32 个；返回 201 与来源列表 |
| `DELETE /api/skill-source-labels/ID` | 空对象；预设标签隐藏，自定义标签移除；返回来源列表 |
| `PATCH /api/skill-source-labels/ID` | 仅 `{hidden:false}`，恢复预设标签；返回来源列表 |

所有写接口拒绝查询参数、未知字段及未知标签；要求现有同源和会话令牌校验。布尔值不接受数字替代。

## 0.4.10：项目存放位置与自动迁移

所有写接口继续要求同源、会话令牌和 JSON 请求体。`bootstrap.capabilities.project_storage` 表示支持项目存放位置设置。

`GET /api/project-storage` 返回 `{root,configured_root,project_count,existing_roots,available,error,affects:"new_projects"}`。`existing_roots` 为全部活动项目的 `{id,name,root}`；已回收的整个项目不在其中。`root` 是当前有效位置，`configured_root` 是持久化偏好。配置磁盘不可用时返回 `available:false` 和具体 `error`，工作台与设置仍可打开；新建项目明确失败，不回退另一目录。

`POST /api/project-storage` 仅接受 `{root:绝对目录}`，验证已有、可写的本机目录后原子保存偏好。保留旧 API 的“仅作用于后续新项目”语义，不迁移既有项目；`affects` 描述此配置接口的边界。**界面仅在没有活动项目时直接调用它；有项目时走下面的全量活动项目迁移，不能先切换根目录再复制。** 普通 `PATCH /api/settings` 拒绝 `project_storage_root`，不能绕过路径验证。已保存的位置优先于环境变量。

| 接口 | 请求与返回 |
| --- | --- |
| `POST /api/project-storage/migration/preview` | 仅 `{root,project_ids:[ID,...]}`；返回 `{token,projects,total_bytes,warnings}` |
| `POST /api/project-storage/migration` | 仅 `{token}`；接受后返回 HTTP 202 和 `{job_id}`，异步执行 |
| `GET /api/project-storage/migration/jobs/ID` | 返回 `{id,state,message,completed,total,result,error}`，`state` 为 `running`、`done` 或 `error` |
| `GET /api/project-storage/migration/status` | 返回 `{active_job_id:ID或null}`；后台重启导致旧任务 404 时用于确认当前是否仍有迁移 |

预览必须包含当前**全部活动项目**，1–1000 个不同 ID；与最新项目列表不一致返回 409，要求刷新后重新预览。每个 `projects` 条目含 `id,name,source_root,target_root,files,bytes,external_references`。目标目录按分类层级和名称分配，重名不覆盖；客户端必须展示源/目标及容量，由用户“确认保存并迁移”后才提交 token。预览令牌有效期 10 分钟，执行前仍复查目录、文件身份、内容及索引，变化时拒绝执行。

已接受的 token 在任务记录保留期间重复提交返回同一 `job_id`，用于首个响应丢失时重试，不重复复制。任务记录是进程内有界记录，最多保留 16 个；后台重启或记录过期后查询返回 404，不能据此判断迁移成功或失败。此时读取 `migration/status`：存在活动任务则继续跟踪；没有活动任务则读取实际项目和存放位置，解除前端忙碌并提示核对，必要时重新预览，不能显示假成功。普通网络中断保留令牌/任务 ID 并重试，不启动另一份操作。

`completed/total` 表示已校验文件数/总文件数；阶段说明使用 `message`，提交完成前即使达到总数也不能自行判断成功。`done.result` 含 `migrated,projects,recovery_manifest,database_backup,warnings,originals_retained:true`；完成后另取存放位置快照。`error` 状态显示后台原因，原件保留。没有迁移取消接口。

迁移运行期间 GET/HEAD 仍可读取，其他写请求统一返回 HTTP 409；唯一允许进入的写接口是迁移 POST，用于同 token 幂等重试，不允许同时开启另一任务。开始时也会拒绝未完成的导入/扫描或竞争写操作。界面开始前须处理未保存文稿、画板和属性草稿，不自动保存/丢弃；运行中禁止关闭迁移弹窗及新的写操作。

执行将文件复制到独立暂存目录并逐项校验大小与 SHA-256；受控改写的 Markdown 链接以新内容校验。全部复核通过后才提交索引路径与新存放位置，项目及资源 ID 保持。源目录作为备份保留，外部引用原件与路径不变；不搬迁程序或应用数据目录。失败保留原件、回退未完成的提交；应用数据 `project-migrations/` 保存数据库备份与阶段恢复记录。若在数据库/设置提交间中断，下次启动按数据库全部旧路径或全部新路径恢复一致的设置；遇混杂状态保留两边并报错，不猜测删文件。此恢复不自动续传复制，不能将任务内存记录当作持久恢复日志。

新项目采用 `layout_version:1` 中文物理目录，例如 `个人作品/练习/文本/剧本.md`。旧项目默认补充 `layout_version:0` 并保持路径；仅显式迁移可将目标副本升级到布局 1，受控重写可处理的内部 Markdown 链接，警告通过预览/结果返回。项目库后续改名、重新归类仍不搬动既有文件；同父级已登记的物理分类目录可能继续复用，不承诺每次逻辑名称都映射为同名磁盘路径。

`POST /api/import` 支持 `mode:"copy"|"reference"`：界面默认 copy，省略 mode 则保留旧 reference 契约。copy 将支持的文件/目录复制到所选项目、分类和子目录，保留原件且不覆盖同名；reference 仅登记原位置。ZIP 始终受控解压到项目，不采用引用行为。


## 项目分类及全部内容删除（源码新增）

- `POST /api/project-folders/ID/delete-contents/preview {}`：只读递归范围预览，返回分类数、活动项目列表/路径、资源数及 10 分钟有效的确认 token。一次最多 500 个项目。
- `POST /api/project-folders/ID/delete-contents {token}`：校验分类层级、项目和资源身份后，在一个事务中将全部活动项目及资源移入映序回收站，并删除分类树。范围变化、过期或重复 token 返回 409，不部分提交。返回 `entries:[{id,kind:"project"}]` 与 `project_ids`。
- 此接口不直接删除磁盘文件。前端可将返回的 `entries` 交给已有 `/api/trash/delete-preview` 和 `/api/trash/delete`，仅回收此次删除的项目根目录，不清空其他回收条目。磁盘预览始终要求显式确认；共享引用、未知成员、目录变化及失败仍沿用现有保护。
- 分类层级不恢复；未清理磁盘的项目可从映序回收站逐项目恢复到“未分类”。用户取消后续磁盘预览时保留项目回收记录。


## 项目目录同步与已有文件拖入（源码修复）

- `POST /api/project-files/sync {project_id}`：异步、仅扫描该项目自有根来源，返回 job_id；需要原有同源与令牌保护。单次自动扫描最多 50000 个条目、24 层，不轮询外部来源。桌面进入项目或重新获得焦点时触发，3 秒内合并重复请求；旧宿主仍可用 `/api/rescan` 手动同步。
- 项目内已知分类目录按 `category_paths(project)` 确认，登记真实子文件夹，不创建或搬动磁盘目录。未变化内容也会修正对应 category/folder_id，保留用户标签、历史及元数据；外部引用继续保留逻辑归类，回收站内容不会被扫描复活。
- `/api/import` 新增可选布尔 `move_owned`（仅 `mode:copy` 可使用，默认 false）。桌面原生文件拖入传 true：属于当前项目的普通文件通过原有移动流程整理，同一路径为无操作；项目外文件仍复制。手动复制导入不改变语义，真实同名目标不覆盖。
- `disk_file_identities` 是增量识别缓存，不是文件内容模型。仅在平台提供稳定创建时间、独立文件标识，旧路径消失、无其他目标记录且无回收冲突时跟随项目内移动。没有历史身份或无法可靠确认时不推断关联，不以文件名或相同字节合并文稿。
