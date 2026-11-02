# RSI 实验（实验性）

RSI（recursive self-improvement）是一套**实验性离线闭环**：用固定评测集给 agent profile 打分，提出白名单内的小改动，仅当分数**严格提升**时采纳候选，再导出 YAML 供人工审阅。

**目前唯一支持的使用方式是命令行。** 没有 TUI 面板，也没有 ACP 入口。请在终端使用 `icode rsi …`。任务保存在 `<config_dir>/rsi/tasks/`（macOS/Linux 上通常为 `~/.chrys/rsi/tasks/`），与聊天会话、workflow 会话相互独立。

在线评测和在线 optimize 使用 `ApprovalMode.BYPASS`（与 `icode run` 相同）。导出的 profile 在依赖之前务必人工审阅。

## 闭环如何串起来

```text
  数据集 JSON           基线 profile
       │                     │
       ▼                     ▼
  validate ──► create 任务 ──►（可选）eval 基线
                                  │
                                  ▼
                            optimize
                     ┌──────────┴──────────┐
                     │  给基线打分         │
                     │  提出 mutation      │
                     │  门控再打分         │
                     │  更好才采纳         │
                     └──────────┬──────────┘
                                  │
                    tree / get / best.json / analysis/
                                  │
                                  ▼
                         审阅 → install（导出）
```

| 闭环中的位置 | 你在做什么 | 常用命令 |
|--------------|------------|----------|
| 准备用例 | 定义 prompt 与声明式检查 | 编辑数据集 JSON → `dataset validate` |
| 打开工作区 | 创建独立任务目录并挂上数据集 | `create --dataset …` |
| 可选冒烟打分 | 优化前先看当前 agent 表现 | `eval --task …` 或 `eval --dataset …` |
| 改进 | 基线 → 改动 → 门控 → 采纳/拒绝，共 N 轮 | `optimize` |
| 查看 | 读分数、演进树、分析、候选 YAML | `get` / `tree` / 任务目录下的文件 |
| 导出 | 把最佳候选拷出去审阅（不会热加载） | `install` |

`icode rsi start` **不是**上面这条闭环。它仍跑 Phase 1 的 **mock** provider，只用来锻炼任务 worker 与树投影。真打分用 `eval`；带门控的搜索用 `optimize`。

## 典型操作指南（live optimize）

目标：在小型 `script_based` 数据集上，用真模型改进 profile，再导出胜出候选供审阅。

仓库自带的 live 样例是 **`examples/rsi/complex-calc/`**（Rust 复数计算器）。需要现成数据集时直接用这棵树；完整的 create → eval → optimize → install 说明见 [演示样例：Rust 复数计算器](#演示样例rust-复数计算器)。下面步骤是同一套闭环，适用于你自己编写的任意数据集。

### 1. 编写数据集

创建 JSON 文件（`chrys-rsi.dataset.v1`）。每个 case 需要 `prompt` 和 `evaluation.expect` 检查（和/或可信 `script`）。Live 跑法请**省略** `fixture`（fixture 是给 CI stub 用的罐头输出）。

若题目是在**代码树**上加特性 / 修 bug，请加 `workdir` 种子。源路径按**只读**处理：RSI 会物化一份私有副本（或 git clone / worktree）到 case 目录，**不会**原地打开你的原目录。优先 `strategy: "copy"` 或 `"git_clone"`。`"git_worktree"` 更快，但与源仓共享 `.git`，隔离更弱。

`create --dataset` 时，每个种子会快照到任务的 `input/seeds/<case_id>/`，并改写存盘数据集为从该快照 `copy`，之后的 eval/optimize 不再访问调用方原来的路径。

Prompt 里不要直接写出将要检查的子串或文件内容。否则基线可能因「猜到答案」而通过，optimize 就没有真正可修的失败。

为什么做这一步：数据集是唯一的评分标准。Optimize 不会发明新的判分规则，只会尝试提高这些 case 的通过率。

### 2. 校验文件

```bash
icode rsi dataset validate ./suite.json
```

为什么：在拷进任务之前抓住格式错误。此时还不打分。

### 3. 创建任务并挂上数据集

```bash
icode rsi create \
  --name live-probe \
  --max-iterations 2 \
  --dataset ./suite.json \
  --instruction "Append the missing requirements so the checks pass. Do not replace existing instructions."
```

会打印形如 `rsi-…` 的任务 id。数据集复制到 `<task>/input/`。`--instruction` 是给 **optimizer** 模型的优化说明（偏好怎样的 mutation），不是 case 里那个 agent 的用户 prompt。`--max-iterations` 是 `optimize` 的默认 epoch 上限（mock `start` 也会用到）。

为什么：每次 optimize 都需要持久任务工作区来放候选、scorecard、analysis 和 `best.json`。

### 4.（可选）先给当前 agent 打一次分

```bash
icode rsi eval --task rsi-<id> -a Code -m <model>
```

或不调模型的干跑（每个 case 需要 `fixture`）：

```bash
icode rsi eval --task rsi-<id> --stub
```

为什么：在烧 epoch 之前确认数据集与 headless 路径可用。若直接 `optimize` 可跳过（optimize 总会先打基线分）。

### 5. 跑带门控的 optimize

```bash
icode rsi optimize rsi-<id> --agent Code -m <model> --epochs 2 --timeout 180
```

一次 `optimize` 内部会：

1. **Baseline** — 用基线 profile（`Code`，除非 `--profile` 指向 YAML）跑完全部 case。
2. **Propose** — optimizer 模型阅读失败摘要，只能发出白名单 mutation（`append_instructions`、`upsert_inline_skill`、`remove_inline_skill`）。
3. **Materialize** — 写入 `<task>/run/candidates/epoch-N/`。
4. **Gate** — **只重跑仍失败的 case**，与此前已通过结果合并；仅当**整套分数严格升高**时采纳。
5. 直到 epoch 用尽、分数已完美，或模型返回空的 `{"mutations":[]}`（主动停止）。模型调用失败或非法 JSON 会 **drop** 该轮并继续；被丢弃的提议不会安装。

Stdout 含 `baseline_score`、`best_score`、`best_candidate`、`epochs`、`dropped`、`perfect`。

为什么：这才是真正的 RSI 搜索。不传 `--agent` 的 stub optimize 用于 CI 与接线检查，不会调用模型。

常用变体：

```bash
# 提议模型与跑 case 的模型分开
icode rsi optimize rsi-<id> --agent Code --optimizer-model <proposer> -m <tester>

# 基线 profile 来自 YAML（不必磁盘上已有同名 --agent）
icode rsi optimize rsi-<id> --agent Code --profile ./base.yaml -m <model>

# Stub optimize（不调模型）：CI / 干跑
icode rsi optimize rsi-<id> --epochs 2
```

### 6. 查看结果

```bash
icode rsi get rsi-<id>
icode rsi tree rsi-<id>
```

也可直接打开磁盘上的任务目录（默认在 `~/.chrys/rsi/tasks/rsi-<id>/`）：

| 路径 | 含义 |
|------|------|
| `run/best.json` | 指向最佳已采纳候选 |
| `run/candidates/<id>/` | Profile YAML + `mutations.json` |
| `run/analysis/epoch-N.json` | 失败摘要与提议的 mutation |
| `run/evaluations/` | 各标签的 scorecard 与 `cases.jsonl` |
| `tree.json` | 演进树（`ADOPTED` / `REJECTED`） |

为什么：导出前判断胜出改动是真改进，还是过拟合补丁。

### 7. 导出供审阅（可选写入 agents 目录）

```bash
# 默认：复制最佳 YAML 到 <task>/export/<name>.yaml
icode rsi install rsi-<id>

# 或指定路径
icode rsi install rsi-<id> --out ./candidate.yaml

# 显式写入用户 agents 目录（两个开关都要）
icode rsi install rsi-<id> --user --user-dir ~/.chrys/agents
```

为什么：RSI 止于**导出**，不会热加载进正在跑的聊天会话。是否使用这份 YAML 由你决定。

### 8. 结束后清理

```bash
icode rsi list
icode rsi delete rsi-<id>
```

## 命令一览

| 命令 | 在 RSI 中的角色 | 做什么 |
|------|-----------------|--------|
| `dataset validate` | 准备 | 校验数据集 JSON；不建任务、不调模型 |
| `create` | 准备 | 创建 `<config_dir>/rsi/tasks/<id>/`；可选把 `--dataset` 拷进 `input/` |
| `list` / `get` | 查看 | 列出任务，或查看单个任务元数据 / 最近 optimize 摘要 |
| `eval` | 度量 | 跑一次 `script_based` 打分（stub fixture 或 live `--agent`） |
| `optimize` | 改进 | 完整门控环：基线、提议、门控、采纳/拒绝 |
| `tree` | 查看 | 打印演进树（mock `start` 或 `optimize` 之后） |
| `install` | 导出 | 拷出最佳候选 YAML；除非 `--user` + `--user-dir`，否则不写用户 agents |
| `delete` | 清理 | 删除 `CREATED` 或已结束任务的目录 |
| `start` | **仅 mock** | 排队跑 Phase 1 mock provider（不是真 optimize） |

## 数据集格式（chrys-rsi.dataset.v1）

```json
{
  "schema_version": 1,
  "dataset_id": "demo",
  "cases": [
    {
      "case_id": "hello-file",
      "prompt": "Write hello.txt containing hello world",
      "workdir": {
        "from": "./seeds/mini-app",
        "strategy": "copy"
      },
      "evaluation": {
        "method": "script_based",
        "expect": {
          "response_contains": ["hello"],
          "files_exist": ["hello.txt"],
          "file_contains": { "hello.txt": ["hello world"] }
        },
        "script": "optional_check.py"
      },
      "fixture": {
        "response": "Created hello.txt",
        "files": { "hello.txt": "hello world\n" }
      }
    }
  ]
}
```

- 仅支持 `evaluation.method: script_based`（本版不做 LLM-as-judge）。
- **交给 agent 的输入**：`prompt`（用户消息）+ 物化后的 case 工作目录（以及 `--agent` / `--profile` 的基线 profile）。`evaluation.*` 只用于判分，不会发给模型。
- **`workdir`**（可选）：从源目录为 case 播种私有 cwd。
  - `from` — 绝对路径，或相对数据集 JSON 所在目录的路径
  - `strategy` — `copy`（默认；任意目录树）、`git_clone`（本地 `--no-hardlinks` clone）、或 `git_worktree`（detach checkout；共享 `.git`，隔离较弱）
  - `ref` — 可选；`git_clone` / `git_worktree` 的 commit/分支
  - `copy` 源中解析到源树外的 symlink 会被拒绝
- `fixture` 为 `--stub` / CI 提供罐头 `response` 与文件；live `--agent` 请省略。
- 可选 `script` 必须位于数据集目录内；以关闭的 `stdin` 运行，并接收 `CHRYS_RSI_WORKDIR` / `CHRYS_RSI_RESPONSE_PATH`。请将其视为可信本地代码。

评测产物写入输出目录下的 `eval-<id>/scorecard.json` 与 `cases.jsonl`。

### 演示样例：Rust 复数计算器

`examples/rsi/complex-calc/` 是 **RSI 的正式 live 演示样例**：单个编程 case、空 workdir 种子、可信 `script_based` 判分。它展示完整离线闭环——基线常在难题上失败，optimize 提出白名单 mutation，后续 epoch 可以把套件分数提到 `1.0`（`perfect=True`），且不会热加载进聊天会话。

#### 目录里有什么

| 路径 | 作用 |
|------|------|
| `dataset.json` | `chrys-rsi.dataset.v1`，单个 case（`complex-calc`）；prompt 要求实现名为 `complex_calc` 的 Cargo 二进制 |
| `seed/` | 空目录（`from: "seed"`，`strategy: "copy"`）。`create` 时会快照；agent 必须从零写出项目 |
| `check.py` | 可信判分：`cargo build`，再跑多组算式与非法输入退出。**期望值只写在这里**，不进 prompt / seed |

需要 `PATH` 上有 `cargo`。可信脚本超时 300s（覆盖冷编译）。跑 case 的 `--timeout` 建议至少约 600s，以便容纳构建与工具循环。

#### 为什么适合当演示

- **难度够，基线经常是 `0.0`。** 普通四则运算可能过；判分还要求右结合 `2^3^2` → `512+0i`、倒数 `(2-i)^(-1)`、裸 `i` / `-i`、无括号优先级，以及畸形输入如 `1++2i` 必须**非零退出**（不能静默把 `++` 收成 `+`）。
- **标准答案不在 workdir。** 逛目录的 agent 无法从种子读到期望表；答案只在数据集里的 `check.py`（随任务拷贝）。
- **Optimize 有明确可修点。** 典型失败是解析 / 优先级 / 非法退出。成功的 epoch 常是 `append_instructions` 和/或 `upsert_inline_skill`（例如简短的「严格词法 / 拒绝 `1++2i`」技能），而不是整段替换 profile。
- **Live runner 先加载 builtins**，再叠候选 profile，因此 Code 声明的 Explore/General 会像普通 headless 会话一样解析到。

#### 端到端跑一遍

在仓库根目录（或能解析到数据集路径的 cwd）：

```bash
# 1. 校验 + 建任务（把 seed/ 快照进任务）
icode rsi dataset validate examples/rsi/complex-calc/dataset.json
icode rsi create --name complex-calc --max-iterations 2 \
  --dataset examples/rsi/complex-calc/dataset.json \
  --instruction "Prefer clear parsing, right-associative ^, and strict rejection of malformed operator sequences. Append guidance; do not replace existing instructions."

# 2. 可选：搜索前先给 Code 打一次分
icode rsi eval --task rsi-<id> -a Code -m <model> --timeout 600

# 3. 门控搜索（总会先打 baseline）
icode rsi optimize rsi-<id> --agent Code -m <model> --epochs 2 --timeout 600

# 4. 查看 + 导出供人工审阅（不会热加载）
icode rsi get rsi-<id>
icode rsi tree rsi-<id>
icode rsi install rsi-<id> --out ./complex-calc-Code.yaml
```

把 `rsi-<id>` 换成 `create` 打印的任务 id。`-m` 指向已配置的模型 profile。本机冷 `cargo` 较慢时加大 `--timeout`。

#### 怎样读一次成功的跑分

Optimize 的 stdout 类似：

```text
task_id=rsi-…
baseline_score=0.0
best_score=1.0
best_candidate=epoch-2
epochs=2
dropped=0
perfect=True
```

含义：

- **`baseline_score=0.0`** — 未改动的 Code 至少挂了一项判分（常见是 `2^3^2` 或 `1++2i`）。
- **`best_candidate=epoch-N`** — 该轮 mutation 因套件分数**严格上升**而被 **ADOPT**。
- **`best_score=1.0` / `perfect=True`** — 在这份数据集上，采纳候选通过了 case（构建 + 全部算式 + 非法退出）。分数满分时 RSI 会提前结束。
- 产物在 `<config_dir>/rsi/tasks/rsi-…/`：`run/best.json`、`run/candidates/epoch-N/Code.yaml`、`run/analysis/epoch-N.json`（提议的 mutation）、`run/evaluations/`（各轮 scorecard / `cases.jsonl`）。

第一轮仍可能是 `0.0`（进树但未采纳，或增益不够）；真正 ADOPT 可能发生在更晚的 epoch。编程评测**非确定性**：同样命令不一定每次都到 `perfect=True`。若分数卡在 `0.0`，可再跑 `optimize` 或加大 `--epochs`。

`perfect=True` 只表示**这份套件**在导出的 profile 下做对了——不等于同类编程题都会过，也不等于应未经审阅就把 YAML 装成日常 Code。

#### 相对基线改了什么（一次成功跑分示例）

在一次 live 跑到 `baseline_score=0.0` → `best_candidate=epoch-2` / `perfect=True` 的任务里，**被采纳**的 profile 仍是 builtin Code，只叠加了白名单 mutation。与 `run/candidates/baseline/Code.yaml` 的 diff：

1. **`append_instructions`** — 在 `instructions` 末尾追加一小段，对准一直拖垮套件的判分失败：二进制 `+` 后紧跟一元 `+`（例如 `1++2i`）必须非零退出；不得把 `++` 收成 `+`；保留合法一元减（`-i`、`-(1+2i)`）与负指数（`2^-3`）。
2. **`upsert_inline_skill`** — 新增 inline skill `complex-expression-parser`：运算符与字面量分开分词；求值前校验 token 序列；拒绝 `1++2i`（不要把第二个 `+` 当一元加）；在表达式开头、`(` 之后、`^` 之后保留一元减；重申 `^` 右结合 / 仅整型指数与优先级；成功输出必须是无空格的 `A+Bi` / `A-Bi`，整数部分不带 `.0`。

profile 其余部分未改（tools、approval、子 agent、原有 instruction 正文不变）。

同一次搜索在树上的样子：

| 候选 | 父节点 | Mutation | 门控分数 | 结果 |
|------|--------|----------|----------|------|
| baseline | — | — | `0.0` | 作为起点 ADOPT（套件失败；常见 findings：`2^3^2` 和/或 `1++2i`） |
| epoch-1 | baseline | 仅 `append_instructions`（更宽的「严格词法 / `++` / `^`」说明） | `0.0` | REJECTED — 仍挂在 `1++2i`（有时还有别的陷阱） |
| epoch-2 | baseline | 上文的 `append_instructions` + `upsert_inline_skill` | `1.0` | ADOPTED / best — 构建 + 全部算式 + 非法退出均过 |

Epoch-2 的父节点仍是 **baseline**，不是 epoch-1：被拒绝的 epoch 不会叠进下一轮候选。本机可用 `icode rsi tree <task>`、`run/analysis/epoch-N.json`，以及 `diff run/candidates/baseline/Code.yaml run/candidates/epoch-2/Code.yaml` 核对。之后重跑可能写出不同措辞；预期形态是对着 failure summary 的小段 instruction / skill 叠层，而不是整份重写 profile。

## Optimize 行为（细节）

允许的 mutation：

- `append_instructions` — 追加到 profile instructions（从不整段替换）
- `upsert_inline_skill` / `remove_inline_skill` — 按名称管理 inline skills

默认 stub optimize 使用确定性 proposer 与 profile-aware stub runner（指令标记形如 `rsi:case:<case_id>`），不会调用模型。

基线之后，后续每一轮只重跑仍然失败的 case，已经通过的结果保持不变，并且不再复查。因此候选分数把那些已通过的 case 仍视为通过。

`--agent NAME` 把每个候选的评测改为 headless 会话，并让模型按同一白名单提出改动。基线 profile 是 `NAME`，除非 `--profile` 指向一份 YAML。`--optimizer-model` 选择提议模型；`-m` 选择跑 case 的模型。省略 `--optimizer-model` 时，提议模型与跑 case 的模型相同。只传 `--optimizer-model`、不传 `--agent` 时仍会调用提议模型，但候选分数由 stub runner 计算。Live 候选 runner 会先加载 builtins 与用户 agents，再把突变后的 profile 注册上去，因此 Code 声明的 Explore/General 等子 agent 会像普通 iCode 会话一样解析到。

## 状态与边界

- 实验性：人工审阅之前，不要把导出当作生产 agent 配置。
- 仅 CLI：RSI 没有 TUI 或 ACP 入口。
- `icode rsi start` 仍是 mock 训练循环，不会执行 optimize。
- Case 工作区在任务树下；会逛目录的 agent 可能读到 `input/dataset.json`。编写 prompt 与布局时，尽量让 expect 内容不容易从任务目录直接发现。
