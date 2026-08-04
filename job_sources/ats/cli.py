"""Local command-line adapter for bounded ATS detection."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
from typing import Sequence

from .batch import detect_ats_urls, serialize_ats_batch_report

_MAX_INPUT_BYTES = 128 * 1_024


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.exit(2, f"{self.prog}: invalid_arguments\n")


class _InputError(Exception):
    pass


def _worker_count(value: str) -> int:
    try:
        workers = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("invalid worker count") from None
    if not 1 <= workers <= 8:
        raise argparse.ArgumentTypeError("invalid worker count")
    return workers


def _load_urls(input_name: str) -> tuple[str, ...]:
    try:
        if input_name == "-":
            raw_input = sys.stdin.buffer.read(_MAX_INPUT_BYTES + 1)
        else:
            with Path(input_name).open("rb") as input_file:
                raw_input = input_file.read(_MAX_INPUT_BYTES + 1)
    except OSError:
        raise _InputError("input_unavailable") from None
    if len(raw_input) > _MAX_INPUT_BYTES:
        raise _InputError("input_too_large")
    if any(len(line) > 8_192 for line in raw_input.splitlines()):
        raise _InputError("input_too_large")
    try:
        content = raw_input.decode("utf-8")
    except UnicodeDecodeError:
        raise _InputError("input_invalid_utf8") from None
    urls = tuple(line.strip() for line in content.splitlines() if line.strip())
    if not urls:
        raise _InputError("input_empty")
    if len(urls) > 100:
        raise _InputError("input_too_many")
    return urls


def _discard_failed_stdout_buffer() -> None:
    """Redirect the current stdout descriptor so shutdown cannot retry failed data."""
    try:
        null_fd = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(null_fd, sys.stdout.fileno())
        finally:
            os.close(null_fd)
    except (AttributeError, OSError, ValueError):
        pass


def main(argv: Sequence[str] | None = None) -> int:
    """Run ATS detection for an explicit local URL-list file."""
    parser = _SafeArgumentParser(prog="ats-detect")
    parser.add_argument("--input", required=True)
    parser.add_argument("--format", choices=("json", "jsonl"), default="json")
    parser.add_argument("--workers", type=_worker_count, default=4)
    args = parser.parse_args(argv)

    try:
        urls = _load_urls(args.input)
    except _InputError as error:
        print(f"ats-detect: {error}", file=sys.stderr)
        return 2
    try:
        report = detect_ats_urls(urls, max_workers=args.workers)
        serialized = serialize_ats_batch_report(report, format=args.format)
    except Exception:
        print("ats-detect: detection_failed", file=sys.stderr)
        return 1
    try:
        print(serialized, flush=True)
    except Exception:
        _discard_failed_stdout_buffer()
        print("ats-detect: detection_failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
