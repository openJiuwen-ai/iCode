# 主题文件参考

iCode 支持通过主题文件自定义终端用户界面（Terminal User Interface，TUI）的外观。本页介绍主题文件的格式、常用配色字段和加载规则。通过界面修改并保存主题，请参阅[自定义主题](../guides/configuration/themes.md)。

## 文件位置与命名

主题文件保存在以下目录中；目录不存在时先创建。

| 平台 | 目录 |
| --- | --- |
| macOS / Linux | `~/.chrys/themes/` |
| Windows | `%APPDATA%\chrys\themes\` |

主题文件扩展名必须为 `.yaml` 或 `.yml`，建议使用 UTF-8。iCode 也会自动检测 GBK/GB18030 等常见旧编码，以及带 BOM 的 UTF-16/UTF-32。通过 iCode 保存的主题使用 UTF-8。文件应直接存放在上述目录中；iCode 不读取子目录中的主题文件。

无法按检测到的编码解码的文件与无效 YAML 文件一样，会被跳过并报告警告。

主题名称为文件名去除 `.yaml` 或 `.yml` 扩展名后的部分。主题名称须满足以下要求：

* 以英文字母或数字开头，只使用英文字母、数字、点、下划线和连字符。
* 不能与内置主题重名，比较时不区分大小写。内置主题名称包括 `chrys`、`chrys-legacy`、`chrys-ansi`、主题列表中的 Textual 主题（如 `nord`、`textual-dark`），以及已停用的名称 `chrys-dark`。
* 不能以 `.yaml` 或 `.yml` 结尾，比较时不区分大小写，例如不能使用 `my-theme.yaml.yaml`。

`.yaml` 和 `.yml` 文件名称相同时，优先使用 `.yaml`；如果 `.yaml` 文件无效，仍会尝试使用 `.yml`。同名主题只保留一个。

新增或修改主题文件后，重启 iCode 加载，再按 **F9** 在主题列表中选择对应主题即可应用。

## 文件格式

以下是自定义深色主题 `icode-ocean.yaml` 的示例：

```yaml
# 整体配色
primary: "#5c9ded"
secondary: "#7aa2c7"
accent: "#70c1b3"
warning: "#e5b567"
error: "#e57373"
success: "#81c784"
dark: true
background: "#18212b"
foreground: "#e6edf3"
surface: "#202c38"
panel: "#263442"

# 指定区域的配色
variables:
  footer-background: "#18212b"
  footer-foreground: "#e6edf3"
  input-selection-background: "#5c9ded 35%"
  markdown-block-background: "#202c38"
```

`primary` 是唯一必填字段，其余字段均可按需设置。未指定的颜色由 iCode 自动补齐，具体规则见[默认配色规则](#默认配色规则)。

## 配置字段

主题文件由两部分组成：

* **顶层字段**：设置主题的整体配色。
* **`variables`**：设置特定界面区域的颜色和样式。

`variables` 必须是字符串到字符串的映射。顶层未知字段会导致该主题被跳过。

### 整体配色

以下字段设置主题的基础颜色。

| 配置目标 | 字段 | 说明 |
| --- | --- | --- |
| 主色 | `primary` | 主题的主要标识色，用于标题和强调背景 |
| 次要强调色 | `secondary` | 用于与主色区分的元素 |
| 点缀色 | `accent` | 用于少量需引起注意的元素，通常与 `primary`、`secondary` 形成对比 |
| 状态色 | `warning`、`error`、`success` | 分别用于警告、错误和成功状态的背景、边框及文字 |
| 整体背景 | `background` | 未被控件覆盖区域的默认背景色 |
| 默认文字颜色 | `foreground` | 应在 `background`、`surface` 和 `panel` 三种背景上均清晰可读 |
| 控件背景 | `surface` | 控件的默认背景色，通常用于位于整体背景之上的控件 |
| 区域区分色 | `panel` | 用于在视觉上区分界面中的某个区域与主要内容 |
| 深色或浅色模式 | `dark` | `true` 为深色，`false` 为浅色；影响未填写的背景色和自动配色 |
| ANSI 配色模式 | `ansi` | 默认为 `false`。设为 `true` 时启用终端 ANSI 配色模式，支持使用 `ansi_red`、`ansi_default` 等颜色值；实际 ANSI 颜色由终端配色决定。此模式忽略顶层 `surface` 和 `panel` 的设置，两者均使用透明色 |

颜色字段必须是字符串。十六进制值需要加引号，例如 `"#5c9ded"`；也可以使用 `"rgb(92, 157, 237)"` 或 `"red"` 等颜色表示方式。

顶层颜色字段可用 `"rgba(92, 157, 237, 0.35)"` 表示 35% 不透明度，不支持 `"#5c9ded 35%"` 这种百分比后缀写法。

字段不能留空或设置为 `null`。不需要配置时直接省略。

### 按钮、底栏、选区和光标

以下字段配置在 `variables` 下，仅影响对应的界面元素。`variables` 下的颜色值支持百分比后缀，例如 `input-selection-background: "#5c9ded 35%"` 表示以 35% 不透明度混合该颜色。百分比混色适用于 RGB 颜色；`ansi_*` 颜色忽略不透明度。

| 配置目标 | 字段 |
| ---------- | --------------------------------------------------- |
| 按钮文字       | `button-flat-foreground`                            |
| 按钮悬停文字、背景 | `button-hover-foreground`、`button-hover-background` |
| 按钮禁用文字、背景 | `button-disabled-foreground`、`button-disabled-background` |
| 底栏背景、文字    | `footer-background`、`footer-foreground`             |
| 底栏快捷键背景、文字 | `footer-key-background`、`footer-key-foreground`     |
| 输入选区背景     | `input-selection-background`                        |
| 块状光标背景、文字  | `block-cursor-background`、`block-cursor-foreground` |
| 块状光标文字样式   | `block-cursor-text-style`                           |

未设置的按钮颜色使用主题默认配色。

`block-cursor-text-style` 接受 Textual 文字样式，例如 `"bold"`（默认，粗体）、`"italic"`、`"underline"`、`"reverse"` 或 `"none"`（不附加文字样式）。

### 边框、控件和内容区域

以下字段同样配置在 `variables` 下。未设置的字段使用下表中的默认配色。

| 字段 | 未填写时的颜色 | 影响的界面元素 |
| --- | --- | --- |
| `border-color` | 各区域原有的配色 | 统一普通界面边框；消息、警告和错误状态边框仍保留各自颜色，按钮边框仍跟随按钮背景 |
| `tool-group-title-color` | `warning` | 工具调用组标题 |
| `control-background`、`control-background-muted` | `surface` | 控件的背景及弱化背景 |
| `control-disabled-background` | `background` | 禁用控件背景 |
| `control-foreground-muted` | `text-disabled` | 控件的弱化文字 |
| `overlay-background` | `panel` | 鼠标悬停提示和界面通知的背景 |
| `markdown-block-background` | `surface` | Markdown 引用块和围栏代码块的背景 |
| `hatch-color` | `foreground` 的 15% 不透明度混合色 | 空状态区域的斜线纹理 |

滚动条可通过 `scrollbar`、`scrollbar-hover`、`scrollbar-active` 配置普通、悬停和拖动状态的颜色。`scrollbar-background` 同时决定这三种状态的轨道背景；已有的 `scrollbar-background-hover` 和 `scrollbar-background-active` 可分别覆盖对应状态。

## 默认配色规则

非 ANSI 主题中，未指定的字段使用以下默认值，或根据其他颜色自动计算。ANSI 模式使用终端配色，不应按下表推断最终显示颜色。

| 未填写的字段 | 使用的值 |
| --- | --- |
| `dark` | `true`，即深色模式 |
| `secondary`、`accent`、`warning` | 与 `primary` 相同 |
| `error`、`success` | 与 `secondary` 相同 |
| `background` | 深色模式为 `#121212`；浅色模式为 `#efefef` |
| `foreground` | 背景色的反色 |
| `surface` | 深色模式为 `#1e1e1e`；浅色模式为 `#f5f5f5` |
| `panel` | 根据 `surface`、`primary` 和深浅色模式混合生成 |

以下可选字段位于文件顶层，用于调整自动生成的配色。

| 字段 | 默认行为 | 可调整的内容 |
| --- | --- | --- |
| `luminosity_spread` | 默认 `0.15` | 控制从基础颜色生成明暗色阶时的变化幅度；填写有限数值，不接受 `.nan` 或 `.inf` |
| `boost` | 深色模式且未填写 `panel` 时，在自动生成的面板颜色上叠加与背景对比的黑色或白色，不透明度为 4% | 填写颜色字符串。深色模式且未填写 `panel` 时替代该叠加色，调整自动生成的面板亮度，并作为 `block-hover-background` 的默认值（该颜色的 10% 不透明度）；浅色模式或已填写 `panel` 时不生效 |

顶层还接受 `text_alpha` 字段，须填写有限数值，但目前不会改变显示颜色。

## 加载失败

加载时发现 YAML 格式错误、缺少 `primary`、未知顶层字段或无效字段值时，iCode 会显示警告并跳过该文件。根据警告检查对应文件，修正后重启加载；其他有效主题仍可使用。

主题通过加载后，如果应用时发现 `variables` 值无法用于对应的界面样式，iCode 会显示警告并回退到默认主题 `chrys`。回退不会覆盖已保存的主题选择，修正文件后可重启重试。
