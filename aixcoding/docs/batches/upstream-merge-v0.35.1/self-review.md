# 自评审记录：追上游 v0.35.1（upstream-merge-v0.35.1）

- 责任人：周彬
- 状态：已确认
- 确认日期：2026-11-09
- 确认意见：无特别意见
- 开发 AI：CodeBuddy（Kimi-K3.1-Pro）
- 评审 AI：CodeBuddy/BM:GLM-5.3（文档一轮 + 收尾独立评审一轮）

> 记录档：签认一次，后续追加评审记录不触发重签。评审历程与裁决记录详见 verification.md 第六节。

## 一、评审历程

| 轮次 | 发现 | 处置 |
|---|---|---|
| 对照前三批次自审（开发 AI，签认前） | 5 处缺口：上批次固定关注文件未纳入、build.ps1 PBS 版本未坐实、uv cache-keys 分支切换坑、R11 消歧、{{!command}} 信任链路复核 | 全部补入 design（语义适配点/环境核查/风险节/R2 节/假设节） |
| 文档评审（CodeBuddy/BM:GLM-5.3，独立复测 30+ 项事实） | **blocker 0**；F1（--strict 草案窗口期未说明，中）+ F2（非合并数 33→32）+ F3（PET/BUDDY 查证对象应为 settings.py）+ F4（INDEX 欠账两批次）+ F5（本地化措辞偏宽） | 全部处置：F1 design 补窗口期说明+提交时点约束；F2 实测订正 32；F3 两文档同步订正（settings.py 10+/10−）；F4 INDEX 补两行；F5 措辞精确化；check_docs 复跑 OK |
| R2 增补报批 | fork-full 暴露 2 处上游新测试品牌断言冲突（C143/C144） | 责任人批准；随 design 重签（含 tui 联动） |

## 二、实施期自审发现（自身问题清单）

1. **退役记录表格式首版不自洽**：本批次 design 退役表用 7 列自造格式，被 strict 当现役
   登记解析（duplicate id C028 + tui orphan）——按 environment-config 先例改 4 列
   （id/原落点/退役日期/依据）+ tui 现役行删除与指向注记后收敛。教训：退役/登记表格
   严格沿用既有先例列结构，工具解析对格式敏感。
2. **C144 适配首跑漏第二处断言**：message 文案改后同测试 `display_message.args` 的
   `app: "iCode"` 绑定仍红——复跑定位后补全（同点不新增 marker，计数 1 保持，
   v0.34.0 C102 教训已吸收）。教训：同一测试的品牌断言须整函数通查，不只修第一处。
3. **豁免表适配联动测试遗漏**：_common.py 移除 mo 豁免行后，我方
   `test_report_renders_full_view` 的 `assert "chrys.mo" in out` 未同步——定制全套复跑
   暴露，改为断言现存 po 豁免项。教训：工具注册表变更须同步其渲染测试期望。
4. **环境变更链较长**：.python-version 3.14.10 → uv 过旧不认识 → uv self update 0.13.7 →
   minor 链接 junction 448 → .venv 删除被拒 → move 改名绕过 → 旧目录入本地 exclude。
   每步均为在案已知坑或本机策略实证，未引入仓库改动。
5. **fork-full 后台日志分析**：沿用 v0.34.0 教训（输出落盘 + findstr 提取 + 单跑定位），
   34 失败一轮归因完毕（448 计数与清单对账 32+2=34 完整），未采信 lastfailed 缓存。

## 三、遗留风险与 nit

- PR 双 CI 待 push 后跑（aixcoding-tests + 上游 ci.yml 桥接）；
- TUI 人工冒烟留人工补验（先例）；headless --version=0.35.1 已过；
- R3 与 mo 禁跟踪测试的中间态信号待 add 后复跑收敛（机制已定位，非真实违规）；
- 本机 31 个 WinError 448 环境族 + guard symlink ×2 在案（实跑归 PR CI；OS 级对照已坐实）；
- C004/C140 淘汰候选 AI 建议不命中/保留，**最终裁决待责任人**（verification 第五节）；
- 离线打包 PBS 缓存 20261026 与系统 Python 3.14.10 为环境待办，不阻塞本批次；
- 旧 `.venv_old_pre_0351/` 本地排除残留，清理留手动。

## 五、2026-11-09 收尾独立评审（CodeBuddy/BM:GLM-5.3，与开发 AI 同工具异模型）

**评审依据**：SELF-REVIEW-HOWTO §二 模板逐项执行；对象 = merge 提交 edec1539
（父 a7efff2e + 7c423b06，420 文件 +26997/−4487，工作树干净）。

**结论：通过（blocker 0）**；R2 逐文件对名/R3/R11/幻觉 API/vacuous test（两文件 54 passed
复跑+改坏即红）/冲突解 4 处/配套适配/机检复现/决策链留痕全过。findings 5 项（全 nit）处置：

- **F1**（verification R2 行 81/88 为登记前时点）→ **已修**：补 commit 终态 82/90 并标注时点；
- **F2**（design:87 C027 marker ×2 笔误，实际 ×1）→ **已修**：措辞更正，随 design 重签；
- **F3**（plan 状态节时点漂移）→ **已修**：刷新为 edec1539 已落待 push 终态；
- **F4**（R2 表头 '文件' warn 噪音，与 branding-and-command 历史同款）→ **不修留痕**：
  工具宽容忽略不阻塞，日后统一治理 R2 表头写法时一并处理；
- **F5**（两轮评审同模型共盲风险提示）→ **知悉留痕**：文档轮与实现轮检查面重叠度低，
  可接受；终裁在责任人。

评审状态披露：merge commit 已落未 push 符合逐条授权纪律；后续补充 add（本次修订文档）
→ push 仍逐条请示。

## 四、结论

文档一轮外部评审收敛（blocker 0，F1–F5 全处置）+ 对照前三批次自审补强 5 处 +
R2 增补批准与重签闭环；4 处 merge 冲突与 2 处全量暴露品牌冲突均按 design/增补报批解决
并验证；strict 101 行/138 marker/6 退役全绿，真实语义冲突清零（剩余失败均为环境性/
中间态在案）。可进入收尾（提交 + PR + 冒烟）。
