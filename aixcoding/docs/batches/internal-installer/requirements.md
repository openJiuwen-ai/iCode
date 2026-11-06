# 需求：内网安装器与发布约定迁移（internal-installer）

> 活文档：随时更新、不签认（git 留痕）；意图确认由 design 签认承载。
> 来源：ROADMAP §4 C 类行 1"内网安装器与发布约定"（2026-11-05 PR #94 拆行后首行；
> 前提"体系保留"同日责任人已定并随该 PR 入库）。
> 修订记录：2026-11-05 初版（A1 立项，ADR-0012 判定＝完整 §6 管线，责任人裁决）。

## 背景与问题

内网用户无法访问公网（PyPI/GitHub），0.28 旧分支为此建立了一套**内网安装/分发体系**：
内网 HTTP 镜像充当"应用商店"，提供 prod/test 双渠道
（`channels/cli/*.txt` 指针文件）与 PyApp 自包含离线制品（用户机器无需 Python/uv）。

**镜像地址双写法并存（职责分裂，2026-11-05 全仓核查，design D5 收敛）**：用户文档
入口＝域名 `aicoding.s.bocsys.cn/aixcoding/file/aixcoding/`（仅 `release/INSTALL.md`
2 处），脚本内部默认＝IP `82.187.34.98` 同路径（`install.sh:21` / `install.ps1:8`
的 `ICODE_BASE_URL` 默认值 + 头注释示例 + 0.28 `README.md:101`）；实际安装流程
混搭——域名仅用于下载脚本第一步，channels/制品下载全程走 IP。两种独立故障面：
内网 DNS 不解析域名则文档入口失效；IP 变更则脚本默认失效（仅
`ICODE_BASE_URL` 应急覆盖可救）。注意 `82.187.34.98` 同 host 另承载认证/数据
服务（`/csas`、`/aicoding/api`——auth/catalog 批次已迁入新分支的
`environments.py`/`catalog.py` 消费），服务器迁移时与文件镜像需同批评估（非本
批次范围）。新分支 `README.md` 无镜像段（0.28 的 download root 说明未随 0.31
重建带入），是否补段属上游文件改动、design 可选议（触发 R2）。

新分支（0.33.0 基线）现状缺口：

- `scripts/install.sh` / `install.ps1` 为**上游 0.33.0 公网版**（GitHub/GitCode 下载源
  ＋ C4 品牌适配），无 `--from` 离线模式、无 channels 机制；
- `release/` 目录不存在（0.28 的 `release/INSTALL.md` 为 fork 纯新增——上游原仓亦无
  `release/`；**撞名考古 2026-11-05：0.28 三分发物料全系 fork 新增，上游 v0.28.0 无
  install.*，上游 0.29.1+ 才在 scripts/ 同名路径引入公网版**——A1 新增文件全部落
  `aixcoding/release/`（design R-1a），R4 合规、零 R2）；
- 内网镜像发布约定（先传 `releases/cli/<完整版本>/`、最后改 `channels/cli/*.txt`
  指针）仅存于 0.28 `install.sh` 头注释，无独立文档。

判定（ROADMAP 行 1，2026-11-05 评审收敛）：**有改动的迁——按 0.33/C4 契约适配迁移、
非原样平移**。

## 0.28 资产清单（考古基线，2026-11-05）

| 资产 | 0.28 形态 | 新分支现状 | 迁移性质 |
|---|---|---|---|
| `scripts/install.sh` | 433 行内网版：镜像下载＋`--from` 离线＋channels＋SHA-256＋`icode-*` 旧制品回退＋glibc/musl 检查 | 上游公网版（C4 适配） | **纯新增至 `aixcoding/release/`**（0.28 系 fork 新增、上游撞名占据 scripts/；避开撞名落合规目录，design D1/R-1a） |
| `scripts/install.ps1` | 51 行内网版：仅在线、仅 x86_64、无 `--from`/SHA-256/回退 | 上游公网版（C4 适配） | 同上 |
| `release/INSTALL.md` | 106 行用户手册：三安装方式＋平台矩阵＋FAQ | 不存在 | **新增**（fork 独有，上游无此目录） |
| channels/镜像发布约定 | `install.sh` 头注释 16 行 | 无 | **文档化**（批次内落独立说明或并入 INSTALL 附录） |
| 发布指南 | `aixcoding/doc/aixocding_cli_发布Releases操作指南.md`（GitHub CD 流程，D 类历史文档） | 无（D 类待去向裁决） | 非本批次范围（见非目标） |

**两平台功能不对称**（0.28 实测）：ps1 无 `--from` 离线安装、无 SHA-256 校验、无
`icode-*` 回退、仅 x86_64（Windows 离线走手工 `.\aixcoding-cli.exe install`）；
sh 侧额外执行 `create_launch_command`（创建 `~/.local/bin/aixcoding` 旧命令软链）。

**命令契约差异**：0.28 用户命令 Linux/macOS＝`aixcoding`、Windows＝`aixcoding-cli`
（两平台不一致，INSTALL.md 26/42 行实证）；C4 已统一为 `aixcoding-cli`——迁移必须
落在 C4 契约上，不重新引入旧命令。

**环境变量**：内网版消费 `ICODE_BASE_URL`、`ICODE_CHANNEL`；上游 0.33 公网版消费
`ICODE_VERSION`、`ICODE_SOURCE`（上游契约，不清零）。

**品牌残留**（0.28 内网版实测，`iCode-TUI` 包名无关）：①rc 注释
`# Added by iCode installer`（install.sh:369，长期可见）；②musl 报错文案
"iCode 不支持 musl"（:170/:176）；③临时目录 `/tmp/icode-install.*`（:409，仅 shell
版；ps1 侧为 `aixcoding-install-<guid>` 干净）；④帮助文本 `ICODE_*` 变量名；⑤`icode-*`
旧制品兼容回退（:239-240/:290-291）。

## 目标

- 迁入内网安装器两脚本，按 0.33/C4 契约适配（ROADMAP 行 1 五要点）：
  1. `create_launch_command` 改造——不引入 C4 已废弃的 `aixcoding` 旧命令；
  2. `ICODE_BASE_URL`/`ICODE_CHANNEL` 更名 `AIXCODING_*` 采**双读兼容**
     （显式参数 > `AIXCODING_*` > 旧 `ICODE_*` > 默认值；显式参数层补 `--base-url`，
     评审 M5）；上游 `ICODE_VERSION`/
     `ICODE_SOURCE` 属上游契约不触碰；
  3. ps1/sh parity：**维持不对称 + 文档明示（design D2 已裁）**，Windows 补齐为将来项；
  4. 镜像 IP 与 INSTALL.md 域名差异收敛为**唯一推荐入口：统一 IP + 失败提示逃生门
     （design D5 已裁，2026-11-05）**；①自动回退/②统一域名两案对比留档 design；
  5. 品牌清理：①②③新脚本直接调整，存量 rc 注释不回清；⑤`icode-*` 兼容
     **不保留（design R-2a 已裁，评审 B2）**——新脚本仅认 `aixcoding-cli-*` 制品，
     `--from` 遇旧包报错引导，支持手册中的手工解压旧包事实提示除外。
- 迁入 `INSTALL.md` 至 `aixcoding/release/`：定位为一般用户简明手册，只保留默认正式版本
  安装、升级、卸载和求助路径，命令统一 `aixcoding-cli`，不暴露内部协议细节。
- 新增 `aixcoding/docs/install-uninstall-support.md`：面向开发与技术支持，承接版本/下载
  地址、变量/渠道、全安装方式、冲突、PATH、卸载残留和历史包提示等详细口径。
- 内网镜像发布约定文档化（从脚本头注释独立成文，落点 design 定）。

## 非目标

- **构建体系**（`build.*` / `build_offline_dist.*` / `fetch_rg.*` / offline 约束文件）：
  已在 0.32.2 基线验证可用（2026-11-02 离线打包配方），不属本批次；
- **分发身份 `iCode-TUI` 裁决**（ROADMAP 行 2）：独立呈报需求侧，本批次不碰
  `DISTRIBUTION_NAME` 及其 3+1 处 UI 引用；
- **C1 版本更新检测内部化**：独立批次；本批次不动 `update_check.py`；
- **用户指南 docs/ 双语树**：独立文档批次（依赖 C5 分发身份结论）；
- **内网镜像服务端**（目录布局/服务配置）：服务端不动，客户端按既有协议消费；
- **D 类历史文档去向**（含发布Releases操作指南.md）：责任人逐份裁决，本批次仅引用。

## 用户场景

1. **内网在线安装**（推荐路径）：`curl …/install.sh | bash`（或 ps1 `irm|iex`）→
   读 channel 指针取版本 → 下载（按需显式 SHA-256 校验，R-3b）→ `aixcoding-cli
   install` → 新开终端 `aixcoding-cli --version` 验证；
2. **渠道切换**：`--channel test` / `$env:AIXCODING_CHANNEL='test'`（旧 `ICODE_*` 名
   仍生效）取测试版；切回 prod 前清除变量；
3. **离线拷贝安装**：Linux/macOS `bash install.sh --from ./aixcoding-cli-<平台>-v<版本>-offline.zip`；
   Windows 维持手工 `.\aixcoding-cli.exe install`（D2 已裁维持不对称，`--from` 补齐为将来项）；
4. **解压直接启动**（不装命令）：`./aixcoding-cli`（含 macOS 隔离标记提示、旧包
   `icode` 兼容提示）；
5. **覆盖升级**：重跑安装命令，会话与配置（`~/.chrys`/`%APPDATA%\chrys`）保留；
6. **发布方操作**：上传制品至 `releases/cli/<完整版本>/` → 最后更新
   `channels/cli/{prod,test}.txt` 指针（约定文档化后可循文档执行）。
7. **技术支持处理复杂安装/卸载**：一般用户按 INSTALL.md 完成默认操作；需要指定版本、
   测试渠道、镜像覆盖、冲突处理或完整清理时，支持人员按 install-uninstall-support.md
   提供命令，不向普通用户转嫁内部协议细节。

## 验收口径（草案，design 细化）

- 新分支（0.33.0 基线）上，内网在线/离线/直接启动三路径 + 双渠道切换可走通；
  Windows 按 D2 已定范围验收：在线脚本 + 手工 exe，不补 `--from`/SHA-256；
- **新脚本与新制品路径**品牌口径（第四轮复审 M1 与 design 统一）：不得把
  `iCode`/`icode` 作为当前产品品牌或推荐启动命令；`iCode-TUI` 分发身份提示与
  旧 alias 迁移/保护诊断（C4 `command_migration.py`）中的必要历史引用**不算
  残留**；普通新装路径保持零旧品牌输出；支持手册历史包事实提示与历史包
  自身输出不在此验收面；存量 rc 注释豁免；
- `--base-url`/`AIXCODING_*`/`ICODE_*` 优先级符合目标②定义；
- C4 契约不回归：**正常路径**安装产物命令为 `aixcoding-cli`（+内部 `chrys`
  二进制），不出现旧 `aixcoding` 软链；既有外部命令冲突的升级路径例外（保留
  外部命令、以 `chrys` 启动并提示，与上游 0.33 语义同构——design §13.4）；
- 上游 `ICODE_VERSION`/`ICODE_SOURCE` 契约不受影响（上游 install 路径行为不变或
  按处置裁决验证）。
