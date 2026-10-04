<!--
Copyright (c) 2026 chana4afk & 四川爱赋康文化科技有限公司. All rights reserved.
原始方法学版权：iCode（openJiuwen / Huawei, Apache-2.0），Copyright (c) Huawei Technologies Co., Ltd. 2026；本文件在其方法学基础上脱敏准备，尊重并保留华为原始版权。
商标声明：iCode、Chrys、openJiuwen 为其各自权利人的商标，本示例未获其背书。
许可：本示例以 Apache License 2.0 贡献（与上游同许可）。
署名：示例贡献者 / 组织（非个人名）；保留上游 NOTICE 精神。
贡献声明（Contribution Notice）：本贡献由 chana4afk & 四川爱赋康文化科技有限公司 在 iCode（openJiuwen / Huawei, Apache-2.0）方法学基础上脱敏准备；尊重并保留华为原始版权，依 Apache-2.0 许可向上游提交，CLA 由贡献者签署。

-->

# iCode 依赖版本钉选指南（getting-started）

**许可**：Apache-2.0
**商标声明**：iCode、Chrys、openJiuwen 为其各自权利人的商标，本示例未获其背书。
**适用**：希望把 iCode（chrys）作为**外部活依赖**引入自己项目的团队。

---

## 1. 为什么要钉选版本

iCode 以 Apache-2.0 许可开源。把它作为 git 依赖引用（而非 fork / vendoring / 内化改写）是最简洁、合规的消费姿态：上游发版即能跟随，且不会触发专有/开源混淆。

为避免"依赖漂移"导致契约不兼容，建议**钉选上游 tag**，并建立版本台账与定期重核纪律（例如每 90 天）。

---

## 2. 上游事实（以官方仓库为准）

| 项 | 值（示例） |
|----|----|
| 上游仓库（git） | `https://atomgit.com/openJiuwen/iCode.git` |
| 项目代号 | chrys |
| 版本 | v0.28.0（请核对你方获取时的最新 tag） |
| 许可证 | Apache-2.0 |
| requires-python | >=3.14（请以实际发布的元数据为准） |
| ACP 协议依赖 | agent-client-protocol（版本随上游） |

> 上述版本号为撰写时的示例值。请始终以官方仓库的 tag 与 `pyproject.toml` 元数据为准。

---

## 3. 钉选方式（git tag + rev）

建议以 **git URL + tag** 引用（非分支、非裸 commit），并在锁文件（如 `uv.lock` / `poetry.lock`）中固定 rev：

```
source = { git = "https://atomgit.com/openJiuwen/iCode.git?rev=v0.28.0#<commit-rev>" }
```

或直接在 `pyproject.toml` 声明：

```toml
[tool.uv.sources]
chrys = { git = "https://atomgit.com/openJiuwen/iCode.git", tag = "v0.28.0" }
```

---

## 4. 隔离运行环境

iCode 需要较新的 Python。建议用独立虚拟环境（如 `uv` 或 `venv`）隔离，避免污染宿主 Python：

| 项 | 建议 |
|----|----|
| 项目根 | `<your-project>/runtime`（请替换为你方实际路径） |
| venv 绝对路径 | `<your-project>/runtime/.venv` |
| 宿主 Python | 若不满足上游 requires-python，请隔离，勿污染宿主 |
| 安装方式 | `uv add "git+https://atomgit.com/openJiuwen/iCode.git@v0.28.0"` |
| 安装结果 | `chrys==0.28.0`（from git source） |
| 落盘位置 | `.venv/lib/pythonX.Y/site-packages/chrys/`（由 git 源安装，非本仓复制） |

---

## 5. 验证命令与输出

### 5.1 import 验证
```bash
uv run python -c "import chrys; print(chrys.__file__); print(chrys.__version__)"
```
预期：打印 `chrys.__file__` 与版本号，退出码 0。

### 5.2 CLI 验证
```bash
uv run chrys --help
```
预期：打印 usage 与可用子命令（如 `run` / `agents` / `models` / `acp` / `serve` / `workflow` / `install`），退出码 0。

---

## 6. 合规姿态（consumption，非 absorption）

- ✅ **只引用不复制**：iCode 经 `uv add git+...` 以外部 git 依赖形式接入，锁文件以 git source 记录；源码安装在 `.venv/.../site-packages/chrys/`，**未** copy 进本仓源码树。
- ✅ **未 vendoring**：无内联源码、无 `vendor/` 目录。
- ✅ **未改 iCode 源码**：上游 tag 原样引用，未做任何 patch/改写。
- ✅ **隔离 venv**：独立 Python venv，与宿主解耦。
- ⚠️ **venv 不提交**：`runtime/.venv` 保持 untracked（或加 `.gitignore`）。本指南执行过程中**未做任何 git commit/push**。

---

## 7. Git 处理

- 本指南涉及的 `runtime/.venv` 为虚拟环境，**不纳入版本控制**（保持 untracked，或加 `.gitignore`）。
- 是否 commit / push 由你方自行决定；本指南不构成提交建议。

---

## 8. 元数据

- 记录日期：2026
- 许可：Apache-2.0（本指南以 Apache-2.0 贡献；iCode 本身亦为 Apache-2.0）
- 商标声明：iCode、Chrys、openJiuwen 为其各自权利人的商标，本示例未获其背书。
