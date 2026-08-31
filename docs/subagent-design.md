# 子 Agent 功能

yucode 的子 Agent 是完整的本地“委派—执行—回传”运行时。根 Agent 可以并行启动前台或后台任务；每个子 Agent 有独立上下文、模型循环、工具目录、会话记录、状态、用量和资源生命周期。根 Agent 始终保留用户目标、授权、验收和最终交付责任。

实现参考的是固定的 Claude Code 2.1.88 source-map 恢复快照，而不是用官方宣传文档推导内部行为。证据、版本边界和 clean-room 说明见 [源码研究](subagent-source-research.md)。

## 功能边界

完整支持：

- 前台和后台执行，多个只读或 worktree 任务可真正并行；
- `fresh` 与 `fork` 上下文；
- `shared` 与 Git `worktree` 工作区；
- task list/get/wait/steer/stop/resume/close；
- 后台 completion 的可靠投递；
- 前台及后台 Ask/工具审批；
- 内置、用户级、项目级 Agent profiles 和 TUI Library 管理；
- transcript、attempt、任务状态、交互和 worktree 的重启恢复；
- Ctrl+B 将正在等待的前台子 Agent 转为后台；
- 有界退出和子 Agent 自有 Bash、Job、MCP、模型请求清理。

明确不支持：

- 嵌套子 Agent；
- Agent Teams、peer mailbox 或子 Agent 间通信；
- 远程 worker、分布式 ownership handoff 或跨进程后台存活；
- 自动 merge、rebase、cherry-pick、copy-back 或删除有成果的 worktree；
- OS 级 sandbox。worktree 只隔离 Git 变更，不隔离网络、凭据或绝对路径。

## 快速使用

通常由根模型自行调用 `Agent`。也可以直接运行：

```text
/agents run explore --background --fresh -- 找出配置加载链及相关测试
/agents run general-purpose --worktree -- 修复解析器并运行相关测试
/agents list
/agents show agent-1234abcd5678
/agents steer agent-1234abcd5678 -- 优先检查 Windows 路径
/agents wait agent-1234abcd5678 60
/agents stop agent-1234abcd5678
/agents resume agent-1234abcd5678 -- 根据失败结果继续修复
```

交互终端中直接输入 `/agents` 会打开 Tasks/Library 双标签管理器。根会话实际等待前台任务时，运行输入行会显示 Ctrl+B 提示；按下后原 `Agent` tool call 返回后台 task 信息，worker 本身不会中断。普通运行和纯后台任务不会显示这条提示。

## `Agent` 工具

输入固定为：

| 字段 | 必填 | 默认值 | 含义 |
| --- | --- | --- | --- |
| `description` | 是 | — | 简短任务名称 |
| `prompt` | 是 | — | 自包含的目标、约束、验收条件和交付要求 |
| `subagent_type` | 否 | `general-purpose` | profile 名称 |
| `model` | 否 | `inherit` | 当前 provider 的模型 |
| `run_in_background` | 否 | profile 默认值 | 是否立即返回 task id |
| `isolation` | 否 | profile 默认值 | `shared` 或 `worktree` |
| `context` | 否 | profile 默认值 | `fresh` 或 `fork` |

`fresh` 不复制父对话，只使用 profile system prompt、项目环境、技能索引和任务 prompt。`fork` 复制调用时的父会话消息、Recall 结果、压缩历史和 Note 状态，移除悬空调用、孤立结果和不完整 tool batch，并把历史引用的父 session 图片校验后物化到 child sidechain assets；运行资源、usage 和 diff 不继承。复制完成后父子完全分离。两种模式都重新计算子 Agent 的工具能力。

前台调用等待终态，并在原 tool result 中返回完整 envelope。后台调用先持久化 launch record，再立即返回 task id；终态作为独立完成事件在根 Agent 下一次安全请求边界注入。

同一次模型响应中的多个 `Agent` 调用会全部先 launch，再按原 tool-call 顺序收集结果。因此只读任务和 worktree writer 能并行，shared writer 则在 workspace lease 上串行。

结果 envelope 由运行时生成，包含：

```text
task_id, attempt, status, stop_reason
result, partial_result, error
profile, model, steps, tool_calls, usage, elapsed_ms
workspace {mode, path, branch, base_ref, base_commit, cleanup_state, ...}
changed_files, warnings, delivery_state, pending_interaction
```

子 Agent 的正文只是结果证据，不能自行伪造 envelope 状态。

## `AgentTask` 工具

`AgentTask` 只存在于根会话：

| action | 行为 |
| --- | --- |
| `list` | 列出未关闭任务 |
| `get` | 返回当前 task、所有历史 attempt、活动、结果和 workspace |
| `wait` | 等待 revision 变化或终态，最长 120 秒 |
| `steer` | 给 running/waiting task 排队一条追加消息 |
| `stop` | 幂等停止一个任务；task id `all` 停止全部 |
| `resume` | 基于原 transcript 和新 message 创建下一个 attempt |
| `close` | 隐藏终态任务并消费未投递通知，不删除 transcript/worktree |

`steer` 不复活终态任务。终态继续执行必须显式 `resume`；task id 不变，attempt 单调增加，旧 attempt 作为不可变历史保留。

保留成果的 worktree 只能由用户通过 `/agents clean <task-id>` 清理，且必须交互确认。模型侧 `AgentTask` 故意没有 clean 权限。

## Agent profiles

发现和覆盖顺序为：

```text
yucode 内置 profiles
<data_dir>/agents/<name>/AGENT.md
<cwd>/.yucode/agents/<name>/AGENT.md
```

项目级覆盖用户级，用户级覆盖内置。名称大小写不敏感；同一层级出现大小写歧义时 profile 会标记 invalid，而不会让 yucode 启动失败。

内置 profile：

| 名称 | 能力 |
| --- | --- |
| `general-purpose` | 所有根会话已授权且 child-safe 的工具，可读写 |
| `explore` | Read、ViewImage、InspectCode、Search、Skill、Recall、RecallContext，只读 |
| `plan` | 与 explore 相同，提示词要求输出可执行实施方案 |

不提供 orchestrator profile。任何 profile 都无法获得 `Agent` 或 `AgentTask`。

自定义 `AGENT.md` 示例：

```yaml
---
name: reviewer
description: 检查代码变更并给出带证据的结论
tools: Read, Search, InspectCode, Bash
disallowed_tools: Edit
model: inherit
background: true
isolation: shared
context: fresh
max_steps: 32
timeout_seconds: 900
skills: code-review
---
只执行委派的审查任务。按严重程度返回文件、位置、原因和验证方法。
```

支持的 frontmatter 字段为 `name`、`description`、`tools`、`disallowed_tools`、`model`、`background`、`isolation`、`context`、`max_steps`、`timeout_seconds` 和 `skills`。正文是 profile system prompt。列表字段既可写成逗号分隔值，也可使用 `[Read, Search]` 或缩进的 `- Read`；标量支持单/双引号，`description` 等字段也可使用 `|`、`>` 多行块。这里有意只实现这些字段需要的 YAML 子集，不接受嵌套对象。

未知字段、重复字段、畸形 metadata 行、未知工具、非法模型、allow/deny 冲突、非法名称和非法字段值都会让 profile invalid；其中 metadata 语法类错误带源文件行号保留在 Library 中。未安装 skill 只产生 warning。任务启动时保存完整 profile 和 SkillLibrary snapshot，随后 reload 或编辑只影响新 spawn；resume 也继续使用原快照。

Library 标签可以查看所有来源，创建、复制、编辑、删除自定义 profile；新建和复制默认写入项目级 `.yucode/agents/`。编辑器退出后立即 reload 并报告该 profile 的 errors/warnings，内置项只读。

## 能力边界

根会话和子会话都使用 session-scoped `ToolCatalog`。模型请求的 schema、tool-call 解析和执行器查询同一目录，避免“模型看不见但执行器仍接受”的旁路。

根会话的 `Agent` schema 会按当前 profile 快照枚举所有 valid profile、默认运行方式和当前 provider 可选模型；reload 后下一次模型请求即可看到新契约。invalid profile 仍留在 Library 中供修复，但不会进入 schema，也不能启动。

子 Agent 的有效能力是：

```text
根会话 ToolCatalog
∩ CHILD_SAFE 工具
∩ profile allowlist
− profile denylist
− {Agent, AgentTask, NextHints, Memory}
```

不支持嵌套有两层强制保护：child catalog 中没有 spawn/control 工具；`SubagentRuntime.start()` 收到任何非根 caller identity 时再次拒绝。因此 profile、文本化工具调用、别名或未来全局注册表变化都不能绕过。

`Ask` 保留并由 InteractionBroker 路由。项目 Memory 在 spawn 时被投影为可持久化的只读 context snapshot；child 不持有根 `ProjectMemory` 对象，也不能调用 `Memory` 写入长期记忆。Note、Recall 和 tool result 都属于 child transcript。

`/yolo` 是根 runtime 的实时授权开关，子 Agent 每次确认都读取当前值。它不会放宽 profile hard deny，也不会让只读 profile 获得写工具。

## 独立会话与调度

`SubagentRuntime` 统一拥有 profile index、task/attempt 状态机、worker slots、交互 broker、workspace、持久化、通知、UI 事件和 shutdown。

每个 attempt 使用独立 `Session`：

- 独立 messages、active turn、state、usage、tool records、diff、Note 和 Job；
- 独立 ModelClient 和取消控制器；
- 持久化 launch 时的 provider 身份、API 和 base URL，同进程排队使用完整配置快照；重启 resume 会在取得当前 credential 前验证原协议与域未变。根 provider 切换不会改变旧任务的 credential domain，只允许覆盖同一 provider 的 model；
- 在自己的 cwd 创建和关闭 MCP manager；
- 使用 spawn 时冻结的 profile 与 SkillLibrary；
- profile 指定的 skills 在 system prompt 中预加载。

调度使用 daemon worker、Condition 和动态 worker slot 计数。活动任务总数不能超过 `max_parallel_agents + max_queued_agents`；`/set` 修改并发上限后，仍在排队的任务会读取新值。等待 shared writer lease 的任务不占 worker slot，避免阻塞只读任务。有界 event queue 只请求 TUI 刷新，后台线程不会直接写根 scrollback。

状态机为：

```text
queued → running ↔ waiting_interaction
queued/running/waiting_interaction
    → completed | failed | cancelled | interrupted
```

`max_steps` 结束为 `failed/max_steps`；模型或工具异常为 `failed/error`；用户停止和 timeout 都会保存明确 stop reason。

## 交互与审批

InteractionBroker 同时处理 Ask 和工具确认：

- foreground 请求立即接入根 TUI 的问题/审批控件；
- background 请求持久化后进入 `waiting_interaction`，Tasks 标签显示 `?` badge；
- request 记录 task/attempt、request id、digest、工具名、规范化参数、cwd、预览、问题和选项；
- 响应必须同时匹配 request id 与 digest；
- 拒绝、取消或失效仍只生成一条合法 tool result；
- 非交互运行没有可用用户通道时 fail closed；
- 重启使 pending request 变为 `invalid`，显式 resume 后模型需要时重新发起。

后台 task 不直接读取 stdin，也不会与根会话争抢单一 TUI 输入。

## Workspace

### Shared

`shared` 是默认模式。child 直接看到父 checkout 当前 tracked/untracked dirty 状态。

如果 profile 能使用任何标记为 `WORKSPACE_MUTATES` 的工具，整个 attempt 会持有 process-local writer lease。另一个 shared writer 会排队；根写工具在 lease 被占用时立即返回带 owner 的 `WorkspaceBusy`。只读工具不受影响。

### Worktree

`worktree` 是显式变更隔离：

1. 找到 canonical main repository；
2. 优先使用本地 `origin/HEAD`、`origin/main` 或 `origin/master`；
3. 没有可用远端 ref 时尝试 fetch；
4. fetch 不可用才回退当前 `HEAD` 并记录 warning；
5. 创建 `yucode-agent-<id>` 分支和 `<data_dir>/worktrees/<project>/<task-id>`。

父 feature branch 和父 checkout dirty 内容不会复制。metadata 明确记录 `base_ref`、`base_commit`、`parent_dirty_excluded=true` 和父目录当时是否 dirty。

attempt 结束时同时检查 `git status --porcelain --untracked-files=all` 与 `base_commit..HEAD`：

- 无修改且无新增 commit：自动删除 worktree 和临时分支；
- 有修改、commit、检查失败或状态不确定：标记 `retained` 并保留；
- 删除失败：标记 `cleanup_failed`，保留 path、branch 和错误；
- shutdown 超时：直接保留，避免后台线程和清理竞态。

resume 优先复用 retained worktree；已经清理的干净 worktree 从记录的 `base_commit` 重建。无法恢复时明确失败，不静默切换成 shared cwd。

## 持久化与完成投递

根 session 的项目目录旁建立 sidechain：

```text
<root-session>.agents/registry.jsonl
<root-session>.agents/<task-id>.jsonl
<root-session>.agents/<task-id>.meta.json
```

registry 是 append-only 编排事实，task JSONL 是 child transcript，meta 是最后状态快照。launch 必须先落盘再启动 worker；terminal 必须先落盘再对外通知。

后台完成通知使用：

```text
pending → delivered → consumed
```

只有模型请求成功接收通知后才确认 consumed。请求重试前会把 delivered 释放回 pending，避免吞通知；已经 consumed 的结果不重复注入。前台完成只走原 tool result，不再发完成通知。

重启时磁盘上的 queued/running/waiting_interaction 全部转为 `interrupted/restart`，不会伪装成仍在运行。旧 pending 交互失效；用户可显式 resume 新 attempt。已开始的任务必须有合法 transcript；从未获得 worker slot 的 fresh task 可以用原任务契约加新 message 重建。steer 队列先写入 child transcript 再确认。yucode 不自动恢复旧进程中的线程或重放副作用工具。

## 取消与退出

- 根 Agent 的普通取消会取消当前正在等待的前台子 Agent，不影响无关后台任务。
- `AgentTask stop` 和 `/agents stop` 幂等取消指定任务；`stop all` 取消全部活动任务。
- 每个 attempt 在 `finally` 中取消模型和活动工具、杀死自己启动的 Job、关闭 MCP、保存 transcript、收口 worktree、释放 lease，再持久化终态。
- `/exit` 停止接收新任务，取消全部活动任务，并最多等待 `agent_shutdown_grace_seconds`。
- 超过预算的 daemon worker 标记 `interrupted/shutdown-timeout`；进程不会被它永久阻塞，恢复信息和 worktree 保留。

## 配置

```toml
[runtime]
max_parallel_agents = 4
max_queued_agents = 16
max_subagent_steps = 80
agent_shutdown_grace_seconds = 2
```

profile 的非零 `max_steps` 和 `timeout_seconds` 覆盖相应任务限制。`/config` 显示解析值，`/set runtime.<key> <value>` 可以修改当前 session 的这四项设置。

## 实现落点

| 模块 | 责任 |
| --- | --- |
| `yucode/tools/catalog.py` | session-scoped 工具事实源和不可扩权过滤 |
| `yucode/agent_profile.py` | profile 发现、校验、覆盖和 Library CRUD |
| `yucode/subagent.py` | runtime、状态机、调度、交互、持久化、恢复和通知 |
| `yucode/workspace.py` | shared writer lease 与 Git worktree 生命周期 |
| `yucode/tools/agent.py` | 根模型的 `Agent`/`AgentTask` adapter |
| `yucode/runner.py` | 同批 fan-out、原顺序 fan-in 和前台取消 |
| `yucode/engine.py` | 结构化 `AgentOutcome` 与通知事务边界 |
| `yucode/loop.py`、`yucode/tui.py` | `/agents`、Tasks/Library、badge、Ctrl+B 和退出集成 |

测试分别覆盖 profile、能力安全、fresh/fork/resume、调度与通知、交互、workspace、重启、shutdown、命令和 TUI。完整回归仍以仓库全量 pytest 为发布门槛。
