# 工具

yucode 使用工具来检查你的项目并对其执行操作。你描述想要的结果，agent 负责选择工具。工具调用在运行时实时显示在终端中，它们与你手动输入的命令（`/` 命令）相互独立。只读工具可以并发运行；可能改变系统的操作会请求确认，除非已启用 `--yolo` 或 `/yolo`。

(built-in-tools)=
## 内置工具

::::{list-table}
:header-rows: 1
:widths: 24 76
:class: tool-reference

* - 工具
  - 作用
* - **`Read`**
  - 打开一个或多个 UTF-8 文件中选定的行范围。返回的每一行都带有锚点，供之后的编辑校验。

    缩短后的结果如下所示：

    ```html
    <Read path="yucode.py">
      <file_stat mtime_ns="..." size="222039"/>
      <total_lines>5031</total_lines>
      <range>684:687</range>
      <content hashline-numbered>
        anchor=684:234ew | class Tool:
        anchor=685:7xy0d |     NAME: ClassVar[str] = ""
        anchor=686:5exvk |     DESCRIPTION: ClassVar[str] = ""
      </content>
    </Read>
    ```

    在 `684:234ew` 中，`684` 是从零开始的行号，`234ew` 是该行内容的短哈希。行号用于定位编辑位置，哈希用于证明该行自读取以来没有发生变化。
* - **`ViewImage`**
  - 打开一个本地 PNG、JPEG、WebP 或单帧 GIF 文件，作为当前模型的可视输入。agent 可以主动用它查看截图、设计稿、图表和生成的作品。工作区以外的图片需要确认，且当前 provider/model 必须支持图片输入。
* - **`Search`**
  - 使用不区分大小写的正则表达式查找文本，可选地按路径或文件名模式限定范围。它会跳过隐藏文件、二进制文件和被 gitignore 忽略的文件，并返回可编辑的锚点。
* - **`InspectCode`**
  - 通过[代码符号索引](#code-symbol-index)查找定义、引用、实现、调用者、被调用者和文件大纲。它用于查找代码结构，而非精确文本。
* - **`Edit`**
  - 通过插入、替换或删除内容来创建或修改一个 UTF-8 文件。对于带锚点的改动，`Edit` 会回传 `Read`、`Search` 或 `InspectCode` 返回的 `line:hash` 值。yucode 会在写入前立即检查当前行，如果哈希不再匹配，就<span class="marker">拒绝执行编辑</span>。成功的编辑会出现在 [`/diff`](usage.md#reviewing-changes) 中。

    :::{figure} ../snapshots/yucode-edit-preview.png
    :alt: Edit 确认预览提议的 diff
    :width: 100%
    :align: center

    Edit 确认在批准前预览提议的改动。
    :::
* - **`Bash`**
  - 在项目中运行一条 shell 命令并实时输出。超过 `runtime.bash_wait_timeout` 仍在运行的命令会<span class="marker">自动转为后台任务</span>。

    :::{figure} ../snapshots/yucode-bash-live-preview.gif
    :alt: Bash 工具调用在 yucode 中实时流式输出命令结果
    :width: 100%
    :align: center

    Bash 输出随命令运行实时显示。
    :::
* - **`Job`**
  - 启动或管理后台命令：查看输出、等待、列出或停止。同样的任务也可以通过 `/ps` 查看。
* - **`Recall`**
  - 当对话中只放入了缩短的结果时，可以检索<span class="marker">某个更早工具调用的完整结果</span>，或选定的行范围。
* - **`RecallContext`**
  - 按从新到旧的顺序列出已存储的压缩片段，通过 `seg.N` 键检索摘录，或用不区分大小写的正则（如 `cache prefix|task memory`）搜索标题和文本。列表支持分页；搜索结果限制匹配行数。片段标题按需加载，不会占用每个请求。
* - **`Note`**
  - 查看或更新任务的目标、计划、成功检查和已学到的事实。更新会写入持久的对话历史，因此能保持只追加式 prompt 缓存前缀不变，也不会编辑文件。

    <div class="term-shot" role="img" aria-label="Note 更新打印在终端中的效果：目标行和检查行，一个各项标记为完成、进行中或等待的计划，以及一列已学到的事实。"><span class="fs-goal">goal: 交付 tokenizer 修复</span><span class="fs-goal">check: pytest -q 通过</span><span class="fs-sel">plan:</span><span class="fs-add">  - [x] 复现失败的测试</span><span class="fs-doing">  - [~] 修复 tokenizer</span><span>  - [ ] 更新 changelog</span><span class="fs-sel">known:</span><span class="fs-add">  + 测试使用 pytest -q 运行</span></div>

    计划项用 `[x]` 标记为已完成、`[~]` 进行中、`[ ]` 等待中，或 `[-]` 受阻。
* - **`Ask`**
  - 暂停下来，等待一个真正需要你参与的决策。问题可以附带选项和一个推荐项。

    <div class="term-shot" role="img" aria-label="Ask 提示：先显示问题，然后是一个列出两个选项的选择器，推荐项已预选，并为高亮选项提供预览行。"><span class="fs-user">用哪种方案？</span><span> </span><span>选择：</span><span class="fs-dim">  j/k 移动，/ 搜索，Esc/q 返回/取消</span><span class="fs-sel">&gt;  1. 重构 <span class="fs-i fs-add">(推荐)</span></span><span class="fs-dim">   2. 重写</span><span class="fs-dim">  │ 提取模块 +87 -12</span></div>

    按 `Esc` 可拒绝回答该问题；不选择而直接输入，则以自由文本作答。
* - **`NextHints`**
  - 提供 2–3 条模型在回答之后建议的简短下一步提示。它们以可选中的 chip 形式出现在空闲提示符处；`Tab` 切换焦点，`Enter` 提交，`/hints` 可将其关闭。全部为 `NextHints` 的一批调用会在单次模型调用中结束当前回合。

    <div class="term-shot" role="img" aria-label="NextHints 调用后空闲提示符处的终端：上方是回答文本，然后是一个带光标的空提示符、一行间隔，以及一排三个青色建议 chip，chip 之间以灰色竖线分隔，中间的 chip 反色高亮。"><span>一切就绪，可以审查了。</span><span> </span><span class="fs-prompt">&gt; <span class="fs-caret">▏</span></span><span> </span><span><span class="fs-i fs-sel"> 运行测试 </span><span class="fs-i fs-dim"> │ </span><span class="fs-i fs-tab-on"> 查看 diff </span><span class="fs-i fs-dim"> │ </span><span class="fs-i fs-sel"> 提交工作 </span></span></div>

    `Tab` 移动高亮；`Enter` 将焦点所在的 chip 作为你的下一条消息提交。
* - **`Skill`**
  - 在需要时加载已安装技能的完整指令。仅当安装了技能时才会出现；参见[技能](skills.md)。
* - **`MCP`**
  - 描述或调用已连接 MCP 服务器上的工具，并读取其资源。仅当连接了服务器之后才会出现；参见 [MCP](mcp.md)。
::::

(provider-side-tools)=
## Provider 侧工具

某些 provider 在作答过程中可以<span class="marker">自行搜索网络</span>。模型自行搜索、阅读页面并附上来源作答，无需 yucode 运行任何东西。该功能默认关闭；可通过 [`builtin_tools`](configuration.md#provider-side-tools) 开启。

每次搜索都会显示一行；运行期间，状态栏中会出现 `web search` 阶段；答案下方会列出来源：

```text
  ├ web search httpx timeout configuration
The client accepts a `timeout` argument taking either a float or a `Timeout` object.

**Sources**

1. [Timeouts — HTTPX](https://example.com/httpx/timeouts)
```

来源只在 provider 报告时才会出现；并非所有 provider 都会报告。

与上面的工具不同，搜索永远不会请求确认——它发生在模型自己的回复内部，因此唯一的控制手段就是你是否启用它。它读取的是不受信任的网页文本，而且会让回合变得比平时更大。当 agent 无人值守运行，或问题本身比较敏感时，请保持关闭。

(code-symbol-index)=
## 代码符号索引

yucode 内置**代码符号索引**，用于<span class="marker">结构化导航</span>——无需依赖外部语言服务器即可查找定义、调用者、引用和实现。该索引<span class="marker">为每个项目分别构建</span>。

### 它是什么

索引是一个静态的符号数据库（函数、类、方法、变量等），内容提取自你项目的源文件。它由名为 [code-symbol-index](https://github.com/hit9/code-symbol-index) 的库构建，该库支持广泛的编程语言。

当索引可用时，`InspectCode` 工具可以：

- **查找符号** —— 按名称模糊匹配
- **检查符号** —— 显示其定义和成员
- **列出引用** —— 项目中的调用、读取、写入和类型引用
- **追踪调用链** —— 传递性的调用者和被调用者
- **文件大纲** —— 单个文件的符号树

询问 `MCPManager` 定义在哪里时，返回的是符号本身，而不是所有提到该词的行：

<div class="term-shot" role="img" aria-label="InspectCode find 查询 MCPManager，返回匹配的符号及其类型、文件、行范围，以及匹配是精确还是模糊。"><span><span class="fs-i fs-dim">query:</span> MCPManager</span><span><span class="fs-i fs-dim">count:</span> 3</span><span> </span><span class="fs-dim">symbols:</span><span>  - <span class="fs-i fs-dim">name:</span> <span class="fs-i fs-sel">MCPManager</span></span><span>    <span class="fs-i fs-dim">kind:</span> class</span><span>    <span class="fs-i fs-dim">file:</span> yucode.py</span><span>    <span class="fs-i fs-dim">range:</span> 4271:5374</span><span>    <span class="fs-i fs-dim">score:</span> <span class="fs-i fs-add">exact</span></span><span>  - <span class="fs-i fs-dim">name:</span> <span class="fs-i fs-sel">TestMCPManagerDiscovery</span></span><span>    <span class="fs-i fs-dim">kind:</span> class</span><span>    <span class="fs-i fs-dim">file:</span> tests/test_mcp.py</span><span>    <span class="fs-i fs-dim">range:</span> 272:573</span><span>    <span class="fs-i fs-dim">score:</span> <span class="fs-i fs-dim">fuzzy</span></span></div>

每条命中都带有其文件和行范围，因此 agent 能精确打开正确的行。同一个索引也会以同样的方式回答“谁调用了这个”和“什么实现了这个”。

```{note}
没有索引时，`InspectCode` 会报告索引不可用。在项目中运行一次 `/index` 即可构建它。
```

### 构建与同步

<span class="marker">运行 `/index` 构建或重建索引。</span>首次构建会遍历每个源文件；后续构建从上一个快照同步，速度会快得多。加上 `force` 参数可以从零重建。

当索引已存在时，yucode 会在启动时于后台刷新它。在一个 agent 回合结束后，它会<span class="marker">自动更新小批量变更的源文件</span>；当大量变更使其过期时，运行 `/index`。`/status` 显示当前状态：

| 状态 | 含义 |
|---|---|
| **synced** | 索引已是最新，随时可用 |
| **stale** | 已过期；等待后台刷新或运行 `/index` |
| **syncing** | 后台刷新正在进行中 |
| **missing** | 尚无索引；运行 `/index` |
| **error** | 索引构建或同步失败；`/status` 显示详细信息 |

项目索引存储在 `.code-symbol-index/index.sqlite` 中，覆盖 Python、JavaScript、TypeScript、Go、Rust、C、C++、Java 等语言。
