# 验证记录：内网安装器与发布约定迁移（internal-installer）

> 活文档：追加验证证据，不删除旧记录；命令均在仓库根以大写盘符执行。
> 本记录只覆盖本地自动验证与明确标注的兼容性冒烟，不代表真实制品端到端验收已完成。

## 2026-11-05：实现后本地验证

环境：Windows 11 x64，Python 3.14.8；Windows PowerShell 5.1 与 pwsh 均实际参与 ps1
契约测试。运行前清空 `PYTHONPATH`，设置 `PYTHONUTF8=1`。

### 1. 安装器定向契约测试

命令：

```bat
uv run pytest aixcoding/tests/internal_installer/test_install_internal_mirror_scripts.py -n0
```

结果：**44 passed / 46 skipped，exit 0**（90 collected）。其中 42 个 Windows 用例在
pwsh / Windows PowerShell 5.1 上实跑，2 个静态契约用例实跑；46 个 POSIX 用例按
design §10 已批准的 R13 平台豁免在 Windows 跳过。

覆盖：prod/test 与显式版本 URL、base URL/channel 四级优先级、显式 SHA-256、离线包、
旧包拒绝、冲突两分支与外部文件字节不变、幂等、渠道降级、管道 stdin、退出码 126、
Rosetta/glibc/musl、品牌上下文、ps1 包结构/自检/install 故障及地址逃生门。

### 2. 统一验证入口

命令：

```bat
uv run python aixcoding/tools/verify.py --smart --paths <本批全部新增/修改路径>
```

结果：**Fork verify Summary: PASSED (exit 0)**。

- custom：**511 passed / 50 skipped**（561 items）。
- upstream：Smart Test 收集 36420 个非集成候选后，判定本批为非运行时变更，最终
  0 targets / 0 tests；输出 `Smart Test found no applicable pytest tests`。
- 该上游结果仅表示无适用的局部测试，不表述为上游全量测试通过。

### 3. 登记、文档与格式门禁

| 命令 | 结果 |
|---|---|
| `uv run python aixcoding/tools/gen_customizations.py --strict` | OK；98 registration rows / 135 markers / 10 designs；0 retired id / 0 warning；零新增登记、零漂移 |
| `uv run python aixcoding/tools/check_docs.py` | OK；扫描 20 ADR / 28 design / 26 self-review |
| `uv run ruff check --no-fix aixcoding/tests/internal_installer/test_install_internal_mirror_scripts.py` | All checks passed |
| `uv run ruff format --check aixcoding/tests/internal_installer/test_install_internal_mirror_scripts.py` | 1 file already formatted |
| `git diff --exit-code -- scripts/install.sh scripts/install.ps1 tests/app/test_install_scripts.py` | 零输出、exit 0；上游脚本与测试零改动 |

### 4. POSIX 脚本额外兼容性冒烟

当前机器的 `bash` 默认命令指向无可用发行版的 WSL，不能运行目标 POSIX 测试。另发现
Git Bash 后执行：

```bat
"C:\Program Files\Git\bin\bash.exe" -n aixcoding/release/install-internal-mirror.sh
"C:\Program Files\Git\bin\bash.exe" aixcoding/release/install-internal-mirror.sh --help
```

结果：两命令均 exit 0；帮助输出使用 AIxCoding / `aixcoding-cli`、统一 IP 和
`AIXCODING_*` 推荐变量。**这只是语法与帮助路径兼容性证据，不计作 Linux/macOS 通过。**

实现自查据此将 `${OS,,}` 改为显式平台 `case` 映射，避免依赖 macOS 系统 Bash 3.2
不支持的小写展开；修改后上述两项冒烟与统一验证均已重跑通过。

## 2026-11-06：一般用户 / 技术支持双层手册

- `aixcoding/release/INSTALL.md` 重写为一般用户版，仅保留默认安装、升级、卸载与求助路径。
- 新增 `aixcoding/docs/install-uninstall-support.md`，承接版本、完整下载地址、渠道/变量、
  全安装方式、冲突、PATH/rc、卸载残留和排障细节；已登记 `aixcoding/docs/INDEX.md`。
- requirements、design、test-plan 和静态契约测试同步按双层范围对账：用户版不含
  `--channel`、环境变量、内部 `chrys` 或镜像协议，支持版覆盖完整技术口径。
- 定向静态/品牌测试：**2 passed**；ruff lint/format 通过。
- 统一验证入口：**511 passed / 50 skipped，PASSED**；上游 Smart Test 对非运行时变更无适用
  用例。`check_docs.py` OK；strict 保持 98 登记行 / 135 marker、0 warning。

## 2026-11-06：第 2 轮独立评审复验

GLM-5.3 在完整工作区独立评审中复跑并确认：

- `check_docs.py`：OK（20 ADR / 28 design / 27 self-review）。
- `gen_customizations.py --strict`：98 登记行 / 135 marker / 0 warning，零新增、零漂移。
- `uv run pytest aixcoding/tests/internal_installer/test_install_internal_mirror_scripts.py -n0`：
  **44 passed / 46 skipped，exit 0**，与本文件第 1 节记录一致。

该复验是独立评审证据，不新增任何平台实测结论；下列五项缺口保持不变。

## 尚未执行的验收与覆盖缺口

1. **Linux/macOS 真实契约测试未运行**：46 个 POSIX 用例的 Windows skip 不是通过证据；
   须在 Linux/macOS CI 或实机重跑同一文件。
2. **0.33.0 真实制品端到端未运行**：内网在线 prod/test、POSIX `--from` 离线、解压直接
   启动、Windows 在线与手工 exe 均未在本轮连接真实镜像执行；design §13.1 保持未完成。
3. **Windows on Arm 未实测**：当前仅验证 x64 Windows 契约与文档中的仿真口径，不能宣称
   Arm 仿真已实机验证。
4. **真实外部命令升级未实测**：自动测试已断言 fake 外部文件字节不变及安装目录内命令
   验证，真实包内 alias 创建/冲突保护仍随真实制品验收确认。
5. **发布与回滚操作未实操**：publishing.md 的上传改名、先制品后指针、IP 迁移与回滚
   步骤本轮只完成文档对账，未变更真实镜像。

后续证据应按 test-plan.md 的真实制品步骤追加到本文件；在上述缺口关闭前，不得将本批
描述为全部验收完成。
