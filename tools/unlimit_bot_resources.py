#!/usr/bin/env python3
"""Emergency *temporary* cgroup-v2 relief, without restarting a running bot.

Run on the Docker host. Default: inspect only. --apply requires root and writes
only the identified container's cgroup. Docker HostConfig is NOT updated: use
the uncapped Compose definition at the next SAFE recreation for persistence.
No OOM killer disabling, process signalling, Docker update/restart or DB writes.
"""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys


class UnsafeTarget(RuntimeError):
    pass


def inspect_container(name):
    result = subprocess.run(
        ['docker', 'inspect', '--type', 'container', '--format',
         '{"Id":{{json .Id}},"State":{{json .State}}}', name], capture_output=True, text=True, check=True, timeout=15)
    # Request identity/state only, never Config.Env or other credentials.
    record = json.loads(result.stdout)
    state = record.get('State', {})
    cid, pid = record.get('Id', ''), state.get('Pid', 0)
    if (not re.fullmatch(r'[0-9a-f]{64}', cid) or not isinstance(pid, int)
            or pid <= 1 or not state.get('Running') or state.get('Restarting')
            or state.get('Paused')):
        raise UnsafeTarget('Container must be running, unpaused and not restarting.')
    return cid, pid, state.get('StartedAt')


def locate_cgroup(cid, membership, root=Path('/sys/fs/cgroup')):
    """Fail closed on v1, host root, non-Docker groups or path traversal."""
    if not re.fullmatch(r'[0-9a-f]{64}', cid):
        raise UnsafeTarget('Invalid container ID.')
    paths = [line[3:] for line in membership.splitlines() if line.startswith('0::')]
    if len(paths) != 1:
        raise UnsafeTarget('Unified cgroup v2 required; no changes made.')
    path = Path(paths[0])
    if not path.is_absolute() or '..' in path.parts:
        raise UnsafeTarget('Unexpected cgroup path.')
    if path.name not in (cid, f'docker-{cid}.scope'):
        raise UnsafeTarget('Cgroup leaf does not match the full Docker container ID.')
    base = root.resolve()
    target = (base / str(path).lstrip('/')).resolve()
    if not target.is_relative_to(base) or target == base:
        raise UnsafeTarget('Refusing to modify the host/ancestor cgroup.')
    if not (base / 'cgroup.controllers').is_file():
        raise UnsafeTarget('Expected host cgroup-v2 mount not found.')
    return target


def planned_changes(target):
    # Validate all required controls before writing any of them.
    for key in ('memory.max', 'cpu.max'):
        if not (target / key).is_file():
            raise UnsafeTarget(f'{key} unavailable; no changes made.')
    cpu = (target / 'cpu.max').read_text().split()
    if len(cpu) != 2 or not cpu[1].isdigit() or int(cpu[1]) <= 0:
        raise UnsafeTarget('Unexpected cpu.max format.')
    desired = [('memory.max', 'max'), ('cpu.max', f'max {cpu[1]}')]
    # Also remove container soft throttle/swap ceilings if those controls exist.
    for key in ('memory.high', 'memory.swap.max'):
        if (target / key).is_file():
            desired.append((key, 'max'))
    return [(target / key, value) for key, value in desired]


def change_limits(changes, *, apply=False, guard=lambda: None):
    for path, value in changes:
        before = path.read_text().strip()
        print(f'{path.name}: {before} -> {value}')
        if apply and before != value:
            guard()  # Ensure Docker has not restarted/replaced the process.
            path.write_text(value + '\n')
            if path.read_text().strip() != value:
                raise UnsafeTarget(f'Could not verify {path.name}; inspect current controls.')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--container', default='telegram_bot_container')
    parser.add_argument('--apply', action='store_true', help='apply live, temporary relief (root only)')
    args = parser.parse_args(argv)
    try:
        if args.apply and os.geteuid() != 0:
            raise UnsafeTarget('--apply must run as root on the Docker host.')
        identity = inspect_container(args.container)
        cid, pid, _ = identity
        membership_file = Path(f'/proc/{pid}/cgroup')
        membership = membership_file.read_text()
        target = locate_cgroup(cid, membership)
        changes = planned_changes(target)

        def guard():
            if (inspect_container(args.container) != identity
                    or membership_file.read_text() != membership):
                raise UnsafeTarget('Container changed during operation; stopped. Reinspect before retrying.')

        guard()
        print(f'Container: {args.container} ({cid[:12]}); cgroup: {target}')
        print('WARNING: no fixed cap means the bot can exhaust HOST resources.')
        change_limits(changes, apply=args.apply, guard=guard)
        guard()
        if args.apply:
            print('Verified live controls. NO restart performed. Docker HostConfig is unchanged.')
            print('TEMPORARY: old limits may return on restart/update. Recreate from uncapped Compose ONLY when safe.')
        else:
            print('READ ONLY: nothing changed. Use --apply to apply these live, temporary changes.')
        print('Host/VM and ancestor-cgroup limits still apply; they are not modified.')
        return 0
    except (UnsafeTarget, OSError, subprocess.SubprocessError, ValueError) as exc:
        # Do not echo CalledProcessError output (Docker inspect can contain secrets).
        detail = type(exc).__name__ if isinstance(exc, subprocess.SubprocessError) else str(exc)
        print(f'ERROR: {detail}. No restart attempted; if applying, some earlier writes may have succeeded.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
