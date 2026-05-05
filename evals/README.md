# yucode Agent 评测

这是只面向 yucode 开发者的评测工具，不属于公开 `yucode` CLI。它用可复现任务、隔离运行环境和确定性隐藏测试衡量 coding agent，而不是让另一个模型给答案打分。

## 不影响安装和发布

- 入口是 `python -m evals`，没有修改 `yucode` 命令。
- `evals/` 不在 setuptools 的显式 package 列表里，也不会进入发布 wheel。
- SWE-bench 是可选的 `eval` dependency；普通 `uv tool install git+https://github.com/qwerlty123/yucode.git` 不会安装它。
- CI 只测试评测框架本身，不调用真实模型，也不运行 Provider benchmark。

## 第一次运行

在仓库根目录安装开发依赖：

```sh
uv sync --extra dev
```

启动 Docker Desktop。先验证内置的真实 Provider 任务清单；这一步不会调用模型：

```sh
uv run python -m evals validate evals/examples/profiles/provider-suite.toml
```

正式运行方式见下方“现成评测套件”。

## 个人开发者工作流

评测清单只接受 schema V2。能力与执行 Profile 的唯一目录是 [`catalog.toml`](catalog.toml)。确定性的代码、工具、会话和协议行为由 `pytest` 验证；`evals` 只保留需要真实模型参与的能力评测。

中断后的实验可以继续，单个失败 attempt 也可以创建不可变重试：

```sh
uv run python -m evals resume private-evals/suite.toml \
  --agent yucode --config ~/.yucode/config.toml \
  --output .yucode/evals/<experiment>

uv run python -m evals retry private-evals/suite.toml \
  --agent yucode --config ~/.yucode/config.toml \
  --output .yucode/evals/<experiment> \
  --task fix-parser --repetition 1
```

## 现成评测套件

仓库内置 8 个 live V2 任务，位于 `profiles/provider-suite.toml`，覆盖默认 coding、预构建代码索引、ViewImage、真实图片附件、Provider builtin tools、同 Session Provider 切换、strict tools 和 cache token telemetry。不支持的条件能力会在调用模型前记为 N/A。

先验证全部清单，不会调用模型：

```sh
uv run python -m evals validate evals/examples/profiles/provider-suite.toml
```

运行当前 Provider 的 live Profile；这会产生真实 token/费用：

```sh
uv run python -m evals run evals/examples/profiles/provider-suite.toml \
  --agent yucode --config ~/.yucode/config.toml \
  --output .yucode/evals/provider-formal
```

最小 V2 task：

```toml
schema_version = 2
id = "fix-parser"
prompt = "prompt.md"
targets = ["agent.coding.patch"]
profile = "coding_default"
category = "coding"
allowed_tools = ["Read", "Edit"]
step_budget = 4
expected_artifact = "src/parser.py"

[source]
type = "local"
path = "source"

[environment]
image = "python:3.12"
network = "provider-only"

[grader]
path = "grader"
command = ["python", "{grader}/grade.py"]

[success]
require = ["within_budget", "verifier_passed", "expected_artifact_exists", "normal_success_stop"]

[limits]
max_agent_steps = 30
agent_timeout_seconds = 600
grader_timeout_seconds = 60
memory = "2g"
cpus = 2.0
pids = 256
```

条件能力在 preflight 检查 provider、model、索引、图片等要求。不满足时结果为 `not_applicable`，不会调用模型。正式 Yucode Profile 使用 `--agent yucode` 和当前配置的 Provider；live 运行不默认进入 CI。

每个实验包含 `runs.sqlite3`。`jobs` worker 用原子 claim 并发领取任务；完成的 attempt 不覆盖，retry 创建新 attempt。runner 在 agent 运行中退出会把原 attempt 记为 infra 并创建 recovery attempt；patch 已落盘时，resume 从干净源码重跑 grader，不再次调用模型。

## 私有任务格式

建议把真正的评测集放在私有仓库，不要把隐藏测试放入 yucode 公共仓库：

```text
private-evals/
├── suite.toml
└── tasks/
    └── fix-parser/
        ├── task.toml
        ├── prompt.md
        ├── source/
        ├── environment/
        │   └── Dockerfile
        └── grader/
            └── grade.py
```

套件清单使用 V2：

```toml
schema_version = 2
name = "private-regression"
tasks = ["tasks/*/task.toml"]

[defaults]
repetitions = 3
agent_timeout_seconds = 1800
grader_timeout_seconds = 900
max_steps = 200
jobs = 1
network = "provider-only"
```

`task.toml` 使用上方的最小 V2 task 格式。

也可以用固定 Git revision 作为源代码：

```toml
[source]
type = "git"
url = "https://github.com/example/project.git"
revision = "完整 commit SHA"
```

grader 命令可使用 `{workspace}`、`{grader}` 和 `{output}`。退出码 `0` 是通过，其他退出码是失败；`grade.json` 仅提供诊断信息，不能覆盖退出码。`base_must_fail = true` 会先验证未修改代码确实无法通过；声明 `gold_patch` 时还会验证参考补丁能够通过。两者一起使用可以防止“原题已经通过”或“任务本身无解”制造错误分数。

隐藏的 `grader/` 和 gold patch 不能位于 Docker build context 内。`validate` 会拒绝这种清单，agent 容器也不会挂载 grader。生成 patch 后，系统会创建第二份干净源码、应用 patch，再单独挂载 grader 执行测试。

## 联网策略

每个任务可选三种策略：

- `provider-only`：默认值。agent 只能经 CONNECT proxy 访问当前任务实际使用的 Provider URL 精确主机名和 443 端口；同 Session 切换 Provider 时会一并放行参与切换的主机。
- `offline`：agent 容器完全断网，适合本地 mock provider。
- `full`：允许普通容器网络，只应用于任务明确需要联网的场景。

grader 始终使用 `--network none`。provider key 通过 worker 的 stdin 传入，不写入容器命令行、环境变量、workspace、实验元数据或 session 文件。

## 结果在哪里

默认目录是 `.yucode/evals/<时间>-<suite>-<agent>/`：

```text
experiment.json       # 实验、模型和环境元数据；不含 key
results.jsonl         # 从 SQLite 原子导出的机器可读结果
runs.sqlite3          # jobs、attempt、checkpoint、resume 和 retry
summary.json          # 聚合指标
report.md             # 人工阅读报告
runs/<task>/<n>/attempt-<n>/
├── run.json
├── trace.jsonl
├── evidence.json
├── patch.diff
├── agent.log
├── session.jsonl
├── grader.log
└── grade.json
```

重新生成报告：

```sh
uv run python -m evals report .yucode/evals/<experiment>
```

比较改动前后两次实验：

```sh
uv run python -m evals compare \
  .yucode/evals/<baseline> \
  .yucode/evals/<candidate> \
  --output .yucode/evals/comparison.json
```

比较按 `(task_id, repetition)` 配对，会列出 improvements 和 regressions；不要拿任务集合不同的两个总分直接比较。

compare 会生成 `ComparabilityCertificate`。task/prompt/source/grader、Agent、工具 schema、Profile、Docker、provider/model/wire、网络和资源配置存在未声明差异时，只输出描述性并排数据，退出码为 `5`；LocalDebug 永远不可比较。允许的实验因素必须显式声明：

```sh
uv run python -m evals compare baseline candidate \
  --allow-difference agent_config.model
```

也可以直接运行单因素实验矩阵。示例矩阵只改变 temperature，其余 resolved manifest 差异会由可比性证书自动拒绝：

```sh
uv run python -m evals matrix evals/examples/profiles/provider-suite.toml \
  --matrix evals/examples/profiles/temperature-matrix.toml \
  --agent yucode --config ~/.yucode/config.toml \
  --output .yucode/evals/temperature-matrix
```

每个 variant 保存独立 SQLite、证据和报告；矩阵总览写入 `matrix.json`。`single_factor = true` 时，variant 只能声明一个逻辑变化字段。

Release gate 默认只做结构检查，数值门槛必须由策略文件显式给出：

```toml
[gate]
pass_at_1 = 0.8
all_at_k = 0.6
```

```sh
uv run python -m evals gate .yucode/evals/<candidate> \
  --comparison comparison.json --policy gate.toml
```

没有 clean baseline 时先只跑结构 gate：

```sh
uv run python -m evals gate .yucode/evals/release-candidate \
  --policy evals/examples/release-gate.toml
```

CLI 退出码：`0` 通过、`1` 能力失败、`2` 合同无效、`3` 基础设施失败、`4` gate 失败、`5` 不可比较、`130` 取消。迁移脚本可显式使用 `--exit-zero-on-capability-failure`。

## 与 pytest 的区别

`pytest` 验证类、函数和协议实现是否按预期工作，默认不评价真实模型解决任务的能力：

```sh
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider
```

`python -m evals` 则把 Yucode 和真实 Provider 当作被测 Agent：准备独立源码，运行完整模型/工具循环，捕获 patch，在干净副本中执行隐藏 grader，再生成 trace、evidence、统计和 gate 结果。能由固定输入和固定输出判断的行为应放在 `pytest`，不再作为 eval 重复运行。

## 如何判断好坏

- `pass@1`：每个任务第一次是否成功，最接近日常“一次完成”的能力。
- `pass@k`：每个任务在 k 次中至少成功一次，反映能力上限。
- `all@k`：每个任务 k 次全部成功，反映稳定性。对 coding agent，这个指标通常比只看最好一次更重要。
- `run success rate`：所有独立运行的成功率，同时报告 95% Wilson 区间。任务很少时区间会很宽，不应据此下强结论。
- tokens、模型调用、工具调用错误、wall time 和估算成本：衡量达到同一成功率需要多少资源。

建议版本门槛：任务集合和模型配置固定；`pass@1` 提升；`all@3` 不下降；不能出现新的配对 regression；`infra_error` 必须为 0；token、成本或耗时的大幅增长需要能换来相应成功率。少量内置任务只能验证链路和特定机制，不能代表整体智能水平；稳定判断至少使用数十个覆盖真实失败模式的任务。

状态含义：

- `passed` / `failed`：agent 正常结束，隐藏测试通过 / 未通过。
- `timeout`：agent 超出 wall-time 预算。
- `agent_error`：provider、模型协议或 agent 运行失败。
- `infra_error`：源码准备、Docker、patch 应用或 grader 基础设施失败。它不是能力失败，必须先修复再比较。

如需成本估算，可在 yucode 配置中增加每百万 token 的价格；这部分只供 eval 读取：

```toml
[eval.pricing]
prompt_per_million = 1.00
completion_per_million = 4.00
cached_read_per_million = 0.10
cached_write_per_million = 1.25
```

没有配置价格时，token 仍会统计，成本显示为 `not configured`。

## SWE-bench Verified

安装可选依赖：

```sh
uv sync --extra dev --extra eval
```

先从一个明确实例开始；命令必须给出 `--instance-ids` 或 `--limit`，避免意外运行整个数据集：

```sh
uv run python -m evals swebench \
  --instance-ids astropy__astropy-12907 \
  --repetitions 3
```

默认数据集为 `princeton-nlp/SWE-bench_Verified`。系统使用官方 instance image 运行 yucode，按重复次数分别生成标准 predictions JSONL，再调用 `swebench.harness.run_evaluation` 做最终判定。报告记录 SWE-bench package 版本、所选数据的内容指纹、image digest 和 base commit。

SWE-bench 很占磁盘和内存；正式批量运行前，应先确认 Docker Desktop 的磁盘空间和 `linux/amd64` 模拟/运行能力。

## 评测其他 agent

命令 adapter 接受 JSON argv 模板：

```sh
uv run python -m evals run private-evals/suite.toml \
  --agent command \
  --agent-command '["other-agent", "--workspace", "{workspace}", "--prompt-file", "{prompt_file}"]'
```

外部 agent 的 task 使用 `profile = "command_coding"` 和 `allowed_tools = []`。可用占位符是 `{workspace}`、`{prompt_file}` 和 `{artifact_dir}`。正式 Docker 模式要求该命令已经安装在任务 image 中；本地模式可用于先调通 adapter。无论外部 agent 如何报告自己的结果，最终 pass/fail 都只由同一个隐藏 grader 决定。

命令 adapter 没有 yucode provider 配置可供 allowlist 推导，因此它的任务应显式使用 `network = "offline"`，或在确实需要普通网络时使用 `full`；`provider-only` 目前只适用于内置 yucode adapter。
