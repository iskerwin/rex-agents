# rex-agents

Rex 的侧边面板：Claude Code、Codex、Grok 还剩多少额度，当前标签页里 Claude Code 会话的 advisor，今日和本月费用，以及运行中的任务。

```bash
uv tool install .            # 装到 PATH，Python 代码改动后加 --force --reinstall 重新运行
uv run --group dev pytest
```

数据来自 [agents-usage](https://github.com/iskerwin/agents-usage) 的 `agents-usage --json --claude-live`，要先装好它。面板每 60 秒刷新一次；开着几个面板时，agents-usage 在同一分钟内只查询一次配额。只依赖 Python 标准库。

## 加载

在 Rex 的配置（`~/.config/rex/init.lua`）里：

```lua
dofile("/path/to/rex-agents/rex/init.lua").setup({ bin = { "/path/to/rex-agents" }, width = 1/3 })
```

按 shift+cmd+u，或在命令面板里运行 “Toggle Agents Panel”，在当前标签页右侧打开或关闭面板，和标签页里跑的是什么无关。`width` 是面板占标签页宽度的比例（默认 1/3，可以设成 1/4），`key` 换快捷键，`key = false` 不绑定。面板显示本机的用量，所以只用于本机的会话。

## 面板

每个 agent 一个框，每个额度窗口一行：剩余量的长条、剩余百分比和距重置的时间，长条上亮的竖线是匀速用量此刻应剩的位置，剩余 ≤40% 标 `!`，≤20% 标 `!!`。下面是费用（框底标着数据是多久前的），最后是运行中的任务，一直排到面板底部。面板矮时每个 agent 收成一行。

同一个标签页里有开着 advisor 的 Claude Code 会话时，Claude 框下面多一个 Advisor 框：主模型和 advisor、主模型在不在跑、最近一次调用，以及各个时机（定方案前、同一个错误两次、收尾前、中途确认）的调用次数。这些来自那个会话自己的日志（`~/.claude/projects/*/<会话>.jsonl`），时机的判断方式和 [advisor-live](https://github.com/iskerwin/advisor-live) 相同，不需要装它。

## 隐私

Advisor 框只读取 Claude Code 日志里的 advisor 调用、模型名、工具名和工具是否失败，不读消息内容；失败结果只读前 80 个字符，用来判断是不是同一个错误，只留在内存里。

## 许可证

[MIT](LICENSE)
