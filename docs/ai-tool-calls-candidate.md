# 工具调用内核候选

0.4.27 / calls.3，2026-10-02。这是独立源码候选；没有构建完整安装包、替换安装版或发布。沿用原任务、冻结轮次、固定技能、人工交接、收件与审核。calls.2 的曜核只读选型适配见 [aihub-interop-candidate.md](aihub-interop-candidate.md)，不进入本协议执行接口。calls.3 的显式本轮成果关联见 [ai-call-receipts-candidate.md](ai-call-receipts-candidate.md)。

## 当前可验证的范围

AI 协作的已有轮次详情增加默认收起的“工具调用”。展开后才读取连接和本轮请求，页面没有空闲轮询。用户可以登记一个明确的本机兼容接口，检查其声明，填写简单参数，显式提交、核对及申请取消。没有安装、启动、扫描或托管生成服务。

本期实现的是调用与恢复内核。兼容适配器使用本文的试用 HTTP 协议，不直接兼容任意 MCP、ComfyUI、音乐/视频厂商 API 或 AI Hub。实际项目文件、冻结技能与完整执行资料的传输、结果文件下载和自动收录尚未接入。已放入本轮生成目录的文件可在 calls.3 中明确关联，仍不自动审核。传出的 context 仅绑定 task/run/input_digest，不能把这个摘要当成完整执行输入。

服务“完成”只保存结果描述；大小和摘要仍为服务声明，标记 declared_unverified/downloaded=false。不会新增素材条目、自动收件、通过审核或完成项目。原人工交接及显式收件继续使用原有流程。

## 连接及协议

连接地址只允许 http://127.0.0.1:明确端口/普通可选路径；localhost 规范化到 127.0.0.1。拒绝远程地址、认证 URL、查询/片段、网络发现、代理转发及重定向。凭据只保存环境变量名称，值仅在请求时留于内存；不进入任务、声明快照、查询输出或错误原文。

服务提供 GET /capabilities，返回 application/json 的 200：

```json
{
  "protocol": "yingxu-http-v1",
  "service_id": "stable-service-instance",
  "revision": "1",
  "execution_binding": "declaration_sha256",
  "operations": [{
    "id": "example",
    "name": "示例操作",
    "input_schema": {
      "type": "object",
      "properties": {"prompt": {"type": "string", "maxLength": 200}},
      "required": ["prompt"],
      "additionalProperties": false
    },
    "supports": {"query": true, "lookup": true, "cancel": false, "idempotency": true}
  }]
}
```

service_id 必须标识仍能核对原 job 的服务实例/账本；仅同端口、同名字不够。重建后丢失原账本的服务须换身份。声明原 UTF-8 字节和 SHA-256 冻结于每次尝试，不用重新序列化的 JSON 冒充原文摘要。

execution_binding 是必需的能力声明。适配服务必须在同一个原子临界区核对提交的 expected_declaration_sha256、选择对应执行操作并登记请求；若当前声明不同则拒绝，不能开始生成。映序在提交前再次检查声明摘要，并核对后续 job 响应回报的声明摘要。服务只做提交前预检、忽略这个条件或不回报执行版本时不兼容此试用协议。这个合同及响应证据不替代对服务实现的真实验收。

输入声明支持有限的 object/array/string/number/integer/boolean/null、enum 和上下限；未知 schema 关键字拒绝。顶层必须 object，所有对象限定已知字段。页面只渲染 string/number/integer/boolean；复杂输入仍使用原人工交接方式。没有动态代码执行或第三方 schema 依赖。

提交 POST /jobs：

```json
{
  "request_id": "映序持久请求标识",
  "operation_id": "example",
  "expected_declaration_sha256": "冻结声明原字节的64位十六进制SHA-256",
  "parameters": {"prompt": "合成参数"},
  "context": {"task_id": "原任务", "run_id": "冻结轮次", "input_digest": "输入摘要"}
}
```

query 对应 GET /jobs/{job_id}；lookup 对应 GET /requests/{request_id}；cancel 对应 POST /jobs/{job_id}/cancel，body 为原 request_id。GET 必须 200；POST 可以 200/201/202，但都必须返回经过身份校验的 JSON：

```json
{
  "service_id": "stable-service-instance",
  "request_id": "原请求标识",
  "job_id": "原服务任务标识",
  "declaration_sha256": "该job实际绑定的冻结声明摘要",
  "state": "succeeded",
  "results": [{"name": "合成说明.txt", "mime_type": "text/plain", "size_bytes": 12}]
}
```

服务状态限定 queued/running/succeeded/failed/cancel_requested/cancelled。已获得 job_id 后，后续响应必须是同一 job。结果最多 32 项，仅保留 name/mime_type/size_bytes/sha256/resource_id 等非秘密描述；丢弃工具路径、URL、headers 和响应原文。签名 URL 或密钥不能通过其他保留字段绕过边界。

## 持久与生命周期

新表 ai_call_connections/ai_call_attempts 附属于原数据库，不改变旧任务/轮次/收件含义。尝试保存原连接修订、服务身份、实际非秘密参数、声明原文及操作快照、请求/job 标识。不可变选择字段由数据库触发器保护。

工作台先持久接受，再由单个有界工作线程提交。同一任务轮次的同一 idempotency_key + 相同连接/操作/参数返回原尝试；不同参数返回 409。提交响应丢失后不会重发 /jobs；只核对原请求。重启时 submitting 恢复 unknown，尚未提交的 accepted 标记 not_submitted；重启不自动查询或重新生成。

核对操作不能覆盖尚在排队的提交或取消；这种情况下返回 409，原队列保留。只有明确的本地取消可以移除尚未提交的请求。GET 预检之后服务刚好重发声明时，合成服务以原子摘要条件拒绝执行；映序对非成功 POST 响应保留待核对记录，不假定远端已执行或自动重发。

查询/取消前重新检查原连接修订和服务身份。配置或服务变化时暂停核对，保留旧记录，不向新服务查询旧任务。只按该操作实际声明的能力查询、重查和取消；cancel_requested 不表示 cancelled。

每轮最多 100 次尝试；列表每页最多 48 条，使用 limit/offset 显式加载更早记录。默认本地队列 8 个、单 worker、网络总截止时间 2 秒、有限退避查询窗口 10 秒。远程等待和延迟查询不计全局写忙碌；本地提交及取消与迁移写门禁协调。退出时有界停止本地请求，不强制取消远端任务。

页面切换项目/任务/轮次后丢弃迟到绘制；在途请求的原参数/请求键保留。重绘后当前控件同步写忙碌释放。未保存文稿不得提交。服务状态不可读时退出握手返回未允许，保留画布，恢复后可重新退出。

## 工作台 HTTP 边界

- /api/ai-connections：GET 列出；POST 保存非秘密连接，必须提供 expected_revision。
- /api/ai-connections/{id}：GET 私有工作台配置描述。
- /api/ai-connections/{id}/check：POST 空对象，读取操作声明，不试生成。
- /api/ai-calls/status：GET local_busy/local_queued/queued/remote_pending。
- /api/ai-tasks/{task}/runs/{run}/calls：GET 有界分页；POST 显式新尝试，短时返回 202。
- 同路径 /{attempt}：GET 原记录；/{attempt}/query 或 /cancel：POST 空对象，短时返回 202。

全部控制及私有读取使用原同源工作台会话校验。只读 MCP Bearer 不接受这些接口；MCP 没有新增执行、写入、生成或收件工具。

## 后续门槛

继续完成执行资料组装、一个明确的真实工具适配器，以及受控结果文件获取/收件闭环，再做独立桌面包。下载须有单文件/总量/磁盘预算，媒体效果、真实 AI 客户端、浏览器与原生退出单独验收。曜核只读选型已用独立适配器处理，执行队列与声明版本锁定仍未接入；本期协议不是已冻结的跨产品执行契约。旧版本和数据继续保留。
