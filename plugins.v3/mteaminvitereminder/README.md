# 馒头下级登录提醒（MoviePilot V3，开发预览版）

读取 MoviePilot 站点管理中的馒头 API Key，定期检查**直接被邀账号**，通过 MoviePilot 已配置通知渠道将名单汇总发送给邀请人。不向下级发邮件，不自动登录，不代下级保活，不读取浏览器 Cookie。

## 当前验证状态

版本 `0.1.1`。38 项离线单元测试通过，并在已有 MoviePilot V3.0.10-1 的容器环境完成真实 API 读取验证。

- 使用 `/invite/getUserInviteHistory`，真实响应为包含 `uid` 的账号列表；`getUserInviteInfo` 返回邀请额度，不能用作下级名单。
- 已验证本人资料、直接下级 `parentId`、等级、封存状态与 `memberStatus.lastBrowse` 可通过现有站点 Key 查询。
- 只读站点 API，不修改站点账号。未知结构、疑似分页、资料缺失均明确标记。
- MP 运行时加载、配置页及通知送达应在安装后验证；实际通知渠道尚未配置时请保留推送关闭。

## 安装与试运行

插件目录：`plugins.v3/mteaminvitereminder/`，插件 ID：`MTeamInviteReminder`。

第三方市场仓库为 `https://github.com/bctjo/MoviePilot-Plugins`，可加入 MP 的 `PLUGIN_MARKET`（多个仓库用英文逗号分隔）。市场读取 `main` 分支以及 `package.v3.json`。

已有 MP V3 可使用 `PLUGIN_LOCAL_REPO_PATHS` 同步本地插件仓库联调，修改后按宿主支持方式重载。不要直接用这个目录覆盖整个 MP 配置目录。

1. 在 MP 站点管理中确认馒头 API Key 已配置，无需把 Key 填入插件。
2. 启用插件，保留“通过 MP 渠道推送给我”为关闭；勾选“保存后立即检查一次”。
3. 确认详情页是否取得完整名单，并与浏览器邀请页逐项核对数量和 UID。
4. 验证下级的等级、`parentId`、`parked`、`enabled`、`memberStatus.lastBrowse`，尤其封存和隐私设置。
5. 自动名单接口未适配时，可临时选择 `manual`，填入直接下级 UID。每一个仍需要资料接口的 `parentId` 与本人 UID 匹配，不能绕过权限。
6. 确认数据和提醒范围后启用 MP 推送；通知渠道需允许“站点”类型。

## 判断和通知

- 默认北京时间每天 09:00 检查；可配置五位 cron，时区固定为 `Asia/Shanghai`。
- 普通等级 `1`～`6` 且 **完整时间差 >30 天**才提醒；恰好 30 天不提醒，不按自然日粗略取整。
- `role >=7` 跳过（包括高级及特殊等级，保守处理）。
- 未封存账号适用 40 天期限；低于 Veteran User 的封存账号按 90 天；Veteran User 封存后跳过。
- 已禁用或已达到期限的账号保留详情供人工核实，不再作为提前提醒。
- `lastBrowse` 缺失时不退回 `lastLogin`，因为凭据登录时间不能等价代表最后浏览。
- 不考虑未消费余额带来的“禁用代替删除”差异，通知只称“保留期限”，不承诺某日一定删号。
- 每次未登录周期按“本人 UID + 下级 UID + 最后浏览时间”抑制重复通知；下级重新访问后，未来再次超过阈值可重新提醒。
- 通知每批最多 15 人，不截断后将未展示账号误标为已通知。每批提交后保存状态。
- 宿主 `post_message` 不返回渠道送达回执，页面只显示“已提交 MP 通知渠道”；提交后数据库写入失败或进程崩溃仍可能导致下次重复，不提供严格的恰好一次投递。
- 无法判断的账号也会汇总通知，避免隐私限制或接口异常长期被忽略。
- 默认请求间隔 2 秒，最低 1 秒；不开并发扫描。未验证为官方速率保证，实际限流时应降低频率。

## 开发和测试环境

不需要在开发电脑安装 Docker 或启动完整 MP。纯逻辑和 HTTP/通知边界可离线测试；真实导入、加载、配置页、任务重载和通知渠道必须在已有 MP V3 验证。

采用当前 V3 稳定 `app.sdk` 与公开 `app.db.oper.site.SiteOper`；早期 V3 若缺少这些入口，需先核实具体版本。本预览版未声称兼容所有历史 V3 小版本。

插件不增加运行时第三方依赖，复用宿主 APScheduler；其余使用 Python 标准库。测试使用 pytest。正式宿主测试按官方插件仓 `tests/README.md`，用匹配 MP 的后端环境运行：

```sh
MOVIEPILOT_BACKEND_PATH=/path/to/MoviePilot /path/to/MoviePilot/.venv/bin/python -m pytest tests/v3/mteaminvitereminder
```

离线替身测试与真实宿主测试不是同一项验证；离线通过不能替代真实 MP 加载。联调优先保留关闭推送，只提供去除邮箱、IP、Key 的结构样例，不提交实际站点响应。

参考：[官方 V3 开发指南](https://github.com/jxxghp/MoviePilot-Plugins/blob/main/docs/Plugin_Development.md)、[API 规则](https://wiki.m-team.cc/zh-tw/api)、[账号规则](https://wiki.m-team.cc/zh-tw/account-rules)、[本人保活提醒参考插件](https://github.com/andyxu8023/MoviePilot-Plugins/tree/main/plugins.v3/mteamkeepalive)。
