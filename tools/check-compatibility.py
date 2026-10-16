#!/usr/bin/env python3
"""Bestehende Python-/Git-Laufzeiten headless prüfen, ohne etwas zu installieren."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent.parent
CORE_PATTERNS = (
    'test_state_contracts.py', 'test_branch_inventory.py', 'test_measurement_refresh.py',
    'test_work_views.py', 'test_snapshot_contract.py', 'test_path_bytes.py',
    'test_repository_topology.py', 'test_diff_modes.py',
)


def executable(value: str) -> str:
    found = shutil.which(value)
    if found is None:
        raise ValueError('executable unavailable: ' + value)
    return str(Path(found).absolute())


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--python', action='append', dest='pythons', metavar='EXECUTABLE')
    parser.add_argument('--git', action='append', dest='gits', metavar='EXECUTABLE')
    parser.add_argument('--suite', choices=('full', 'core'), default='full',
                        help='full includes existing safety, CLI and release fixtures; core checks the feature contracts')
    parser.add_argument('--screens', action='store_true', help='also verify all generated README images')
    args = parser.parse_args(argv)
    try:
        pythons = list(dict.fromkeys(executable(p) for p in (args.pythons or [sys.executable])))
        gits = list(dict.fromkeys(executable(p) for p in (args.gits or ['git'])))
    except ValueError as exc:
        parser.error(str(exc))
    results = []
    with tempfile.TemporaryDirectory(prefix='gmf-compat-') as temp:
        bin_dir = Path(temp) / 'bin'
        bin_dir.mkdir()
        for git in gits:
            link = bin_dir / 'git'
            if link.is_symlink():
                link.unlink()
            link.symlink_to(git)
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1',
                       PATH=str(bin_dir)+os.pathsep+os.environ.get('PATH', ''))
            for python in pythons:
                python_link = bin_dir / 'python3'
                if python_link.is_symlink():
                    python_link.unlink()
                python_link.symlink_to(python)
                probe = subprocess.run([python, '-c',
                    'import curses,hashlib,json,platform,sys; from pathlib import Path; import gitmaster_flash as g; print(json.dumps(dict(python=platform.python_version(),system=sys.platform,program_version=g.__version__,source_sha256=hashlib.sha256(Path(g.__file__).read_bytes()).hexdigest())))'],
                    cwd=ROOT, env=env, capture_output=True, text=True)
                git_probe = subprocess.run([str(link), '--version'],cwd=ROOT, env=env, capture_output=True, text=True)
                record = dict(python_executable=python,git_executable=git,suite=args.suite,checks=[])
                results.append(record)
                try:
                    versions = json.loads(probe.stdout)
                    git_version = re.search(r'\b(\d+)\.(\d+)\.(\d+)',git_probe.stdout)
                    if probe.returncode or git_probe.returncode or git_version is None:
                        raise ValueError('runtime probe failed')
                    record.update(versions,git_version=git_probe.stdout.strip())
                    if tuple(map(int,versions['python'].split('.')[:2])) < (3,9):
                        raise ValueError('Python 3.9 or later required')
                    if tuple(map(int,git_version.groups())) < (2,36,0):
                        raise ValueError('Git 2.36 or later required for worktree list --porcelain -z')
                    if versions['system'] not in ('darwin','linux'):
                        raise ValueError('only macOS and Linux are covered by this check')
                except (ValueError, KeyError, TypeError) as exc:
                    record.update(ok=False, error=str(exc))
                    continue
                patterns = ('test_*.py',) if args.suite == 'full' else CORE_PATTERNS
                commands = [[python,'-m','unittest','discover','-s','tests','-p',pattern] for pattern in patterns]
                if args.screens:
                    commands.append([python, 'docs/make-screens.py', '--check'])
                for command in commands:
                    started = time.monotonic()
                    run = subprocess.run(command,cwd=ROOT, env=env, capture_output=True, text=True)
                    output = run.stdout+run.stderr
                    count = re.search(r'Ran (\d+) tests?',output)
                    skipped = re.search(r'skipped=(\d+)', output)
                    check = dict(command=command[1:],exit_code=run.returncode,
                                 elapsed_seconds=round(time.monotonic()-started,3))
                    if count:
                        check['tests'] = int(count[1])
                    if skipped:
                        check['skipped'] = int(skipped[1])
                    if run.returncode:
                        check['diagnostic'] = output[-6000:]
                    record['checks'].append(check)
                    if run.returncode:
                        break
                record['ok'] = all(check['exit_code'] == 0 for check in record['checks'])
                print(f"checked Python {versions['python']} / {git_probe.stdout.strip()}: "
                      + ('PASS' if record['ok'] else 'FAIL'),file=sys.stderr,flush=True)
    payload = dict(contract=1, results=results, ok=all(record['ok'] for record in results))
    print(json.dumps(payload, indent=2))
    return 0 if payload['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
