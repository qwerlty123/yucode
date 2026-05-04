# 命令

在提示符处输入 `/` 命令,即可检查状态、切换模型、管理会话,或随时配置运行时
行为。运行 `/help` 查看内置参考。

## 查看状态

**`/status`** —— 一览运行时的一切:工作区路径、会话 id、当前 provider 与模型、
计算出的上下文压缩预算填充百分比、对话历史、prompt-cache 命中率、代码索引状态、
后台任务,以及是否有可用更新。

```{figure} ../snapshots/yucode-status-command.png
:alt: /status 命令显示工作区、会话、provider、上下文和代码索引状态
:width: 600px
:align: center

一目了然的 /status 输出。
```

**`/diff`** —— 查看最近一个回合或整个会话的改动。参见
[查看改动](usage.md#reviewing-changes)。

<div class="term-shot" role="img" aria-label="The diff viewer: a Latest and Session tab above a list of changed files, each with added and removed line counts, and a key hint along the bottom."><span><span class="fs-i fs-tab-on"> Latest </span><span class="fs-i fs-dim"> │ </span><span class="fs-i fs-tab-off"> Session </span></span><span> </span><span class="fs-sel">&gt; <span class="fs-i fs-add">+45</span> <span class="fs-i fs-del">-12</span> docs/usage.md</span><span class="fs-dim">  <span class="fs-i fs-add">+12</span> <span class="fs-i fs-del">- 3</span> yucode.py</span><span class="fs-dim">  <span class="fs-i fs-add">+ 4</span> <span class="fs-i fs-del">- 0</span> tests/test_mcp.py</span><span> </span><span class="fs-dim">  [list] ↑/↓ or j/k move · ←/→ or h/l tab · Enter open · r refresh · Esc/q close [1/3]</span></div>

两个标签页决定查看范围;每一行是一个被改动的文件,含新增与删除的行数。
按 `Enter` 打开所选文件的 diff。

**`/ps`** —— 列出活动中的后台任务(参见 [工具](tools.md#built-in-tools))。
每行显示任务 id、状态、命令与已运行时间。

**`/skills`** —— 按名称、来源和描述列出所有已安装的 [技能](skills.md)。

**`/config`** —— 显示活动配置:provider 块、运行时设置及其解析后的值。

## 代码索引

**`/index [force]`** —— 构建或重建驱动 `InspectCode` 的代码符号索引。首次构建
会遍历每个源文件;之后的同步很快。添加 `force` 可从零重建。详见
[代码符号索引](tools.md#code-symbol-index)。

## 切换模型

**`/provider [NAME]`** —— 显示或切换当前 provider。不带参数时列出所有已配置的
provider(参见 [配置](configuration.md#providers)),并可交互式选择;带名称则立即
切换。

**`/model [MODEL]`** —— 显示或切换当前 provider 的模型。不带参数时打开交互式
选择器,列出已配置与已发现的模型。切换模型时还会提示选择推理级别。

**`/reason [EFFORT]`** —— 显示或设置推理级别。取值:`off`、`minimal`、
`low`、`medium`、`high`、`xhigh`、`max`。yucode 会将已知模型家族映射到
最接近的支持级别;无法识别的 provider 与模型保持所选值。不带参数时打开选择器。

```{figure} ../snapshots/yucode-demo-switching-providers-models.gif
:alt: 在会话中交互式切换 provider 和模型
:width: 600px
:align: center

会话中途切换 provider 与模型。
```

## 会话管理

**`/name [TEXT]`** —— 显示或设置本会话的名称。参见 [名称](usage.md#names)。

**`/sessions [all]`** —— 浏览已保存的会话并重新进入其中某个;`/resume` 是同一
命令。参见 [切换会话](usage.md#switching-sessions)。

**`/compact`** —— 立即总结并压缩对话。yucode 会自动把长会话维持在预算之内,
但 `/compact` 可随时手动裁剪。

**`/yolo`** —— 切换确认提示。在永久关闭前请先阅读 [安全](safety.md)。

**`/hints`** —— 切换闲置提示符处的模型建议下一步提示芯片(`NextHints` 工具)。
参见 [工具](tools.md)。

**`/strict`** —— 切换严格工具调用 schema(OpenAI / DeepSeek)。

**`/api [API]`** —— 选择或设置用于访问模型的请求协议(`auto`、`chat`、
`responses`、`anthropic`)。`/provider` 与 `/model` 也会在其选择链中确认协议,
因为正确的协议取决于你刚选中的模型。

一个端点若服务多个模型家族,往往在不同的协议上暴露它们;OpenAI 兼容的
`/models` 列表也不会说明哪个协议服务于哪个模型——因此 `/model` 提供的模型仍
可能被判定为不受支持。遇到这种情况,用 `/api` 换一个协议(或用 `auto` 从 URL
与模型重新推断)。回复会指明生效的协议,而历史与协议无关,所以会话中途切换是
安全的。

**`/set KEY VALUE`** —— 为会话设置 `provider.*` 或 `runtime.*`;键与(取值为
固定集合时的)值均支持 Tab 补全。示例:`/set provider.response_timeout 900`。

**`/resend`** —— 取消并重新发送进行中的模型请求,无需重启当前回合。在模型
请求等待时输入即可;当代理正在运行工具或处于两次模型调用之间时无效。分隔线
会短暂显示重试,保留其计时器与等待脉冲,然后为替代请求回到 `working`。自动
重试同样显示尝试次数与简要原因,如 `retrying 2/6 · timeout`,随后在该请求
继续时显示 `working · attempt 2/6`:

<div class="term-shot" role="img" aria-label="The running divider briefly changes from working to retrying while preserving its green waiting pulse and elapsed timer, then returns to working as the replacement model request continues."><span class="fs-divider">──── <span class="fs-i fs-add">●</span> working (11s) ────────────────────</span><span class="fs-prompt">+&gt; /resend</span><span class="fs-divider">──── <span class="fs-i fs-add">●</span> retrying (12s) ──────────────────</span><span class="fs-divider">──── <span class="fs-i fs-add">●</span> working (14s) ────────────────────</span></div>

## MCP

**`/mcp`** —— 管理 [MCP](mcp.md) 服务器连接。子命令:

| 用法 | 效果 |
|---|---|
| `/mcp` | 列出服务器与连接状态 |
| `/mcp connect <server> [server ...]` | 立即连接服务器 |
| `/mcp disconnect <server>` | 断开某个服务器 |
| `/mcp tools [server]` | 列出已连接服务器的工具 |

## 帮助与退出

**`/help`** —— 显示内置的命令与工具参考。

**`/exit`, `/quit`** —— 退出 yucode。会话会自动保存,可用 `-c` 或 `--resume`
恢复。
