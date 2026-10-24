<!--
Copyright (c) 2026 chana4afk & 四川爱赋康文化科技有限公司. All rights reserved.
原始方法学版权：iCode（openJiuwen / Huawei, Apache-2.0），Copyright (c) Huawei Technologies Co., Ltd. 2026；本文件在其方法学基础上脱敏准备，尊重并保留华为原始版权。
商标声明：iCode、Chrys、openJiuwen 为其各自权利人的商标，本示例未获其背书。
许可：本示例以 Apache License 2.0 贡献（与上游同许可）。
署名：示例贡献者 / 组织（非个人名）；保留上游 NOTICE 精神。
贡献声明（Contribution Notice）：本贡献由 chana4afk & 四川爱赋康文化科技有限公司 在 iCode（openJiuwen / Huawei, Apache-2.0）方法学基础上脱敏准备；尊重并保留华为原始版权，依 Apache-2.0 许可向上游提交，CLA 由贡献者签署。

-->

# 部署手册 · 通用「fail-closed（默认拒绝）」等保三级 hooks

**版本**：v1.0 · 2026
**配套**：`../playground/security_fail_closed_hook.py`、示例 `hooks.yaml`
**许可**：Apache-2.0
**商标声明**：iCode、Chrys、openJiuwen 为其各自权利人的商标，本示例未获其背书。

---

## 0. 一句话纪律

**未通过部署前校验，禁止部署；校验失败 = 配置坏 = 必须中止，不得带着损坏（或被禁用的）hooks 继续运行。**

> 根因背景：某些智能体运行时的全局 hooks 配置在解析失败时，引擎可能发 Warning 并「禁用 hooks 继续」而非全阻断，从而破坏 fail-closed 前提。本手册以「部署前强制校验 + 运行时兜底」双闸堵死该路径。

---

## 1. 配置文件落位（iCode 原生自动加载路径）

`hooks.yaml` 为可读性命名。**落地真实实例时改名 `hooks.yaml`**，置于运行时自动加载路径之一：

- `<config_dir>/hooks/hooks.yaml`，或
- `<workspace>/.chrys/hooks/hooks.yaml`

`scripts/` 子目录随 hooks 配置一并部署，钩子脚本 `run.path` 相对该配置目录解析。

示例 `hooks.yaml`（5 个 `before_tool_call` 钩子，全部 `execution.mode: blocking` + `execution.on_error: block`）：

```yaml
version: 1
settings:
  max_parallel_hooks: 8
  shutdown_grace_seconds: 5
hooks:
  - id: redline-fullscan
    event: before_tool_call
    enabled: true
    description: "R1-R8 等保三级通用红线触发词全量扫描；命中即 fail-closed 否决"
    run: { type: script, path: scripts/security_fail_closed_hook.py }
    execution: { mode: blocking, timeout_seconds: 10, on_error: block }
  - id: outbound-scan
    event: before_tool_call
    enabled: true
    description: "外发载荷扫描：密钥/PII/批量取数违规即失败闭合否决"
    run: { type: script, path: scripts/security_fail_closed_hook.py }
    execution: { mode: blocking, timeout_seconds: 10, on_error: block }
  - id: tool-whitelist
    event: before_tool_call
    enabled: true
    description: "仅放行已注册工具，未知/影子工具 fail-closed 否认"
    run: { type: script, path: scripts/security_fail_closed_hook.py }
    execution: { mode: blocking, timeout_seconds: 10, on_error: block }
  - id: fs-write-protect
    event: before_tool_call
    enabled: true
    description: "拦截对凭据文件/关键系统路径的写入"
    run: { type: script, path: scripts/security_fail_closed_hook.py }
    execution: { mode: blocking, timeout_seconds: 10, on_error: block }
  - id: permission-selfcheck
    event: before_tool_call
    enabled: true
    description: "受控工具种类调用前校验调用者授权或委托令牌；越权即停"
    run: { type: script, path: scripts/security_fail_closed_hook.py }
    execution: { mode: blocking, timeout_seconds: 10, on_error: block }
```

> 注：上述示例把 5 个卡点都指向同一个脚本的「主调度」入口；也可拆成 5 个独立脚本，与 `run.path` 一一对应。拆分的实现示例可参考 `../playground/security_fail_closed_hook.py` 内的 5 个 `scan_*` 函数。

---

## 2. 强制部署前校验（MANDATORY GATE）

在任一将 hooks 投入运行时之前，MUST 执行结构 + fail-closed 不变量校验；任一断言失败即非零退出，部署流水线 MUST 中止（不得带着损坏配置继续）。

校验断言（失败即非零退出 → 部署中止）：

1. YAML 可解析（解析失败 = "配置坏"，直接 fail）；
2. 顶层含 `version/settings/hooks`，`version==1`；
3. 预期 hook（redline-fullscan / outbound-scan / tool-whitelist / fs-write-protect / permission-selfcheck）全部存在；
4. 每个 hook：`event==before_tool_call`、`enabled==true`、`run.type==script` 且脚本文件存在；
5. **fail-closed 不变量**：每个 hook `execution.mode==blocking` 且 **`execution.on_error==block`**（禁止 fail-open）。

该脚本是「配置坏 → 不部署」的硬闸门：即便运行时本身对坏配置"温柔降级"，部署侧先行拦截。

---

## 3. 运行时兜底（即便个别钩子未加载也不漏判）

即便全部校验通过、运行时因故部分未挂载某 hook，仍有两层兜底：

- **工具白名单兜底未知工具**：任一未登记工具名（含影子工具）均 `block`（fail-closed）。工具注册表应经环境变量 `ALLOWED_TOOLS` 注入。
- **`on_error: block` 默认 deny**：任一 hook 进程崩溃 / 超时 / 退出非 0，运行时仍否决调用，不会因钩子异常而放行。

---

## 4. 环境变量注入（部署侧）

| 变量 | 含义 | 缺省 |
|------|------|------|
| `ALLOWED_TOOLS` | 逗号分隔的已注册工具名（生产应覆盖为真实注册表） | 内置保守基线 |
| `AUTH_PROFILES` | 逗号分隔的授权 profile（供 `permission-selfcheck` 判定越权） | 默认开发常用集（示例占位） |

注入示例：

```bash
export ALLOWED_TOOLS="Read,Write,Edit,Grep,Glob,Bash,Task,WebFetch,WebSearch,shell,mcp,filesystem.read,filesystem.write,delete"
export AUTH_PROFILES="<delivery-lead>,<security-agent>,<backend-agent>,<architect>"
```

> 生产环境请把 `<...>` 占位符替换为你方真实、已注册的 agent profile 选择器。

---

## 5. 部署后冒烟（建议，非强制）

复用实测管线跑若干用例（含「拦截」与「合法放行」两类），确认 fail-closed 全 PASS。任一应被拦截的用例被放行 → 回滚并复查。

---

## 6. 版本对齐说明

目标版本 schema 以**你方安装的运行时实测契约为准**。后续运行时升版时须重跑校验 + 冒烟（建议建立版本台账与定期重核纪律，例如每 90 天）。
