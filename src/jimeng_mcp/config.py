"""Private local state and explicit spending settings; no shared credentials."""
from __future__ import annotations
import json
import os
from pathlib import Path
import stat
import sys
import tempfile

DEFAULTS = {'version': 1, 'read_only': False, 'max_credits': 0, 'video_policy': 'general'}


def default_data_dir() -> Path:
    if os.environ.get('JIMENG_MCP_DATA_DIR'):
        return Path(os.environ['JIMENG_MCP_DATA_DIR']).expanduser().absolute()
    if sys.platform == 'darwin':
        return Path.home() / 'Library' / 'Application Support' / 'jimeng-mcp'
    return Path(os.environ.get('XDG_DATA_HOME', str(Path.home() / '.local' / 'share'))) / 'jimeng-mcp'


def ensure_private_dir(path: Path) -> Path:
    path = path.expanduser().absolute()
    if path.is_symlink():
        raise ValueError('The private data directory must not be a symbolic link')
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError('The data directory must be owned by you with mode 0700; choose a new private directory')
    return path.resolve()


def read_settings(root: Path) -> dict:
    filename = root / 'settings.json'
    if not filename.exists():
        return dict(DEFAULTS)
    if filename.is_symlink():
        raise ValueError('Settings must not be a symbolic link')
    info = filename.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077 or info.st_size > 16384:
        raise ValueError('Settings must be an owned private regular file')
    values = json.loads(filename.read_text(encoding='utf-8'))
    if not isinstance(values, dict) or set(values) != set(DEFAULTS) or values['version'] != 1:
        raise ValueError('Unsupported settings format')
    if type(values['read_only']) is not bool or values['video_policy'] not in ('general', 'sample_first'):
        raise ValueError('Invalid local policy')
    cap = values['max_credits']
    if cap != 'unlimited' and (type(cap) is not int or not 0 <= cap <= 100000):
        raise ValueError('max_credits must be an integer from 0 to 100000 or unlimited')
    return values


def save_settings(root: Path, values: dict) -> None:
    # Validate before replacing the existing policy.
    if set(values) != set(DEFAULTS) or type(values.get('read_only')) is not bool:
        raise ValueError('Invalid settings')
    cap = values.get('max_credits')
    if cap != 'unlimited' and (type(cap) is not int or not 0 <= cap <= 100000):
        raise ValueError('Invalid credit limit')
    if values.get('video_policy') not in ('general', 'sample_first') or values.get('version') != 1:
        raise ValueError('Invalid video policy')
    fd, filename = tempfile.mkstemp(prefix='.settings-', dir=root)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(values, stream, ensure_ascii=False, indent=2)
            stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
        os.replace(filename, root / 'settings.json')
        descriptor = os.open(root, os.O_RDONLY)
        try: os.fsync(descriptor)
        finally: os.close(descriptor)
    finally:
        Path(filename).unlink(missing_ok=True)


def configure_environment(directory=None) -> Path:
    if os.name != 'posix':
        raise ValueError('Native Windows is not supported yet. Run jimeng-mcp inside WSL2 (Ubuntu), or use macOS/Linux.')
    root = ensure_private_dir(Path(directory) if directory else default_data_dir())
    values = read_settings(root)
    os.environ['JIMENG_MCP_DATA_DIR'] = str(root)
    paths = {'DREAMINA_CANVAS_STATE_DIR': 'canvas', 'JIMENG_RESOURCE_STATE_DIR': 'resources',
             'JIMENG_DIRECTOR_STATE_DIR': 'director', 'JIMENG_POLICY_STATE_DIR': 'policy'}
    for key, name in paths.items():
        os.environ[key] = str(ensure_private_dir(root / name))
    os.environ['JIMENG_WEB_SESSION_FILE'] = str(root / 'web-session.json')
    os.environ['DREAMINA_CANVAS_PROFILE'] = 'jimeng-mcp'
    os.environ['JIMENG_READ_ONLY'] = '1' if values['read_only'] else '0'
    os.environ['JIMENG_MAX_CREDITS'] = str(values['max_credits'])
    os.environ['JIMENG_VIDEO_WORKFLOW_POLICY'] = values['video_policy']
    os.environ.pop('JIMENG_PROJECT_POLICY_FILE', None)
    os.environ['JIMENG_MCP_PUBLIC_PATH'] = '/mcp'
    bin_dir = ensure_private_dir(root / 'bin')
    for key, name in (('DREAMINA_CANVAS_BIN', 'dreamina-canvas'), ('DREAMINA_PLUGIN_BIN', 'dreamina')):
        candidate = bin_dir / name
        if key not in os.environ and candidate.is_file() and not candidate.is_symlink():
            os.environ[key] = str(candidate)
    return root
