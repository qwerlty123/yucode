# 配置

yucode 默认读取单个 TOML 文件 `~/.yucode/config.toml`。可用 `yucode --init-config`
生成带注释的初始配置,或用 `--config <path>` 指向其他文件。

<span class="marker">只有 `[provider]` 块是必需的。</span> 其他所有键都回退到
内置默认值,因此最小配置只需一个 provider。随时可用 `/config` 检查解析后的配置。

(providers)=
## Provider

yucode 支持 OpenAI 兼容的 Chat Completions 与 Responses API,以及 Anthropic
Messages API。定义一个或多个 `[provider.<name>]` 块,并用 `[provider] active`
选择其中一个:

```toml
[provider]
active = "default"

[provider.default]
url = "https://api.deepseek.com"
key = "sk-..."
model = "deepseek-v4-flash"
```

对大多数端点来说,这三个字段就足够了。yucode 会选用常规协议,只应用必要且有
文档说明的兼容性调整。显式设置始终优先。用 `/config` 检查结果。

定义更多块即可使用更多 provider。用 `/provider [NAME]` 在它们之间切换,用
`/model [MODEL]` 切换当前模型。

### API 协议

除非你的端点需要显式协议,否则保持 `api = "auto"`:

| 值 | 含义 |
|---|---|
| `auto` | 尽可能推断协议;否则使用 Chat Completions |
| `chat` | OpenAI 兼容的 Chat Completions |
| `responses` | OpenAI 兼容的 Responses |
| `anthropic` | Anthropic 兼容的 Messages |

以 `/chat/completions`、`/responses` 或 `/messages` 结尾的 URL 也会选择相应
协议。

(optional-provider-settings)=
### 可选的 provider 设置

大多数用户无需设置这些项。

| 键 | 默认值 | 含义 |
|---|---|---|
| `api` | `auto` | 上文所示的 API 协议 |
| `stream` | `true` | 流式输出模型内容;拒绝流式或 Chat `stream_options` 的端点请关闭 |
| `image_input` | `auto` | 图片能力:自动学习、强制设为 `on`,或用 `off` 禁用 |
| `reasoning` | `medium` | 推理级别:`off`、`minimal`、`low`、`medium`、`high`、`xhigh` 或 `max`;会话中可用 `/reason` 修改 |
| `available_models` | — | `/model` 显示的额外模型 |
| `temperature` | — | 采样温度;默认省略 |
| `max_tokens` | `0` | 每次模型请求的输出 token 上限(含推理);`0` 表示交由 provider 决定(Anthropic 会发送保守的 8K)。16K 仍从输入预算中为回答预留,与 `max_context_tokens` 一对一扣减 |
| `timeout` | `120` | 传输不活动超时(秒) |
| `response_timeout` | `600` | 总生成时长上限(秒);`0` 表示禁用 |
| `prompt_cache_key` | `auto` | 稳定的 prompt-cache 键;设为 `off` 可省略 |
| `strict_tools` | `false` | 在支持处请求严格函数 schema;可用 `/strict` 切换 |
| `extra_body` | `{}` | OpenAI 兼容请求体的额外字段 |
| `builtin_tools` | `[]` | provider 自行运行的工具,原样透传;见下文 |
| `chat_reasoning` | `auto` | 特定 provider 的 Chat 推理格式;通常保持 `auto` |

三种协议默认都启用流式。若兼容端点不支持,可在该 provider 块中设置
`stream = false`,或对当前会话使用 `/set provider.stream off`。

`timeout` 用于发现停止传输数据的连接。流式推理可能让该计时器无限期保持活动,
因此 `response_timeout` 单独将完整模型响应默认限制为十分钟。达到总时限会取消
请求且不自动重试;只有刻意允许无限生成时才设为 `0`。

对于有文档记载的推理约束的 provider/模型组合,yucode 会把所选级别映射到最接近
的接受值。未知的 OpenAI 兼容端点与模型名走通用路径而非白名单;若自动选择有误,
可显式设置 `api` 与 `chat_reasoning`。`/config` 显示解析后的推理级别,`/status`
显示当前模型与 provider 报告的缓存使用情况。

<a id="provider-side-tools"></a>
## Provider 侧工具

有些 provider 可以自行执行网络搜索;参见
[Provider 侧工具](tools.md#provider-side-tools)了解它在会话中的表现。把想要的
工具按 provider 文档的写法列在 `builtin_tools` 中:

```toml
[provider]
url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
model = "qwen3-max"
api = "responses"
builtin_tools = [{ type = "web_search" }, { type = "web_extractor" }]
```

| Provider | 条目 |
|---|---|
| OpenAI (Responses) | `{ type = "web_search" }`,可选带 `search_context_size` 或 `filters` |
| Qwen (Responses) | `{ type = "web_search" }`;也可带 `web_extractor` |
| Anthropic | `{ type = "web_search_20250305", name = "web_search", max_uses = 5 }` |
| Z.AI / BigModel | `{ type = "web_search", web_search = { enable = "True" } }` |
| Kimi / Moonshot | `{ type = "builtin_function", function = { name = "$web_search" } }` |
| OpenRouter | `{ type = "openrouter:web_search" }`;也可带 `openrouter:web_fetch`、`openrouter:datetime` |

有一个 provider 是通过 [`extra_body`](#optional-provider-settings) 在其他地方
配置搜索的:Qwen 的 Chat Completions 端点接受 `enable_search`。DeepSeek 没有
网络搜索。

内置工具只适用于表中所示的 API。若切换到其他 API,yucode 会保留设置但不发送
这些工具;切回来时它们会再次生效。用 `/config` 检查它们是否处于活动状态。若
yucode 报告不支持的条目,请与你的 provider 的示例对比。

使用 `image_input = "auto"` 时,yucode 通过所选的标准 API 发送附件图片。会话
期间,成功的图片请求会为对应的 provider 与模型记下;只有显式的"不支持图片"
响应才会禁用后续的图片提交。若端点的能力已知,可直接设为 `on` 或 `off`。
切换到禁用图片输入的 provider 或模型后,历史图片仍可作为文本标签读取。

## 运行时

可选;省略时使用表中所示的默认值。

| 键 | 默认值 | 含义 |
|---|---|---|
| `yolo` | `false` | 启动时不启用确认提示 |
| `quick_hints` | `true` | 让模型提供可选择的下一步提示芯片;可用 `/hints` 切换 |
| `max_context_tokens` | `262144` (256K) | 使用模型上下文窗口的多少,这决定了自动上下文压缩预算。它是预算而非窗口大小:1M 窗口的模型可调高,较小的模型调低 |
| `max_agent_steps` | `200` | 单个回合内工具步骤的上限 |
| `shell_timeout` | `60` | shell 命令的最大存活时长(秒) |
| `bash_wait_timeout` | `10` | 运行中的命令转为后台任务前的等待时长;`0` 禁用提升 |
| `max_parallel_tools` | `4` | 并发执行的只读工具调用上限;`1` 禁用并行 |
| `session_retention_days` | `7` | 删除这么多天未使用的已保存会话,启动时在后台清扫;`0` 表示永久保留 |
| `theme` | `auto` | 终端主题:`auto`、`light` 或 `dark`;可被 `--theme` 覆盖 |

部分调优值可用 `/set` 为当前会话修改(Tab 补全会列出支持的键)。`/yolo` 切换
`yolo`。

## 数据位置

```toml
[paths]
data_dir = "~/.yucode"   # sessions, input history, OAuth tokens, user skills, update cache
```

会话存放在 `<data_dir>/projects/<project>/` 下,每个工作目录一个目录。每个目录
存放该项目的会话日志与一个 `latest` 指针,因此恢复时会话始终限定在其所属的
项目内。最后一个会话过期后,项目目录会被删除。

每个日志旁都有一个小的 `<uid>.meta.json`,保存会话选择器显示的内容——名称、
开场白、回合数。日志始终是权威来源;删除这个旁路文件只会让该会话在列表中
失去标签。

`<data_dir>/history.txt` 保存跨所有项目的输入历史,可用 Up 与 Ctrl-P 回溯。
其上限为 512 KB,只保留最近的条目。
