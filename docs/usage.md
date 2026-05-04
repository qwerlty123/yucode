# 交互

yucode 在你的终端中以对话形式运行。你输入一个请求,代理通过[工具](tools.md)逐步完成它,而你全程参与其中——引导、回答问题、审查改动。

(follow-ups)=
## 后续消息

你可以在 yucode 工作时继续输入。如果随后有新的模型步骤开始,已提交的后续消息会并入当前任务;否则它将成为下一个任务。仍留在编辑器中的草稿绝不会因中断而被提交——第一次 `Ctrl-C` 只会丢弃它。

<div class="term-shot" role="img" aria-label="Terminal view: yucode is working on a request while two follow-up messages wait below a divider reading 'working, 2 queued'."><span class="fs-user">• refactor the MCP manager</span><span class="fs-tool">  Read yucode.py</span><span class="fs-tool">  Edit yucode.py</span><span class="fs-divider">──── working (12s) [ 2 queued ] ─────────────</span><span class="fs-queued">+ also update the tests</span><span class="fs-queued">+ and bump the version</span><span class="fs-prompt">&gt; <span class="fs-caret">▏</span></span><span class="fs-hint">  ↑ recalls queued · Ctrl-C interrupts</span></div>

分隔线下方的 `+` 表示正在等待下一个模型步骤。在那个边界处——当前工具调用批次结束后(如果有的话)——所有等待中的后续消息会连同下一个模型请求按顺序一起发送,并作为普通用户消息移到分隔线上方。在该请求成功完成之前,它们在内部仍可重试;请求失败则会把它们移回分隔线下方,作为排队输入。

| 按键 | 时机 | 作用 |
|---|---|---|
| `Enter` | 代理工作时 | 排队一条后续消息,等待下一个模型步骤 |
| `Ctrl-C` | 代理工作时 | 丢弃编辑器中的草稿;编辑器为空时中断任务——代理尚未作答则撤回消息,已作答则记录中断 |
| `Ctrl-C` | 空闲提示符 | 清空输入行 |
| `Ctrl-U` | 任意提示符 | 清空整个输入行,回合继续运行 |
| `Up` / `Ctrl-P` | 工作时且编辑器为空 | 召回最新一条排队消息 |

中断分为两种情况。如果代理尚未作答,`Ctrl-C` 会*撤回*消息:它被丢弃,永远不会进入对话记录或已保存的会话,就像从未发送过一样(你的输入历史仍可用 `Ctrl-P` 召回它)。一旦代理已作答或运行过工具,`Ctrl-C` 就是*中断*:已经展示的工作保留,回合被标记为已中断,让 yucode 知道它提前结束了。

## 流式模型输出

模型输出默认在交互终端中流式显示,适用于 OpenAI 兼容的 Chat Completions、OpenAI Responses 和 Anthropic Messages。文本到达时出现在分隔线上方:模型暴露推理时用 `thinking`,回答用 `responding`。

<div class="term-shot" role="img" aria-label="Terminal view: streamed reasoning appears above a divider labeled thinking, followed by streamed answer text and a divider labeled responding."><span class="fs-dim">  thinking</span><span class="fs-output">    I should inspect the existing implementation first.</span><span class="fs-divider">──── thinking (4s) ─────────────────────</span><span class="fs-dim">  responding</span><span class="fs-output">    I found the issue in the request path.</span><span class="fs-divider">──── responding (7s) ───────────────────</span></div>

实时文本是有上限的预览,不是第二条对话记录。完成后它会清空,最终回答在正常的 Rich 记录中渲染一次。工具调用参数在调用完成前一直缓冲,因此不完整的 JSON 绝不会作为面向用户的输出出现。常规情况下无需任何流式设置;拒绝流式的端点可以通过 `provider.stream = false` 或 `/set provider.stream off` 禁用。

## Bash 输出

Bash 运行时,其实时输出停留在 `working` 分隔线上方。命令运行期间,一个空行会把最后一行实时输出与那条分隔线隔开。命令结束后,yucode 在记录中为每个输出流保留至多三行。灰色的 `output · Ctrl-O for more` 行会打开一个更大的、有上限的预览:按 `Ctrl-O` 浏览最近十个已完成的 Bash 预览,最新的在前。用 `j`/`k` 或方向键选择,`Enter` 打开;`Esc` 返回列表,`Ctrl-O` 或 `q` 关闭查看器。完整结果仍存储在它的 `tr.N` 键下。每个查看器画面都会在其低调的带标签分隔线上方留一个空行,与终端滚动历史隔开。

<div class="term-shot" role="img" aria-label="A completed Bash command with bounded output, followed by the Ctrl-O list of recent Bash commands and one larger output preview, each separated from scrollback by a labeled rule."><span class="fs-tool">  Bash  pytest -q</span><span class="fs-dim">    ├ output · 14.7s Ctrl-O for more</span><span class="fs-dim">    │ stdout:</span><span class="fs-output">    │   708 passed in 14.84s</span><span class="fs-dim">    └ stored tr.18</span><span> </span><span class="fs-divider">──── Bash outputs · latest 3 ───────────────</span><span class="fs-sel">&gt;  1. tr.18  Bash pytest -q</span><span class="fs-dim">   2. tr.17  Bash git diff --check</span><span class="fs-dim">   3. tr.16  Bash git status --short</span><span> </span><span class="fs-divider">──── Bash output · tr.18 ──────────────────</span><span class="fs-dim">  Bash pytest -q</span><span class="fs-dim"> </span><span class="fs-dim">  stdout:</span><span class="fs-dim">    708 passed in 14.84s</span><span class="fs-dim"> </span><span class="fs-dim">  Esc / ← back · Ctrl-O / q closes</span></div>

## 状态栏

提示符下方的一行概括了运行时状态。空闲时它显示稳态:当前供应商与模型、推理等级、上下文占用和最新一次请求的缓存命中率(合并显示为 `ctx 23% · cache 98%`)、代码索引状态,以及任何后台任务。MCP 与技能计数、更新通知在相关时出现。缓存比率在第一次请求后显示,并随每次响应刷新,`/status` 对整个会话报告同样的数字。

代理工作时,角色颜色让位于一条在整行上滚动的蓝紫渐变,实时计数器随之加入:重试或尝试提示,以及 `step N/M` 计数器——当回合进入 `max_agent_steps` 的最后五分之一时出现,表示回合即将被截断。

提示符上方的 `working` 分隔线标明当前阶段——`thinking`、`responding`,或当[供应商侧工具](tools.md#provider-side-tools)在请求内部运行时的 `web search`。

<div class="term-shot" role="img" aria-label="The status bar in two states. At rest: provider and model, reasoning, context fill with the cache ratio, and index, each in its role color. While working: the same line rendered as a blue-to-purple sweep with a bright band, plus a step counter near the cap."><span><span class="fs-i fs-dim">idle    </span><span class="fs-i sb-base">dashscope/qwen3.7-plus</span><span class="fs-i sb-sep"> | </span><span class="fs-i sb-reason">high</span><span class="fs-i sb-sep"> | </span><span class="fs-i sb-ctx">ctx 23% · cache 98%</span><span class="fs-i sb-sep"> | </span><span class="fs-i sb-index">index ✓</span></span><span><span class="fs-i fs-dim">working </span><span class="fs-i sb-sweep-a">dashscope/qwen3.7-plus | high | </span><span class="fs-i sb-sweep-hi">ctx 41% · cache 95%</span><span class="fs-i sb-sweep-b"> | index ✓ | step 160/200</span></span></div>

## 命令

在提示符处输入 `/` 命令来检查状态、切换模型、管理会话,或即时调整运行时行为。完整列表见[命令参考](commands.md),或在会话中运行 `/help`。

## 提及

两种行内引用,输入时都会 Tab 补全:

- `@server` 或 `@server.tool`——按需连接 [MCP](mcp.md) 服务器,并把代理指向该服务器或工具。<span class="marker">连接在你断开之前一直保持活跃。</span>
- `$skill`——把[技能](skills.md)的完整说明注入当前回合。

## 按键与输入编辑

**交互式选择器**(模型选择器、MCP 管理器、diff 查看器)支持:

- `j` / `k` 或方向键移动
- `g` / `G` 跳到顶部 / 底部
- `/` 搜索,`Enter` 接受,`Esc` 取消

**输入行**支持:

- 历史召回与补全
- `Ctrl-C`——清空当前输入;运行时输入为空则中断回合(代理尚未作答则撤回)
- `Ctrl-U`——清空整个输入行,空闲提示符和后续消息编辑器都一样
- `Ctrl-D`——从空提示符退出
- `Ctrl-R`——反向搜索历史
- `Ctrl-O`——浏览最近十个已完成的 Bash 输出预览;再按一次关闭
- `Ctrl-X Ctrl-E` 或 `Ctrl-G`——在 `$VISUAL` / `$EDITOR` 中编辑当前输入(回退到 vim)

```{figure} ../snapshots/yucode-working-input-editor.png
:alt: 在外部编辑器中编辑后续消息
:width: 600px
:align: center

在外部编辑器中输入一条后续消息。
```

当你打开编辑器回复代理时,它最近的回复会追加在一条 git 风格的剪刀线下方,这样你撰写时就能看到自己在回复什么(全屏编辑器会隐藏那段滚动历史):

<div class="term-shot" role="img" aria-label="External editor view: the draft being composed on top, a git-style scissors line, then the agent's most recent reply below it for reference; everything below the scissors line is stripped before the message is sent."><span class="fs-user">yes, add the reconnect test and cap the backoff at 30s</span><span class="fs-dim">&nbsp;</span><span class="fs-divider"># ------------------------ &gt;8 ------------------------</span><span class="fs-dim"># Reference only: everything below the scissors line is stripped before your</span><span class="fs-dim"># message is sent. The agent's most recent reply follows for reference.</span><span class="fs-dim">&nbsp;</span><span class="fs-prompt">I split McpManager into StdioTransport and HttpTransport, each closing its own</span><span class="fs-prompt">client in close(). Want me to add a test for the reconnect path?</span></div>

从剪刀线向下的所有内容在消息发送前都会被剥离;你自己输入的剪刀线则原样保留。较长的回复只截取其最近的行。

### 图片输入

将本地已有图片的路径直接粘贴或输入到提示符中。yucode 会把路径替换为类似 `[Image #1 · screenshot.png]` 的行内标签,让你在继续编辑周边文字时,能确切看到将要提交哪些图片。相对路径从工作区解析;带引号的路径和反斜杠转义的空格均可接受。

<div class="term-shot" role="img" aria-label="The input prompt after recognizing a local screenshot path as an editable inline image label."><span class="fs-prompt">&gt; explain <span class="fs-i fs-sel">[Image #1 · screenshot.png]</span> and fix the layout<span class="fs-caret">▏</span></span></div>

支持 PNG、JPEG、WebP 和单帧 GIF 文件。yucode 使用所选的标准 API 发送图片。如果供应商明确拒绝图片输入,该结果会在本次会话中被记住,之后的图片提交会被阻止,且不会清空草稿。排队的后续消息、恢复的会话以及禁用了图片输入的供应商,都会保留可读的图片标签。参见 [`provider.image_input`](configuration.md#optional-provider-settings) 以覆盖自动检测。

(sessions)=
## 会话

<span class="marker">你的工作会自动保存</span>——对话、编辑和 diff 都与启动时的项目目录绑定,因此被中断的会话可以从停止处继续。默认情况下,七天未触碰的会话会被移除,在 yucode 启动时于后台清扫;它会报告移除了多少。恢复会话会重置其计时,所以你反复返回的会话永远不会被移除。设置 `runtime.session_retention_days = 0` 可无限期保留。

从命令行恢复:

```sh
yucode -c              # resume the latest session in this project
yucode --resume        # same, explicit
yucode --resume UID    # resume a specific session by id, from any directory
yucode --resume "fd leak"   # or by name, or by the first few characters of an id
```

会话按项目存储,因此 `-c` 和裸 `--resume` 永远不会触及另一个项目的历史——即使你最近的会话在别处。`UID` 会跨所有项目查找,所以你可以从任何位置按 id 恢复会话。

名称或 id 前缀会先在当前项目中搜索,然后才是所有项目——因此移动目录后,你仍可以按名称恢复会话。当查询匹配到多个会话时,yucode 会<span class="marker">列出候选而不是猜测</span>。

恢复时,对话会重放到你的滚动历史中,包括每次编辑产生的 diff。长 diff 在那里会被裁剪;`/diff` 始终保留完整文本。

(names)=
### 名称

每个会话都有名称,便于日后辨认。它最初是你输入的第一行,在代理有了当前目标后变为该目标,并会一直保持你通过 `/name` 设置的值:

```text
/name                 # show the current name and where it came from
/name auth refactor   # set your own; nothing overwrites it afterwards
```

名称只是标签,不是身份——多个会话可以共享同一名称,id 才是每个会话唯一性的来源。名称在决定时一次性确定,不会从对话中重新读取,所以昨天在某个名称下找到的会话,今天仍在同一名称下——即使它的早期消息已被压缩掉。

(switching-sessions)=
### 切换会话

`/sessions`——或等价的 `/resume`——列出已保存的会话,最新在前,并显示每个会话多久前被触碰以及运行了多少轮。输入可跨名称和开头行过滤,按 Enter 重新进入其中一个:

<div class="term-shot" role="img" aria-label="The session picker: a searchable list of saved sessions, each showing its name, age, and round count, with the current session marked, above a preview of the highlighted session's id, opening message, and directory."><span class="fs-divider">──── Sessions ─────────────────────────────</span><span class="fs-sel">&gt; port the tool runner to asyncio<span class="fs-i fs-dim">  ·  2h ago · 14 rounds</span></span><span class="fs-dim">  split the large test modules<span class="fs-i fs-dim">  ·  yesterday · 31 rounds</span></span><span class="fs-dim">  fix the fd leak in MCPFileTokenStore<span class="fs-i fs-dim">  ·  3d ago · 1 round</span></span><span class="fs-dim">  what I am doing right now<span class="fs-i fs-dim">  ·  just now · 2 rounds · current</span></span><span> </span><span class="fs-dim">  uid   20260728074943-e22e69e8-070</span><span class="fs-dim">  start port the tool runner to asyncio, starting with Bash</span><span class="fs-dim">  where ~/dev/github/yucode</span><span> </span><span class="fs-hint">  ↑/↓ or j/k move · / search · Enter open · Esc close</span></div>

`/sessions all` 将列表扩展到当前项目之外,并在每行加上该会话的目录。选择某个会话会结束当前会话——它先被保存——然后在其位置启动下一个,与用 `--resume` 启动完全一样。选择当前所在的会话,或按 `Esc`,不会有任何变化。

在回合之间从提示符运行它。代理工作时,yucode 会说明原因并请你先按 `Ctrl-C`:在回合中途切换会话会放弃一个已经在途的请求。

在名称功能出现之前保存的会话,会以其 id 列出,直到下次保存为止。

(reviewing-changes)=
### 查看改动

`/diff` 打开一个交互式、带选项卡的查看器,包含两个视图:

- **Latest**——最近一轮请求期间发生的变化
- **Session**——自会话开始以来所有内容的净 diff

用 `j`/`k`、`g`/`G` 和 `/` 搜索导航;按 `Esc` 关闭。

```{figure} ../snapshots/yucode-diff-list.png
:alt: Interactive diff list showing changed files from the latest turn
:width: 600px
:align: center

选择一个要 diff 的文件。
```

```{figure} ../snapshots/yucode-diff-file-detail.png
:alt: Side-by-side file diff with syntax highlighting
:width: 600px
:align: center

已修改文件的并排详情视图。
```

### 长会话

yucode 会自行把长对话保持在可用的工作预算内,按需总结较旧的上下文,让会话可以无限期运行。运行 `/compact` 立即裁剪,或 `/status` 查看当前的上下文与 token 用量。
