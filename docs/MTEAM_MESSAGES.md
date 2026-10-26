# 馒头站内信发送接口

2026-10-26 核对了官方 [OpenAPI 定义](https://test2.m-team.cc/api/v3/api-docs)、[Swagger UI](https://test2.m-team.cc/api/swagger-ui.html) 和生产站点的[前端 API 调用代码](https://static.m-team.cc/static/js/main.e715ce06.js)。官方定义明确包含发送功能，并非仅有消息读取接口。

## 发送方式

插件使用 `POST https://api.m-team.cc/api/msg/send`，通过 `x-api-key` 请求头传递 MP 站点管理中配置的邀请人 API Key。官方 [API 规则](https://wiki.m-team.cc/zh-tw/api) 要求第三方工具使用 API Access Token；插件不使用浏览器 Cookie 或账号密码。

官方 `MessageForm` 定义的三个必填字段如下：

| 字段 | 类型 | 用途 |
| --- | --- | --- |
| `to` | integer / int64 | 收件人 UID |
| `title` | string | 非空标题 |
| `content` | string | 非空正文 |

可选字段 `options` 为字符串数组，包含 `saveMsg`（保存发件记录）及 `delOri`。插件仅使用 `saveMsg`。

官方前端 `/msg/send` 调用经过默认表单转换器；插件使用表单字段提交，不把整个 `MessageForm` 当作 JSON 正文或名为 `form` 的单个参数。官方 Wiki 也提示 Swagger 的 JSON/form、参数位置和必填标记可能与实际行为不一致。

## 插件行为与验证范围

“通过馒头站内信提醒下级”默认关闭，可与 MP 汇总推送分别启用。站内信只发给本次已确认符合提醒条件的直接下级，标题为“馒头账号登录提醒”，正文说明未浏览天数和浏览器访问要求。每次检查仍符合条件就再次发送。

每次发送前等待 2 秒；不自动重试超时请求。单个收件人失败不会中断其余账号；详情显示每个账号的发送状态，并汇总成功与失败人数。任务停止时保留此前已发送的结果。

离线测试覆盖表单、API Key、重复提醒、收件人筛选、单人失败后继续发送、MP 汇总和中断状态。尚未使用真实账号发送测试信，因此生产环境 API Key 权限、表单实际兼容性及送达仍需真实宿主确认。官方 `Member.acceptpms` 包含 `yes`、`no`、`friends`，收件设置可能影响发送；接口成功也不等于收件人已阅读。
