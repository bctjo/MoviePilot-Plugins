# bctjo 的 MoviePilot 插件

面向 **MoviePilot V3** 的个人插件仓库，仅发布本人开发或维护的插件。目前包含：

| 插件 | 版本 | 用途 |
| --- | --- | --- |
| [馒头下级登录提醒](plugins.v3/mteaminvitereminder/README.md) | 0.1.2 | 检查馒头直接下级的等级和最后浏览时间，通过 MP 通知渠道汇总提醒邀请人 |

## 安装

在 MP 的插件市场仓库设置中添加以下地址，保留原有官方仓库：

```text
https://github.com/bctjo/MoviePilot-Plugins
```

刷新插件市场，搜索「馒头下级登录提醒」并安装。仅适配 V3，V1/V2 索引为空。无需在插件中填写 API Key，自动读取 MP 站点管理中已有的馒头配置。

首次运行开启插件、勾选立即检查，建议先关闭推送并核对结果。配置好 MP 通知渠道后，可开启推送。默认北京时间每天 09:00 检查，未浏览超过完整 30 天且等级低于 Extreme User 的直接下级进入判断；封存、豁免和已到保留期限等情况按[详细说明](plugins.v3/mteaminvitereminder/README.md)处理。

本插件不自动登录，不代下级保活，不向下级发送邮件，不收集或上传站点凭据。通知汇总发送给 MP 管理员，由管理员提醒相关下级。

## 验证情况

- 39 项离线测试通过；HTTP、数据库和通知使用替身，不依赖真实账号。
- 已在 MoviePilot V3.0.10-1 完成真实插件加载、分栏配置页、日志和结果表格验证。
- 真实后台扫描 19/19 成功，扫描中再次保存启用配置后仍完整结束。
- 外部通知渠道的实际送达仍需用户按自己配置验证，插件只能确认已提交 MP 通知渠道。

## 反馈与开发

问题反馈请使用本仓库 [Issues](https://github.com/bctjo/MoviePilot-Plugins/issues)，提供插件版本、MP 版本及去除敏感信息后的日志。不要提交 API Key、密码、Cookie 或完整站点响应。

[开发与测试说明](docs/DEVELOPMENT.md)。本仓库基于 [MoviePilot 官方插件仓库](https://github.com/jxxghp/MoviePilot-Plugins) fork，沿用 GPL-3.0 许可证及部分通用校验/测试引导工具；发布分支已清理其他作者的插件代码和市场定义，不自动同步上游插件全集。

官方 Wiki 仓库收录申请正在准备中，尚未声明已被收录。
