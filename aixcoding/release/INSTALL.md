# AIxCoding 内网安装与卸载手册

本文供内网用户使用。安装包已内置 Python 和依赖，无需另装 Python；模型服务仍需按实际
环境配置。

支持 Linux、macOS 和 64 位 Windows。Windows on Arm 使用 x64 安装包仿真运行。
如果不确定电脑 CPU 架构或应选择哪份安装包，请联系技术支持。

## 1. 在线安装（推荐）

### Linux / macOS

打开终端，复制并执行以下整行命令：

```bash
curl -fsSL http://82.187.34.98/aixcoding/file/aixcoding/install.sh | bash
```

不要加 `sudo` 或在 root 账户下运行；安装只写入当前用户目录，无需管理员权限。

### Windows

打开 PowerShell，复制并执行以下整行命令：

```powershell
irm http://82.187.34.98/aixcoding/file/aixcoding/install.ps1 | iex
```

### 验证安装

等待安装完成，**重新打开终端或 PowerShell**，执行：

```bash
aixcoding-cli --version
```

能够显示版本信息后，执行以下命令开始使用：

```bash
aixcoding-cli
```

## 2. 离线安装

### 第一步：取得安装文件

向技术支持获取与你的系统匹配的安装文件：

| 系统 | 需要的文件 |
|---|---|
| Linux / macOS | `install.sh` 和离线安装包 |
| Windows | Windows ZIP 离线安装包，不需要安装脚本 |

如果技术支持提供的是下载链接，在能联网的电脑上打开链接下载即可。随后通过组织允许的
移动介质或文件传输方式，将文件复制到目标电脑。Linux/macOS 的 `install.sh` 和安装包
应放在同一目录。

### 第二步：在目标电脑上安装

#### Linux / macOS

进入存放 `install.sh` 和安装包的目录。以下命令中的 `<安装包文件名>` 替换为实际文件名，
包括扩展名：

```bash
bash install.sh --from ./<安装包文件名>
```

例如，发布方提供的文件为 `.tar.gz` 时，命令末尾就是该文件的完整名称。安装完成后，
按 §1 的“验证安装”操作。

#### Windows

1. 解压 ZIP 安装包。
2. 在解压目录打开 PowerShell。
3. 执行：

```powershell
.\aixcoding-cli.exe install
```

安装完成后，重新打开 PowerShell，按 §1 的“验证安装”操作。

## 3. 只解压直接运行（不安装）

如果只需临时使用，可以不安装命令。

Linux / macOS，在解压目录打开终端：

```bash
chmod +x ./aixcoding-cli
./aixcoding-cli
```

Windows，在解压目录打开 PowerShell：

```powershell
.\aixcoding-cli.exe
```

macOS 如果提示文件被隔离，或任何平台提示包与系统/架构不匹配，请停止操作并联系技术
支持，不要尝试运行来源不明的文件。

## 4. 升级

使用同一安装方式重新安装即可，**不需要先卸载**。已有的配置和会话会保留：

- 在线安装：重新执行 §1 的命令。
- 离线安装：取得新版安装文件后，重新执行 §2 的命令。

## 5. 卸载

### 5.1 普通卸载（保留配置和会话）

打开终端或 PowerShell，执行：

```bash
aixcoding-cli uninstall
```

按提示确认后，程序会被删除，配置、登录信息、模型配置和会话等用户数据会保留，便于
以后重新安装并继续使用。

### 5.2 同时删除程序和用户数据

> **警告：以下操作会同时删除配置、登录信息、模型配置和会话等数据。
> 执行前请备份需要保留的内容。**

确认不再需要这些数据后，执行：

```bash
aixcoding-cli uninstall --purge
```

下载的安装脚本、压缩包和解压目录不会被一律自动删除；确认不再需要后，可手工删除。

### 5.3 只解压直接运行后的清理

如果没有执行过安装，直接删除解压目录即可。

如果已经运行过程序，希望同时清理它产生的配置或会话，请先按 §5.1 或 §5.2 在解压目录
执行卸载，再删除解压目录：

```bash
# Linux / macOS
./aixcoding-cli uninstall

# Windows PowerShell
.\aixcoding-cli.exe uninstall
```

## 6. 遇到问题

- **提示找不到 `aixcoding-cli`**：先关闭并重新打开终端或 PowerShell；仍无法使用时，
  联系技术支持。
- **提示命令冲突、安装失败或卸载失败**：不要反复尝试其他命令，保留完整提示信息并联系
  技术支持。
- **无法下载或提示网络错误**：确认内网连接；仍失败时，将错误信息提供给技术支持。
- **拿到的是旧版安装包**：不要直接套用本手册的新脚本安装，联系技术支持获取当前包。
- **需要指定版本、使用测试版本、修改下载地址或彻底清理 PATH 等残留**：由技术支持
  按内部详细手册处理。
