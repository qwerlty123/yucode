# 上下文与缓存

每个请求包含的内容不止最新一条消息。yucode 先把稳定的会话上下文放在前面，再发送一份只追加（append-only）的对话记录。这既能让 agent 保持信息充分，又能让受支持的 provider 精确复用之前用户与工具之间的边界。

## 模型收到的内容

<div class="term-shot" role="img" aria-label="从首到尾的消息上下文：系统指令、含会话启动时间的项目环境、可选的项目记忆、技能与 MCP 索引，然后是一份只追加的对话，包含用户消息、助手回复、工具结果、Note 状态变更、恢复事件以及偶尔出现的上下文压缩检查点。"><span class="fs-goal">─ 稳定会话前缀 ─────────────────────────────</span><span>  系统指令      <span class="fs-i fs-dim">agent 应如何工作</span></span><span>  项目环境      <span class="fs-i fs-dim">目录 · 本地启动时间 · 操作系统 · shell</span></span><span>  项目记忆、技能与 MCP 索引   <span class="fs-i fs-dim">仅在可用时</span></span><span class="fs-goal">─ 只追加对话 ──────────────────────────</span><span>  用户 · 助手 · 工具 <span class="fs-i fs-dim">常规回合历史</span></span><span>  Note 调用与结果   <span class="fs-i fs-dim">目标 · 计划 · 事实 · 检查</span></span><span>  生命周期事件         <span class="fs-i fs-dim">按用户本地时区的恢复时间</span></span><span>  当前回合             <span class="fs-i fs-dim">始终最后追加</span></span></div>

环境只在会话开始时记录一次启动时间，格式为带数值时区偏移的本地 ISO 时间戳，例如 `2026-07-30T20:34:56+08:00`。恢复会话时会追加一条携带新本地时间的用户角色生命周期事件。这些时间戳用户和模型都可以直接读取；无需进行 UTC 转换，后续请求中也不会插入不断变化的日期块。

工具定义会与消息堆栈一并发送：内置工具、安装了技能时的 `Skill`，以及来自<span class="marker">当前已连接服务器</span>的 MCP tools 与 resources。已配置但未连接的服务器不会占用上下文。

若项目已有跨 session 记忆，yucode 会在一个 cache generation 的第一次投影时生成一份有界 topic 索引。普通回合中该快照保持不变，因此不会让每轮请求的缓存前缀漂移；当前 session 新写入的记忆通过 `Memory` 工具结果可见。成功 compaction 已经替换旧前缀并开启新的 cache generation，此时会一次性清除内存索引快照，让下一次投影从磁盘加载最新索引。记忆正文不会常驻上下文，agent 用 `Memory get/search` 按需读取。

当模型暴露 reasoning 时，yucode 会把返回的协议数据保存在会话中，但只重放当前 provider 期望的内容。某些 API 要求跨回合保留 reasoning；另一些则会忽略旧的 reasoning（除非启用了 preserved-thinking 选项），但多步工具调用内部仍然需要它。当协议要求时，不透明签名和加密 reasoning 会原样返回，但不会作为模型可读文本展示。

启用 [provider 侧搜索](tools.md#provider-side-tools) 后，它所读取的页面由 provider 而非 yucode 加入上下文，因此不会被缩短，上下文填充度也无法预先计算它们：带搜索的回合会比不带搜索的回合更大，而且大多数 provider 会在 token 费用之外另行收取搜索费用。

## 保持上下文可控

大型工具结果在进入对话之前会被缩短；agent 之后可以随时通过 `Recall` 取回完整结果。重复出现的技能指令和 MCP 描述会被替换为对首次完整副本的引用，而不是再次完整发送。

### 上下文压缩

当估算的请求接近 `runtime.max_context_tokens` 时，yucode 会**压缩上下文**：对话中较早的部分被替换为一份简短摘要，最近的消息保持原样。估算基于有效的 Chat、Responses 或 Anthropic 请求，因此 provider 将要丢弃的 reasoning 不会导致过早压缩。触发时会预留配置的 provider 输出上限（未指定时为 16K）、工具 schema，以及至少 4K 的安全余量。会话在同一回合内继续，长任务无需中断。

状态栏和 `/status` 中显示的上下文填充度是 provider 报告的上一请求 token 数；压缩仍以估算值为触发依据，该估算预判的是下一个请求。

活动上下文中的检查点摘要是有损的，但每次压缩还会把被移除消息的一段有界逐字摘录保存为**历史段（history segment）**。只追加会话日志中更早的快照仍然是权威的冷数据来源，压缩不会改写它们。

<div class="term-shot" role="img" aria-label="上下文压缩把较早的活动对话替换为单个检查点，其中包含摘要、完整工作状态和段指针。RecallContext 可以列出、搜索并取回有界的逐字摘录，而只追加会话日志保留更早的快照作为冷数据来源。"><span class="fs-goal">─ 活动上下文（热） ────────────────</span><span>  检查点       <span class="fs-i fs-dim">摘要 · 目标 · 计划 · 事实 · 检查 · seg.N</span></span><span>  最近的消息  <span class="fs-i fs-dim">保持原样</span></span><span class="fs-dim">─ 可召回段（温） ──────────</span><span>  seg.1 · seg.2    <span class="fs-i fs-dim">仅在需要时列出/搜索</span></span><span class="fs-dim">─ 只追加会话日志（冷） ──────</span><span>  更早的快照<span class="fs-i fs-dim"> 原始消息</span></span><span> </span><span class="fs-dim"><span class="fs-i fs-goal">RecallContext(list/search/get)</span> 查找摘录</span></div>

段标题不会出现在每个请求中。agent 用 `RecallContext` 按最新优先列出它们、对存储的标题和文本进行正则搜索，或取回选定的 `seg.N` 摘录。`Note` 可以查看当前的目标、计划、事实和检查；更新会保留在它们原有的工具调用历史中，直到压缩检查点整合出完整的当前状态。

运行 `/compact` 可以立即压缩，而不必等待阈值触发，例如在开始大规模重构之前。`/status` 会报告会话已经进行过多少次压缩。

## 提示缓存

提示缓存让 provider 复用请求中未变化开头的计算。下一个请求通常以相同的指令、环境、工具和较早对话开头，因此只需处理新追加的尾部。Note 更新和恢复事件会追加到对话末尾，不会移动较早的断点。一次压缩会有意开启一个新的缓存纪元（cache epoch）。连接 MCP server、变更已安装的技能、切换模型，或以其他方式改动靠前的部分，都可能降低下一个请求的缓存命中率。

<div class="term-shot" role="img" aria-label="两个请求条。两者都以相同的长阴影前缀开头，provider 会复用该前缀；每个请求只有较短的新尾部会被重新处理。"><span>上一个  <span class="fs-i fs-goal">████████████████████████</span><span class="fs-i fs-dim">░░░░░░</span></span><span>下一个      <span class="fs-i fs-goal">████████████████████████</span><span class="fs-i fs-dim">░░░░░░░░░░</span></span><span> </span><span class="fs-dim">          <span class="fs-i fs-goal">█</span> 复用的前缀    ░ 重新处理</span></div>

请求只会被复用到第一个差异点为止，因此靠近开头的变化——连接 MCP server、安装技能、切换模型、启用 provider 侧工具——都会缩短可复用的前缀。这正是稳定部分被放在前面的原因。

OpenAI 兼容的 provider 可能会自动复用匹配的前缀；在 provider 支持的情况下，yucode 会提供一个稳定的缓存键（cache key）。对于 Anthropic，yucode 会明确把工具和系统指令标记为可缓存的临时前缀（ephemeral cacheable prefix）。各 provider 的支持程度和计费方式有所不同。

### 查看命中率

`/status` 报告整个会话和最新请求的缓存读取（cache-read）token，并在 provider 暴露时报告缓存写入（cache-write）token：

<div class="term-shot" role="img" aria-label="/status 的 usage 行，显示调用次数、token 总数、整个会话的缓存 token，以及最新请求的缓存 token。"><span><span class="fs-i fs-dim">usage</span>  调用 14; 总计 182304; 缓存 <span class="fs-i fs-add">148992/162880</span> (91.5%); 最近 <span class="fs-i fs-sel">21120/22016</span> (95.9%)</span><span class="fs-dim">                                  └─ 整个会话 ─┘        └─ 最新请求 ─┘</span></div>

最新请求的命中率还会实时显示在状态栏中，位于上下文填充度旁边，并随每次响应更新。

该比例因 provider、模型、提示长度和对话内容而异。当请求前缀对齐时，它<span class="marker">可以达到 90–99%</span>。这是观测结果，并非保证的比率。
