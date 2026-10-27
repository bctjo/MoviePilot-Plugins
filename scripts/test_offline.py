"""运行不依赖完整 MoviePilot 的离线边界测试，不代表宿主集成验证。"""
import os
from pathlib import Path
import subprocess
import sys


def main():
    """用当前 Python 的 pytest 和最小宿主替身执行插件测试。"""
    root = Path(__file__).resolve().parents[1]
    failed = False
    for plugin in sorted((root / 'plugins.v3').iterdir()):
        target = 'tests/v3/' + plugin.name
        if not plugin.is_dir() or not (root / target).is_dir():
            continue
        env = dict(os.environ)
        env['OFFLINE_PLUGIN_PATH'] = str(plugin)
        env['PYTHONPATH'] = str(root / 'tests') + os.pathsep + env.get('PYTHONPATH', '')
        print('Offline checks: ' + plugin.name, flush=True)
        code = subprocess.call([sys.executable, '-m', 'pytest', '-p', 'offline_host',
                                '--confcutdir=' + target, target, '-q'], cwd=root, env=env)
        failed = failed or code != 0
    return int(failed)


if __name__ == '__main__':
    raise SystemExit(main())
