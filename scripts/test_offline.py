"""运行不依赖完整 MoviePilot 的离线边界测试，不代表宿主集成验证。"""
import os
from pathlib import Path
import subprocess
import sys


def main():
    """用当前 Python 的 pytest 和最小宿主替身执行插件测试。"""
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env['OFFLINE_PLUGIN_PATH'] = str(root / 'plugins.v3/mteaminvitereminder')
    env['PYTHONPATH'] = str(root / 'tests') + os.pathsep + env.get('PYTHONPATH', '')
    target = 'tests/v3/mteaminvitereminder'
    return subprocess.call([sys.executable, '-m', 'pytest', '-p', 'offline_host',
                            '--confcutdir=' + target, target, '-q'], cwd=root, env=env)


if __name__ == '__main__':
    raise SystemExit(main())
