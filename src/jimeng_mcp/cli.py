"""Local terminal setup and stdio server entry point."""
from __future__ import annotations
import argparse
import json
import logging
import os
import sys
from . import __version__
from .config import configure_environment, read_settings, save_settings


def parser():
    result = argparse.ArgumentParser(prog='jimeng-mcp', description='即梦 MCP：你负责脑洞，Agent 负责开工。')
    result.add_argument('--version', action='version', version=__version__)
    result.add_argument('--data-dir', help='Private local directory; defaults to this OS user\'s application data directory')
    commands = result.add_subparsers(dest='command')
    commands.add_parser('serve', help='Run MCP over stdio (also the default)')
    config = commands.add_parser('config', help='View or change local write and generation policy')
    group = config.add_mutually_exclusive_group()
    group.add_argument('--read-only', action='store_true')
    group.add_argument('--allow-writes', action='store_true')
    config.add_argument('--max-credits', help='Nonnegative per-operation cap, or unlimited; default 0 blocks generation')
    config.add_argument('--video-policy', choices=('general', 'sample_first'))
    commands.add_parser('client-config', help='Print a generic MCP client JSON configuration; no tokens included')
    from . import onboarding
    onboarding.configure_parser(commands)
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    try:
        root = configure_environment(args.data_dir)
        if args.command in (None, 'serve'):
            from .runtime import serve
            return serve(root)
        if args.command == 'config':
            values = read_settings(root)
            changed = args.read_only or args.allow_writes or args.max_credits is not None or args.video_policy is not None
            if args.read_only: values['read_only'] = True
            if args.allow_writes: values['read_only'] = False
            if args.max_credits is not None:
                values['max_credits'] = 'unlimited' if args.max_credits == 'unlimited' else int(args.max_credits)
            if args.video_policy is not None: values['video_policy'] = args.video_policy
            if changed: save_settings(root, values)
            print(json.dumps({'settings': values, 'restartMcpAfterChanges': changed,
                'note': 'Credit limits apply per supported operation, not an aggregate monthly budget. Some web/plugin operations require unlimited.'}, ensure_ascii=False, indent=2))
            return 0
        if args.command == 'client-config':
            print(json.dumps({'mcpServers': {'jimeng': {'command': sys.executable,
                'args': ['-m', 'jimeng_mcp', '--data-dir', str(root), 'serve']}}}, ensure_ascii=False, indent=2))
            return 0
        from . import onboarding
        return onboarding.run(args)
    except KeyboardInterrupt:
        print('Cancelled.', file=sys.stderr)
        return 130
    except (ValueError, OSError, RuntimeError) as exc:
        # Only local setup diagnostics. Upstream payloads/credentials are never
        # interpolated into the protocol output or logged by this entry point.
        print('jimeng-mcp: ' + str(exc), file=sys.stderr)
        return 2
