"""Load the isolated, bundled operation adapters in the MCP subprocess."""
import importlib
from pathlib import Path
import sys

BRIDGE_ROOT = Path(__file__).resolve().parent / '_bridge'


def load_bridge():
    location = str(BRIDGE_ROOT)
    if location not in sys.path:
        sys.path.insert(0, location)
    names = ('adapter', 'server', 'mcp_server')
    for name in (p.stem for p in BRIDGE_ROOT.glob('*.py') if p.stem != '__init__'):
        existing = sys.modules.get(name)
        if existing and Path(getattr(existing, '__file__', '')).resolve().parent != BRIDGE_ROOT:
            raise RuntimeError('A conflicting module is already loaded; launch jimeng-mcp as its own process')
    return tuple(importlib.import_module(name) for name in names)
