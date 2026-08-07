# MCP 工具与个人复盘闭环

状态：local-ready-for-agent

## Problem

ChatGPT 需要结构化读取日报并记录个人观察，但不能改变排名、触发交易或获得账户信息。

## Solution

以 MCP Streamable HTTP 暴露带输入/输出 Schema 和准确安全注解的只读、追加写入工具。
所有工具只调用应用服务；机器事实以结构化结果返回。

## User Stories

- 可读取日报、候选、次日确认、历史和策略版本。
- 可维护命名观察列表，记录候选事件和复盘说明。
- 次日行情只有显式调用时获取，来源及时间随响应返回。
- 策略激活必须携带明确确认，不允许历史重写。

## Implementation Decisions

- 使用 Python MCP SDK v2 的 `MCPServer` 和 Streamable HTTP。
- HTTP 只监听 `127.0.0.1:8765/mcp`。
- 写操作采用幂等键或追加事件，删除操作标注 destructive。
- 不接受余额、仓位、券商凭证、订单或任意 URL/shell 参数。

## Testing Decisions

- 断言公开 Schema、annotations、结构化错误、幂等和输入边界。
- 使用 fake 应用服务证明读取不改写排名、次日检查不会后台轮询。

## Out of Scope

公共插件、多用户授权、下单、持仓、推送和持续盯盘。
