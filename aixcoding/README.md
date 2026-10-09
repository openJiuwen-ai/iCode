# aixcoding

AIxCoding 二开定制专区。此目录为**不进 wheel 发布包**的定制内容（wheel 仅打包 `src/chrys`，另有 `docs/` 用户指南经 force-include 随包分发，与本目录无关）。

- `docs/`：内部设计文档，纯中文、无双语义务，按功能分子目录（如 `telemetry/` 数据上报）
- `telemetry-mock/`：数据上报 mock server（Python 标准库零依赖，仅供开发调试；M1 时创建）

进包的定制源码在 `src/chrys/aixcoding/`，定制测试在 `tests/aixcoding/`。
