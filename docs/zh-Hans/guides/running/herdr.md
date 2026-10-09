# 在 Herdr 里使用 iCode

[Herdr](https://herdr.dev) 是一个为编码 agent 设计的终端多路复用器:它把 agent 会话排布在各个 pane 里,监视它们的状态,并在需要你时通知你。在 Herdr pane 里运行 `icode` 时,iCode 会自动把自己的状态告诉 Herdr,双方都不需要任何开启或配置操作。

## 你会看到什么

TUI 在 Herdr pane 里启动后:

- Herdr 侧栏把该 pane 列为 agent **icode**,`herdr agent list` 也会显示它及其状态。
- 状态跟随你的会话:
  - **working** —— 回合运行中,例如你刚提交了一个提示词。
  - **blocked** —— iCode 在等你:某个工具等待审批,或 agent 向你提了一个问题。
  - **idle** —— 回合已结束,包括你用 Ctrl+C 中断的情况。
- 当 pane 位于后台标签页时,回合结束或需要你决策的那一刻,Herdr 可以发送系统通知并播放提示音。通知和声音的设置在 Herdr 侧。
- Herdr 中等待 agent 状态的命令,例如 `herdr agent wait`,对该 pane 有效。

## Herdr 重启后恢复会话

会话打开期间,iCode 还会告诉 Herdr 该 pane 正在运行哪个会话。Herdr 服务器重启后,pane 会自动执行 `icode --session <会话id>`,带你回到原来的对话。

> **Note**
>
> 会话恢复需要 Herdr 0.9.2 或更高版本。旧版本下状态上报仍然有效,只是自动恢复不可用。

## 在 Herdr 之外

该集成在 Herdr 之外完全不起作用:没有 Herdr 的环境变量,iCode 不会启动任何额外进程,行为与在任何其他终端中完全一致。
