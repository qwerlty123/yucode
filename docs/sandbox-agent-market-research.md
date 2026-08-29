# Coding agent sandbox 调研与 yucode 建议

调研日期：2026-08-27

## 结论

当前 coding agent 的隔离方案大致分成两类：

- 本地交互式 agent 使用操作系统原生沙箱，在保留本机工具链的同时限制文件写入和网络，例如 Claude Code 与 OpenAI Codex。
- 无人值守或处理不可信仓库的云端 agent 使用每任务独立的临时 VM、microVM 或 Actions 环境，例如 Cursor Cloud Agents、GitHub Copilot coding agent 与 E2B。

两类方案都把 sandbox 与审批分开：sandbox 是不可越过的能力上限，审批只决定是否允许某次受控提权。网络、凭据、Git 权限、资源限额和审计还需要分别治理。

对 yucode，推荐的目标不是只包装 `BashTool`，而是建立宿主机控制面与沙箱工具面的 seam：TUI、模型请求、provider key 和 session 留在宿主机；Read、Edit、Search、InspectCode、ViewImage、Bash、Job 和本地 stdio MCP 由一个持久 sandbox worker 执行。

## yucode 当前状态

- `README.md:13` 与 `docs/safety.md:5-8` 已明确说明没有 sandbox。
- `yucode/tools/shell.py:205-207` 和 `yucode/tools/shell.py:457-462` 直接以当前用户权限启动前台及后台 shell。
- `yucode/tools/files.py:121-150`、`yucode/tools/files.py:189-200` 和 `yucode/tools/search.py:54-93` 允许 Read、ViewImage、Search 指向工作区外，只把它们升级为一次确认；`yucode/tools/files.py:346-412` 也会解析并读写已存在的工作区外 Edit 目标。
- `docs/safety.md:13` 说明 `--yolo` 或 `/yolo` 会跳过确认。因此确认是交互护栏，不是隔离边界。
- `yucode/tools/shell.py:28-31` 自己也正确地把只读命令分类器定义为启发式规则，而不是 sandbox。
- `evals/docker.py:45-126` 已有离线、全网络、provider allowlist 三种网络租约；`evals/docker.py:169-178` 已有内存、CPU、PID 限额；`evals/docker.py:353-374` 已有工作区挂载和容器执行。这些概念可以复用，但评测执行器本身不应直接成为交互式产品 runtime。

## 市面方案

| 产品 | 隔离形态 | 网络策略 | 对 yucode 的启示 |
|---|---|---|---|
| Claude Code | macOS Seatbelt、Linux/WSL2 bubblewrap；另有可包住完整进程的 sandbox runtime | 默认不预授权域名，越界访问进入权限流程 | Bash-only 隔离适合本地交互，但必须明确 Read/Edit、MCP、hooks 等是否在同一边界内，并支持 fail-closed |
| OpenAI Codex | macOS Seatbelt；Linux bwrap + seccomp；提供 `read-only`、`workspace-write`、`danger-full-access` | `workspace-write` 默认关闭网络 | 直接借鉴三档模式；sandbox 和 approval 独立；保护 `.git`、agent 配置等可持久化元目录 |
| Cursor Cloud Agents | 每 agent 独立 Firecracker microVM | 互联网默认开启，可按用户、环境、团队配置域名策略 | 无人值守任务依赖强隔离、短期凭据、独立分支和人工合并门禁，而不是逐命令确认 |
| GitHub Copilot coding agent | GitHub Actions 驱动的临时环境 | 默认出站防火墙和推荐 allowlist | setup、runtime、MCP 必须分别定义网络边界；单个 Bash 防火墙不覆盖所有执行路径 |
| E2B | 每 sandbox 独立 Firecracker microVM | 可关闭或按 IP、CIDR、域名 allow/deny | 远程 sandbox 应作为可替换 adapter；凭据尽量由控制面或 egress proxy 注入 |

### Claude Code

Claude Code 的内置 sandbox 主要隔离 Bash 及子进程；macOS 使用 Seatbelt，Linux/WSL2 使用 bubblewrap。官方另行说明，Read/Edit、WebFetch、MCP servers、hooks 等并不全部处于 Bash sandbox 内，完整无人值守隔离应包住整个进程或使用容器/VM。受管环境可以要求 sandbox 不可用时直接失败，并禁止命令以非沙箱方式重试。

来源：

- [Sandboxing](https://code.claude.com/docs/en/sandboxing)
- [Choose a sandbox environment](https://code.claude.com/docs/en/sandbox-environments)
- [Anthropic Sandbox Runtime](https://github.com/anthropic-experimental/sandbox-runtime)

### OpenAI Codex

Codex 将 sandbox 与 approval 作为正交控制。其文件模式为 `read-only`、`workspace-write`、`danger-full-access`；`workspace-write` 可以增加 writable roots，但默认关闭网络，并保护 `.git`、`.agents`、`.codex` 等元目录。当前本地实现使用 macOS Seatbelt 与 Linux bwrap + seccomp。OpenAI 的内部部署还会集中限制 full access、网络目的地并记录工具、审批与网络决策。

来源：

- [Codex sandbox](https://learn.chatgpt.com/docs/sandboxing)
- [Agent approvals and security](https://learn.chatgpt.com/docs/agent-approvals-security)
- [Codex configuration reference](https://developers.openai.com/codex/config-reference)
- [Running Codex safely at OpenAI](https://openai.com/index/running-codex-safely/)

### Cursor Cloud Agents

Cursor 已将 Background Agents 更名为 Cloud Agents。每个 agent 在独立 Firecracker microVM 中运行，生命周期包含创建环境、clone、执行、持久化结果、创建 draft PR 和回收。互联网默认开启，但可配置 allow-all、默认规则加 allowlist 或 allowlist-only。凭据分为普通变量、Runtime Secrets 和 Build Secrets，并推荐短期 OIDC token。

来源：

- [Cursor Cloud Agent security](https://cursor.com/docs/cloud-agent/security)
- [Cursor Secrets and Network](https://cursor.com/docs/cloud-agent/security-network)

### GitHub Copilot coding agent

Copilot coding agent 为每个任务使用 GitHub Actions 驱动的临时环境，并允许通过专用 setup workflow 配置依赖。默认出站防火墙只覆盖 agent Bash 启动的进程，官方明确指出 MCP servers 和 setup steps 不在同一个覆盖面内。它使用专门的 Agents secrets，限制 agent 只能写任务分支，且不能自行批准或合并 PR。

来源：

- [Customize the cloud agent environment](https://docs.github.com/en/copilot/how-tos/copilot-on-github/customize-copilot/customize-cloud-agent/customize-the-agent-environment)
- [Customize the firewall](https://docs.github.com/en/copilot/how-tos/copilot-on-github/customize-copilot/customize-the-firewall)
- [Configure secrets and variables](https://docs.github.com/en/copilot/how-tos/copilot-on-github/customize-copilot/customize-cloud-agent/configure-secrets-and-variables)
- [Risks and mitigations](https://docs.github.com/en/copilot/concepts/agents/cloud-agent/risks-and-mitigations)

### E2B

E2B 把每个 sandbox 放在 Firecracker microVM 中，提供完整 Linux、文件系统、终端和 Git。网络可整体关闭，或按 IP、CIDR、域名配置 allow/deny；其域名过滤依赖 HTTP Host 与 TLS SNI，因此 QUIC/HTTP3 等旁路需要单独阻断。它还提供模板、pause/resume/kill、超时策略和受保护的控制面访问。

来源：

- [E2B coding agents](https://docs.e2b.dev/use-cases/coding-agents)
- [Internet access](https://docs.e2b.dev/network/internet-access)
- [Secured access](https://docs.e2b.dev/sandbox/secured-access)
- [Sandbox persistence](https://docs.e2b.dev/sandbox/persistence)

## 推荐架构

```text
宿主机控制面
TUI / ToolRunner / ModelClient / Session / provider credentials / approval
                              |
                       SandboxRuntime
                              |
              JSONL 或 Unix socket 事件协议
                              |
沙箱工具面
Read / Edit / Search / InspectCode / ViewImage / Bash / Job / stdio MCP
                              |
        Seatbelt(macOS) | bwrap+seccomp(Linux) | Docker/remote adapter
```

`SandboxRuntime` 应是一个深 module：外部 interface 只负责启动、执行、取消和关闭；路径、进程组、环境清洗、网络代理、资源限额和 backend 差异都藏在 implementation 内。至少有 Seatbelt、bwrap 和测试 adapter，seam 不是为未来假设出来的。

宿主机只在用户确认后把工具调用送进 worker。Edit 使用两阶段协议：worker 先返回 diff 与绑定当前文件状态的 opaque lease；确认后再提交，提交时重新校验 lease。worker 崩溃或 sandbox 拒绝时，`ToolRunner` 仍为每个调用返回一个失败结果，保持现有 replay 契约。

推荐配置形态：

```toml
[sandbox]
mode = "workspace-write"       # read-only | workspace-write | danger-full-access
backend = "auto"               # seatbelt | bubblewrap | docker
network = "off"                # off | allowlist | full
allowed_hosts = []
writable_roots = []
protect_git = true
fail_if_unavailable = true
max_processes = 256
memory = "4g"
```

默认策略应为：

- 工作区可读写，系统与工具链只读；每 session 临时目录可写。
- `.git`、`.yucode`、shell startup files、provider 配置和凭据目录递归只读或不可见。
- 不继承宿主机完整环境；只传 PATH、locale、TERM 等白名单变量，HOME 指向隔离临时目录。
- 工具网络默认关闭。模型 provider 请求仍由宿主机控制面发出；需要下载依赖时按域名或单次调用受控提权。
- 不挂载 SSH agent、Docker socket、云凭据目录或整个 home。
- sandbox 请求启用但 backend 不可用时直接失败，绝不静默回退到宿主机执行。
- `--yolo` 只改变 approval，不改变 sandbox mode、network 或 writable roots。

## 建议实施顺序

1. 定义 `SandboxPolicy`、威胁模型、配置校验和 `/status` 展示；确保 `danger-full-access` 明确等价于当前行为。
2. 引入 `SandboxRuntime` seam 与持久 worker 协议，先提供测试 adapter 和一个真实 native adapter。
3. 将 Bash、Job、Search 的 subprocess 与 Read/Edit/Search/ViewImage/InspectCode 的模型可控路径统一移入工具面；保留 session、Memory、Ask、远程 MCP 在控制面。
4. 将本地 stdio MCP 移入工具面；在此之前，sandbox 模式下默认禁用它，避免虚假的完整隔离声明。
5. 增加 macOS Seatbelt 与 Linux bwrap + seccomp adapter；请求 sandbox 时 fail-closed。
6. 实现网络 proxy、域名 allowlist、环境清洗以及 CPU、内存、PID、磁盘和时间限额。
7. 复用评测 Docker 执行器的网络与资源控制概念，增加 Docker/远程 adapter，服务无人值守和不可信仓库。
8. 用黑盒集成测试覆盖绝对路径、`..`、符号链接竞态、后台任务、进程组、`/proc`、网络直连、云 metadata、Git 元目录和 worker 崩溃。

第一版可先 opt-in；稳定后将 Git 仓库默认设为 `workspace-write`，非 Git 目录默认设为 `read-only`。用户显式要求 sandbox 时，无论处于哪个发布阶段都必须 fail-closed。
