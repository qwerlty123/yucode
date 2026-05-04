# 技能

**技能**是一包可复用的指令，agent 只在需要时才加载——比如发布检查清单、项目约定、多步骤流程。把<span class="marker">完整文本留在上下文之外、直到使用时才加载</span>，意味着你可以拥有大量技能，而不会让每个请求变得臃肿。

## 创建与安装技能

### 技能的组成

一个技能是一个文件夹，内含一个带有 `name` 和 `description` frontmatter 的 `SKILL.md` 文件：

```
~/.yucode/skills/
  release-notes/
    SKILL.md
    generate.py        # optional bundled script
```

```markdown
---
name: release-notes
description: Draft release notes from the git log since the last tag.
---

1. Run `git log $(git describe --tags --abbrev=0)..HEAD --oneline`.
2. Group the commits by type and summarize each group.
3. If a bundled script is needed, run it with Bash — see paths below.
```

在技能被使用之前，yucode 只能看到 `name` 和 `description`——完整正文按需加载。

### 技能的来源

yucode 从三个来源发现技能：

- yucode 自带的内置技能
- `.yucode/skills/` —— 项目本地，随仓库一起提交
- `~/.yucode/skills/` —— 你的个人技能，随处可用（自定义 `paths.data_dir` 后位于 `<data_dir>/skills/` 下）

当名称冲突时，项目技能覆盖用户技能，用户技能覆盖内置技能。用 `/skills` 列出当前可用的内容以及哪个来源胜出。

每次安装都包含 **`yucode-help`**——一份覆盖安装、配置、provider、命令、会话、工具、安全与故障排查的简明手册。当问题涉及 yucode 时，agent 可以加载它；你也可以用 `$yucode-help` 显式请求。如果手册无法解答该问题，它会引导 agent 查阅对应版本的源码和测试。

## 使用技能

- **按需** —— 当技能与你的请求相关时，yucode 会自行加载。
- **行内** —— 在消息中输入 `$name`（支持 Tab 补全）自行加载技能，<span class="marker">仅在该回合生效</span>。

```{figure} ../snapshots/yucode-skill-mention.png
:alt: 用 $skill 提及在行内加载技能的指令
:width: 600px
:align: center

用 $name 行内加载技能。
```

### 附带脚本

技能可以在 `SKILL.md` 旁边附带辅助脚本。在正文中，`{skill_dir}`（或 `${SKILL_DIR}`）会展开为该技能目录的绝对路径，因此指令可以引导 agent 通过 Bash 运行某个脚本：

```markdown
Run the generator: `python {skill_dir}/generate.py`
```
