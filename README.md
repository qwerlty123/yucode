<h1 align="center">yucode</h1>

<p align="center">
  <img src="snapshots/yucode1.gif" alt="yucode 编辑代码并运行工具" width="600">
</p>

<p align="center">
  一个我自己在用、维护并定制的 coding agent。
</p>

<p align="center"><a href="README.md">English</a></p>

## 安全

**使用风险自负。** yucode 会在启动环境中编辑文件并执行 shell 命令，不提供 sandbox 隔离。需要隔离时，请使用容器或虚拟机。

<p align="center">
  <img src="snapshots/yucode2.gif" alt="yucode 恢复保存的 session" width="600">
</p>
<p align="center"><sub>恢复保存的 session，包括对话和工具调用历史。</sub></p>

## 亮点

- **Prompt-cache 友好：** 稳定的请求前缀让支持缓存的 provider 复用计算，缓存命中率可达 90–99%；`/status` 会显示 provider 返回的实际结果。
- **代码导航：** 通过可搜索的代码索引跳转到定义、调用者和实现。
- **实时追加指令：** agent 工作时仍可输入；`Enter` 将消息排入下一次模型调用，`Ctrl-C` 先清除草稿，输入为空时中断当前任务。
- **锚点编辑：** 结构化编辑使用 `line:hash` 锚点，文件内容过期时会被拒绝。
- **可恢复 session：** 对话、工具调用、diff 和工作记忆可通过 `-c` 或 `--resume` 恢复。
- **内置 diff viewer：** `/diff` 展示最新一轮改动以及整个 session 的累计变更。
- **MCP 与 skills：** 按需连接 Model Context Protocol server，加载 Markdown 指令包。
- **Provider 侧联网搜索：** 可选启用 Provider 自带的搜索工具（OpenAI、Qwen、Anthropic、Z.AI），每次搜索及其来源都会显示在对话记录中。
- **Provider 兼容：** 支持 OpenAI-compatible API 与 Anthropic。

## 安装

需要 macOS 或 Linux、Python 3.11+ 和 [uv](https://docs.astral.sh/uv/)。

```sh
uv tool install git+https://github.com/qwerlty123/yucode.git
yucode --init-config
```

在 `~/.yucode/config.toml` 中填写 provider：

```toml
[provider]
active = "default"

[provider.default]
url = "https://api.deepseek.com"
key = "sk-..."
model = "deepseek-v4-flash"
```

然后运行：

```sh
yucode
```

升级：`uv tool upgrade yucode`，从仓库拉取最新提交。

## 链接

- [文档](https://github.com/qwerlty123/yucode) — 完整的使用指南和参考。
