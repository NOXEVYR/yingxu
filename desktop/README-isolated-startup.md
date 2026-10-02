# Stage I 原生隔离入口

这是独立源码验收入口，默认正常启动的 8791、AppID 和数据语义保持。编译件身份仍来自 `yingxu/__init__.py` 的版本与 build，但新 EXE 的 SHA 是 Stage I，不能称为验收了封存 H EXE。这里没有安装、发布或新模型生成。

根验收进程先创建并持有合成后台，再以隐藏 Popen 启动：

```text
YingXu-StageI-Candidate.exe --isolated-test
  --root <Stage I native-staging absolute directory>
  --test-root <owned existing private temporary directory>
  --data <test-root/data> --projects <test-root/projects>
  --evidence-root <test-root/evidence>
  --port <owned loopback backend port, 1024..65535, never 8791>
  --test-id <fresh canonical nonempty UUID>
  --webview-root <verified existing pinned fixed WebView runtime>
  [--preflight-only] [--timeout-seconds 20..180]
```

所有目录先由父进程创建。路径必须完整包含盘符及根分隔符，例如 `C:\test\data`；`C:relative`、`\relative` 不因 Windows 的 IsPathRooted 返回 true 而获准。data/projects/evidence 必须分别在 test-root 内、互不交叠；源根、运行库与 test-root 不交叠。拒绝 UNC、链接祖先、默认应用数据与项目目录、缺失候选源码标记、证据文件重用。测试 runtime 只读，不搜索或回退系统 WebView，不改共享 ACL。父进程应先按锁定清单校验已有 runtime，且给子进程使用环境变量白名单，不继承真实服务密钥。

test-root 中必需普通、非链接/硬链、最多 2048 字节的 `.yingxu-isolated-owner.json`，恰好以下五字段，路径使用与参数规范化结果相同的绝对字符串：

```json
{"schema":"yingxu-native-isolated/1","test_id":"<CLI UUID>","program_root":"<CLI root>","data_root":"<CLI data>","projects_root":"<CLI projects>"}
```

preflight 只 GET 指定服务的 `/api/health` 与 `/api/settings`。检查 app/ok、version/build/program/data 身份，以及 capture_enabled、automatic_update_check、automatic_update_download 都严格为 false。不启动 Python/后台、不扫描项目 DB；错误服务或身份不符在窗口前拒绝，错误只返回固定 code，无消息框。MCP、外部引用与技能扫描源由父合成后台配置为关闭/空，入口不会擅自更改后台设置。

完整模式使用真实 StudioWindow/WebView/原首页：无任务栏、无激活，窗体置于当前整个虚拟桌面范围之外；不注册托盘、截图热键或文件 IPC，不执行关联注册、更新恢复、外链 shell。使用独立测试 AppID/mutex/event。页面资源只准本次 127.0.0.1:port 的 GET（排除 updates/open/external），另准一次固定 POST `/api/project-files/sync`，这是原首页在合成项目中的本地索引同步；其他写操作、派单、成果导入、更新和外部资源全部拒绝。不能把这一模式称为零写入或原生编辑/收件功能验收。

收到严格的 desktop-ready 后，等有界的索引同步结束，读取硬编码的公共 DOM 事实并用 WebView CapturePreview 截图。没有任意脚本文件、屏幕/鼠标/键盘/剪贴板操作或模型调用。输出根为私有证据目录：

- `preflight.json`：preflight-only 成功，无窗体。
- `ready.json`：版本/build/program/data、实际 browser_version/source URL、offscreen/window_hidden/taskbar/tray/hotkey、desktop_ready、公共 DOM entry/项目名、索引同步 POST 和被阻止请求计数。
- `preview.png`：仅此 WebView 的首页图像，不是桌面截图。
- `failure.json`：固定错误 code；缺失参数等尚未建立可信根的错误只写 stdout。

父进程看到 ready 后创建普通、非链接/硬链且不超过 128 字节的 `exit.request`，仅关闭本次窗体，不终止后台。默认 90 秒超时（20..180 可选）；截图与 ready 写完前超时/提前退出都是失败。父进程仍须持有自己的句柄，并在验证后清理本次服务。此模式暂未接可选 PNG 文件声明。

```text
python -B desktop/build.py --sdk-package <existing locked SDK> --output <owned Stage I EXE> --manifest-output <owned build JSON> --isolated-test-units
```

该参数只执行独立控制台参数/身份单元，不运行完整原生候选、后台或 WebView；不使用 `--test` 的已有 GUI fixture。完整原生验收由父 runner 在预检后单独执行。WebView 页面资源边界不等于已证明浏览器自身后台组件的全部网络行为，实际完整验收仍要记录网络与进程证据。
