# 设计：内网安装器与发布约定迁移（internal-installer）

<!--
签认规则（R10）：责任人本人编辑本头部（状态→已确认、填日期、确认意见可选），AI 不得代填。
签认后修改正文需重新签认。校验：uv run python aixcoding/tools/check_docs.py
-->

- 责任人：周彬
- 状态：已确认
- 确认日期：2026-11-05
- 确认意见：无特别意见

> 修订记录：2026-11-05 初版（requirements 事实基线 + D1-D5 裁决项呈报）。
> 2026-11-05（二）责任人预裁决（聊天侧，签认生效）：D1 并存新增+D1.1 平铺命名、
> D2 维持不对称（成本表述精确化）、D3 保留回退、D4 publishing.md 按推荐；D5 改采
> 统一 IP + 逃生门，四处联动更新。
> 2026-11-05（三）**外部 AI 评审（Blocker 3 / Major 3 / Minor 2，全 confirm）处置版**：
> B1 落点违规（R4/DIRECTORY §2）→ 裁决 R-1a 新建 `aixcoding/release/` + DIRECTORY
> 判定树补行；B2 D3 与删除 create_launch_command 冲突 → 裁决 R-2a **砍 D3**；B3 遗漏
> 0.33 冲突预检等 → 补 §6 R12 采纳/否决清单；M4 校验链路不一致 → 裁决 R-3b 维持
> 0.28 显式 `--sha256`（自动校验登记将来项）；M5 补 `--base-url` 参数；M6 补 ADR 判定
> /隐式行为假设/回滚三节（§8/§9/§12）；m7 INDEX.md 登记；m8 R13 注记。撞名考古
> 订正：0.28 三物料系 fork 纯新增（上游 v0.28.0 无 install.*），上游 0.29.1+ 撞名
> 占据 `scripts/` 同名路径。
> 2026-11-05（四）**第二轮复审（Blocker 2 / Major 3 / Minor 3，全 confirm）处置版**：
> B1 C136-C138 必成孤儿登记（`gen_customizations.iter_upstream_files` 以 `is_custom`
> 跳过 aixcoding/ 树、marker 不扫描）→ §7 改"不适用"、验收改 strict 零新增；B2
> `aixcoding/release/` 规范闭环不完整（DIRECTORY §1/§4 与专区 README 定性"纯开发
> 资产"未改）→ 裁决 **B2a 完整闭环 + 新增 ADR-0020**（三处规范同步修订随批次，ADR
> 判定 §8 改"触发"）；M3 旧脚本指引会重新引入旧命令/品牌残留 → 报错引导仅指向
> 镜像最新制品、品牌验收限定新脚本/新制品路径；M4 预检系两分支语义（有自有安装
> 警告继续/无则中止）、stdin 重定向与退出码 126 均系 0.33 新增（:302-305）、降级
> 防护实际为"自动安装防降级（显式 `--version` 允许）"；M5 回滚两处事实修正（含
> 规范/导航文件修改、镜像回滚目标=最近合规新脚本）；m6 残留待定措辞、m7 Windows
> on Arm 走 x64 仿真现状钉死、m8 R13 理由改准与笔误。
> 2026-11-05（五）**第三轮复审（Blocker 1 / Major 2 / Minor 3，全 confirm）处置版**：
> B1 上位总纲未同步 → `DEVELOPMENT.md` §2 根目录定性补修订（§5 规范清单、§12
> 回滚清单、ADR-0020 同步范围联动）；M2 判定树双匹配（INSTALL.md 先命中"文档"
> 行）→ `DIRECTORY.md` §2 分发行**前置于文档行**、文档行加"非随制品分发"限定；
> M3 冲突继续分支与验收矛盾 → §9 兜底分路径（正常=安装目录内 aixcoding-cli /
> 冲突=chrys + 外部文件不动）、§10 断言外部文件内容不变、§13.4 验收例外口径、
> requirements 同步；m4 requirements 品牌验收收窄统一＋"下载校验"改按需显式；
> m5 stdin 行残留错误行号说明删除（0.28 全脚本无 `</dev/null>`）；m6 风险 6 改
> strict 基线漂移、回滚清单按文件列、INSTALL.md 平台矩阵补 Arm 仿真口径。
> 2026-11-05（六）**第四轮复审（Major 1 / Minor 3，全 confirm）处置版（收尾轮）**：
> M1 品牌验收与 C4 合法诊断冲突 → §3.5/§10/§13.2 与 requirements 统一为"品牌
> 残留判定口径"（禁作当前品牌/推荐命令；`iCode-TUI` 分发身份提示与旧 alias
> 迁移/保护诊断的必要历史引用豁免；测试上下文断言）；m2 §2/§8/§13.5 三处规范
> 清单补 `DEVELOPMENT.md`；m3 冲突路径兜底与探测改安装目录内绝对路径、"存在
> 且可执行"对齐上游；m4 回滚用户侧加"正常路径"限定。

## 0. 前置事实

1. **撞名考古**：上游 v0.28.0 的 `scripts/` 无 `install.sh/ps1`（git ls-tree 证实）——
   0.28 三分发物料全系 fork 纯新增；上游 0.29.1 起（随 iCode-TUI 上架 PyPI 的 release
   工程化）在**同名路径**引入自己的公网版脚本，新基线（0.31+）该路径遂成上游活跃
   文件（带 C4 marker C111-C114 与上游测试套 C119-C124）。
2. **上游已有 install 脚本测试**：`tests/app/test_install_scripts.py`（fake curl /
   web cmdlet stand-ins + URL 级契约断言）。
3. **0.33 包内 `install` 命令已完备 alias 创建**（`installer.py:536-568`
   `_install_command_alias`：POSIX 软链、Windows 硬链/复制、冲突跳过、legacy 清理）。
4. fork 仓库公开；上游公网版脚本的实际消费者仅 clone 仓库的开发者。
5. 0.28 无落点规范（`aixcoding/docs/standards/` 系 2026-10-25 阶段二建立），其
   `scripts/`+根级 `release/` 落法在规范体系下不可复用（DIRECTORY §2 例外清单为空）。

## 1. 裁决项定案（D1-D5 + 评审裁决 R-1/R-2/R-3，签认生效）

| # | 裁决 | 定案 |
|---|---|---|
| D1 | 上游 `scripts/install.{sh,ps1}` 处置 | **并存新增**：上游两脚本零触碰（marker/测试原样），内网版落 `aixcoding/release/`（R-1a），**避开撞名路径** |
| D1.1 | 文件命名 | `install-internal-mirror.sh` / `install-internal-mirror.ps1`（平铺于 `aixcoding/release/`） |
| D2 | ps1/sh parity | **维持不对称** + 文档明示（Windows on Arm / `--from` / SHA-256 自动化均为将来项）。Windows on Arm 现状钉死（复审 m7）：接受 **x64 仿真**——`Is64BitOperatingSystem` 在 ARM64 为 true、下载 x86_64 包，仿真路径可用即不拒绝；Arm 原生制品发布时再评估 |
| D3 | `icode-*` 旧制品兼容 | **不保留（R-2a 砍）**：新脚本仅认 `aixcoding-cli-*` 制品；`--from` 遇 `icode-*` 报错引导——**仅指向"从镜像获取最新版制品"，不推荐旧版安装脚本**（旧脚本会重新引入已淘汰的 `aixcoding` 软链与 `iCode` 品牌残留，复审 M3）；支持手册中的手工解压旧包事实提示保留（不经脚本路径） |
| D4 | 发布约定文档落点 | 批次内 `publishing.md` |
| D5 | 镜像唯一入口 | **统一 IP `82.187.34.98` + 失败提示逃生门**（域名从文档与脚本默认移除；IP 迁移代价 = 改脚本默认+改文档+重新分发，责任人裁定接受，前提判断＝IP 稳定） |
| R-3 | 校验模式 | **维持 0.28 显式 `--sha256`**（可选参数，不自动请求 `SHA256SUMS.txt`，镜像协议零变更；自动校验登记将来项——明文 HTTP 下其防损坏价值与成本约 20 行脚本 + 发布方多传一 CD 已生成文件，需求出现时再启） |

## 2. 总体方案

```
aixcoding/release/                          ← 新增：分发物料目录（R-1a/B2a；DEVELOPMENT §2 + DIRECTORY
                                             §1/§2/§4 + 专区 README + INDEX 规范闭环 + ADR-0020 随批次）
├── install-internal-mirror.sh                       ← 内网版 POSIX 安装器（0.28 433 行适配改造）
├── install-internal-mirror.ps1                      ← 内网版 Windows 安装器（0.28 51 行适配改造）
└── INSTALL.md                              ← 一般用户安装/卸载简明手册
aixcoding/docs/
├── install-uninstall-support.md            ← 开发/技术支持详细手册（INDEX 登记）
aixcoding/docs/batches/internal-installer/
├── publishing.md                           ← 发布方文档（镜像协议、上传流程、改名映射）
aixcoding/tests/internal_installer/
└── test_install_internal_mirror_scripts.py          ← 契约测试（复刻上游 _Server/fake curl 模式）
scripts/install.sh / install.ps1            ← 上游 0.33.0 公网版，零触碰
```

镜像侧（服务端不动）：根 `install.sh/ps1` = 仓内 `install-internal-mirror.*` 上传改名；
`channels/cli/{prod,test}.txt`、`releases/cli/<完整版本>/` 维持 0.28 协议。

## 3. 脚本设计（install-internal-mirror.sh 为主，ps1 同构映射）

以 0.28 `install.sh` 为基线，适配改动：

1. **删除 `create_launch_command`（:342-356）与 `show_installed_version` 的
   `aixcoding` 探测分支（:308）**——0.33 包内 `install` 命令已建 `aixcoding-cli`
   alias（前置事实 3）。
2. **迁入 0.33 冲突预检**（`check_aixcoding_cli_command` 同构，**两分支语义**，
   复审 M4 修正）：PATH 上发现归属外部程序的 `aixcoding-cli` 时——①已有自有安装
   （自有 `chrys` **存在且可执行**，对齐上游 `-x` 检查，第四轮复审 m3）→ **警告并
   继续**（升级场景，上游 :150-155）；②无自有安装 → **中止报错**（防"安装成功但
   命令仍属别的软件"，:157-160；包内 `_install_command_alias` 遇冲突仅警告返回
   None，无自有安装时脚本侧必须前置拦截）。测试覆盖两分支（§10）。
3. **环境变量双读 + `--base-url` 显式参数**：优先级＝显式参数（新增 `--base-url`，
   与 `--channel`/`--version` 同构）> `AIXCODING_BASE_URL` > `ICODE_BASE_URL` > 默认；
   channel 同构（`--channel` > `AIXCODING_CHANNEL` > `ICODE_CHANNEL` > prod）。
   帮助文本只宣传 `AIXCODING_*` 与显式参数；上游 `ICODE_VERSION/SOURCE` 不在消费面。
4. **D5 统一 IP**：默认 `http://82.187.34.98/aixcoding/file/aixcoding`；连接失败
   报错含"若服务器已迁移或需指定其他地址，用 `--base-url <地址>` 或设
   `AIXCODING_BASE_URL=<地址>` 后重试"。
5. **品牌清理**：rc 注释 → `# Added by AIxCoding installer`（存量不回清）；musl
   报错 "AIxCoding 不支持 musl"；临时目录 `icode-install.*` → `aixcoding-install.*`；
   包校验失败报错仅提示 `aixcoding-cli` 可执行文件（D3 砍，`或 icode` 提示删除）。
   **品牌残留判定口径**（第四轮复审 M1）：不得把 `iCode`/`icode` 作为当前产品
   品牌或推荐启动命令；两类**合法必要引用**不算残留——①`iCode-TUI` 分发身份
   提示（本批次不动，归 C5 裁决）；②旧 alias 归属保护/迁移/删除失败诊断中对
   历史命令 `icode` 的引用（C4 `command_migration.py` 行为，保护用户文件所必需）。
   测试按**上下文断言**（旧字符串仅出现于上述诊断语境），普通新装路径保持零旧
   品牌输出。
6. **D3 落地**：制品候选仅 `aixcoding-cli-<平台>-v<版本>-offline.{tar.gz,zip}`；
   解压验证仅认 `aixcoding-cli` / `aixcoding-cli.exe`；`--from` 传入 `icode-*`
   命名包 die 报错引导。
7. 其余（channels 解析、平台检测含 glibc/musl、`--sha256` 显式校验、`--from`、
   smoke test）逻辑平移，`aixcoding` 提示语改 `aixcoding-cli`；另按 §6 清单**采纳
   0.33 新增行为**：install 主调用 stdin 重定向（`</dev/null`）、退出码 126 +
   TMPDIR noexec 提示、幂等跳过、Rosetta 检测。

ps1（51 行基线）：双读+IP 默认同上；无 `--from`/冲突预检外的扩展（D2）。

## 4. 用户手册、支持手册与 publishing.md

- **`aixcoding/release/INSTALL.md`**：随制品分发的一般用户简明手册。默认正式版本在线/
  离线安装、临时运行、升级、普通卸载与 `--purge` 风险提示；不展开 channel、版本目录、
  变量优先级、内部 `chrys` 和 PATH 清理，复杂问题引导联系技术支持。
- **`aixcoding/docs/install-uninstall-support.md`**：新增长期支持手册并登记 INDEX，面向
  开发与技术支持。承接完整版本/下载 URL、变量与渠道、全部安装方式、冲突两分支、
  Windows on Arm x64 仿真、显式 SHA-256、PATH/rc、卸载残留和历史 `icode` 包事实提示；
  历史引用不构成当前品牌或推荐命令。
- **publishing.md**：镜像目录协议、发布流程（先传 `releases/cli/<版本>/` 后改
  `channels/cli/*.txt`）、prod/test 灰度语义、`install-internal-mirror.* → install.*` 上传
  改名映射、IP 迁移操作序（改脚本默认→发版→重传镜像→文档同步，D5 对策）；
  校验口径注明"脚本侧显式 `--sha256` 可选校验；`SHA256SUMS.txt` 上传可选
  （脚本不消费，供手工核对）"。

## 5. 上游改动清单（R2）

**零上游文件改动**（并存 + 合规落点的直接收益；README 补镜像段评估过并放弃，
将来文档批次考虑）。规范联动修订清单（均 aixcoding 树内文档、非 R2，随本批次
落盘）：`DEVELOPMENT.md` §2 根目录定性、`DIRECTORY.md` §1/§2/§4（判定树分发行
前置于文档行）、专区 `aixcoding/README.md`、`INDEX.md` 登记、`ADR-0020`（第三轮
复审 B1 补 DEVELOPMENT.md 联动）。

## 6. 0.33 上游行为采纳/否决清单（R12：迁移批次须对照目标基线同类机制）

| 0.33 行为 | 位置 | 0.28 内网版 | 判定 |
|---|---|---|---|
| `check_aixcoding_cli_command` 冲突预检 | install.sh:143-161 | 无 | **采纳迁入，两分支语义同构**（§3.2：有自有安装警告继续 / 无则中止；复审 M4） |
| Rosetta 检测（x86_64 壳报 aarch64） | install.sh:135-137 | 无 | **采纳**（macOS 正确选包） |
| 幂等跳过（同版本已装 + alias 在则跳过重装） | install.sh:268-283 | 仅提示"将覆盖升级" | **采纳**（升级幂等增强，行为更优；仅 POSIX sh——ps1 维持重跑即装，见 §3 两脚本增强清单不对称） |
| 自动安装防降级（`is_newer`；显式 `--version` 仍允许降级） | install.sh:113-121 等 | 无（重跑即装） | **否决**——内网 channel 切换属**自动安装路径**且含合法"降级"（test 版本可低于 prod），同构上游会阻断渠道切换；渠道自由语义保留（复审 M4 行为名称对准） |
| install 主调用 stdin 重定向（`</dev/null`） | install.sh:302 | 无（全脚本无 `</dev/null`；第三轮复审 m5 核正） | **采纳新增**（piped 脚本 stdin 不泄入子进程，0.28 无此防护） |
| 退出码 126 + TMPDIR noexec 提示 | install.sh:303-305 | 无 | **采纳新增**（tmp 目录不可执行场景给出可行动提示，复审 M4 已核） |
| Windows 真实架构检测（on Arm 区分） | install.ps1 | 无（硬编码 x86_64） | **否决**（随 D2；Windows on Arm 制品内网未发布） |
| GitHub/GitCode 双源回退 | install.sh:60-83 | 无（内网单源） | **否决**（N/A：镜像单源；地址级回退由 `--base-url`/环境变量逃生门承担） |
| 自动下载 SHA256SUMS.txt 校验 | install.sh:237-265 | 无（显式 `--sha256`） | **否决（R-3b）**，登记将来项（§1 R-3 行） |

## 7. 定制点登记预告

**不适用**（复审 B1）：本批次全部产物落 aixcoding/ 树内——`gen_customizations.py`
的 `iter_upstream_files()`（:121-131）以 `is_custom(rel)` 跳过 aixcoding/ 文件，
marker 不被扫描；为纯定制资产登记 C id（前版草案 C136-C138）必成孤儿登记、
`--strict` 报 error。故本批次**零登记**，脚本内亦**不加 marker**；`--strict`
预期结果＝零新增登记、零漂移（相对 C4 后基线 98 登记行/135 marker 不变）。

## 8. ADR 判定

`aixcoding/release/` 新顶层目录 + `DEVELOPMENT.md` §2 上位总纲与 `DIRECTORY.md`
§1/§2/§4 及专区 README 定性修订（"纯开发资产"→含分发物料）是否触发新 ADR——
**判定：触发（复审 B2a 裁决 + 第三轮 B1 补上位总纲）**。
判定 checklist：①有被认真考虑但否决的备选（`aixcoding/tools/`——语义错位；
批次目录——过程记录语义；恢复 `scripts/` 上游树——违反 R4/DIRECTORY §2 且与
上游撞名）✔；③约束未来工作模式（此后所有"随制品分发物料"按本规则落位，后来者
会问"为什么有这个目录"）✔。**随批次出 ADR-0020**（分发物料目录设立：撞名考古
背景、备选否决理由、DIRECTORY 定性修订），正文含被否决备选；ADR 与 design 同批
签认。

## 9. 对上游隐式行为的假设（依赖清单）

| 假设 | 依据 | 失效后果 |
|---|---|---|
| 包内 `install` 创建 `aixcoding-cli` alias（POSIX 软链/Windows 硬链） | `installer.py:536-568`（0.33 实码） | 安装完成但无新命令——**分路径兜底**（第三轮复审 M3；路径精确化第四轮 m3）：正常路径 smoke test 后验证**安装目录内** `aixcoding-cli --version`；冲突继续路径（外部命令被保留）验证**安装目录内** `chrys --version`（POSIX `~/.local/bin/chrys --version`、Windows 安装目录内 `chrys.exe --version`——避免 PATH 污染）并提示以 `chrys` 启动，**不以 PATH 上的 `aixcoding-cli` 作安装验证**（那是外部程序） |
| 包内 `install` 遇外部同名命令仅警告不失败 | `installer.py:559-561` 返回 None | "成功"但 PATH 上 `aixcoding-cli` 仍属外部——§3.2 预检两分支已覆盖：无自有安装时前置中止；有自有安装（升级场景）警告继续且该路径验收口径见 §13.4 |
| PyApp 制品解压根含平台命名可执行文件（`aixcoding-cli[.exe]`） | 0.28 解压验证逻辑 + 0.33 制品名 | 解压验证 die——INSTALL.md/发布侧对账兜底 |
| `chrys --version` 输出可用于已装版本探测 | 0.28/0.33 同构 | 幂等跳过失效，退化为重装（无害） |

## 10. 测试策略

- `aixcoding/tests/internal_installer/test_install_internal_mirror_scripts.py`：复刻上游
  `_Server` + fake curl / web cmdlet stand-ins 模式，伺服 **0.28 镜像协议**
  （`channels/cli/*.txt` + `releases/cli/<版本>/aixcoding-cli-*`，**不伺服也不
  断言 `SHA256SUMS.txt` 请求**——R-3b：脚本不消费）。断言：URL 请求序列、
  优先级（`--base-url` > `AIXCODING_*` > `ICODE_*` > 默认）、`--from` 本地路径、
  `--from` 遇 `icode-*` 报错、冲突预检**两分支**（外部 `aixcoding-cli` + 自有安装
  →警告继续且**断言外部文件内容未被改动**（第三轮复审 M3，非仅返回码/警告）；
  无自有安装 →中止，复审 M4）、幂等跳过、品牌输出**按上下文断言**（第四轮复审
  M1：普通新装路径零 `iCode/icode`；`iCode-TUI` 分发身份提示与旧 alias 迁移/保护
  诊断（C4 `command_migration.py` 输出）中的历史 `icode` 引用为合法必要，断言其
  仅出现于诊断语境、不作为品牌或推荐命令；限定**新脚本与新制品路径**，历史包
  自身输出不在此验收面（复审 M3）。
- ps1 测试非 Windows skip：**R13 豁免已批准**——形态＝测试 skipif；理由＝被测
  `install-internal-mirror.ps1` 仅支持 Windows 且真实路径依赖 Windows 侧环境（包内
  `chrys.exe install` 行为），跨平台 pwsh 不能替代该真实执行环境；已评估正当
  替代＝pwsh 跨平台运行脚本逻辑——否决（脱离被测真实路径，测试价值归零），与
  上游 `test_install_scripts.py` 既有 skip 形态同构（复审 m8 修正理由表述）；
  批准指向＝本 design（签认即批准）；实施时就地注释记录。
- POSIX 测试 Windows skip：**R13 豁免已于 2026-11-05 获责任人补充批准**——形态＝
  与上游 `_POSIX_ONLY` 同构的 `skipif(sys.platform == "win32")`；理由＝被测
  `install-internal-mirror.sh` 仅支持 Linux/macOS 的 Bash 真实路径，Windows 无该执行环境；
  已评估 Git Bash 等兼容层，不能代表目标平台，测试证据不成立。Windows 跳过不计为
  POSIX 通过，仍须 Linux/macOS 实跑；批准指向＝本 design，实施时就地注释记录。
- 上游 `tests/app/test_install_scripts.py` 零改动。

## 11. 风险与对策

| # | 风险 | 对策 |
|---|---|---|
| 1 | 服务器迁移/IP 变更（D5 固有代价） | 失败提示含 `--base-url`/环境变量指引；publishing.md 迁移操作序 |
| 2 | 双读变量与镜像协议漂移 | publishing.md 固化协议快照；channels 解析保持宽容（首行 trim） |
| 3 | 仓内两套 install 并存认知混乱 | DIRECTORY 判定树 + INDEX 一句话简介区分；AGENTS 不动 |
| 4 | 0.28→0.33 脚本行为漂移（PyApp 解包等） | smoke test 前置；实施期 0.33 制品全路径实测 |
| 5 | 旧包用户装不了（D3 砍的接受面） | 报错引导文案 + INSTALL.md 加注；镜像旧制品由发布方按 publishing.md 清理节奏处置 |
| 6 | `--strict` 基线漂移（本批零登记零 marker，预期基线 98 行/135 marker 不变） | 实跑 `--strict` 前后对照 |

## 12. 回滚方案

- **代码/文档**：批次 PR git revert 即净——改动＝aixcoding 树内**新增文件** +
  **存量修改按文件列清单**（第三轮复审 m6）：`DEVELOPMENT.md` §2、`DIRECTORY.md`
  §1/§2/§4、`aixcoding/README.md`、`INDEX.md`，全部在 aixcoding 树内、零上游耦合，
  revert 连规范修订一并回收。
- **镜像**：回滚目标＝**最近一份符合 C4 契约的新脚本版本**（镜像侧重传上一版
  `install-internal-mirror.*` 改名产物）；**不以 0.28 旧脚本为回滚目标**——旧脚本会重新
  创建 `aixcoding` 软链并写 `iCode` rc 注释（与 C4 契约冲突），确需回退 0.28 时
  须同步公告旧 alias 副作用与手工清理方式。`channels/releases` 协议未变，制品
  无需回滚。
- **用户侧**：**正常路径**下本脚本安装产物（`chrys` + `aixcoding-cli` alias）在
  各新脚本版本间同构，版本间回滚无需用户动作；冲突升级例外沿用 §13.4 口径（外部
  命令被保留、以 `chrys` 启动，第四轮复审 m4 限定）；与 0.28 旧脚本产物**不同构**
  ——旧脚本额外创建 `aixcoding` 软链（复审 M5）。

## 13. 验收（对齐 requirements 验收口径）

1. 内网在线（prod/test）、`--from` 离线、解压直接启动三路径在 0.33.0 制品上实测
   走通（Windows 按 D2：在线 + 手工 exe 两式）；
2. **新脚本与新制品路径**品牌口径（第四轮复审 M1）：不得把 `iCode`/`icode` 作为
   当前产品品牌或推荐启动命令；`iCode-TUI` 分发身份提示与旧 alias 迁移/保护诊断
   （C4 `command_migration.py`）中的必要历史引用**不算残留**；普通新装路径保持
   零旧品牌输出；历史包自身输出与支持手册历史包事实提示不在此验收面
   （复审 M3）；
3. 优先级断言绿（`--base-url` > `AIXCODING_*` > `ICODE_*` > 默认）；
4. C4 契约不回归：**正常路径**安装产物命令＝`aixcoding-cli`（+内部 `chrys`）、
   无 `aixcoding` 软链；**既有外部命令冲突的升级路径**（§3.2 分支①）例外——
   保留外部命令、以 `chrys` 启动并提示（与上游 0.33 语义同构，第三轮复审 M3）；
   外部命令文件内容在整个安装过程中不被改动（测试断言）；
5. 上游测试套与 C111-C124 零改动零漂移；`gen_customizations --strict` **零新增
   登记、零漂移**（B1：本批次无 C id）；`DEVELOPMENT.md` §2、DIRECTORY §1/§2/§4、
   专区 README、INDEX、ADR-0020 随批次落盘；
6. 用户 INSTALL.md 只含一般用户默认操作；install-uninstall-support.md/publishing.md
   与脚本行为逐项一致（命令/变量/IP/渠道/卸载/旧包口径），并登记 INDEX。
