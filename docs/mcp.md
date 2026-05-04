# MCP

yucode 可以连接 [Model Context Protocol](https://modelcontextprotocol.io) 服务器，并通过自身的 `MCP` 工具调用服务器上的 tools。服务器可以是**远程**（HTTP）或**本地**（stdio），而且<span class="marker">在你连接服务器之前，模型不会接触到它的任何内容</span>。

## 配置服务器

每个服务器对应一个 `[mcp.<name>]` 配置块。

### 远程（HTTP）

```toml
[mcp.example]
url = "https://example.com/mcp"
bearer_token_env_var = "EXAMPLE_MCP_TOKEN"  # optional: send a bearer token from this env var
# auth = "oauth"                            # optional: use interactive OAuth instead
# auto_connect = true                       # optional: connect at startup (default false)
```

### 本地（stdio）

```toml
[mcp.filesystem]
command = "npx"
args = ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
# env = { FOO = "bar" }   # optional extra environment variables
# auto_connect = true
```

### 选项

| 键 | 适用 | 含义 |
|---|---|---|
| `url` | 远程 | 服务器端点（与 `command` 互斥） |
| `command`, `args` | 本地 | 要启动的程序及其参数 |
| `env` | 本地 | 传给子进程的额外环境变量 |
| `auth = "oauth"` | 远程 | 通过交互式 OAuth 进行身份验证 |
| `bearer_token_env_var` | 远程 | 发送 `Authorization: Bearer <env var>` |
| `env_http_headers` | 远程 | HTTP 头 → 存放其值的环境变量的映射 |
| `auto_connect` | 两者 | 启动时连接而不是按需连接（默认 `false`） |

## 连接

服务器<span class="marker"><strong>默认为手动连接</strong></span>——在连接之前它们一直处于非活动状态，不消耗任何成本。对于你始终需要的服务器，可以设置 `auto_connect = true`。连接方式有：

- **`/mcp`** — 打开交互式管理器，切换某个服务器的开或关。

```{figure} ../snapshots/yucode-mcp-list.png
:alt: MCP 服务器管理器，列出所有已配置服务器及其连接状态
:width: 600px
:align: center

/mcp 交互式服务器管理器。
```
- **`@server`** 在消息中提及 — 按需连接。连接会保持活动，直到你
  断开它。

```{figure} ../snapshots/yucode-mcp-mention.png
:alt: 使用 @server 提及按需连接 MCP 服务器
:width: 600px
:align: center

通过 @-提及按需连接服务器。
```
- **`/mcp connect <server> [server ...]`** / **`/mcp disconnect <server>`** — 终端环境下的备用方式。
- **`/mcp tools [server]`** — 列出已连接服务器上的 tools。

一条命令连接多个服务器时会并发启动它们；交互式 OAuth 浏览器流程会串行执行，以免相互干扰。

服务器连接后，yucode 可以像使用其他工具一样使用它的 tools。服务器标记为只读的 tools 无需提示即可运行；任何可能改变状态的操作都会先请求{ref}`确认 <built-in-guardrails>`。

### 身份验证

- **Bearer token** — 设置 `bearer_token_env_var`（或通过 `env_http_headers` 设置自定义头）。
- **OAuth** — 设置 `auth = "oauth"`。连接时会执行授权流程；断开时会清除已保存的登录状态。

```{admonition} 信任
:class: warning
本地（stdio）服务器会在你的机器上运行程序，远程服务器则会收到 agent 发送给它的任何内容。只连接你信任的服务器。
```
