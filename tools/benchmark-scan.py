#!/usr/bin/env python3
"""Lokalen Scan an neutralen Repos messen; keine persönlichen Repos oder Netzaufrufe."""
from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import platform
import statistics
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import gitmaster_flash as g


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repetitions', type=int, default=5)
    args = parser.parse_args(argv)
    if not 1 <= args.repetitions <= 25:
        parser.error('--repetitions must be between 1 and 25')
    results = []
    saved_environment = dict(os.environ)
    demo_environment = g._demo_env()
    try:
        os.environ.clear()
        os.environ.update(demo_environment)
        with tempfile.TemporaryDirectory(prefix='gmf-benchmark-') as temp:
            root = Path(temp).resolve()
            repo, _ = g._demo_repo(root, 'project')
            g._demo_extra_remote(root, repo, 'backup')
            g._demo_extra_remote(root, repo, 'mirror')
            git_version = g.run_git(repo, '--version', timeout=10).stdout.strip()
            cfg = copy.deepcopy(g.DEFAULT_CONFIG)
            for count in (1, 25):
                if count == 25:
                    for i in range(24):
                        g._dgit(repo, 'branch', f'topic-{i:02}')
                for fetch in (False, True):
                    durations = []
                    calls = []
                    for _ in range(args.repetitions):
                        original = g.run_git
                        counter = [0]

                        def measured(*args, **kwargs):
                            counter[0] += 1
                            return original(*args, **kwargs)

                        g.run_git = measured
                        try:
                            started = time.perf_counter()
                            status = g.collect_status(repo, root, cfg, fetch)
                            elapsed = time.perf_counter() - started
                        finally:
                            g.run_git = original
                        if (status.error or status.branches_error
                                or status.worktrees_error or status.submodules_error):
                            raise RuntimeError('fixture scan failed: ' + json.dumps(g.status_dict(status)))
                        durations.append(elapsed)
                        calls.append(counter[0])
                    results.append(dict(
                        branches=count, remotes=3, fetch=fetch, seconds=durations,
                        median_seconds=statistics.median(durations), git_calls=calls,
                    ))
    finally:
        os.environ.clear()
        os.environ.update(saved_environment)
    print(json.dumps(dict(
        version=g.__version__, measured_at=g.utc_now(), python=platform.python_version(),
        platform=sys.platform, git_version=git_version, results=results,
    ), indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
