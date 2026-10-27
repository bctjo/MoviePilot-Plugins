"""仅供离线边界测试的最小宿主替身，不属于运行时或可发布插件。"""
import importlib.util
import os
import sys
import types
from unittest.mock import Mock


def stub(name, **attrs):
    mod = types.ModuleType(name)
    mod.__path__ = []
    mod.__dict__.update(attrs)
    sys.modules[name] = mod
    if '.' in name:
        parent, key = name.rsplit('.', 1)
        setattr(sys.modules[parent], key, mod)
    return mod


for name in ('app', 'app.sdk', 'app.db', 'app.db.oper', 'app.schemas', 'app.plugins'):
    stub(name)
stub('app.sdk.config', settings=types.SimpleNamespace(PROXY=None))
stub('app.sdk.logging', logger=Mock())
stub('app.sdk.network', RequestUtils=Mock())
stub('app.sdk.plugin', _PluginBase=type('_PluginBase', (), {}))
stub('app.sdk.scheduler', add_plugin_once_job=Mock(return_value=True), remove_plugin_once_job=Mock())
stub('app.db.oper.site', SiteOper=Mock())
stub('app.schemas.types', MessageType=types.SimpleNamespace(SiteMessage='site'))
path = os.environ['OFFLINE_PLUGIN_PATH']
plugin_id = os.path.basename(path)
spec = importlib.util.spec_from_file_location('app.plugins.' + plugin_id, path + '/__init__.py', submodule_search_locations=[path])
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
setattr(sys.modules['app.plugins'], plugin_id, mod)
spec.loader.exec_module(mod)
