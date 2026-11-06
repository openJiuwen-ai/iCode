# 内网镜像发布约定（internal-installer）

本文面向发布方，约定 AIxCoding 内网安装脚本、离线制品和渠道指针的发布方式。服务端目录
布局不在本批次改造范围内；发布方按既有协议上传即可。

## 1. 仓内物料与上传名称

| 仓内文件 | 镜像根目录中的名称 | 消费方 |
|---|---|---|
| `aixcoding/release/install-internal-mirror.sh` | `install.sh` | Linux / macOS 用户 |
| `aixcoding/release/install-internal-mirror.ps1` | `install.ps1` | Windows PowerShell 用户 |
| `aixcoding/release/INSTALL.md` | 可作为安装手册随制品分发 | 内网用户 |

仓内使用 `install-internal-mirror.*` 是为了与上游 `scripts/install.*` 公网安装器并存。上传镜像时
必须按上表改名为 `install.*`；不要覆盖或改动仓内上游脚本。

## 2. 镜像目录协议

默认镜像根地址：

```text
http://82.187.34.98/aixcoding/file/aixcoding
```

根目录下维持以下结构：

```text
install.sh
install.ps1
channels/
└── cli/
    ├── prod.txt
    └── test.txt
releases/
└── cli/
    └── <完整版本>/
        ├── aixcoding-cli-linux-x86_64-v<完整版本>-offline.tar.gz
        ├── aixcoding-cli-linux-aarch64-v<完整版本>-offline.tar.gz
        ├── aixcoding-cli-macos-x86_64-v<完整版本>-offline.tar.gz
        ├── aixcoding-cli-macos-aarch64-v<完整版本>-offline.tar.gz
        └── aixcoding-cli-windows-x86_64-v<完整版本>-offline.zip
```

- 平台目录使用完整版本，不带开头 `v`；制品文件名中的版本带 `v` 前缀。
- 实际可提供 `.zip` 形式的 POSIX 包；POSIX 在线安装先尝试 `.tar.gz`，缺失时再尝试当前
  命名的 `.zip`。
- channel 文件首行是完整版本，可有开头 `v`、CRLF 和首尾空白，客户端会归一化。
- 仅发布当前 `aixcoding-cli-*` 制品。新脚本不兼容也不回退到 `icode-*` 历史制品。
- Windows 当前只发布 x86_64 包；Windows on Arm 依赖 x64 仿真，不得将该包标成 Arm 原生包。

## 3. 发布顺序与渠道语义

1. 用既有构建链生成并冒烟验证目标平台的 0.33 系列真实离线制品。
2. 上传完整制品集合到 `releases/cli/<完整版本>/`，确认各 URL 可下载、包根目录含
   `aixcoding-cli` 或 `aixcoding-cli.exe`。
3. 如需灰度，先将 `channels/cli/test.txt` 指向该版本并完成 test 渠道验证。
4. 验证通过后再更新 `channels/cli/prod.txt`。channel 指针必须最后更新，避免用户读到
   尚未上传完成的版本。
5. 从镜像重新下载 `install.sh` / `install.ps1`，分别核对与仓内 `install-internal-mirror.*`
   内容一致，再按 `aixcoding/release/INSTALL.md` 走用户路径。

prod 与 test 是独立渠道指针，不承诺版本号单调递增。客户端允许从较高的 prod 版本切到
较低的 test 版本，不启用公网脚本的自动防降级逻辑。

## 4. SHA-256 口径

- POSIX 脚本只消费用户显式传入的 `--sha256 <64位十六进制摘要>`；Windows 脚本无此参数。
- 发布方可选择上传 `SHA256SUMS.txt` 供人工核对，但脚本**不会自动请求或消费**该文件，
  上传它不属于安装协议的必要条件。
- 向需要显式校验的用户提供摘要时，应对应实际上传的包字节；更换包后必须重新计算。
- 内网明文 HTTP/IP 直连沿用 ADR-0018 的既定边界；显式摘要是按需防损坏措施，不在本批
  扩展为自动信任链。

## 5. 镜像地址迁移

默认地址变更需要同时处理脚本和文档，操作序如下：

1. 修改两份 `install-internal-mirror.*` 的默认根地址，并同步 `INSTALL.md`、本文及其他现行引用。
2. 完成契约测试和真实制品验收，发布新脚本版本。
3. 将新脚本按第 1 节改名上传到镜像根目录，再同步分发更新后的安装手册。
4. 保留旧地址可用直至用户迁移窗口结束；无法保留时，公告使用 `--base-url` / `-BaseUrl`
   或 `AIXCODING_BASE_URL` 指向新地址。旧 `ICODE_BASE_URL` 仅为兼容输入，不作为新公告的
   推荐配置。

## 6. 回滚

- 镜像脚本回滚目标是最近一份符合当前 AIxCoding / `aixcoding-cli` 命令契约的
  `install-internal-mirror.*` 版本，按第 1 节改名重传。
- 不以 0.28 旧安装脚本为回滚目标；旧脚本会创建已废弃命令并写入旧品牌配置。
- channel 与 releases 协议未变，已发布制品无需因脚本回滚而改名或重传。
- 若必须回退到 0.28 旧制品体系，需另行公告历史 alias 副作用和手工清理方式，不得把该
  路径描述为无损回滚。
