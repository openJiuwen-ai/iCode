# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Interactive gallery of Markdown, math, UML and every supported Mermaid family.

Usage:
    uv run python playground/markdown_viewer.py

Keybindings:
    s — Switch to streaming demo
    r — Reset to the full static document
    c — Toggle copy buttons on code fences
    t — Toggle the table of contents (useful in narrow terminals)
    q — Quit
"""

from __future__ import annotations

import asyncio

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Footer

from chrys.app.tui.screens.dialogs.diagram import DiagramDialog
from chrys.app.tui.theme import CHRYS_THEME, TuiVariableDefaultsMixin
from chrys.app.tui.widgets.markdown.copyable import CopyableMarkdown
from chrys.app.tui.widgets.markdown.diagram.messages import DiagramOpenRequested
from chrys.app.tui.widgets.markdown.stream import MarkdownStream
from chrys.app.tui.widgets.markdown.toc import MarkdownTableOfContents
from chrys.app.tui.widgets.markdown.viewer import VirtualizedMarkdownViewer
from chrys.app.tui.widgets.markdown.widget import VirtualizedMarkdown

MARKDOWN = r"""
# Markdown Rendering Gallery

这是 iCode 当前 Markdown 渲染能力的交互式画廊：中英文排版、代码、表格、数学公式和 Mermaid 图表。

**快速预览：** [基础格式](#basic-markdown) · [公式](#math) · [UML 与架构](#uml-and-architecture) ·
[数据图表](#data-charts) · [规划图表](#planning-and-structure) · [回退示例](#fallback-and-boundaries)

左侧目录可跳转；`t` 隐藏目录以增加正文宽度；`s` 演示流式输出；`r` 恢复画廊；
`c` 切换代码复制按钮；`q` 退出。调整终端宽度可观察表格、公式和图表的变化。

---

## Basic Markdown

### Text and styles

普通文字、**粗体**、*斜体*、***粗斜体***、~~删除线~~，以及 `inline_code()` 可以混合使用。
**加粗文字中包含 *斜体* 和 `代码`**。中文 English 123 混排，用于观察字宽和自动折行。

转义标点：\*不是斜体\*、\_不是强调\_、\[not a link\]。HTML 保留文字：<file>、List<String>。

这两行源码之间是普通换行，
会在同一段落里排版。

反斜杠硬换行：第一行\
第二行。<br>这里使用 `<br>` 再换一行。

#### Heading level 4

##### Heading level 5

###### Heading level 6

标题支持六级；页面标题、章节和小节展示了前三个级别。

### Lists and quotes

- 无序列表第一项，含 **强调**。
- 第二项含嵌套内容：
  1. 有序子项。
  2. 子项中的 `代码` 与 $x^2$。
     - 第三层，观察缩进和换行。
- 最后一项。

3. 有序列表可以从 3 开始。
4. 后续序号自动递增。

- [x] 清单标记作为列表文字显示。
- [ ] 这里不是可点击的复选框。

> 引用可以包含 **粗体**、链接和公式 $E = mc^2$。
>
> > 第二层引用。
> >
> > - 引用中的列表。
>
> 回到第一层。

### Container layout

引用内的标题、表格、代码和公式会保留引用边界；反向嵌套时，引用位于列表项内部。

> #### 引用中的标题
>
> | 项目 | 结果 |
> |------|------|
> | 嵌套排版 | 保留边界 |
>
> ---
>
> - 列表中的代码块：
>
>   ```python
>   result = 42
>   ```
>
>   同一个列表项中的下一段。
>
> $$\frac{n!}{(n-k)!}$$
>
> 引用结束前的最后一段。

- 列表中的引用：
  > 第一层引用。
  > - 引用内再嵌套列表；缩窄窗口时，续行与正文对齐。
  >
  > 回到引用段落。

  回到外层列表项。

9999. 长序号与自动折行：abcdefghijklmnopqrstuvwxyz，正文和复制结果都应完整。
10000. 序号位数增加后，仍为正文预留正确的宽度。

### Tables and links

| 文字 | 示例 | 数值 |
|------|------|------|
| **文字样式** | `code` | 123.45 |
| 第一行<br>第二行 | $x^{n+1}$ | 42 |
| 中文和 English | [本页公式](#math) | -7 |

链接：[HTTPS 链接](https://example.com)、[引用式链接][example]、<https://example.com/docs>。
显式 HTTP(S) 地址会自动成为链接：https://example.com/docs。普通文件名 `app.py` 保持文字。

[example]: https://example.com/reference "Reference link"

图片以图标和替代文字显示，不会下载图片：

![示例图片的替代文字](https://example.com/preview.png)

### Code fences

```python
from math import factorial


def choose(n: int, k: int) -> int:
    "Return a binomial coefficient."
    return factorial(n) // (factorial(k) * factorial(n - k))
```

```json
{"name": "iCode", "features": ["markdown", "math", "mermaid"], "enabled": true}
```

```bash
# 代码中的美元符、LaTeX 和 HTML 都保持源码。
printf '%s\n' "$HOME/$USER"
echo '$x^2$' '\frac{1}{2}' '<br>'
```

    缩进代码块也保留原文。
    **bold** $x^2$ <br>

---

## Math

行内公式只占一行；块公式可显示二维排版。鼠标选择复制当前显示的内容。

### Inline notation

| 类型 | 渲染效果 |
|------|----------|
| 两种行内定界符 | $E = mc^2$，\(ax^2 + bx + c = 0\) |
| 强调与标点 | **$x^2$**，*$x_1$*，Is $x^2$? |
| 上下标与角度 | $x^{n+1}$，$a_{i,j}$，$90^\circ$，${}^{14}C$ |
| 函数与复杂度 | $O(n \log n)$，$2\sin x$，$\operatorname{tr}(A)$ |
| 分式与阶乘 | $\frac{n!}{(n-k)!}$，$\frac{(2n-1)!!}{(2n)!!}$ |
| 根号与重音 | $\frac{\sqrt{3}}{2}$，$\hat{\beta}_1$，$\hat\sigma^2$ |
| 集合与关系 | $\{x : x > 0\}$，$x\not\in A$，$f\circ g$ |
| 字体与正文 | $\mathbf{v}$，$\mathbb{R}$，$\mathcal{L}$，$\mathrm{d}x$ |
| 积分与求值 | $\int_0^1 x^2\,\mathrm{d}x$，$\left.x^2\right\vert_0^1$ |
| 注记与模运算 | $a\overset{def}{=}b$，$a\equiv b\pmod n$ |
| 绝对值与范数 | \(\lvert -x\rvert\)，$\lVert v\rVert$ |
| 行中块定界符 | \[x^2+y^2=z^2\]，$$x^2+y^2=z^2$$ |

### Three display forms

`math` 代码块：

```math
x = \frac{-b \pm \sqrt{b^2 - 4ac}}{2a}
```

`$$...$$`：

$$
\sum_{k=0}^{n}\binom{n}{k}x^k y^{n-k} = (x+y)^n
$$

`\[...\]`：

\[
\int_0^1 x^2\,\mathrm{d}x = \left.\frac{x^3}{3}\right|_0^1 = \frac{1}{3}
\]

### Roots and nested fractions

```math
\sqrt[3]{\frac{a+b}{c}} + \frac{1}{1+\frac{1}{x}}
```

### Matrices and delimiters

```math
A=\begin{pmatrix}a&b\\c&d\end{pmatrix}
\quad B=\begin{bmatrix}1&0\\0&1\end{bmatrix}
\quad \det A=\begin{vmatrix}a&b\\c&d\end{vmatrix}
```

```math
\begin{matrix}a&b\\c&d\end{matrix}
\quad\begin{Bmatrix}a&b\\c&d\end{Bmatrix}
\quad\begin{Vmatrix}a&b\\c&d\end{Vmatrix}
\quad\begin{smallmatrix}a&b\\c&d\end{smallmatrix}
```

### Cases and aligned equations

```math
|x|=\begin{cases}x&x\geq 0\\-x&x<0\end{cases}
```

```math
\begin{aligned}
a &= b+c \\
  &= d \\
f(x) &:= x^2+1
\end{aligned}
```

```math
\begin{gathered}x+y=3\\x-y=1\end{gathered}
\qquad
\begin{split}(x+y)^2 &= x^2+2xy+y^2\\ &= 9\end{split}
```

### Limits, accents and annotations

```math
\lim_{x\to0}\frac{\sin x}{x}=1
\qquad \sum_{i=1}^{n}i=\frac{n(n+1)}{2}
```

```math
\int\limits_0^1 f(x)\,\mathrm{d}x
\qquad \sum\nolimits_{i=1}^{n}x_i
\qquad \prod_{i=1}^{n}x_i
```

```math
\hat{\beta}_1+\bar{x}+\vec{v}+\dot{x}+\ddot{x}
\qquad a\overset{def}{=}b \qquad x\underset{n\to\infty}{\longrightarrow}y
```

```math
\boxed{\mathbb{E}[X]=\sum_{i=1}^{n}p_i x_i}
```

### Formulas inside containers

> 引用中的块公式：
>
> $$\frac{a+b}{c}$$

1. 列表里的公式保持缩进：

   ```math
   \frac{(n+1)!}{n!}=n+1
   ```

---

## UML and Architecture

当前 UML 示例使用 Mermaid。点击图表下方的 **Open full diagram / 打开完整图表** 可查看完整内容。
在完整视图中，空格切换图表和源码，`c` 复制 Mermaid 源码，`Esc` 返回。

### Flowchart

```mermaid
flowchart LR
    Start([Start]) --> Check{Valid?}
    Check -->|yes| Save[(Database)]
    Check -->|no| Retry[Retry]
    Retry -.-> Check
    Save ==> End([Done])
```

### Groups and metadata

这个示例带分组；可对照图表旁的简化提示查看当前效果。

```mermaid
flowchart TD
    subgraph client[Client]
        A[Web] & B[Mobile] --> C[Gateway]
    end
    C --> D@{shape: cyl, label: Orders}
    C --> E@{shape: rect, label: Customer's order}
```

### Sequence diagram

这个示例包含编号、激活和注释；未完整呈现的部分会显示简化提示。

```mermaid
sequenceDiagram
    autonumber
    actor User
    participant API
    participant DB@{ "type": "database", "alias": "DB#59;read" }
    User->>API: Request
    activate API
    API->>DB: SELECT order
    DB-->>API: Result
    Note over API,DB: Cache miss; read from storage
    API-->>User: Response
    deactivate API
```

### Class diagram

```mermaid
classDiagram
    class Repository~T~ {
        +find(id) T
        +save(item) bool
    }
    class User {
        +String name
        +login() bool
    }
    class Session
    Repository o-- User : stores
    User "1" --> "*" Session : owns
```

### State diagram

```mermaid
stateDiagram-v2
    [*] --> Idle
    Idle --> Running : start
    Running --> Paused : pause
    Paused --> Running : resume
    Running --> [*] : finish
```

### Entity relationships

```mermaid
erDiagram
    USER ||--o{ ORDER : places
    USER {
        int id PK
        string name
    }
    ORDER {
        int id PK
        int user_id FK
        decimal total
    }
```

### Requirements

```mermaid
requirementDiagram
    requirement reliable {
        id: REQ-01
        text: Preserve user data
        risk: high
        verifymethod: test
    }
    element storage {
        type: component
    }
    storage - satisfies -> reliable
```

### C4 context

```mermaid
C4Context
    Person(user, "Customer", "Places orders")
    System(shop, "Shop", "Order system")
    System_Ext(pay, "Payments", "External gateway")
    Rel(user, shop, "Orders")
    Rel(shop, pay, "Pays")
```

### C4 containers

```mermaid
C4Container
    Container(api, "API", "Python", "Handles requests")
    ContainerDb(db, "Database", "PostgreSQL", "Stores orders")
    Rel(api, db, "Reads and writes", "SQL")
```

### C4 components

```mermaid
C4Component
    Component(handler, "Handler", "Python", "Validates requests")
    Component(repo, "Repository", "Python", "Stores orders")
    Rel(handler, repo, "Calls")
```

### Architecture

```mermaid
architecture-beta
    group backend(cloud)[Backend]
    service api(server)[API] in backend
    service db(database)[Database] in backend
    service cache(database)[Cache] in backend
    api:R --> L:db
    api:B --> T:cache
```

---

## Data Charts

### Pie proportions

```mermaid
pie showData title Work distribution
    "Features": 50
    "Tests": 30
    "Docs": 20
```

### Bars and lines

```mermaid
xychart-beta
    title "Quarterly revenue"
    x-axis "Quarter" [Q1, Q2, Q3, Q4]
    y-axis "Revenue" 0 --> 100
    bar "Actual" [40, 65, 70, 90]
    line "Target" [45, 60, 75, 85]
```

### Horizontal chart

```mermaid
xychart horizontal
    title "Latency"
    x-axis [Read, Write, Search]
    y-axis "ms" 0 --> 100
    bar [20, 55, 80]
```

### Quadrants

```mermaid
quadrantChart
    title Effort and impact
    x-axis Low effort --> High effort
    y-axis Low impact --> High impact
    quadrant-1 Plan
    quadrant-2 Do now
    quadrant-3 Later
    quadrant-4 Reconsider
    Search: [0.25, 0.8]
    Export: [0.7, 0.6]
    Theme: [0.3, 0.2]
```

### Treemap

```mermaid
treemap-beta
    "Project"
        "Source": 60
        "Tests": 30
        "Docs": 10
```

### Sankey flows

```mermaid
sankey-beta
Visitors,Signup,40
Visitors,Browse,60
Signup,Purchase,15
Signup,Trial,25
```

---

## Planning and Structure

### Journey

```mermaid
journey
    title Release experience
    section Prepare
    Write code: 4: Developer
    Review: 3: Developer, Reviewer
    section Ship
    Test: 5: Reviewer
    Deploy: 4: Developer
```

### Timeline

```mermaid
timeline
    title Project milestones
    January : Prototype
    February : Math : Diagrams
    March : Review : Release
```

### Kanban

```mermaid
kanban
    todo[Todo]
        docs[Write docs]@{ assigned: "Alice" }
    doing[In progress]
        math[Math rendering]@{ assigned: "Bob", priority: 'High' }
    done[Done]
        parser[Markdown parser]
```

### Mindmap

```mermaid
mindmap
    root((iCode))
        Markdown
            Text
            Tables
        Math
            Inline
            Display
        Mermaid
            UML
            Charts
```

### Gantt schedule

```mermaid
gantt
    title Release plan
    dateFormat YYYY-MM-DD
    section Build
    Renderer :a, 2026-10-01, 3d
    Tests :b, after a, 2d
    section Ship
    Documentation :c, after a, 2d
    Release :milestone, after b c, 0d
```

### Git history

```mermaid
gitGraph
    commit id: "base"
    branch rendering
    commit id: "math"
    commit id: "diagrams"
    checkout main
    merge rendering
    commit id: "release"
```

### Packet layout

```mermaid
packet-beta
    title Demo packet
    0-3: "Version"
    4-7: "Flags"
    8-15: "Type"
    16-31: "Length"
    32-63: "Payload"
```

### Block layout

```mermaid
block-beta
    columns 3
    a["Input"] b["Parser"] c["Output"]
    a --> b
    b --> c
```

---

## Fallback and Boundaries

以下是刻意保留的边界示例，不应变成乱码或丢失内容。

### Ordinary text stays ordinary

价格 $5 和 $10；变量 $HOME/$USER、${VAR:-default}、echo $x_1$?。
普通括号 \(in progress\)、\(see [note](https://example.com/guide_(intro)) here\) 保留文字和链接。
行内代码 `$x^2$`、`\frac{1}{2}` 不会被识别为公式。

### Unsupported formula

未知命令整段显示源码：$\unknowncommand{x}$。

```math
\underbrace{a+b}_{explanation}
```

### Narrow viewport

缩窄窗口后，下面过宽的公式应完整折行显示源码；放宽窗口可恢复排版。

```math
\boxed{\frac{a_1+a_2+a_3+a_4+a_5+a_6+a_7+a_8+a_9+a_{10}}{b_1+b_2+b_3+b_4+b_5+b_6+b_7+b_8+b_9+b_{10}}}
```

### Unsupported diagram

这个 Mermaid 边缺少终点，应保留完整代码：

```mermaid
flowchart LR
    A -->
```

其他图语言作为普通代码块显示：

```plantuml
@startuml
Alice -> Bob: Hello
@enduml
```

---

## Try Streaming

按 `s` 逐段写入正文、代码、表格、公式与时序图；观察定界符和代码围栏闭合后内容如何变化。
按 `r` 回到这个完整画廊。
""".lstrip()

STREAM_CHUNKS = [
    "# Streaming Demo\n\n",
    "这是逐段到达的 **Markdown**，包含公式、图表和代码。\n\n",
    "## Inline math\n\n",
    r"复杂度：$O(n \log",
    " n)$，",
    r"以及 \(x^{n+1}",
    "\\)。\n\n",
    "## Display math\n\n$$\n",
    r"x=\frac{-b\pm\sqrt{b^2-4ac}}{2a}",
    "\n$$\n\n",
    "\\[\n",
    r"\int_0^1 x^2\,\mathrm{d}x=\frac{1}{3}",
    "\n\\]\n\n",
    "```math\n",
    r"\begin{pmatrix}a&b\\c&d\end{pmatrix}",
    "\n```\n\n",
    "## Sequence diagram\n\n```mermaid\nsequenceDiagram\n",
    "participant User\nparticipant API\n",
    "User->>API: Request\n",
    "API-->>User: Response\n",
    "```\n\n",
    "## Code and table\n\n```python\n",
    "def square(x):\n    return x * x\n",
    "```\n\n",
    "| Stage | State |\n|-------|-------|\n",
    "| Parse | Complete |\n",
    "| Render | Complete |\n\n",
    "> 流式输出完成。按 `r` 恢复完整画廊。\n",
]


class MarkdownPlayground(TuiVariableDefaultsMixin, App):
    CSS = """
    MarkdownTableOfContents { width: 30; }
    MarkdownTableOfContents > Tree { width: 1fr; }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("s", "stream_demo", "Stream Demo"),
        Binding("r", "reset", "Reset"),
        Binding("c", "toggle_copy", "Toggle Copy Buttons"),
        Binding("t", "toggle_toc", "Toggle Contents"),
    ]

    _copyable: bool = False

    def __init__(self) -> None:
        super().__init__()
        self.register_theme(CHRYS_THEME)
        self.theme = CHRYS_THEME.name

    def compose(self) -> ComposeResult:
        yield VirtualizedMarkdownViewer(MARKDOWN, show_table_of_contents=True)
        yield Footer()

    def on_diagram_open_requested(self, event: DiagramOpenRequested) -> None:
        event.stop()
        self.push_screen(DiagramDialog(event.diagram))

    async def action_stream_demo(self) -> None:
        """Replace content with a streaming demo."""
        md_widget = self.query_one(VirtualizedMarkdown)
        await md_widget.update("")
        md_widget.scroll_home(animate=False)
        stream: MarkdownStream = VirtualizedMarkdown.get_stream(md_widget)
        try:
            for chunk in STREAM_CHUNKS:
                await stream.write(chunk)
                await asyncio.sleep(0.15)
                md_widget.scroll_end(animate=False)
        finally:
            await stream.stop()
        md_widget.scroll_end(animate=False)

    async def action_reset(self) -> None:
        """Restore the original document."""
        md_widget = self.query_one(VirtualizedMarkdown)
        await md_widget.update(MARKDOWN)
        md_widget.scroll_home(animate=False)

    def action_toggle_toc(self) -> None:
        viewer = self.query_one(VirtualizedMarkdownViewer)
        viewer.show_table_of_contents = not viewer.show_table_of_contents

    async def action_toggle_copy(self) -> None:
        """Swap the inner markdown widget between standard and copyable."""
        self._copyable = not self._copyable
        viewer = self.query_one(VirtualizedMarkdownViewer)

        # Remove the current inner markdown widget
        old_md = viewer.query_one(VirtualizedMarkdown)
        source = old_md.source
        scroll_y = old_md.scroll_y
        await old_md.remove()

        # Mount the replacement
        toc = viewer.query_one(MarkdownTableOfContents)
        new_md = CopyableMarkdown() if self._copyable else VirtualizedMarkdown()
        toc.markdown = new_md
        await viewer.mount(new_md, before=toc)
        await new_md.update(source)
        new_md.scroll_to(y=scroll_y, animate=False)
        self.notify("Copy buttons " + ("ON" if self._copyable else "OFF"))


if __name__ == "__main__":
    from chrys.orchestration.startup import bootstrap_runtime

    bootstrap_runtime(setup_telemetry=False)
    MarkdownPlayground().run()
