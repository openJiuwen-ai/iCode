# 需求：功能品牌与命令切换（branding-and-command / C4）

> 活文档：随时更新、不签认（git 留痕）；意图确认由 design 签认承载。
> 输入依据：2026-11-03 责任人重裁——原"C4a 只改运行时文案、C5 再改命令与安装别名"
> 拆分放弃，改为功能性品牌与命令切换一次闭环；纯用户文档继续后置。跨批次决策见
> `aixcoding/docs/standards/adr/0019-品牌定制阶段划分与命名边界决策.md`。

## 背景与问题

fork 定制版以 **AIxCoding** 品牌对内分发，但运行时用户可见界面仍自称上游品牌
**iCode**，实际用户命令、离线制品和安装别名仍为 `icode`。原方案把 `APP_COMMAND`
当作纯文案常量，计划先改帮助文案、后改真实命令；代码盘点确认该常量还直接驱动离线
alias 创建与卸载发现，原拆分会产生帮助、wheel、离线安装和卸载相互不一致的中间态。

命令重命名不是单一常量改动，而是一条跨 wheel、PyApp、release、安装、升级和卸载的
功能契约链。本批次将该链一次切换完成；大量纯文档替换不改变程序行为，继续独立后置。

## 目标与非目标

- 目标：
  - 用户可见运行时品牌从 iCode 切换为 AIxCoding：品牌常量、散点插值、内置 agent
    instructions、serve ASCII banner、安装脚本提示和 zh-Hans catalog 同步。
  - 用户规范命令从 `icode` 切换为 **`aixcoding-cli`**，wheel、CLI 帮助、PyApp 离线
    制品、release 资产、安装脚本和安装后 alias 使用同一命令名。
  - 全渠道采用单一规范命令：wheel 只安装 `aixcoding-cli`，不保留第二个 console
    script；新离线安装只创建新 alias，不建立永久双轨。
  - 支持既有离线安装安全迁移：新 alias 可用后才清理由本产品创建的旧 `icode` alias；
    外部同名命令、无法证明归属的文件和正在运行而不能安全删除的文件不得强删。
  - 卸载同时识别新旧自有 alias，覆盖直接卸载旧版、迁移中断和升级后卸载。
  - 每个运行时品牌定制点有行为锚定测试；命令链补齐 wheel、安装脚本、升级迁移、
    卸载和 release 命名一致性验证。
  - 同步修正会因入口改名立即失效的现行开发操作说明：root AGENTS 的首段入口事实和
    Commands、mock 启动配方、UPGRADE-RUNBOOK 冒烟命令；不做这些文件的其他文案改写。
- 非目标：
  - `docs/`、README 和其他纯说明材料，以及 AGENTS 超出上述最小操作联动的内容——继续
    并入"用户指南文档定制化"工作项，待目标上游版本和其他功能改动确定后集中实施。
  - 内置 workflow 的使用文档、docstring、测试注释和内部符号名的机械改名。
  - `DISTRIBUTION_NAME="iCode-TUI"`、wheel 文件名 `icode_tui-*` 和 PyPI 分发名——C4
    保持上游；是否更名及新旧包身份迁移归 C5。`pyproject [project.urls]` 仍随 C1。
  - import 包、`chrys` 内部安装二进制、`~/.chrys`、`CHRYS_*`、wire/header 名、数据键、
    PyApp `chrys-runtime*` 和 `chrys-offline-dist.tar.gz`——保持内部名。
  - 安装脚本公共环境变量 `ICODE_VERSION` / `ICODE_SOURCE`、卸载器内部
    `ICODE_UNINSTALL_*` 协议、持久数据签名 key 和后台日志 marker——为避免无关兼容
    破坏，本批不改。
  - C5 的分发包身份迁移、内网镜像、channels 和发布策略——不提前并入。
  - TUI/CHAT/WORKFLOW 的内部 `chrys` block 字标和 `_icode_serve_logo` 内部函数名——
    保持原样。

## 用户场景

1. 用户启动 TUI、查看 serve banner、运行 CLI 帮助或与 agent 交互，看到的产品品牌均为
   AIxCoding，帮助中的规范命令为 `aixcoding-cli`。
2. 用户通过 wheel 全新安装，只得到 `aixcoding-cli` 命令；通过离线包全新安装，得到内部
   `chrys` 主程序和用户 alias `aixcoding-cli`。
3. 既有离线用户升级到新版，安装脚本识别新资产和新 alias；迁移成功后，本产品旧
   `icode` alias 被清理，外部同名程序不受影响。
4. 用户执行同版本重装、alias 丢失修复、较新版本防降级或卸载，均按新命令名工作，且
   不遗留可直接识别的旧自有 alias。
5. 文档批次完成前，随包文档仍可能写作 iCode/`icode`；发布说明和 PR 描述明确新命令及
   "重新获取新版安装脚本"的迁移要求。

## 验收标准

- [ ] `APP_DISPLAY_NAME == "AIxCoding"`、`APP_COMMAND == "aixcoding-cli"`；运行时品牌
      散点、Code/QA instructions、serve ASCII banner 和安装脚本品牌提示符合 design。
- [ ] `pyproject [project.scripts]` 只有 `aixcoding-cli = "chrys.app.cli.app:main"`；
      `test_icode_is_the_only_command` 的现有字典断言继续成立，测试名/docstring 跟改。
- [ ] `check_wheel.py` 在三平台验证唯一命令、`--version` 和全部 help usage 均为
      `aixcoding-cli`；`iCode-TUI` 分发名与 `icode_tui-*` wheel 名保持不变。
- [ ] build.sh/build.ps1 产出 `aixcoding-cli[.exe]`；CD matrix artifact、内部 binary、
      release 收集、tar/zip 包内文件、SHA256SUMS 和安装后 smoke 的名称逐跳一致。
- [ ] install.sh/install.ps1 使用新包名、新包内 binary 和新 alias 完成下载、冲突检测、
      完整性检查、修复和防降级；不再依赖旧 alias 才能判定安装完整。
- [ ] 离线迁移矩阵通过：POSIX 自有 symlink、Windows hard link、Windows copy fallback；
      新 alias 失败时保留旧可用 alias；外部新旧命令均不删除；无法证明归属时保留并警告。
- [ ] 卸载清理新旧自有 alias；Windows deferred removal 仍只处理自有 `chrys\bin` 目录。
- [ ] approval 英文 fallback 与中文翻译均使用 `aixcoding-cli approval`；i18n
      extract→update→translate→compile 全链完成，Linux catalog oracle 通过。
- [ ] 原运行时品牌测试、安装/卸载迁移测试、定制行为测试和 release 命名契约测试通过；
      pyproject 变更触发全量上游测试，fork 全量门禁与定制清单 `--strict` 均为 exit 0。
- [ ] AGENTS、现行 mock 配方和 UPGRADE-RUNBOOK 不再指导开发者执行已删除的
      `uv run icode`；`uv run aixcoding-cli --version` 开发入口冒烟通过。
- [ ] 发布说明明确一次性命令迁移和旧安装脚本限制；文档旧命令窗口登记为已知限制，
      不把它误记为程序功能未切换。

## 已知限制

- **纯文档窗口**：本批次后至用户指南批次完成前，`/help`、F8 用户指南、随 wheel 分发
  的 docs/ 和 README 仍可能自称 iCode 并示例 `icode`。程序功能已经切换，照旧命令示例
  操作会失败；发布说明须提供新命令。
- **旧安装脚本过渡**：用户已经保存在本地的旧 install.sh/install.ps1 只识别 `icode-*`
  release 资产，不能发现仅含新资产的版本；须重新获取新版安装脚本。本批不发布双命名
  离线资产来维持旧脚本。
- **安全优先的残留例外**：正在运行的 Windows 旧 alias 或无法证明归属的旧文件可能保留，
  并给出警告；不得为追求文件名清零而删除外部程序。

## 开放问题

- 无方案级阻塞问题，但这不构成开工授权。实施前仍须完成 ADR-0012 开工判定、扩展方案
  双 AI 评审及 design/ADR 签认。登记预告的 marker 分布可在实施期按实跑红面校准；签认后
  任何实质范围变化仍须重签 design，并同步重核 ADR-0019。
