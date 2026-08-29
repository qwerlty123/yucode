# Claude Code 2.1.88 子 Agent 源码研究

> 研究基线固定为第三方 source-map 恢复快照
> [`ChinaSiro/claude-code-sourcemap@a8a678c`](https://github.com/ChinaSiro/claude-code-sourcemap/commit/a8a678cb6244e6770e1e421767ff0987a1d95549)，
> 其中 `package/package.json` 标记版本为
> [`@anthropic-ai/claude-code@2.1.88`](https://github.com/ChinaSiro/claude-code-sourcemap/blob/a8a678cb6244e6770e1e421767ff0987a1d95549/package/package.json#L1-L14)。
> 本文不以 Claude Code 官方文档替代源码分析，也不复制恢复源码、内部提示词或其他专有内容。

## 研究结论

2.1.88 中的普通子 Agent 不是一次额外的模型请求，而是一条独立运行链：父 Agent 提交任务，运行时解析 profile、裁剪工具、建立独立上下文和取消控制，子 Agent 复用完整 query/tool loop，最后由任务对象收口结果、用量、transcript 和 worktree。

对 yucode 最有价值的机制有：

- 普通 Agent 默认使用 fresh context；fork 是另一条显式且受控的路径。
- 模型可见的工具和执行器真正接受的工具都经过裁剪，不能只在提示词里声明“禁止”。
- 前台任务跟随父调用等待和取消；后台任务持有独立 task id、abort、进度与完成结果。
- steer 消息在模型请求的安全边界领取，而不是并发改写正在发送的上下文。
- resume 从 transcript 和 metadata 重建新执行，不复活旧线程栈。
- completion 与最初的后台 launch result 分离，并记录是否已经通知。
- worktree 无变更时清理，有变更时保留路径和分支供父 Agent 检查。
- 普通后台 Agent 在没有 hook 预先裁决时不会直接占用用户交互通道，而是拒绝待审批操作。
- 外部构建的普通 child 工具池默认移除 `Agent`，因此“不支持嵌套”是该版本可验证的真实边界。

yucode 在这些机制上做 clean-room 实现，并有三项明确增强：后台审批/Ask 进入可持久交互队列；任务、attempt、通知和 worktree 事实单独持久化；worktree 清理失败保留 `cleanup_failed` 和完整恢复信息。

## 固定快照中的实现证据

下表只描述固定提交中可观察到的结构，不把符号名当成稳定公开接口。

| 机制 | 可观察行为 | 固定源码位置 |
| --- | --- | --- |
| Spawn 输入 | `Agent` 接收任务描述、prompt、类型、model 和 background；isolation/fork/team 位于额外受控路径 | [`AgentTool.tsx:L81-L155`](https://github.com/ChinaSiro/claude-code-sourcemap/blob/a8a678cb6244e6770e1e421767ff0987a1d95549/restored-src/src/tools/AgentTool/AgentTool.tsx#L81-L155) |
| 启动前校验 | 运行前解析 profile/fork，未知定义和非法递归条件直接失败 | [`AgentTool.tsx:L318-L356`](https://github.com/ChinaSiro/claude-code-sourcemap/blob/a8a678cb6244e6770e1e421767ff0987a1d95549/restored-src/src/tools/AgentTool/AgentTool.tsx#L318-L356) |
| Fresh 与 fork | 普通 profile 使用自身 system prompt 和委派 prompt；fork 才复制父 context | [`AgentTool.tsx:L483-L541`](https://github.com/ChinaSiro/claude-code-sourcemap/blob/a8a678cb6244e6770e1e421767ff0987a1d95549/restored-src/src/tools/AgentTool/AgentTool.tsx#L483-L541) |
| 真实能力裁剪 | worker 工具池独立组装，allow/deny 和系统禁用集合在运行时过滤工具 | [`AgentTool.tsx:L555-L635`](https://github.com/ChinaSiro/claude-code-sourcemap/blob/a8a678cb6244e6770e1e421767ff0987a1d95549/restored-src/src/tools/AgentTool/AgentTool.tsx#L555-L635)、[`agentToolUtils.ts:L70-L225`](https://github.com/ChinaSiro/claude-code-sourcemap/blob/a8a678cb6244e6770e1e421767ff0987a1d95549/restored-src/src/tools/AgentTool/agentToolUtils.ts#L70-L225) |
| 同构执行循环 | `runAgent` 接收 messages、tools、permissions、max turns、worktree 和 progress，在独立上下文复用 query/tool loop | [`runAgent.ts:L248-L329`](https://github.com/ChinaSiro/claude-code-sourcemap/blob/a8a678cb6244e6770e1e421767ff0987a1d95549/restored-src/src/tools/AgentTool/runAgent.ts#L248-L329)、[`L347-L528`](https://github.com/ChinaSiro/claude-code-sourcemap/blob/a8a678cb6244e6770e1e421767ff0987a1d95549/restored-src/src/tools/AgentTool/runAgent.ts#L347-L528) |
| 前后台取消 | foreground 绑定父取消；background 使用自己的 abort controller，之后由 task stop 管理 | [`runAgent.ts:L520-L528`](https://github.com/ChinaSiro/claude-code-sourcemap/blob/a8a678cb6244e6770e1e421767ff0987a1d95549/restored-src/src/tools/AgentTool/runAgent.ts#L520-L528)、[`AgentTool.tsx:L686-L764`](https://github.com/ChinaSiro/claude-code-sourcemap/blob/a8a678cb6244e6770e1e421767ff0987a1d95549/restored-src/src/tools/AgentTool/AgentTool.tsx#L686-L764) |
| 任务与 steer | task 保存 ID、model、abort、status、result/progress、pending messages 和 transcript；follow-up 在安全边界消费 | [`LocalAgentTask.tsx:L116-L191`](https://github.com/ChinaSiro/claude-code-sourcemap/blob/a8a678cb6244e6770e1e421767ff0987a1d95549/restored-src/src/tasks/LocalAgentTask/LocalAgentTask.tsx#L116-L191) |
| 一次性完成通知 | completion 携带 notified 状态、result、usage 和 worktree 信息，避免同一任务重复通知 | [`LocalAgentTask.tsx:L197-L262`](https://github.com/ChinaSiro/claude-code-sourcemap/blob/a8a678cb6244e6770e1e421767ff0987a1d95549/restored-src/src/tasks/LocalAgentTask/LocalAgentTask.tsx#L197-L262) |
| Stop | 校验 task 存在且运行后，通过受管任务触发停止 | [`TaskStopTool.ts:L60-L129`](https://github.com/ChinaSiro/claude-code-sourcemap/blob/a8a678cb6244e6770e1e421767ff0987a1d95549/restored-src/src/tools/TaskStopTool/TaskStopTool.ts#L60-L129) |
| Wait/Get | `TaskOutput` 支持非阻塞查询和有界等待，并返回清理后的结果而非原始 JSONL | [`TaskOutputTool.tsx:L30-L143`](https://github.com/ChinaSiro/claude-code-sourcemap/blob/a8a678cb6244e6770e1e421767ff0987a1d95549/restored-src/src/tools/TaskOutputTool/TaskOutputTool.tsx#L30-L143)、[`L208-L281`](https://github.com/ChinaSiro/claude-code-sourcemap/blob/a8a678cb6244e6770e1e421767ff0987a1d95549/restored-src/src/tools/TaskOutputTool/TaskOutputTool.tsx#L208-L281) |
| Resume | 从 transcript/meta 过滤不可恢复消息，重建 context/tools 并开始新的执行 | [`resumeAgent.ts:L42-L112`](https://github.com/ChinaSiro/claude-code-sourcemap/blob/a8a678cb6244e6770e1e421767ff0987a1d95549/restored-src/src/tools/AgentTool/resumeAgent.ts#L42-L112)、[`L158-L264`](https://github.com/ChinaSiro/claude-code-sourcemap/blob/a8a678cb6244e6770e1e421767ff0987a1d95549/restored-src/src/tools/AgentTool/resumeAgent.ts#L158-L264) |
| Worktree 收口 | 无变更 worktree 清理；有变更则保留并返回 path/branch | [`AgentTool.tsx:L643-L685`](https://github.com/ChinaSiro/claude-code-sourcemap/blob/a8a678cb6244e6770e1e421767ff0987a1d95549/restored-src/src/tools/AgentTool/AgentTool.tsx#L643-L685) |
| 结果 envelope | 结果包含 agent ID/type、正文、工具次数、耗时和 tokens/usage | [`agentToolUtils.ts:L227-L356`](https://github.com/ChinaSiro/claude-code-sourcemap/blob/a8a678cb6244e6770e1e421767ff0987a1d95549/restored-src/src/tools/AgentTool/agentToolUtils.ts#L227-L356) |

## 两个容易误读的版本边界

第一，fresh child 仍会得到项目环境、profile system prompt 和显式任务内容；它只是不继承父对话。fork 才复制父 transcript，而且复制后父子完全分离。

第二，快照里存在 feature-gated 的 fork/team/递归相关字段，不等于普通 Agent 默认启用这些能力。2.1.88 外部构建的常规 child 工具过滤明确移除 `Agent`。yucode 因此不实现嵌套，也不提供深度配置或 orchestrator profile。

## Playbook 对实现的补充

[多 Agent 委派与 Handoff 控制平面](https://meko1.github.io/llm-interview-guide/interview/multi-agent-delegation-handoff-playbook) 用于校准控制面，而不是证明 Claude Code 的内部代码。它带来的有效约束是：

- 委派 prompt 必须是带目标、约束和验收条件的任务契约。
- child capability 必须是 parent capability 的子集。
- context、workspace、credential、effect 和生命周期是不同边界。
- 多个结果必须由父 Agent fan-in 和验收，不能把 child 自报完成当作事实。
- terminal task 不能原地倒流回 running；继续执行应创建新的 attempt。
- 取消、退出和失败都必须清理资源并保存可恢复事实。

playbook 中的远程 worker、fencing token、两阶段 owner handoff、peer mailbox 和分布式 reconciliation 不适合本地单进程 yucode，也不在本功能范围内。

## 映射到 yucode

| 研究结论 | yucode 实现 |
| --- | --- |
| schema 与执行能力必须一致 | session-scoped `ToolCatalog` 同时生成 schema、解析调用并驱动执行 |
| child 能力只收缩 | 根目录 ∩ `CHILD_SAFE` ∩ profile allowlist − denylist − 永久禁止集合 |
| 不支持嵌套 | child catalog 没有 `Agent`/`AgentTask`，runtime 再校验 caller identity |
| child 状态独立 | 每个 attempt 建立独立 `Session`、model、tools、MCP、usage、jobs 和 transcript |
| launch 与 completion 分离 | 后台任务先返回 task id；终态以 `pending → delivered → consumed` 投递 |
| resume 是新执行 | 同 task id 增加 immutable attempt，从合法 transcript 和 workspace 恢复 |
| 可写并发需要所有权 | shared writer lease 或独立 Git worktree |
| 后台不能抢占根输入 | `InteractionBroker` 持久化 Ask/approval，TUI 稍后处理 |

## 可信度与 clean-room 边界

该仓库自述文件说明恢复内容来自 npm source map 的 `sourcesContent`，同时说明自己非官方且目录结构不保证等同原仓库（[README L5-L22](https://github.com/ChinaSiro/claude-code-sourcemap/blob/a8a678cb6244e6770e1e421767ff0987a1d95549/README.md#L5-L22)）。因此：

- 固定快照用于验证机制形状，不作为依赖或许可证来源。
- yucode 的实现名称、数据结构、提示词和代码均独立编写。
- 最终正确性由 yucode 自身的不变量和测试证明，而不是“源码也这样做”。

详细接口和运行规则见 [子 Agent 功能](subagent-design.md)。
