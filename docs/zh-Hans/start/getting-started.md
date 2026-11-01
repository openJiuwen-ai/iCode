# 开始使用 iCode

本教程介绍如何安装 iCode、配置模型，并在项目中使用智能体完成一次开发任务。

## 1. 安装 iCode

使用 [uv](https://docs.astral.sh/uv/getting-started/installation/) 安装 iCode。如果系统中没有 Python 3.14，uv 会自动下载：

```shell
uv tool install iCode-TUI
```

以后升级请运行 `uv tool upgrade iCode-TUI`；有新版本时，iCode 的欢迎页会提示你。卸载请运行 `uv tool uninstall iCode-TUI`。

Windows（x64）和 Linux（x86-64 或 ARM64，glibc 2.27 或更高版本）无需其他准备。Apple 芯片的 Mac 需要先运行 `xcode-select --install` 安装 Xcode 命令行工具，uv 安装时要用它编译一个依赖。

如果以前用过 iCode 离线安装包，请先用 `icode uninstall` 卸载它（见[卸载 iCode](#卸载-icode)）。如果你的版本没有这个命令，请删除它解压到的文件夹：macOS 上是 `~/Library/Application Support/pyapp/chrys`；Linux 上是 `~/.local/share/pyapp/chrys`，设置了 `XDG_DATA_HOME` 时是 `$XDG_DATA_HOME/pyapp/chrys`；Windows 上是 `%LOCALAPPDATA%\pyapp\data\chrys`。然后在 macOS 或 Linux 上删除 `~/.local/bin/chrys`，在 Windows 上删除 `%LOCALAPPDATA%\chrys\bin` 文件夹。

如果 uv 提示 `icode` 已存在，说明已经有同名命令：可能是之前用离线安装包安装的 iCode，也可能是其他同名程序。如果是离线安装包安装的 iCode，请按上文先卸载它，再重新运行安装命令。

### 离线安装包

Intel 芯片的 Mac、Arm 版 Windows、较旧的 Linux 或无法联网的设备，请改用 [iCode Releases](https://github.com/openJiuwen-ai/iCode/releases) 上的预构建离线安装包。安装包已包含 Python 和全部依赖，无需另行安装 Python。

安装脚本会选好适合你电脑的安装包，校验下载的文件并完成安装。以后再运行同一条命令即可升级。在 macOS 或 Linux 上运行：

```bash
curl -fsSL https://raw.githubusercontent.com/openJiuwen-ai/iCode/main/scripts/install.sh | sh
```

在 Windows 上，在 PowerShell 中运行：

```powershell
irm https://raw.githubusercontent.com/openJiuwen-ai/iCode/main/scripts/install.ps1 | iex
```

脚本从 GitHub 下载，GitHub 下载失败时改从 GitCode 下载。如果你所在的网络访问 GitHub 很慢或无法访问，可以从 GitCode 获取脚本，并直接从 GitCode 下载：

```bash
curl -fsSL https://raw.gitcode.com/openJiuwen/iCode/raw/main/scripts/install.sh | sh -s -- --source gitcode
```

```powershell
$env:ICODE_SOURCE = "gitcode"; irm https://raw.gitcode.com/openJiuwen/iCode/raw/main/scripts/install.ps1 | iex
```

要安装指定版本，把命令末尾的 `| sh` 换成 `| sh -s -- --version 0.29.1`。在 Windows 上，先运行 `$env:ICODE_VERSION = "0.29.1"`。用 `$env:` 做的设置在关闭这个 PowerShell 窗口前一直有效。

在 macOS 和 Linux 上，给 `--version` 或 `--source` 显式传入空值（例如 `--version ""` 或 `--source=`）会报错并停止安装。如果通过 shell 变量传入这些选项的值，请确保变量不为空。

如果想手动安装，请下载对应平台的安装包，然后按下面的步骤操作。以下命令中的 `<version>` 和 `<architecture>` 是占位符，请替换为下载文件名中的实际值。

#### macOS

根据处理器选择 `icode-macos-aarch64-v<version>-offline.tar.gz`（Apple 芯片）或 `icode-macos-x86_64-v<version>-offline.tar.gz`（Intel），然后在下载目录运行：

```bash
tar xzf icode-macos-<architecture>-v<version>-offline.tar.gz
chmod +x ./icode
./icode install
```

#### Linux

根据处理器选择 `icode-linux-x86_64-v<version>-offline.tar.gz` 或 `icode-linux-aarch64-v<version>-offline.tar.gz`，然后在下载目录运行：

```bash
tar xzf icode-linux-<architecture>-v<version>-offline.tar.gz
chmod +x ./icode
./icode install
```

Linux x86-64 安装包要求 glibc 2.17 或更高版本，ARM64 安装包要求 glibc 2.18 或更高版本。Alpine Linux 等只提供 musl 的发行版不在预构建包的支持范围内。

#### Windows

下载 `icode-windows-x86_64-v<version>-offline.zip`（Arm 版 Windows 请下载 `icode-windows-aarch64-v<version>-offline.zip`）并解压，在 PowerShell 中进入解压目录，然后运行：

```powershell
.\icode.exe install
```

安装器会尝试将 iCode 加入当前用户的 `PATH`。安装完成后，请打开一个新终端。

### 验证安装

```shell
icode --version
```

命令应输出 iCode 的版本号。如果用 uv 安装后终端找不到 `icode`，请运行 `uv tool update-shell`，然后打开新终端。

使用离线安装包时，在 macOS 或 Linux 上，如果系统找不到 `icode`，请按安装器的提示将 `~/.local/bin` 加入 `PATH`；如果安装器提示 `~/.local/bin/icode` 已存在，该命令会启动其他程序，请改用 `chrys` 命令。在 Windows 上请打开新的终端；如果安装器提示更新用户 PATH 失败，请手动将 `%LOCALAPPDATA%\chrys\bin` 加入用户 `PATH`。

### 卸载 iCode

运行：

```shell
icode uninstall
```

如果安装的是离线安装包，该命令会列出要删除的内容：`icode` 和 `chrys` 命令，以及安装包解压出的文件。确认后即删除。在 Windows 上，它还会把 iCode 从用户 `PATH` 中移除，最后一部分文件会在命令退出后稍等片刻删除。下载的安装包本身仍保留在原处。

如果是用 uv 安装的，该命令只会提示应运行的命令：`uv tool uninstall iCode-TUI`。

iCode 的数据保存在 `~/.chrys`（Windows 上是 `%APPDATA%\chrys`），包括设置、API 密钥、模型配置、智能体、Skill 和会话等。卸载时会保留这个文件夹。如需连同其中的全部内容一并删除，请运行 `icode uninstall --purge`；如果把会话保存在了其他文件夹，那里的 `sessions` 文件夹也会删除。用 uv 安装的，请在运行 `uv tool uninstall` 之前执行，那时 `icode` 命令还在。加上 `--yes` 可跳过确认。

## 2. 在项目中启动 iCode

建议在使用 Git 管理或能够安全恢复的项目中开始本教程。在让智能体修改文件前，请先保存尚未写入磁盘的内容，并使用 Git commit 记录已有变更，以便区分智能体所做的修改，并在需要时恢复到修改前的状态。

在终端中进入要处理的项目目录并启动 iCode：

```shell
cd <project-directory>
icode
```

将 `<project-directory>` 替换为项目目录的实际路径。启动后如需切换目录，请参阅[工作目录](../guides/daily-use/workspaces.md)。

iCode 会先显示 `Starting iCode...`，并在准备期间显示加载界面。进入主界面后，主界面会继续显示加载状态，直到智能体准备就绪。

## 3. 配置模型

1. 按 **F4** 或点击屏幕底部的 `f4 模型`，打开“模型配置”窗口。
2. 点击“新建”。
3. 填写自定义的配置名称，例如 `我的模型`。
4. 在模型选项区域，选择模型提供商和 API 样式。仅 OpenAI 和 DeepSeek（OpenAI）提供 API 样式选项。配置前，请根据模型服务商的文档确认其支持的 API，并按文档建议选择 `Chat Completions` 或 `Responses`。如果服务商仅支持其中一种 API，请选择对应的 API 样式；如果两种 API 均支持，则根据服务商的建议或实际使用需求选择。
5. 填写模型 ID，并根据模型服务的要求填写服务地址和 API 密钥。这些字段名称以所选提供商开头，例如“OpenAI 模型”。
6. 根据模型提供商的说明，填写最大上下文窗口和最大输出词元数。
7. 保存配置并关闭配置窗口。若此前没有可用的模型配置，该配置将自动成为当前使用的模型配置。

> **提示**
>
> 直接填写的 API 密钥会随模型配置文件保存在本机。若不希望将密钥写入配置文件，请[通过环境变量提供 API 密钥](../guides/configuration/models.md#通过环境变量提供-api-密钥)。

### 验证模型连接

在输入栏中提交：

```text
你好
```

iCode 应正常返回问候回复。若模型服务返回错误，请重新检查模型 ID、服务地址、API 密钥等模型配置。

运行期间，输入栏上方的状态栏会显示已运行时长、工具调用次数，以及文字到达时的速度。长时间运行会显示小时和天，例如英文界面的 `1d 4h 20m 50s`，中文界面的 `1天 4小时 20分 50秒`。工具卡片、后台任务、CLI 进度和 F12 耗时面板也支持显示天数；紧凑显示区域只保留较大的时间单位。

智能体答复后，答复标题会显示这一轮的总耗时，例如 `Code Agent - 2:04 AM (6m 5s)`：从你发送消息，或点击 **继续** 或 **重试** 起，到答复完成为止，智能体等待你审批或回答的时间也计算在内。智能体工作期间你发送的消息不会让计时重新开始。重新打开会话时显示的耗时与此相同。

## 4. 使用智能体

### 选择智能体

iCode 默认提供两个主智能体：Q&A Agent（问答智能体）和 Code Agent（代码智能体）。两者都可以阅读项目、分析代码和讨论方案，但定位和可用工具不同：Q&A Agent 主要提供只读能力，适合了解项目、解释代码和分析问题；Code Agent 则可以修改文件、运行命令，适合编写、审查、重构、调试和验证代码。

当前没有正在运行的智能体任务时，可以切换智能体。在输入栏中输入 `#`，输入栏上方会显示智能体列表，可以通过鼠标点击，或使用上下方向键并按回车进行选择。也可以点击输入栏上方状态栏中的智能体名称，在智能体选择窗口中进行切换。

### 使用 Q&A Agent

Q&A Agent 适合在**不修改项目的情况下**了解项目、分析代码、追踪实现和讨论方案。它会基于项目内容给出回答，并尽量提供可追溯的依据。

切换到 **Q&A Agent** 后，在输入栏中输入 `/new` 并回车，开始新会话。

以下示例假设当前项目是一个待办事项应用，仅用于展示使用方式。实际使用时，请根据实际项目调整问题和任务。

```text
请分析任务列表的实现，并为增加“清除已完成任务”功能提出方案。说明需要修改哪些文件以及实现步骤。
```

Q&A Agent 可能会搜索文件、读取文件内容或运行只读命令。工具调用会以卡片形式显示，连续的调用归在同一个标题下。调用进行中，标题显示智能体正在做什么；全部完成后，标题按类别说明调用做了什么，例如 `▸ 读取 3 个文件 · 搜索 2 个模式 · 运行 1 条命令`，如有失败或中断的调用，末尾会标出次数；窗口放不下整行时，先截掉前面的类别，这部分保留。如果只调用了一次且成功，标题改为说明这次调用具体做了什么，例如 `▸ 读取 src/app.py` 或 `▸ 运行 zsh 命令：运行测试`；当前工作文件夹内的路径显示为相对路径。智能体用 `sleep` 等待时，标题显示等待原因和剩余时间的倒计时，例如 `休眠中：等待 CI · 还剩 29 秒`；如不想再等，展开卡片点击**跳过**。完成后标题会说明休眠了多久及原因，例如 `▸ 休眠了 23 秒：等待 CI`。点击标题可显示或隐藏卡片。完成分析后会给出实现方案。

### 使用 Code Agent

Code Agent 适用于需要**修改文件、运行命令和验证结果**的任务。它会先阅读相关代码，再按任务要求实现、调试或重构代码。

Q&A Agent 给出实现方案后，可以在同一会话中切换到 **Code Agent**。切换智能体不会清除当前会话的上下文，Code Agent 可以基于已有方案继续完成开发任务：

```text
请根据刚才讨论的方案实现“清除已完成任务”功能。修改代码并运行必要的测试。
```

如果出现审批对话框，请检查即将执行的工具调用，尤其是命令内容、目标文件和影响范围，确认符合预期后再批准。有关不同审批模式的行为，请参阅[配置审批模式](../guides/configuration/approval.md)。

任务完成后，检查智能体的最终回复，并通过 `/diff` 浏览当前会话记录的文件修改。随后使用 Git 等版本控制工具检查工作目录的完整变更，并确认功能正确、必要的测试已经通过且不包含无关改动。确认无误后，可以提交这些变更。

### 询问 iCode 本身的用法

可以直接在输入栏向智能体询问 iCode 的问题，例如：

- “iCode 有哪些功能？”
- “如何在 iCode 中配置 MCP 服务器？”
- “iCode 的上下文压缩是如何工作的？”

智能体会查阅当前安装版本附带的用户指南，并告诉你参考了哪些页面。如果指南里介绍的功能在当前智能体中没有开启，回答会说明。如果智能体不能读取文件，它会告诉你没有查到指南。

也可以输入 `/help` 或按 **F8** 自己查看用户指南。

### 输入多行消息

按 **Enter** 发送消息，按 **Shift+Enter**、**Ctrl+Enter** 或 **Ctrl+J** 换行。按 **Ctrl+O** 可在更大的编辑器中输入。

有些终端（包括 macOS 自带的“终端”和部分 tmux 配置）无法区分 Shift+Enter 或 Ctrl+Enter 与 Enter，这时按相应的组合键会直接发送消息，请改用 Ctrl+J。

在其他多行输入框中，按 **Enter** 或 **Shift+Enter** 换行。在 Ctrl+O 编辑器中，按 **Ctrl+O** 或 **Ctrl+Enter** 提交。

### 阅读和复制回复

所有智能体的回复都支持 Markdown、数学公式和 Mermaid 图表。你自己发送的消息中的公式不作数学渲染。

列表和引用可以包含标题、表格、代码和公式。

回复中，`$...$` 或 `\(...\)` 包围的公式以单行显示；`math` 代码块、`$$...$$` 或 `\[...\]` 中的块公式可显示分式、矩阵和对齐公式。普通文字、价格和 shell 变量保留原有 Markdown 格式。不支持或过宽的公式会完整显示为可换行的源码。

例如，`$x^{n+1}$` 显示为 xⁿ⁺¹，`$90^\circ$` 显示为 90°。

Mermaid 代码块会显示为图表，点击**打开完整图表**可查看、滚动完整内容。部分图表会简化显示并提示，不支持的图表保留源码。

模型服务以可读文本返回模型的思考时，思考以灰色显示在回复上方的一行中：模型还在写时，**思考：**后滚动显示最新写出的内容；完成后显示**思考：**后接思考的开头，超出行宽的部分不显示。随时点击这一行都可展开查看完整思考或再次折叠，模型仍在写时也可以（`/fold` 可一次折叠或展开全部已完成的思考、工具组和工具调用之间的文字）。模型转入工具调用或回复后，思考会像工具组一样自动折叠；模型在工具调用前写的文字也会折叠为一行，显示其开头。思考没有**复制**按钮，复制回复时也不包含思考。如需在模型写出思考时完整展开，请在设置对话框中开启**默认展开思考**（**通用** → **显示偏好**）。

鼠标选择复制显示结果，不会因屏幕折行额外插入换行。消息标题旁的**复制**保留原始 Markdown 和 LaTeX。重新打开旧会话时，也会使用当前的渲染效果。

## 5. 结束本次使用

若要结束本次使用，输入 `/exit` 或按 `Ctrl+Q` 退出 iCode。

## 下一步

至此，已经完成 iCode 的安装和模型配置，并了解了 Q&A Agent 和 Code Agent 的基本使用方式，以及如何在同一会话中配合完成开发任务。

接下来，可以继续探索：

- [配置智能体](../guides/configuration/agents.md)，创建或修改智能体，并设置其指令、模型、工具和子智能体
- [配置模型](../guides/configuration/models.md)，连接并切换不同的模型服务
- [工作目录](../guides/daily-use/workspaces.md)
- [配置审批模式](../guides/configuration/approval.md)
- 通过 [Skills](../guides/extensions/skills.md) 和 [MCP](../guides/extensions/mcp.md) 扩展 iCode
