#!/usr/bin/env python3
"""Atomically guard protocol task stage transitions."""

from __future__ import annotations

import argparse
import fcntl
import os
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


MIN_STAGE = 0
TERMINAL_STAGE = 7
MAX_STAGE = TERMINAL_STAGE
STAGE_FILE = "task_stage.md"
LOCK_FILE = ".task-stage.lock"
CHECK_POLICIES = {"none", "required", "conditional"}


@dataclass(frozen=True)
class StageDefinition:
    name: str
    check: str


STAGES = (
    StageDefinition("preflight", "none"),
    StageDefinition("task-definition", "none"),
    StageDefinition("discovery", "required"),
    StageDefinition("adr", "conditional"),
    StageDefinition("planning", "required"),
    StageDefinition("implementation", "required"),
    StageDefinition("acceptance", "none"),
    StageDefinition("completed", "none"),
)

if len(STAGES) != TERMINAL_STAGE + 1:
    raise RuntimeError("every protocol stage must have a definition")
if any(stage.check not in CHECK_POLICIES for stage in STAGES):
    raise RuntimeError("every protocol stage must have an explicit check policy")


class StageError(RuntimeError):
    pass


def stage_number(value: str) -> int:
    try:
        stage = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("stage must be an integer from 0 to 7") from error
    if not MIN_STAGE <= stage <= MAX_STAGE:
        raise argparse.ArgumentTypeError("stage must be an integer from 0 to 7")
    return stage


def task_directory(value: str) -> Path:
    task_dir = Path(value).expanduser()
    if not task_dir.is_dir():
        raise StageError(f"task directory does not exist: {task_dir}")
    return task_dir


@contextmanager
def stage_lock(task_dir: Path, *, exclusive: bool) -> Iterator[None]:
    lock_path = task_dir / LOCK_FILE
    with lock_path.open("a", encoding="utf-8") as lock:
        mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        fcntl.flock(lock.fileno(), mode)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def read_stage(task_dir: Path) -> int:
    path = task_dir / STAGE_FILE
    try:
        content = path.read_bytes()
    except FileNotFoundError as error:
        raise StageError(f"stage file does not exist: {path}") from error

    for stage in range(MIN_STAGE, MAX_STAGE + 1):
        if content == f"{stage}\n".encode("ascii"):
            return stage
    raise StageError(f"stage file is not canonical: expected one digit from 0 to 7 and a newline: {path}")


def write_stage(task_dir: Path, stage: int) -> None:
    path = task_dir / STAGE_FILE
    descriptor, temporary_name = tempfile.mkstemp(
        dir=task_dir,
        prefix=f".{STAGE_FILE}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as temporary:
            temporary.write(f"{stage}\n".encode("ascii"))
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
        directory_descriptor = os.open(task_dir, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        temporary_path.unlink(missing_ok=True)


def initialize(task_dir: Path) -> None:
    path = task_dir / STAGE_FILE
    with stage_lock(task_dir, exclusive=True):
        if path.exists():
            raise StageError(f"stage file already exists: {path}")
        write_stage(task_dir, MIN_STAGE)
    print("stage=0")


def current(task_dir: Path) -> None:
    with stage_lock(task_dir, exclusive=False):
        stage = read_stage(task_dir)
    print(f"stage={stage}")


def describe(task_dir: Path) -> None:
    with stage_lock(task_dir, exclusive=False):
        stage = read_stage(task_dir)
    definition = STAGES[stage]
    print(f"stage={stage} name={definition.name} check={definition.check}")


def require(task_dir: Path, expected: int) -> None:
    with stage_lock(task_dir, exclusive=False):
        actual = read_stage(task_dir)
    if actual != expected:
        raise StageError(f"stage mismatch: expected {expected}, actual {actual}")
    print(f"stage={actual}")


def advance(task_dir: Path, expected: int) -> None:
    if expected == TERMINAL_STAGE:
        raise StageError(f"stage {TERMINAL_STAGE} is terminal and cannot advance")
    with stage_lock(task_dir, exclusive=True):
        actual = read_stage(task_dir)
        if actual != expected:
            raise StageError(f"stage mismatch: expected {expected}, actual {actual}")
        next_stage = expected + 1
        write_stage(task_dir, next_stage)
    print(f"advanced={expected}->{next_stage}")


def return_to_implementation(task_dir: Path) -> None:
    with stage_lock(task_dir, exclusive=True):
        actual = read_stage(task_dir)
        if actual != 6:
            raise StageError(f"return requires stage 6, actual {actual}")
        write_stage(task_dir, 5)
    print("returned=6->5")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)

    for name in ("init", "current", "describe", "return-to-implementation"):
        command = commands.add_parser(name)
        command.add_argument("task_dir")

    for name in ("require", "advance"):
        command = commands.add_parser(name)
        command.add_argument("task_dir")
        command.add_argument("stage", type=stage_number)

    return result


def main() -> int:
    args = parser().parse_args()
    try:
        task_dir = task_directory(args.task_dir)
        if args.command == "init":
            initialize(task_dir)
        elif args.command == "current":
            current(task_dir)
        elif args.command == "describe":
            describe(task_dir)
        elif args.command == "require":
            require(task_dir, args.stage)
        elif args.command == "advance":
            advance(task_dir, args.stage)
        elif args.command == "return-to-implementation":
            return_to_implementation(task_dir)
        else:
            raise AssertionError(f"unsupported command: {args.command}")
    except StageError as error:
        print(f"task-stage: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
