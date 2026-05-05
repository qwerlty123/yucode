# yucode

一个运行在终端中的小型编码 agent

yucode 在你的终端中工作：你描述一个任务，它读取代码、编辑文件、运行命令，然后汇报结果。它保持 <span class="marker">稳定的提示词前缀</span>，让支持的 provider 能复用已完成的工作；维护可搜索的代码索引、运行后台任务、记录自己的工作笔记，并能 <span class="marker">从上次停下的地方继续</span>。

```{figure} ../snapshots/yucode1.gif
:alt: yucode 在同一个交互会话中编辑代码并运行工具
:width: 600px
:align: center

Editing code and running tools in one interactive session.
```

```{admonition} 自行承担风险
:class: warning
yucode 会在你启动它的目录中编辑文件并运行 shell 命令，它**没有自己的沙箱**。需要隔离时，请在容器、虚拟机或其他隔离环境中运行。参见 [安全](safety.md)。
```

## 安装与运行

```sh
uv tool install git+https://github.com/qwerlty123/yucode.git
yucode --init-config          # 写入 ~/.yucode/config.toml
# 把 provider 的 url、key 和 model 填进该文件
yucode
```

完整教程：[快速上手](getting-started.md)。

## 它能做什么

```{figure} ../snapshots/yucode2.gif
:alt: yucode 在交互会话中处理仓库任务
:width: 600px
:align: center

Working through a repository task in an interactive session.
```

| 模块 | 简介 |
|---|---|
| **[交互](usage.md)** | 追问、流式输出、按键——你如何驱动 agent。 |
| **[命令](commands.md)** | `/` 命令参考：status、models、sessions、MCP。 |
| **[工具](tools.md)** | 读取、搜索、浏览代码；编辑文件；运行命令；后台任务；可选的 provider 侧网页搜索。 |
| **[会话](usage.md#sessions)** | 你的工作会被保存、命名，可通过 `/sessions`、`-c` 或 `--resume` 恢复。 |
| **[MCP](mcp.md)** | 连接外部的 Model Context Protocol 服务器并使用其工具。 |
| **[技能](skills.md)** | 按需加载可复用的指令包。 |
| **[配置](configuration.md)** | Provider、运行时设置与数据位置。 |

```{toctree}
:hidden:
:caption: Guide

getting-started
usage
context
safety
```

```{toctree}
:hidden:
:caption: Reference

commands
tools
configuration
mcp
skills
```

