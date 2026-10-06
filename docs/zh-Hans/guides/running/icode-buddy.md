# 使用本地 Buddy JSON 接口

`icode buddy --json` 供本地前端读取和更新 TUI 使用的同一份 Buddy 存档。每次调用从标准输入读取一个 UTF-8 JSON 请求，读到 EOF 后向标准输出写入一个 JSON 响应。请求最大为 64 KiB，不调用模型。

每个请求必须包含 `"protocol": "chrys-buddy-v1"` 和下表中的 `action`。只有 `rename` 接受额外的 `name` 字段；其他字段和协议版本均被拒绝。

| 操作 | 结果 |
| --- | --- |
| `status` | 返回已保存的 Buddy，首次使用时返回 `null` |
| `catalog` | 返回所有物种的内置素材和动画节奏 |
| `hatch` | 没有 Buddy 时孵化，否则返回当前 Buddy |
| `pet` | 将存档中的抚摸次数加一；需要已孵化且未静音的 Buddy |
| `rename` | 用非空文本 `name` 字段重命名；需要已孵化的 Buddy |

例如，在 macOS 或 Linux 上：

```sh
printf '%s' '{"protocol":"chrys-buddy-v1","action":"status"}' | icode buddy --json
printf '%s' '{"protocol":"chrys-buddy-v1","action":"rename","name":"栗子"}' | icode buddy --json
```

除 `catalog` 外，成功响应包含 `protocol` 和 `buddy`。非空的 `buddy` 包含存档 `record`，以及计算出的 `level`、`rarity`、`shiny`、`traits` 和 `progress`。重命名沿用 TUI 的名称清理规则和 24 字符限制。

素材响应包含 `protocol` 和 `artwork`。每个物种条目包含 `species`、`width`、`height`、`frames`、`restTicks` 和 `holdTicks`。九个 20×16 帧依次为三个待机姿势、三个抚摸姿势和三个待机眨眼姿势。每帧是按行排列的 1,280 个 RGBA 字节组成的数组。素材来自 iCode 打包的精灵文件，不包含用户的自定义 PNG 覆盖。

接口使用 TUI 的签名存档和同一写入锁：macOS/Linux 为 `~/.chrys/extras/buddy/buddy.json`，Windows 为 `%APPDATA%\chrys\extras\buddy\buddy.json`。接口只读取主存档，不写入备份。存档损坏或不可读，以及主存档缺失但备份仍存在时，接口明确失败，不从备份恢复，也不覆盖损坏内容。请求或存储错误的响应包含 `protocol` 和 `error`，命令退出状态为 1；诊断输出写入标准错误。
