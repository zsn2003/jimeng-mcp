"""Run both adapter and local-transport tests without contacting upstream."""
import os
from pathlib import Path
import subprocess
import sys

root = Path(__file__).resolve().parents[1]
env = dict(os.environ, PYTHONPATH=os.pathsep.join((str(root/'src'), str(root/'src/jimeng_mcp/_bridge'))))
for folder in ('tests/bridge', 'tests'):
    result = subprocess.run([sys.executable, '-W', 'error::ResourceWarning', '-m', 'unittest', 'discover', '-s', folder, '-p', 'test_*.py'], cwd=root, env=env, stdin=subprocess.DEVNULL)
    if result.returncode: raise SystemExit(result.returncode)
