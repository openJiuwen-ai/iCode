# 验证记录：定制版用户手册入库与安装版捆绑（user-manual）

> 活文档（不签认）。交付证据（R7）：实际执行的命令与输出摘要。每次验证追加一节，**不删旧记录**；真实性由双 AI 自审与评审抽查保障。

## 2026-11-09 手册内容定制与链接门禁（Windows 11，本地）

命令：

```
python check_links.py "桌面\用户手册\20261109\0.35更新后\AIxCoding-CLI用户手册.md"
```

结果：`内锚 237 | 失效 0 | 外链 0 | PASS`。第三方外链去链（手册 5 处 + 仓库 en 文档 6 处，
zh 本为纯文本）；裸 URL 55 处逐条裁决保留（内网下载地址 / 代码示例 / CLI 真实报错输出）。
首轮内锚校验曾报 97 个"失效"，确认为校验脚本 slug 算法 bug（`\s` 吞空格），修正算法后 0 失效。

## 2026-11-10 定制手册入库与构建切换验证（Windows 11，本地）

### F8 三级解析实测（行为锚定）

```
CHRYS_DOCS_ROOT=C:\work\iCode\aixcoding\manual 下调用 resolve_docs_root()
→ 输出：F8 解析到: C:\work\iCode\aixcoding\manual（指向 manual: True）
```

### wheel 构建与捆绑比对（SAC 受限机器等价脚本，check_wheel.py 子进程被拦 os error 4551）

```
uv pip install hatchling（清华镜像，临时）→ python -m hatchling build
→ dist\icode_tui-0.35.1-py3-none-any.whl

zipfile 校验：wheel 捆绑 67 = manual 67 = docs 67；捆绑清单 == manual 清单（True）；
逐文件 sha256 抽验（index.yaml + 随机 5 篇）全部一致
```

### 上游回归测试

```
python -m pytest tests\foundation\test_documentation.py tests\app\tui\screens\guides
  tests\service\context\providers\test_documentation.py
  tests\orchestration\workflows\test_documentation.py
  tests\orchestration\invoker\test_documentation.py -q -p no:cacheprovider
→ 92 passed in 169.39s（零跟改）
  【2026-11-10 评审 F2 注：本节运行于 pyproject 捆绑源切换之前，"零跟改"结论已被评审推翻
  ——切换后契约测试必红，正确证据见下方"独立评审第一轮处置"F2 节（92 passed 含 C147 跟改）】
```

### 架构套件排查（确认失败与 manual 无关）

```
python -m pytest tests\architecture -q → 24 failed（全数集中在 test_test_hygiene.py）
根因：当时后台并存 4 个重复全量 pytest 进程，xdist worker 崩溃（node down: Not properly
terminated），非断言失败。清理重复进程后串行重跑：
python -m pytest "tests/architecture/test_test_hygiene.py::<两用例>" -q -n0
→ 8 passed in 295.37s
```

### 环境前提修复：vendored ripgrep

```
现象：tests/app/tui/widgets/test_search_renderers.py::test_grep_real_output_header_regex
  AssertionError（真失败，串行复现）
根因：src/chrys/foundation/vendor/ripgrep/ 为空（构建时下载步骤从未在本机执行）
修复：powershell scripts\fetch_rg.ps1（经代理，rg 15.2.0 x86_64-pc-windows-msvc）
复验：pytest tests\app\tui\widgets\test_search_renderers.py -q -n0 → 35 passed
```

### PDF 交付链路

```
markdown 库（清华镜像临时装、用毕卸）→ 技能脚本 md2html.py（GitHub slug 锚点 + 中文打印 CSS）
→ Edge 无头 --print-to-pdf → 桌面 0.35更新后\AIxCoding-CLI用户手册.pdf
→ 139 页 / 7.0MB；HTML h1-h3 锚点 id 388 个；外部超链接 0
```

### git 收账

```
fork/user-manual 分支（基于 main_aixcoding_cli@b8b02456）：
ed8dd517 build+docs: 定制版用户手册入库 aixcoding/manual 并切换 wheel 捆绑源
  69 files changed, +15287 / −3（manual 67 + pyproject.toml + scripts/check_wheel.py）
aixcoding-launch.py 未提交（责任人裁定本地资产）
均未 push。
```

## 2026-11-10 独立评审第一轮处置（评审方：Claude Code CLI · GLM-5.3，与开发 AI 不同源）

评审报告：桌面 `user-manual批次独立评审报告-20261110.md`（3 blocker / 2 suggestion / 1 nit）。
开发 AI 独立复核实证后，责任人拍板"同意"（1A + 2 必做 + 3 方案一 + 4/5/6 做）。处置：

- **F1（R2 表漏报契约测试连带跟改）→ 已修复**：
  `tests/foundation/test_documentation.py::test_wheel_bundles_docs_where_the_resolver_looks`
  断言键 `force_include["docs"]` → `["aixcoding/manual"]`（+`AIxCoding` 标记，C147）；
  design R2 表增补第 3 行 + 定制点预告 C147；test-plan"零跟改"表述修正。
- **F2（92 passed 证据对应切换前树况）→ 已重跑回填**：

  ```
  python -m pytest tests\foundation\test_documentation.py tests\app\tui\screens\guides
    tests\service\context\providers\test_documentation.py
    tests\orchestration\workflows\test_documentation.py
    tests\orchestration\invoker\test_documentation.py -q -p no:cacheprovider
    --basetemp=C:\work\_pytest_review
  → 92 passed in 27.77s（含 C147 跟改后契约测试，本机无 CI env 不跳过）
  单验契约测试：1 passed in 12.63s（PASSED test_wheel_bundles_docs_where_the_resolver_looks）
  ```

  旧记录（169.39s 节）对应 pyproject 切换前的树况，予以保留并加注（不删旧记录原则）。
- **F3（manual 树 100 处围栏外第三方外链，验收勾选失真）→ 已修复（方案一：真去链）**：
  围栏外 `[text](url)` 全量清点 = 52 唯一 URL / 100 处；逐条分类——仅
  `github.com/openJiuwen-ai/iCode/releases`（产品仓库发布页）属 aixcoding 相关保留（2 处），
  其余 98 处去链保留文字（10 文件：chat-options 41+41、settings 4+4、getting-started 2+2、
  web-tools 2+2、skills 1+1 等）。复扫：

  ```
  scan_manual_links.py → manual 树围栏外第三方外链总数: 2（即保留项 zh/en 各 1）
  wheel 内复扫（重建后）：wheel 内围栏外第三方外链: 0
  ```

- **F4（批次文档未入库 / plan 措辞）→ 本笔提交一并入库**；plan 措辞已改。
- **F5（等价脚本不可复现）→ 脚本落盘**：`verify_wheel_bundle.py`（本目录，SAC 受限机器
  等价校验，Linux CI 仍以 `scripts/check_wheel.py` 为准）。执行输出：

  ```
  wheel: icode_tui-0.35.1-py3-none-any.whl
  捆绑 67 vs manual 67，清单一致: True
  内容哈希不一致: 无
  ```

- **F6（nit）→ 随 F3 联动**，requirements 验收第 2 项证据更新。
- **顺带完成收尾项**：R3 标记补录——pyproject.toml（C145 行）/ check_wheel.py（C146 行）
  补 `AIxCoding` 单行标记，design C145/C146 marker 锚同步为正式标记文本。
- **开发 AI 复检追加发现（评审未覆盖）→ 已修复**：`ruff format --check aixcoding/`
  （CI govern 门，`aixcoding-tests.yml:129`）对 manual 树 6 个 md 的 ```python 围栏报
  unformatted（ruff 0.17 会重排手册中有意对齐的注释）——处置：`[tool.ruff] extend-exclude
  = ["aixcoding/manual"]`（C148，交付内容不参与格式化治理）。复检：

  ```
  ruff format --check aixcoding/ → 317 files already formatted
  ruff check aixcoding/         → All checks passed!
  ```

## 2026-11-10（续）wheel 重建与捆绑复验

manual 树去链后重建（`python -m hatchling build`）：

```
verify_wheel_bundle.py → 捆绑 67 = manual 67，清单一致 True，内容哈希不一致: 无
wheel 内围栏外第三方外链: 0（保留项 2 处在 manual 树源文件，wheel 内同样保留）
```

桌面定稿 MD/PDF 本轮未改（去链针对 manual 树；桌面合并版此前已清、含外链的
chat-options 章节为 manual 树独有），PDF 不需重出。

## 待补项

- [ ] `check_docs.py` 门禁绿证据（本批次文档入库后补跑）；
- [ ] C145/C146/C147 写入 customization-registry 批次清单 + `gen_customizations.py --strict`
      对账输出（R3 标记已补录，仅剩登记与对账）；
- [ ] 评审第一轮修复后的增量复审（HOWTO ③，同一评审 AI 复审增量）与责任人签认；
- [ ] Linux 侧 `check_wheel.py` 正式门禁输出（CI 桥接或无 SAC 环境补跑）。
