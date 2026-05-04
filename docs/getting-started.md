# Getting started

## Install

- yucode supports <span class="marker">macOS and Linux only</span>
- Python 3.11 or newer
- [uv](https://docs.astral.sh/uv/) to install and run

```sh
uv tool install git+https://github.com/qwerlty123/yucode.git
```

### Upgrade

```sh
uv tool upgrade yucode
```

yucode checks the GitHub repository at most once a day (comparing the version in
`pyproject.toml`) and reports an available update at startup and in `/status`.

## Configure

yucode needs one thing to start: <span class="marker">a provider to talk to</span>. Generate a
starter config:

```sh
yucode --init-config
```

This writes `~/.yucode/config.toml`. Only the `[provider]` block is required; every other
setting has a built-in default, and the file lists the common ones as comments.

### Point it at a provider

yucode speaks to any OpenAI-compatible API (and to Anthropic). Open the config and fill in
a provider — for example [DeepSeek](https://api-docs.deepseek.com/):

```toml
[provider]
active = "default"

[provider.default]
url = "https://api.deepseek.com"
key = "sk-..."
model = "deepseek-v4-flash"
```

| Key | Meaning |
|---|---|
| `url` | Base URL of the API |
| `key` | Your API key |
| `model` | Model name to use |

You can define several `[provider.<name>]` blocks and switch between them with `active` (or
`/provider` inside a session). See [Configuration](configuration.md#providers) for optional
provider, runtime, and data settings.

## Start a session

```sh
yucode
```

Type a request in plain language and the agent starts working — reading files, proposing
edits, running commands. Before anything that changes files or runs a command, it asks for
confirmation (unless you pass `--yolo`). You can keep typing while it works; see
[Follow-ups](usage.md#follow-ups).

Exit with `/exit`, `/quit`, or `Ctrl-D`.

## Command-line flags

| Flag | Effect |
|---|---|
| `-c`, `--last`, `--latest` | Resume the most recent session in this project |
| `--resume [UID]` | Resume a saved session; with no `UID`, resumes this project's latest |
| `--yolo` | Skip confirmation prompts for mutating tools |
| `--theme {auto,light,dark}` | Override the configured terminal color theme |
| `--config <path>` | Use a specific config file instead of `~/.yucode/config.toml` |
| `--init-config` | Write a starter config file and exit |
| `-h`, `--help` | Show command-line help and exit |
| `-v`, `--version` | Print the version and exit |
