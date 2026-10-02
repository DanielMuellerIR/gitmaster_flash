#!/usr/bin/env python3
"""Installiert Archiv und Pruefsumme exklusiv, ohne vorhandene Dateien zu ersetzen."""

from __future__ import annotations

import os
from pathlib import Path
import signal
import sys


def install_pair(archive_source: Path, checksum_source: Path,
                 archive_target: Path, checksum_target: Path) -> None:
    """Verlinkt beide Dateien exklusiv und sperrt parallele Release-Laeufe.

    ``os.link`` scheitert atomar, wenn das Ziel schon existiert. Die Sperre
    verhindert zusaetzlich, dass zwei normale Release-Laeufe dasselbe Paar
    gleichzeitig halb sichtbar machen.
    """
    # Ein Signal zwischen den beiden Hardlinks darf kein halbes Paar
    # hinterlassen. Die kurze Transaktion endet vor der Verarbeitung von SIGINT/SIGTERM;
    # danach gilt wieder exakt die Signalmaske des Aufrufers.
    previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM})
    try:
        lock = archive_target.with_name(f".{archive_target.name}.release-lock")
        os.mkdir(lock)
        try:
            os.link(archive_source, archive_target)
            try:
                os.link(checksum_source, checksum_target)
            except BaseException:
                # Das bereits angelegte Archiv gehoert sicher diesem Lauf, weil
                # die Sperre seit davor gehalten wird. So bleibt kein halbes Paar.
                archive_target.unlink()
                raise
        finally:
            lock.rmdir()
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)


def main(argv: list[str]) -> int:
    if len(argv) != 5:
        print("Aufruf: install-release-pair.py ARCHIV QUELLE-SHA ZIEL ZIEL-SHA",
              file=sys.stderr)
        return 2
    try:
        install_pair(*(Path(value) for value in argv[1:]))
    except FileExistsError as exc:
        print(f"FEHLER: Ziel oder Release-Sperre existiert schon: {exc.filename}",
              file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"FEHLER: Release-Artefakte konnten nicht exklusiv abgelegt werden: {exc}",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
