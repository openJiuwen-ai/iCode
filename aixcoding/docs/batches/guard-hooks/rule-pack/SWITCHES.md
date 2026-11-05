# 护栏开关与规则管理

一句话：**护栏默认开着**；`guardctl.py` 管开关，`rules.json` 管规则内容。

本文回答四个问题：

1. 怎么把安全护栏整体关掉 / 打开？
2. 怎么单独开关某几条规则？
3. 怎么增删规则、改规则内容？
4. 对方电脑没有 uv / 不知道怎么跑 python，怎么操作？

> **不需要 uv。** `guardctl.py` 只用 Python 标准库；Windows 上有 `guardctl.cmd`、
> macOS / Linux 上有 `guardctl.sh`，双击 / 直接执行就能跑（见下一节）。文中的
> `uv run python …` 只是仓库开发者的写法。

> 日常运维（排查、误报处理、版本升级）见 [OPERATIONS.md](OPERATIONS.md)；
> 规则设计与映射关系见 [README.md](README.md)。

---

## 零、命令怎么敲起来（对方没有 uv 也能用）

`guardctl.py` **只用 Python 标准库**（3.9+），跟 `uv`、跟 iCode 主程序都没有关系。
先定一个“脚本”指代它，后面所有命令都是「脚本 + 子命令」：

| 场景 | 怎么敲 |
| --- | --- |
| Windows，护栏已装（推荐） | `guardctl.cmd status` |
| Windows，护栏已装，想直接用 python | `python "%APPDATA%\chrys\hooks\guard\guardctl.py" status` |
| macOS / Linux，护栏已装（推荐） | `~/.chrys/hooks/guard/guardctl.sh status` |
| macOS / Linux，想直接用 python | `python3 ~/.chrys/hooks/guard/guardctl.py status` |
| 仓库内（开发调试） | `uv run python src/chrys/kernel/aixcoding/guard/pack/guardctl.py status` |

> 下文统一写成 `guardctl.py <子命令>`，换成上表里的任一种写法即可。

### Windows 最省事：`guardctl.cmd`

装好护栏之后，`<config_dir>/hooks/guard/guardctl.cmd` 就在那儿：

```powershell
& "$env:APPDATA\chrys\hooks\guard\guardctl.cmd" status
& "$env:APPDATA\chrys\hooks\guard\guardctl.cmd" off
```

也可以**直接双击**（不带参数时打印状态，完事后停住等你按键）。

它内部借护栏自带的 `_launcher.ps1` 现找一个能用的解释器：先扫 PATH（跳过 Microsoft
应用商店那个只会弹商店的 0 字节替身），再找 iCode 打包版自带的解释器
（`%LOCALAPPDATA%\pyapp\data\chrys\…\python.exe`）。**所以不需要 uv，也不需要
PATH 里有 python。**

### macOS / Linux 最省事：`guardctl.sh`

装好护栏之后，`<config_dir>/hooks/guard/guardctl.sh` 就在那儿：

```bash
~/.chrys/hooks/guard/guardctl.sh status
```

`guardctl.sh` 是 Windows 那个 `guardctl.cmd` 的 POSIX 对应物：先扫 PATH 找
`python3` / `python`，再找 iCode 打包版自带的解释器
（`~/.local/share/pyapp/data/chrys/…/python/bin/python3`）。安装时已经带上了可执行位，
直接跑即可；仓库里那份还没加位时用 `sh guardctl.sh status` 也一样。

不想用脚本就直接 `python3 ~/.chrys/hooks/guard/guardctl.py status`。

### 连 python 都没有：直接改状态文件

开关全在一个 JSON 文件里，文本编辑器就能改——`<config_dir>/hooks/guard/guard_state.json`：

```jsonc
{
  "version": 1,
  "enabled": true,                            // 总开关：false = 整个护栏停用
  "rules": { "T-PY-01": false },              // 单条规则：false = 关掉这条
  "write_scope": { "allow_outside_workspace": false }  // false = 只允许写工作区内
}
```

改完**立即生效**，不用重启 iCode（状态文件每次 hook 触发都重读）。删掉这个文件
= 回到出厂默认（总开关开、规则按出厂、写工作区外不限制）。

> 顺带一提：护栏的 hook 本身就是 Python 脚本，iCode 靠它拦截请求——所以只要护栏
> 还在正常工作，机器上就一定有一个能用的 Python 解释器。

---

## 一、先分清三个文件

| 文件 | 谁维护 | 会被覆盖吗 | 管什么 |
| --- | --- | --- | --- |
| `<config_dir>/hooks/guard/rules.json` | 规则包（随 iCode 发布） | **会** —— 每次启动 iCode 都重新安装覆盖 | 规则内容、词典、**出厂**开关与设置 |
| `<config_dir>/hooks/guard/guard_state.json` | 你（用 `guardctl.py` 改） | 不会 | 总开关、单条规则开关、写入范围开关 |
| `guardctl.py` | 规则包 | 会 | 开关管理程序本体（安装后落在 `<config_dir>/hooks/guard/`） |
| `<config_dir>/hooks/guard/guardctl.cmd` | 规则包 | 会 | Windows 上的免 uv 入口（双击即可，见第零节） |
| `<config_dir>/hooks/guard/guardctl.sh` | 规则包 | 会 | POSIX 上的免 uv 入口（见第零节） |

`<config_dir>` 在 Windows 上是 `%APPDATA%\chrys`，其余平台是 `~/.chrys`。

> **关键**：开关写在 `guard_state.json`，**不要写进 `rules.json`**。
> `rules.json` 是随包分发的出厂文件，iCode 每次启动都会把它重新装到
> `<config_dir>/hooks/guard/`，写进去的开关下次启动就没了。`guard_state.json`
> 不在安装清单里，升级、重装都不会动它。

判定优先级统一是：**状态文件的覆盖 > `rules.json` 的出厂值**。

---

## 二、开关整个护栏（总开关）

**默认就是开的。** 没有状态文件 = 出厂默认 = 开。

```bash
guardctl.py status   # 看当前状态
guardctl.py off      # 关掉整个护栏
guardctl.py on       # 再打开
```

关掉之后：两个 hook 仍然被调用，但**一律放行**，不再拦任何请求；留痕里也不会
再记命中。

改开关**不需要重启 iCode** —— 状态文件每次 hook 触发都重新读。

`status` 会打印四行关键信息，看不懂时先看这里：

```
安全护栏总开关：开
写入范围：不限工作区
开关状态：C:\Users\xxx\AppData\Roaming\chrys\hooks\guard\guard_state.json（未创建，用出厂默认）
规则：40 条 · 开 39 · 关 1
```

---

## 三、开关单条规则

```bash
guardctl.py list                   # 清单 + 每条开/关
guardctl.py disable IN-07          # 关掉一条
guardctl.py enable  IN-07          # 再打开
guardctl.py disable 'T-SHELL-03-*' # 通配符：一次关一族
guardctl.py reset                  # 清除全部规则与写入范围开关覆盖
guardctl.py reset --master         # 连总开关一起恢复出厂
```

* 规则 ID 大小写不敏感：`in-07` 与 `IN-07` 等价。
* 通配符按规则 ID 匹配，`T-SHELL-03-*` 会命中 `T-SHELL-03-1` ~ `-5` 五条。
* ID 写错会报错并给出相近的候选，不会写坏状态文件。
* 出厂就是关的规则只有 `T-OUT-02`（其余输出层规则暂未实现），其余默认都开。

状态文件长这样：

```jsonc
{
  "version": 1,
  "enabled": false,                       // 总开关（缺省 = 开）
  "rules": { "IN-07": false },            // 单条规则覆盖（缺省 = 用 rules.json 的 enabled）
  "write_scope": { "allow_outside_workspace": false }  // 写入范围（缺省 = 不限工作区）
}
```

---

## 四、写入范围（工作区限制）

出厂**不限制**写入工作区外：写到 `C:\Users\...`、`/tmp` 等工作区以外的路径照常
放行，`T-FS-01`（写入白名单）与 `T-SHELL-05`（shell 写到工作目录外）因此**不会触发**。

需要收严时：

```bash
guardctl.py workspace restrict  # 只允许写工作区内
guardctl.py workspace allow     # 放开（出厂行为）
```

`workspace restrict` 之后：

* 写工作区外的路径会被 `T-FS-01` / `T-SHELL-05` 拦下；
* 例外：会话临时目录（`$TEMP/icode-session-*`）照常放行；
* `readonly_globs` 里的帮助文档目录（`**/docs/**`、`**/help/**`）仍然只读。

`guardctl.py list` 里会标出这两条规则当前不生效，底部有一行说明。

---

## 五、增删规则 / 改规则内容

规则**条目本身**在 `rules.json` 里；运行时不能新增条目——要新增或删除，改文件。

### 5.1 先看清楚一条规则长什么样

```bash
guardctl.py show IN-07
```

输出就是 `rules.json` 里那一条的完整 JSON（外加出厂开关 / 现场覆盖 / 当前生效状态），
照着抄改就行。`show` 也支持通配符。

### 5.2 改哪一份 `rules.json`

| 你想改的 | 改哪份 | 怎么生效 |
| --- | --- | --- |
| 临时试一下 | 已安装副本 `<config_dir>/hooks/guard/rules.json` | 立刻生效，但**下次启动 iCode 会被覆盖** |
| 长期生效 | 仓库里的 `src/chrys/kernel/aixcoding/guard/pack/rules.json` | `uv run python -m aixcoding.tools.guard_install_merge` 重新安装（或改完随版本发布） |

### 5.3 新增一条规则

`rules.json` 里有两个数组：`prompt_rules`（提示词层）与 `tool_rules`（工具调用前）。
新规则加到对应数组里即可。

例：拦住"删库"类 shell 命令。先在 `dictionaries` 里加词典，再在 `tool_rules` 里加规则：

```jsonc
"dictionaries": {
  "db_destructive": ["DROP DATABASE", "TRUNCATE TABLE", "drop table"]
}
```

```jsonc
{
  "id": "T-DB-01",
  "name": "删库命令",
  "layer": "数据库",
  "disposition": "block",
  "audit": true,
  "applies_to": { "kinds": ["shell"], "arg_keys": ["command"] },
  "detect": [{ "type": "keyword", "dictionary": "db_destructive" }]
}
```

字段说明：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `id` | 是 | 规则 ID，全局唯一。习惯：`IN-xx` 提示词层，`T-<类别>-<序号>` 工具层 |
| `name` | 是 | 中文名，`guardctl.py list` 显示的就是它 |
| `layer` | 是 | 归类标签（`Bash` / `文件系统` / `网络` …），只用于阅读 |
| `disposition` | 是 | `block`（直接拦）/ `confirm`（要用户确认） |
| `audit` | 否 | 是否留痕，缺省 true |
| `enabled` | 否 | 出厂开关，缺省 true |
| `applies_to` | 工具层必填 | `kinds`（工具类别，如 `shell` / `filesystem.write`）+ `arg_keys`（要扫的参数名） |
| `detect` | 是 | 检测项列表，`type` 决定怎么判；命中任一项即命中 |
| `note` | 否 | 备注，`guardctl.py list` 会显示 |

常用检测类型：

| `type` | 作用 | 额外字段 |
| --- | --- | --- |
| `keyword` | 按**词边界**匹配词典条目（大小写不敏感） | `dictionary` |
| `path` | 按**普通子串**匹配词典条目（大小写不敏感） | `dictionary` |
| `regex` / `secret` | 按正则匹配词典条目 | `dictionary` |
| `extension` | 扩展名匹配 | `dictionary` |
| `ip_cidr` | IP / 网段匹配 | `dictionary` |
| `url` | URL 匹配 | `dictionary`、`mode` |
| `write_scope` | 写入是否超出允许范围 | — |
| `shell_write_target` | shell 命令的写文件目标 | — |
| `mcp_server` / `mcp_args_schema` | MCP 服务与参数 Schema | — |
| `caller_context` | 调用方上下文（工作目录白名单等） | — |
| `rule_refs` | 引用别的规则（复用其检测项） | `rules`（规则 ID 列表） |

词典条目可以是字符串，也可以是对象 `{"value": "...", "name": "显示名", "regex": true}`；
标了 `"regex": true` 的条目一律按正则编译。

新增完**必须**做三件事：

1. 跑自检，确认正例命中、日常操作不被误伤：
   `uv run python -m aixcoding.tests.guard.guard_pack_selftest -v`（只用标准库，不用 uv 安装环境之外的依赖）
2. 在 [MANUAL_TESTS.md](MANUAL_TESTS.md) 补一条手工用例；
3. 改了仓库那份就重新安装：`uv run python -m aixcoding.tools.guard_install_merge`。

### 5.4 改一条规则的处置

改 `disposition`：`block` ↔ `confirm`。想临时改行为又不想动文件，用
`guardctl.py disable <ID>` / `enable <ID>`。

### 5.5 删除一条规则

把那个对象从 `rules.json` 的数组里删掉。删之前确认两件事：

* 没有别的规则用 `rule_refs` 引用它（引用了会直接报错：*rule_refs 引用了不存在的规则*）；
* 词典可以留着——词典和规则是分开的，删规则不影响词典。

删完同样跑一遍自检。

### 5.6 改词典（关键词 / 正则）

`dictionaries` 里的条目就是匹配用的词。新增路径类词条一律写成**带分隔符锚定的
正则**，避免退化成子串匹配（否则项目目录名叫 `crowdstrike` 就会误伤）：

```jsonc
"blocked_path_fragments": [
  { "value": "(?<![A-Za-z0-9])(?:crowdstrike|falcon)(?![A-Za-z0-9])", "regex": true }
]
```

---

## 六、常见问题

| 现象 | 原因 | 怎么办 |
| --- | --- | --- |
| 改了 `rules.json` 没生效 | 改的是已安装副本，被下次启动覆盖了 | 改仓库那份并重新安装 |
| 改了开关没生效 | 改的不是 `<config_dir>/hooks/guard/guard_state.json` | `guardctl.py status` 看"开关状态"那行的路径 |
| 规则显示"开"却就是不拦 | 该规则受写入范围设置影响 | `guardctl.py status` 看"写入范围"那行 |
| 总开关关了还是被拦 | 拦的不是护栏（别的 hook / 审批模式） | `guardctl.py status` 看"Hook 安装"那行 |
| 状态文件坏了 | 手改 JSON 改坏了 | 删掉它 → 回到出厂默认（总开关开、规则按出厂） |
| 想知道到底命中了哪条 | 留痕文件里有完整明细 | 看 `<config_dir>/hooks/guard_audit.jsonl` |

---

## 七、一页速查

| 我要做的事 | 命令 |
| --- | --- |
| 没有 uv / 不知道 python 在哪 | `guardctl.cmd status`（Windows）/ `guardctl.sh status`（Linux，见第零节） |
| 看所有开关 | `guardctl.py status` |
| 关掉 / 打开整个护栏 | `guardctl.py off` / `on` |
| 看规则清单 | `guardctl.py list`（可加通配符，如 `list 'T-SHELL-03-*'`） |
| 关掉 / 打开一条规则 | `guardctl.py disable IN-07` / `enable IN-07` |
| 一次关一族规则 | `guardctl.py disable 'T-SHELL-03-*'` |
| 看一条规则的完整定义 | `guardctl.py show IN-07` |
| 限制 / 放开工作区外写入 | `guardctl.py workspace restrict` / `allow` |
| 清除全部开关覆盖 | `guardctl.py reset`（加 `--master` 连总开关一起清） |
| 改规则内容 | 改 `rules.json` → 自检 → 重新安装 |
| 验证改完没把正常会话打死 | `uv run python -m aixcoding.tests.guard.guard_pack_selftest -v` |

> `guardctl.py` 默认操作**已安装的那份**规则（`<config_dir>/hooks/guard/`），因为护栏
> 运行时读的就是它；没安装时退回脚本同目录的副本。`--rules` / `--state` 可以显式
> 指定别的路径。
