#!/usr/bin/env python3
"""Lokalen Scan mit drei lokalen Remotes messen."""

from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import os
import platform
import statistics
import sys
import tempfile
import time
from pathlib import Path
from types import ModuleType

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import gitmaster_flash as g


def load_baseline(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location("gmf_benchmark_baseline", path)
    if spec is None or spec.loader is None:
        raise ValueError("expected a Python program for --baseline")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def without_measurement_times(value):
    if isinstance(value, dict):
        return {
            key: without_measurement_times(item)
            for key, item in value.items()
            if key not in ("measured_at", "fetched_at") and not key.endswith("_measured_at")
        }
    if isinstance(value, list):
        return [without_measurement_times(item) for item in value]
    return value


def measure(module: ModuleType, repo: Path, root: Path, fetch: bool) -> tuple[float, int, dict]:
    cfg = copy.deepcopy(module.DEFAULT_CONFIG)
    namespace = vars(module)
    original = namespace["run_git"]
    calls = 0

    def measured(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    namespace["run_git"] = measured
    try:
        started = time.perf_counter()
        status = module.collect_status(repo, root, cfg, fetch)
        elapsed = time.perf_counter() - started
    finally:
        namespace["run_git"] = original
    payload = module.status_dict(status)
    if status.error or status.branches_error or status.worktrees_error or status.submodules_error:
        raise RuntimeError("fixture scan failed: " + json.dumps(payload))
    return elapsed, calls, without_measurement_times(payload)


def paired_measurements(
    repo: Path,
    root: Path,
    modules: list[ModuleType],
    repetitions: int,
) -> list[list[dict]]:
    results: list[list[dict]] = [[] for _ in modules]
    for count in (1, 25):
        if count == 25:
            for number in range(count - 1):
                g._dgit(repo, "branch", f"topic-{number:02}")
        for fetch in (False, True):
            durations: list[list[float]] = [[] for _ in modules]
            calls: list[list[int]] = [[] for _ in modules]
            indices = range(len(modules))
            for repetition in range(repetitions):
                # Beide Fassungen messen denselben Ordner. Abwechselnd anfangen,
                # damit sich kein Programm den aufgewärmten Cache reserviert.
                pair = []
                for index in indices if repetition % 2 == 0 else reversed(indices):
                    elapsed, processes, payload = measure(modules[index], repo, root, fetch)
                    durations[index].append(elapsed)
                    calls[index].append(processes)
                    pair.append(payload)
                if any(payload != pair[0] for payload in pair[1:]):
                    raise RuntimeError(f"scan results differ with branches={count} fetch={fetch}")
            for result, seconds, git_calls in zip(results, durations, calls):
                result.append(
                    dict(
                        branches=count,
                        remotes=3,
                        fetch=fetch,
                        seconds=seconds,
                        median_seconds=statistics.median(seconds),
                        git_calls=git_calls,
                    )
                )
    return results


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument(
        "--baseline", type=Path, help="older gitmaster_flash.py to run on the same repos"
    )
    args = parser.parse_args(argv)
    if not 1 <= args.repetitions <= 25:
        parser.error("--repetitions must be between 1 and 25")
    baseline = load_baseline(args.baseline) if args.baseline else None
    modules = [g] if baseline is None else [g, baseline]
    saved_environment = dict(os.environ)
    demo_environment = g._demo_env()
    try:
        os.environ.clear()
        os.environ.update(demo_environment)
        with tempfile.TemporaryDirectory(prefix="gmf-benchmark-") as temp:
            root = Path(temp).resolve()
            repo, _ = g._demo_repo(root, "project")
            g._demo_extra_remote(root, repo, "backup")
            g._demo_extra_remote(root, repo, "mirror")
            git_version = g.run_git(repo, "--version", timeout=10).stdout.strip()
            lists = paired_measurements(repo, root, modules, args.repetitions)
    finally:
        os.environ.clear()
        os.environ.update(saved_environment)
    report = dict(
        version=g.__version__,
        measured_at=g.utc_now(),
        python=platform.python_version(),
        platform=sys.platform,
        git_version=git_version,
        results=lists[0],
    )
    if baseline is not None:
        report["baseline"] = dict(version=baseline.__version__, results=lists[1])
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
