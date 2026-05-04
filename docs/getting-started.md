# 快速上手

## 安装

- yucode 仅支持 <span class="marker">macOS 和 Linux</span>
- Python 3.11 或更高版本
- 用 [uv](https://docs.astral.sh/uv/) 安装和运行

```sh
uv tool install git+https://github.com/qwerlty123/yucode.git
```

### 升级

```sh
uv tool upgrade yucode
```

yucode 每天最多检查一次 GitHub 仓库（比较 `pyproject.toml` 中的版本），并在启动时和 `/status` 中报告可用更新。

## 配置

yucode 启动只需要一件事：<span class="marker">一个可对话的 provider</span>。生成一份初始配置：

```sh
yucode --init-config
```

这会写入 `~/.yucode/config.toml`。只有 `[provider]` 块是必需的；其余每个设置都有内置默认值，文件以注释形式列出了常用设置。

### 指向一个 provider

yucode 可与任何 OpenAI 兼容 API（以及 Anthropic）对话。打开配置，填入一个 provider——例如 [DeepSeek](https://api-docs.deepseek.com/)：

```toml
[provider]
active = "default"

[provider.default]
url = "https://api.deepseek.com"
key = "sk-..."
model = "deepseek-v4-flash"
```

| 键 | 含义 |
|---|---|
| `url` | API 的基础 URL |
| `key` | 你的 API 密钥 |
| `model` | 要使用的模型名称 |

你可以定义多个 `[provider.<name>]` 块，并通过 `active`（或在会话内使用 `/provider`）在它们之间切换。可选的 provider、运行时和数据设置参见 [配置](configuration.md#providers)。

## 开始一个会话

```sh
yucode
```

用自然语言输入一个请求，agent 就开始工作——读取文件、提出编辑、运行命令。在做任何修改文件或运行命令的操作之前，它都会请求确认（除非你传了 `--yolo`）。它工作时你可以继续输入；参见 [追问](usage.md#follow-ups)。

用 `/exit`、`/quit` 或 `Ctrl-D` 退出。

## 命令行参数

| 参数 | 作用 |
|---|---|
| `-c`, `--last`, `--latest` | 恢复该项目中最近的会话 |
| `--resume [UID]` | 恢复已保存的会话；不带 `UID` 时恢复该项目最近的一次 |
| `--yolo` | 跳过修改型工具的确认提示 |
| `--theme {auto,light,dark}` | 覆盖已配置的终端配色主题 |
| `--config <path>` | 使用指定的配置文件，而不是 `~/.yucode/config.toml` |
| `--init-config` | 写入初始配置文件后退出 |
| `-h`, `--help` | 显示命令行帮助后退出 |
| `-v`, `--version` | 打印版本号后退出 |
