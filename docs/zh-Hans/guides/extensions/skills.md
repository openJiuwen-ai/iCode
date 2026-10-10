# 安装和使用 Skills

Skill 是一组可复用的操作说明，可以附带参考资料和脚本，用于扩展智能体处理特定任务的能力。本指南介绍如何为“内置”类型的智能体安装 Skill、确认 Skill 已加载，以及使用 Skill。外部 ACP 智能体是否支持 Skill 以及如何配置，由外部智能体决定。

## Skill 安装位置

根据 Skill 的使用范围和共享需求，选择合适的安装位置：

| 位置 | 适合情况                                 | iCode 中的加载方式 |
| --- |--------------------------------------| --- |
| iCode 用户 Skill 目录 | 希望 iCode 中所有“内置”类型的智能体都能使用           | 始终自动加载 |
| Agent Skills 共享目录 | 希望在 iCode 与其他支持 Agent Skills 的工具之间共享 | 默认自动加载，可针对智能体关闭 |
| `<working-directory>/.agents/skills` | 只希望在当前工作目录中使用                         | 勾选“加载项目 Skills”后才加载，也可针对智能体关闭 |
| 自定义目录 | 希望从指定位置加载                            | 需要在相应智能体配置中手动添加 |

其中，`<working-directory>` 表示当前工作目录。工作目录中的 Skill 随仓库而来，因此需要先在“设置 → 安全 → 项目信任”中勾选“加载项目 Skills”（`project.skills_enabled`），iCode 才会加载。工作目录中有未加载的 Skill 时，iCode 会弹出提示。切换工作目录后，iCode 会重新加载新工作目录中的 Skill。

两个用户级 Skill 目录在不同平台上的路径如下：

| 目录 | macOS 和 Linux | Windows |
| --- | --- | --- |
| iCode 用户 Skill 目录 | `~/.chrys/skills` | `%APPDATA%\chrys\skills` |
| Agent Skills 共享目录 | `~/.agents/skills` | `%USERPROFILE%\.agents\skills` |

对于前三种安装位置，将 Skill 目录复制或移动到对应位置即可。使用自定义目录时，无需移动 Skill，只需在相应智能体的配置中添加 Skill 所在目录。

iCode 会在已配置的 Skill 目录及其最多两层子目录中查找 `SKILL.md` 文件。找到 `SKILL.md` 后，将所在目录识别为一个 Skill 目录，不再继续查找其中嵌套的 Skill。

## 安装并启用 Skill

iCode 用户 Skill 目录始终自动加载。Agent Skills 共享目录默认也会自动加载；当前工作目录中的 Skill 目录在勾选“加载项目 Skills”后加载。若曾在智能体配置中关闭对应来源，需要重新启用。使用自定义目录时，需要将目录添加到相应智能体的配置中。

如需直接在智能体 YAML 中定义不带文件的内嵌 Skill，请参阅[智能体配置文件](../../reference/agent-profile.md#skillsinline)。

如需检查或调整智能体的 Skill 配置，请按以下步骤操作：

1. 在输入栏中输入 `/agents skills`，打开当前智能体的“Skills”标签页。如需配置其他智能体，在左侧列表中选择。
2. 根据 Skill 所在位置，确认对应的 Skill 来源已启用：

   * “从用户文件夹加载 Skills（如果存在）”对应 Agent Skills 共享目录；
   * “从工作文件夹加载 Skills（如果存在）”对应 `<working-directory>/.agents/skills`，仅在“设置 → 安全 → 项目信任”中勾选了“加载项目 Skills”时生效。

3. 如果 Skill 位于其他目录，点击“+ 添加”，填写 Skill 所在目录：

   * 使用相对于工作目录的路径时，勾选“工作区相对路径”。
   * 使用绝对路径时，取消勾选“工作区相对路径”。

4. 如果 Skill 包含脚本，确认脚本扩展名已列入“允许的脚本扩展名”。默认启用 `.py`、`.sh` 和 `.ps1`；其他可选扩展名包括 `.ts`、`.js` 和 `.rb`。运行脚本所需的解释器必须已经安装。
5. 如需调整 Skill 脚本的最长运行时间，在“脚本执行超时（秒）”中填写正整数。
6. 点击“保存”。

> **注意**：Skill 脚本会作为本地进程运行，不受安全沙箱隔离。安装第三方 Skill 前，请检查 `SKILL.md`、脚本及其他相关文件，仅安装来自可信来源的 Skill。iCode 通过 `run_skill_script` 工具运行 Skill 脚本；该工具是否需要确认，取决于智能体审批规则和[当前审批模式](../configuration/approval.md)。

Skill 脚本向标准输出或标准错误输出超过 32 MiB 时，iCode 只保留这部分输出的开头和结尾，中间部分不会保存，结果中会告诉智能体有多少内容没有保留。

## 确认 Skill 已加载

保存智能体配置并返回会话后，iCode 会重新加载 Skill。如果直接在磁盘上添加 Skill 目录或修改其中的文件，iCode 不会实时监控这些变化。下一次发送消息时，iCode 会先重新扫描当前智能体已启用的 Skill 目录，再处理该消息。

可通过以下任一方式确认 Skill 已加载：

* 在输入栏中输入 `/` 后，继续输入 Skill 名称（例如 `/release-`）。自动补全列表会随输入内容筛选匹配项；Skill 名称出现在“已加载 Skills”分组中，即表示已加载。
* 输入 `/runtime`，或点击输入栏上方状态栏右端显示的 Skill 数量，打开“运行时详情”窗口，然后在“Skills”标签页中查看 Skill 名称和来源。

如果 Skill 未出现，请依次检查：

* 使用 Agent Skills 共享目录或工作目录 Skill 目录时，是否已启用对应选项（工作目录 Skill 目录还需勾选“加载项目 Skills”）；使用自定义目录时，是否已将其添加到当前智能体。
* Skill 目录是否位于所选位置或其最多两层子目录中。
* Skill 是否符合[创建 Skill 的基本规范](#创建-skill-的基本规范)。

## 使用 Skill

Skill 通常无需手动调用。当任务与某个 Skill 的描述匹配时，智能体会自动加载该 Skill 的完整说明，并按照其中的指引执行任务。例如：

```text
根据当前仓库从上一个版本以来的变更起草发布说明。
```

也可以在消息开头使用 `/skill-name` 显式调用已加载的 Skill。例如：

```text
/release-notes 根据当前仓库从上一个版本以来的变更起草发布说明。
```

如果 Skill 名称与 iCode 的内置斜杠命令相同，则无法使用 `/skill-name` 显式调用该 Skill。此时，可以在消息中直接指定 Skill 名称，例如：

```text
使用 release-notes Skill，根据当前仓库从上一个版本以来的变更起草发布说明。
```

当前会话首次加载该 Skill 时，会显示 `load_skill` 工具调用卡片，其中包含 `release-notes` Skill 名称；随后智能体会按照 Skill 说明完成任务。

## 同名 Skill 的优先级

从前文列出的[Skill 安装位置](#skill-安装位置)加载的 Skill 都属于目录 Skill，每个 Skill 都包含 `SKILL.md`。iCode 还支持直接在智能体配置文件中定义内嵌 Skill，这类 Skill 不需要 Skill 目录或 `SKILL.md`。

如果发现多个同名 Skill，iCode 只会加载优先级最高的一个。优先级从高到低依次为：

1. 智能体“Skills”标签页中手动添加的目录，越靠上优先级越高（新添加的目录位于列表顶部）；
2. iCode 用户 Skill 目录；
3. Agent Skills 共享目录；
4. 当前工作目录中的 Skill；
5. 智能体配置文件中的内嵌 Skill，按其定义顺序。

如果同一个安装位置或自定义目录中存在多个同名 Skill，其发现顺序不确定。为确保加载指定版本，请仅保留其中一个。

## 附录

### 创建 Skill 的基本规范

每个 Skill 使用独立目录，并在目录根层提供一个 `SKILL.md`。以下示例是一个可加载的最小 Skill：

```markdown
---
name: release-notes
description: 根据提交记录起草面向用户的发布说明
---

# 起草发布说明

1. 读取上一个版本以来的提交记录。
2. 按新增、修复和兼容性变化分类。
3. 只写用户可观察到的变化。
```

#### 编写 `SKILL.md`

`SKILL.md` 建议使用 UTF-8。iCode 也会自动检测 GBK/GB18030 等常见旧编码，以及带 BOM 的 UTF-16/UTF-32。文件开头是由 `---` 包围的 YAML 配置区，也称为 frontmatter；之后是提供给智能体的操作说明。frontmatter 应写成有效的 YAML 键值对象；若不是有效的 YAML，iCode 会尝试逐行读取并记录警告。iCode 按以下规则校验文件内容：

| 内容 | 加载要求 |
| --- | --- |
| frontmatter | 必须存在，且顶层内容应为 YAML 键值对象，不能使用锚点（`&`）或别名（`*`）；长度不超过 16384 个字符，嵌套不超过 64 层 |
| `name` | 必填；必须与 Skill 目录名完全一致；长度为 1～64 个字符；只能包含小写字母、数字和连字符（`-`）。不能包含大写字母、下划线（`_`）等其他字符，也不能以连字符开头或结尾，不能包含连续连字符 |
| `description` | 必填；须为文本，长度不超过 1024 个字符。建议同时说明 Skill 用途和适用场景，帮助智能体判断何时加载 |
| `compatibility` | 可选；须为文本，长度不超过 500 个字符 |
| `license`、`allowed-tools` | 可选；iCode 读取其文本，但不额外校验其内容 |
| `metadata` | 可选；必须是值为纯文本或数字的 YAML 对象，否则 iCode 不会保留该字段 |
| 正文 | iCode 不额外限制长度或结构 |

为了确保 Skill 能够稳定加载，请使用有效的 YAML 编写 frontmatter。文件无法按检测到的编码解码，缺少 frontmatter、`name` 或 `description`，或者字段值未通过上述校验时，iCode 不会加载该 Skill。可选字段写成列表或对象时会被忽略。

几个可选字段分别用于：

* `compatibility`：说明 Skill 依赖的运行环境，例如适用的产品、所需的系统软件包或网络访问条件。
* `license`：声明 Skill 采用的许可证，可以填写许可证名称或 Skill 目录中许可证文件的名称。
* `allowed-tools`：按照 Agent Skills 规范声明可预先批准运行的工具。该字段仍处于实验阶段；iCode 当前只读取并保留字段值，不会据此自动授予工具权限。

iCode 的 Skill 目录和 `SKILL.md` 格式遵循 Agent Skills 通用规范。上述字段的详细写法和 Skill 的完整规范参阅 [Agent Skills 官方规范](https://agentskills.io/specification)。

#### 添加脚本和资源

Skill 可以按需附带脚本、参考资料和其他文本资源。

例如：

```text
release-notes/
├── SKILL.md
├── scripts/
│   └── collect.py
├── references/
│   └── style-guide.md
└── assets/
    └── template.html
```

iCode 只会将以下扩展名的文本文件识别为 Skill 资源：`.md`、`.txt`、`.rst`、`.html`、`.htm`、`.xml`、`.svg`、`.json`、`.jsonl`、`.yaml`、`.yml`、`.toml`、`.csv`、`.tsv`、`.ini`、`.cfg`、`.css`。

文本资源与 `SKILL.md` 使用相同的自动编码检测，建议使用 UTF-8。此外：

* 脚本扩展名必须列入智能体配置的“允许的脚本扩展名”。
* iCode 只读取 Skill 目录根层和直接子目录中的文件。例如，`guide.md` 和 `references/guide.md` 会被发现，`references/api/guide.md` 则会被忽略。
* iCode 会跳过 Skill 目录内部通过符号链接或 Windows 目录联接访问的文件和目录。这两类链接都指向磁盘中的其他位置；如需使用其中的内容，请将实际文件放入 Skill 目录。

不符合扩展名、目录层级或链接规则的文件不会出现在 Skill 的资源或脚本清单中；资源不存在、无法访问或无法解码时，会在读取时失败。这些情况不影响符合要求的 `SKILL.md` 加载。
