# Agent 指南

保持本文件简短。它是入口，而不是第二份设计文档。

## 从这里开始

- 新接触代码库：先读 [概览](DESIGN.md#orientation) 了解目标、模块分层和单个回合的形态；然后浏览 [常见陷阱](DESIGN.md#common-pitfalls)——那些看起来像清理工作、实则不是的改动。
- 在改动跨切面行为或模块归属之前，先读 [DESIGN.md](DESIGN.md)。
- 在引入新的抽象或依赖之前，先沿用最近的既有模式。

## 项目地图

- `yucode/engine.py`：组合 context、model 和 tools 的 agent 回合循环。
- `yucode/context.py`、`yucode/model.py`、`yucode/runner.py`：上下文投影与压缩、provider 请求协议、工具执行生命周期。
- `yucode/update.py`：后台版本检查。
- `yucode/session.py`：持久的语义状态与持久化。
- `yucode/tools/`、`yucode/image.py`、`yucode/mcp.py`、`yucode/skill.py`：纵向功能模块。`tools/` 按能力拆分内置工具集，并在其 `__init__.py` 中持有注册表。
- `yucode/provider_compat.py`：基于证据的 provider 兼容性策略。
- `yucode/loop.py`、`yucode/tui.py`、`yucode/render.py`：命令、交互与展示。
- `tests/`：按子系统与边界分组的行为导向测试。

## 项目工作流

- **测试：** 迭代时运行针对性测试，完成行为改动前运行 `uv run pytest`。
- **质量：** 运行 `uv run ruff check yucode`、`uv run ruff format --check yucode` 和 `uv run pyright`。
- **文档：** 用户可见文档有改动时，更新英文源文件，运行 `make -C docs locale-zh`，更新中文目录，然后构建 `html` 和 `html-zh`。
- **变更日志：** 把用户可见的变更记入 `Unreleased` 下对应的分类；仅内部的重构与文档维护不记。
- **发布（仅在要求时）：** 提升 `pyproject.toml` 与 `yucode/base.py` 的版本，把 Unreleased 条目移到带日期的版本下，运行测试、质量检查、两种文档构建和 `uv build`，提交 `Release X.Y.Z`，并创建轻量标签 `vX.Y.Z`。不要推送或发布。

## 工作规则

- 做最小的一致改动；避免透传包装与投机性特化。
- 优先在最窄的稳定公共边界做黑盒测试。修复 bug 要覆盖复现的失败、预期结果和重要的拒绝路径；完整测试策略见 `DESIGN.md`。
- 模拟外部不确定性，而不是被测的核心行为。保持测试确定且快速。
- 让 `CHANGELOG.md` 与用户可见行为保持一致。
