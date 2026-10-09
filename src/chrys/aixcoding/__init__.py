# ruff: noqa: RUF002
"""iCode 二开定制包（aixcoding）。

与 telemetry 无关的通用基础设施（平台连接配置、统一 HTTP 出口、git 信息、
运行上下文）位于本包顶层，供 telemetry 与将来登录等其他改造共用；数据上报
专属代码收敛在 ``aixcoding.telemetry`` 子包。设计文档见仓库根 ``aixcoding/docs/``
（不进 wheel）。分层约束：只 import ``kernel``/``foundation`` + 标准库/既有三方依赖。
"""
