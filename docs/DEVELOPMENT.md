# 开发与测试

插件代码位于 `plugins.v3/mteaminvitereminder/`，测试位于 `tests/v3/mteaminvitereminder/`。市场只读取 `main` 分支及 `package.v3.json`。当前按源码方式发布，无需 GitHub Release 资产。

## 离线测试

在已有 Python 虚拟环境中安装 `pytest` 和 `apscheduler>=3.10,<4` 后运行：

```sh
python scripts/test_offline.py
python .github/scripts/check_plugin_versions.py
python -m compileall -q plugins.v3
```

`tests/offline_host.py` 仅为最小宿主替身，不属于插件运行时。离线测试用于规则、生命周期、请求和通知边界；不能替代真实 MP 加载或渠道送达测试。CI 运行同样的离线检查，不访问馒头或真实 MP，也不需要用户凭据。

## 真实宿主测试

沿用官方共享测试引导，需要匹配版本 MoviePilot 后端及其完整测试依赖：

```sh
MOVIEPILOT_BACKEND_PATH=/path/to/MoviePilot /path/to/MoviePilot/.venv/bin/python -m pytest tests/v3/mteaminvitereminder
MOVIEPILOT_BACKEND_PATH=/path/to/MoviePilot python scripts/check_v3_imports.py
```

日常开发不要求在本地安装 Docker。真实加载和页面验证可在已有 MP V3 中完成，联调时避免发送通知；发布前核对插件详情、逐步日志和完整人数。

## 维护仓库

只增加本人开发或维护的插件，每个插件对应独立目录、测试和市场定义；不得直接合并上游的全部插件。目录名为主类名的小写，代码版本、市场版本和最新历史条目保持一致。更新插件代码时递增版本，避免安装了相同版本号但内容不同的情况。

参考：[官方开发指南](https://github.com/jxxghp/MoviePilot-Plugins/blob/main/docs/Plugin_Development.md)、[官方发布指南](https://github.com/jxxghp/MoviePilot-Plugins/blob/main/docs/Repository_Guide.md)。通用测试引导及校验工具来源于官方仓库，遵守仓库 GPL-3.0 许可证。
