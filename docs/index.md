# yucode

A small terminal coding agent

yucode works in your terminal: you describe a task, and it reads code, edits files, runs
commands, and reports back. It keeps <span class="marker">stable prompt prefixes</span> so
supported providers can reuse work, maintains a searchable code index, runs background jobs,
tracks its own working notes, and <span class="marker">resumes where you left off</span>.

yucode is the former nanocode, renamed once it outgrew the single file that made it *nano*.

```{figure} ../snapshots/yucode1.gif
:alt: yucode editing code and running tools in one interactive session
:width: 600px
:align: center

Editing code and running tools in one interactive session.
```

```{admonition} Use at your own risk
:class: warning
yucode edits files and runs shell commands in the directory where you start it. It has
**no sandbox of its own**. Run it inside a container, VM, or another isolated environment
when you need isolation. See [Safety](safety.md).
```

## Install and run

```sh
uv tool install git+https://github.com/qwerlty123/yucode.git
yucode --init-config          # write ~/.yucode/config.toml
# add your provider's url, key, and model to that file
yucode
```

Full walkthrough: [Getting started](getting-started.md).

## What it does

```{figure} ../snapshots/yucode2.gif
:alt: yucode working through a repository task
:width: 600px
:align: center

Working through a repository task in an interactive session.
```

| Area | In short |
|---|---|
| **[Interaction](usage.md)** | Follow-ups, streaming, keys — how you drive the agent. |
| **[Commands](commands.md)** | The `/` command reference: status, models, sessions, MCP. |
| **[Tools](tools.md)** | Read, search, navigate code; edit files; run commands; background jobs; optional provider-side web search. |
| **[Sessions](usage.md#sessions)** | Your work is saved, named, and resumable with `/sessions`, `-c`, or `--resume`. |
| **[MCP](mcp.md)** | Connect external Model Context Protocol servers and use their tools. |
| **[Skills](skills.md)** | Load reusable instruction packs on demand. |
| **[Configuration](configuration.md)** | Providers, runtime settings, and data location. |

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

```{toctree}
:hidden:
:maxdepth: 1
:titlesonly:

changelog
```
