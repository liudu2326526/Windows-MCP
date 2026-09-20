# Windows Computer Use 低延迟执行：实施、使用与验收

日期：2026-09-20。本文对应本 fork 的 `RunBatch` / `CancelBatch` 实现，接口以 [`tools/batch.py`](../src/windows_mcp/tools/batch.py) 的模型和 [`desktop/service.py`](../src/windows_mcp/desktop/service.py) 为准。

本实现把连续 GUI 输入、条件等待和结果检查合并为一次 MCP 调用，以减少模型往返。它完成原《Windows Codex Computer Use 低延迟实施技术文档》中 Windows-MCP 执行端的一部分；DSH 原生能力包、产品审批界面、PADS Automation/OLE 适配和 Windows 实机性能验收需要分别实施。本文不描述或推断 Codex Desktop Windows 的私有内部实现。

## 1. 实施范围

| 原方案内容 | 本 fork 的实现或状态 |
|---|---|
| 收窄工具面 | 复用 `--tools` / `--exclude-tools`；推荐配置见下文 |
| 独立观察 | 保留 `Snapshot` / `Screenshot`，Snapshot 返回 `Snapshot ID` |
| 短动作组 | `RunBatch`，1–32 个步骤，默认总预算 30 秒，上限 120 秒 |
| 快照引用 | ID 包含服务实例随机标识与代数；保存有限历史，重启后旧 ID 失效 |
| 前台校验 | 输入前比较当前真实 HWND/PID 与引用快照；可附加进程或窗口约束 |
| UIA 重新定位 | label 从其原快照恢复目标身份，再从新 UIA 树中唯一匹配；名称也从新树匹配 |
| 条件等待与验证 | 每步可包含 `wait_for` 和 `verify`，在服务器内部执行 |
| 取消 | `CancelBatch` 按调用方提供的 `execution_id` 发出协作取消请求 |
| 结果记录 | 返回步骤结果、错误码、耗时和前台身份摘要；没有自动回滚 |
| UIA 语义输入 | 尚未提供 `Invoke` / `ValuePattern.SetValue` 等动作；输入仍走鼠标、键盘或剪贴板 |
| DSH / PADS / 权限产品化 | 提供接入建议；本 fork 不实现 DSH Agent Loop、审批产品或 PADS 业务对象 API |
| 真实性能 | 提供验收步骤；模型回合、p50/p95 和 PADS 成功率需在目标 Windows 环境测量 |

`app` 和 `observe` 不是 `RunBatch` 动作。启动或切换应用用独立 `App` 工具，观察用独立 `Snapshot`；每个批次围绕一个前台窗口组织。弹出新的顶层对话框可能改变 HWND，这时应结束当前批次、重新观察再继续。

## 2. 运行环境与最小工具配置

服务应运行在目标 Windows 主机的交互式桌面会话中；会话保持解锁，目标应用处于前台。Python 版本要求以 `pyproject.toml` 为准，目前为 3.14+。安装仓库依赖使用 uv，不要用公开 PyPI 包代替当前 fork，否则可能没有新增工具。

在 Windows PowerShell 的仓库根目录执行：

```powershell
uv sync --frozen --extra dev
uv run windows-mcp serve --transport stdio --tools Snapshot,Screenshot,RunBatch,CancelBatch
```

这是 4 个工具的最小批处理配置。需要由 Agent 启动或切换应用时，加入 `App`；需要独立等待时加入 `WaitFor`。批次内部等待不需要额外暴露 `WaitFor`。只有使用场景需要时再加入单步输入工具。

通用 MCP 客户端的 stdio 配置示例：

```json
{
  "mcpServers": {
    "windows_cua": {
      "command": "uv",
      "args": [
        "--directory", "C:\\work\\Windows-MCP",
        "run", "windows-mcp", "serve",
        "--transport", "stdio",
        "--tools", "Snapshot,Screenshot,RunBatch,CancelBatch,App"
      ],
      "env": { "ANONYMIZED_TELEMETRY": "false" }
    }
  }
}
```

将 `C:\work\Windows-MCP` 替换为 Windows 上本 fork 的检出路径。该配置必须由 Windows 上的客户端执行；macOS 客户端通过 stdio 启动的是 macOS 进程，不能直接控制另一台 Windows 桌面。

跨主机使用 Streamable HTTP 时，可先在 Windows 回环地址上启动并通过已有的受控隧道连接：

```powershell
uv run windows-mcp serve --transport streamable-http --host 127.0.0.1 --port 8000 --tools Snapshot,Screenshot,RunBatch,CancelBatch,App
```

客户端连接 `/mcp`。若直接绑定远程网卡，使用项目已有的 `WINDOWS_MCP_AUTH_KEY`、TLS 和 IP allowlist 配置。不要把真实令牌提交到配置示例。HTTP 的 stateless 模式不会把桌面快照或运行中的批次变成可跨进程共享的状态；同一次观察、运行、取消必须到达同一服务实例。

## 3. 调用流程

```text
App（可选）
  → Snapshot
  → 模型决定短动作组和验收条件
  → RunBatch：校验前台 → 重新定位 → 输入 → 本地等待/验证
  → 读取步骤结果；状态未知时重新 Snapshot
```

首次观察可调用：

```json
{
  "use_vision": false,
  "use_ui_tree": true,
  "use_annotation": false
}
```

这是优先用于名称或坐标定位的低成本 `Snapshot` 参数。返回文本包含 `Snapshot ID`、窗口身份、控件名、控件类型和坐标，但不显示数字 label。确实需要 label 时，将 `use_vision` 和 `use_annotation` 设为 true，从注释截图中读取交互控件的数字 label，并将它与同次返回的 Snapshot ID 一起使用。纯视觉任务可改用 `Screenshot`。截图被缩放或裁剪时，`loc` 使用实际虚拟桌面像素坐标，必须依照截图返回的缩放和区域信息换算。

### 3.1 填写两个字段并检查结果

下面是 `RunBatch` 参数示例。窗口名、控件名和类型仅作示意，应替换成实际 Snapshot 返回的值；控件类型可能受 Windows 语言和 UIA provider 影响。

```json
{
  "execution_id": "settings-demo-001",
  "snapshot_id": "snap-example-1",
  "window": { "title_contains": "Settings" },
  "timeout": 15,
  "steps": [
    {
      "action": "type",
      "args": {
        "target": { "name": "Project name", "window_name": "Settings", "control_type": "Edit" },
        "text": "demo-project",
        "clear": true
      },
      "verify": {
        "condition": "value_equals",
        "target": { "name": "Project name", "window_name": "Settings", "control_type": "Edit" },
        "value": "demo-project"
      }
    },
    {
      "action": "type",
      "args": {
        "target": { "name": "Release name", "window_name": "Settings", "control_type": "Edit" },
        "text": "release-demo",
        "clear": true
      },
      "verify": {
        "condition": "value_equals",
        "target": { "name": "Release name", "window_name": "Settings", "control_type": "Edit" },
        "value": "release-demo"
      }
    }
  ]
}
```

该例检查输入框中的值，不证明已保存到磁盘或服务器。需要保存时，在宿主完成相应授权后发送单独的保存步骤，并验证应用给出的保存状态；不要仅以按下快捷键或点击按钮作为成功依据。

`window` 可以包含 `title_contains`、精确匹配的 `process_name`、`process_id`，多个条件共同生效。引用快照的 HWND/PID 校验始终执行；这些字段是额外约束。

### 3.2 label、坐标和批量字段

每个 `target` 使用 `loc` 或 UIA 定位字段。`loc` 不能与 `label` / `name` 组合；`label` 可以和 `name` 同时提供，此时 `name` 是原快照目标的附加 guard。`window_name` 和 `control_type` 可以进一步限定 UIA 目标。label 属于其 `snapshot_id`，不是跨快照稳定 ID。

```json
{
  "snapshot_id": "snap-example-1",
  "timeout": 10,
  "steps": [
    {
      "action": "multi_edit",
      "args": {
        "entries": [
          { "target": { "label": 12 }, "text": "demo-project" },
          { "target": { "label": 18 }, "text": "release-demo" }
        ]
      }
    }
  ]
}
```

`multi_edit` 会清空各字段后输入，每步最多 32 个 entry；`multi_select` 使用 `targets` 数组与 `press_ctrl`，每步最多 32 个目标。布局会随输入变化的表单，优先拆为逐字段 `type` 步骤并在字段之间验证。`loc: [x, y]` 只能校验前台身份，不能证明该点仍然属于同一个控件。

省略顶层 `snapshot_id` 时，执行器可为输入批次获取起始快照；坐标或名称定位可以使用这一方式。已经从先前 Snapshot 读到数字 label 时，应明确传其 ID，不能让 label 隐式绑定到后来采集的快照。

### 3.3 动作参数

所有步骤使用 `{ "action": "...", "args": { ... } }`，并可附加 `wait_for` 和 `verify`。未知字段或未知动作会被拒绝。

| action | args |
|---|---|
| `click` | `target`；`button` 为 `left/right/middle`；`clicks` 为 0–5，0 只移动 |
| `type` | `target`、`text`；可选 `clear`、`press_enter`、`caret_position: start/idle/end` |
| `scroll` | `target`；`type: vertical/horizontal`；对应 `direction: up/down/left/right`；`wheel_times` 为 1–100 |
| `move` | `target` |
| `drag` | `target` 为终点；可选 `from_loc: [x,y]`、`duration` 为 0–10 秒 |
| `shortcut` | 非空 `shortcut`，例如 `ctrl+a` |
| `multi_select` | 非空 `targets` 数组；可选 `press_ctrl`，默认 true |
| `multi_edit` | 非空 `entries` 数组，每项包含 `target` 和 `text` |
| `wait` | `duration`，0–120 秒，同时受批次总预算约束 |

不要在 `RunBatch` 中传 `app`、`observe`、`powershell`、`filesystem`、`registry`、`process` 或嵌套 `runbatch`。

### 3.4 等待与验证

`wait_for` 在动作前执行，`verify` 在动作后执行。要等待点击后的异步结果，应把条件放入该点击步骤的 `verify`。

```json
{
  "action": "click",
  "args": { "target": { "name": "Refresh", "window_name": "Inventory", "control_type": "Button" } },
  "wait_for": {
    "condition": "element_enabled",
    "text": "Refresh",
    "window_name": "Inventory",
    "timeout": 3
  },
  "verify": {
    "condition": "text_exists",
    "text": "Refresh complete",
    "timeout": 5,
    "interval": 0.25
  }
}
```

等待支持 `text_exists`、`active_window`、`element_exists`、`element_enabled`、`focused_element`；参数为 `text`、`window_name`、`timeout`（默认 10 秒）、`interval`（默认 0.25 秒）、`use_dom`（默认 false）。它们复用现有 WaitFor 的文本匹配语义，仍会轮询采集 UIA；不是 UIA 事件订阅，也不是应用业务 API。`text_exists` 搜索采集状态中的文本，不能保证字符串一定来自目标控件，应选择足够明确的成功标志。

`value_equals` 仅供 `verify` 使用，需要完整的 `target.name`、`target.window_name`、`target.control_type` 和 `value`。执行器重新采集后检查唯一匹配控件的 UIA value。UIA 未暴露 value、格式化了 value 或目标不唯一时，验证失败；没有 OCR 推断或业务数据回读替代。

### 3.5 取消与时间预算

调用前生成唯一 `execution_id`。另一个能够并发发送 MCP 请求的控制路径可以调用 `CancelBatch`：

```json
{ "execution_id": "settings-demo-001" }
```

返回 `cancellation_requested` 只表示取消信号已登记；等待原 RunBatch 结束后再开始新的输入。`not_found` 表示当前实例没有该活动 ID，可能尚未开始、已经结束或请求到达了错误实例。若宿主将所有工具调用排成独占队列，CancelBatch 排在 RunBatch 后面就无法及时取消；宿主必须提供独立取消入口或允许该控制调用并发进入服务器。

取消和 timeout 是协作机制：本地等待可被打断，执行器在步骤和原生调用之间检查状态；已经进入的 Windows/UIA/输入调用必须先返回，不能保证在 timeout 到点时强制中断。MCP 客户端停止等待也不等于 Windows 动作已经停止。客户端超时应大于批次预算并留传输余量；超时后先确认批次状态，避免盲目重试已经产生副作用的步骤。

## 4. 结果和错误处理

RunBatch 返回 FastMCP `ToolResult`：`structuredContent` 提供结构化结果，文本 content 提供相同 JSON，便于不同 MCP 客户端读取。结果包括 `execution_id`、`status`、`completed_actions`、`side_effects_possible`、`verified`、`error_code`、`total_ms`、`steps`、`final_observation`。逐步骤记录 action、状态、耗时、等待和验证结果。没有输入文本回显，但调用参数仍可能由 MCP 客户端或宿主日志保存，因此宿主仍需对密码等字段进行脱敏。

`completed` 表示所列步骤执行完成；业务成功还要检查预期的验证是否执行并通过。`verified=true` 仅在批次整体成功、至少执行了一次 verify，并且最后一个步骤带 verify 时返回。无最终 verify 的批次不能据此认定业务已完成。

`completed_actions` 只统计执行器已返回的输入步骤，不统计纯 `wait`；一个 `multi_edit` 或 `multi_select` 步骤整体只计 1。进入原生输入 provider 前，步骤就会标记 `side_effects_possible=true`；因此 verify 失败时，该动作仍计入 `completed_actions`，并明确表示副作用可能已经发生。若原生调用抛错，动作是否部分生效可能无法判断，此时 `action_completed=false` 但 `side_effects_possible=true`。

`partial` 表示失败前可能已经产生副作用；不要整体自动重跑。RunBatch 的 `partial` 和 `failed` 结果都设置 MCP `isError=true`，同时保留上述结构化详情。请求 schema、timeout / `stop_on_error`、execution ID 或显式 label 快照绑定在执行前校验失败时，工具直接产生 MCP 错误；Snapshot、窗口或目标在运行时失效则返回带结构化详情的 `failed` 结果。调用方不能只依据传输成功或文本 content 判断成功。

| error_code | 调用方处理 |
|---|---|
| `STALE_STATE` | 停止使用旧 refs；确认当前前台，重新 Snapshot 并规划 |
| `TARGET_NOT_FOUND` | 检查 UIA 目标是否唯一、可见和可用；需要视觉判断时单独截图 |
| `TARGET_MISMATCH` | 核对窗口/进程约束；不自动切换到模糊匹配的其他应用 |
| `VERIFY_FAILED` | 检查已执行步骤和现场；不默认重复输入或提交 |
| `TIMEOUT` | 检查已执行范围和最后状态，区分等待超时与动作执行后超时 |
| `CANCELLED` | 确认批次结束；已经执行的副作用保留 |
| `ACTION_FAILED` | 按具体错误恢复，不把工具成功连接等同动作成功 |

默认 `stop_on_error=true`。即使设为 false，超时、取消和快照失效也会停止后续步骤。`final_observation` 是快照元数据加实时 HWND/PID 摘要，不是完整动作后 UIA 树或业务结果；需要恢复现场时调用 Snapshot。

同一前台桌面不支持多个 Agent 并行输入。服务端的锁串行化受保护的原生观察/输入操作，但不能阻止用户、其他进程或远程桌面软件改变 UI；原生调用前后仍存在系统事件竞争窗口。前台校验和重定位降低误操作风险，不能当成应用隔离或安全沙箱。

## 5. DSH 和策略层接入

DSH 可先通过已有 MCP 客户端消费本 fork，不需要修改 Agent Loop。serverName 为 `windows_cua` 时，工具名通常投影为 `mcp__windows_cua__RunBatch` 等。使用 DSH PTC 时一次顺序调用 Snapshot、RunBatch，并只返回验收所需摘要；中间截图是否附加到模型上下文仍取决于 DSH 的实现。

推荐 Agent 指令：观察目标窗口，规划少量确定动作，给关键步骤设置 verify；遇到新弹窗、窗口变化或目标不唯一时返回模型重新判断。只有任务依赖视觉时才获取截图。需要用户批准的保存、提交、上传、删除等步骤放入独立批次，授权由宿主执行；`ToolAnnotations` 和工具白名单都不是逐动作审批。

本实现没有服务器应用白名单、危险动作语义分类、审批恢复协议或持久会话审计。未来 DSH 集成应把这些放到明确的服务/Provider/Consumer 中，并把模型可见结果写入 session log。批次的 mouse/keyboard 能操作任何已获桌面权限的应用，因此“排除 PowerShell 工具”不等于 GUI 无法打开终端。

## 6. 验证与发布检查

以下为目标 Windows 环境的验证命令，不表示本文编写时已经全部执行。先安装依赖，再运行与改动相关的测试：

```powershell
uv sync --frozen --extra dev
uv run --frozen --extra dev python -m compileall -q src tests
uv run --frozen --extra dev ruff check src/windows_mcp/tools/batch.py src/windows_mcp/desktop/service.py src/windows_mcp/desktop/views.py src/windows_mcp/tools/_snapshot_helpers.py tests/test_batch_tool.py tests/test_stdio_handshake.py
uv run --frozen --extra dev pytest -q tests/test_batch_tool.py tests/test_wait_for_tool.py tests/test_multi_tools.py tests/test_stdio_handshake.py
git diff --check
```

仓库现有 Windows CI 使用 Python 3.14、`uv sync --frozen --extra dev` 和完整 pytest。开发机上的 stub 测试能验证调度和 JSON 逻辑，不能证明 UIA、DPI、前台切换或真实 stdio 服务已经通过。实际提交的 CI 结果和平台范围应记录在 PR 中。

Windows 交互式验收必须覆盖：

1. 标准表单连续填写至少 6 个字段并逐项回读，确认多次内部采集不会错误淘汰当前批次引用。
2. Snapshot 后手动切换前台窗口，旧批次返回 STALE_STATE，错误窗口不收到输入。
3. 在同一窗口移动目标控件或改变列表顺序，label 重新定位到原元素；重名无法唯一定位时拒绝输入。
4. 点击后出现加载状态，verify 在工具内等待并检查完成；将 timeout 缩短，确认后续步骤不再开始。
5. 用独立并发请求取消长等待，检查原批次结束并且后续输入未发生；同时记录原生阻塞调用的实际取消延迟。
6. 测试首次步骤执行后验证失败，确认结果没有暗示已回滚，恢复流程不会重复提交。
7. 在不同 DPI 和多显示器位置执行坐标、拖拽和 UIA 定位；验证截图换算。
8. 检查 stdio 实际工具清单包含 RunBatch 和 CancelBatch，错误调用通过 MCP 错误通道返回。

性能回归至少使用“6 字段表单”“打开应用并等待完成”两种通用场景。保存同一模型和同一 Windows 配置下的原单步链路与批处理链路数据：任务成功率、模型回合数、MCP 调用数、截图数与字节、UIA 采集时间、重试数、总耗时 p50/p95。先以足够重复次数建立基线，再判断是否有稳定收益。

设置 `WINDOWS_MCP_PROFILE_SNAPSHOT=1` 可使用既有快照阶段计时；RunBatch 提供步骤和总耗时。模型耗时、审批时间、端到端 MCP RTT 需要由宿主记录，不能从 RunBatch 的 total_ms 推导。当前重新定位仍采集新 UIA 树，因此减少模型回合并不保证 UIA 时间降低；后续可评估目标窗口子树查询和事件等待。

## 7. 后续工作和交付边界

本次仓库变更提供受约束的 GUI 批处理、前台/快照校验、UIA 重定位、本地等待/验证和协作取消。提交到 fork 或 CI 通过，都不等于已部署到目标 Windows 桌面，也不等于 PADS 业务验收通过。

原方案仍需独立完成的内容：

- DSH 原生 Windows CUA 服务、Provider/Consumer、审批与会话日志投影。
- UIA Invoke、ValuePattern、SelectionPattern 等语义输入及其兼容性验证。
- 应用白名单、逐动作授权和可恢复的执行审计。
- UIA 定向查询、事件等待、状态差量和必要的截图失败现场。
- 指定 PADS 版本的 Automation/OLE/脚本接口调查与 `pads_semantic` 业务工具；PCB 自绘画布的元件、走线和网络不由当前 UIA 树自动识别。
- Windows 实机回归、PADS 场景成功率、性能基线和取消时延验收。

这些工作需要目标 Windows 会话、应用版本和可复现业务场景。实施时按测试证据更新状态；不要将未执行的实机步骤写成已完成。
