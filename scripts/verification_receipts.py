#!/usr/bin/env python3
"""Record a command's result against a bounded local Git workspace state."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid

SCHEMA = 1
MAX_LOG = 16 * 1024 * 1024
MAX_RECEIPT = 1024 * 1024
MAX_ENTRIES = 100_000
MAX_FILE = 64 * 1024 * 1024
CODES = {"PASS_CURRENT": 0, "FAIL_CURRENT": 1, "STALE": 2, "INVALID": 3, "UNKNOWN": 4}
EXCLUSIONS = [
    "Ignored files, Git metadata other than HEAD and index entries, and empty directories",
    "External files, dependencies, environment variables, services, and executable versions",
    "Symlink referent contents; only the link target text is fingerprinted",
    "Files changed and restored entirely between observations",
]
HEX = re.compile(r"[0-9a-f]{64}\Z")
TOKEN = re.compile(r"[0-9a-f]{32}\Z")


class EvidenceError(Exception):
    """A state or artifact cannot be safely inspected."""


def git(cwd: Path, *args: str) -> bytes:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_CONFIG_NOSYSTEM="1")
    result = subprocess.run(
        ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-C", str(cwd), *args],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=30, check=False,
    )
    if result.returncode:
        raise EvidenceError("Git state is unavailable")
    return result.stdout


def check_specials(root: Path, root_fd: int) -> None:
    """Git omits untracked special files; inspect directory entries without following links."""
    ignored = git(root, "ls-files", "--others", "--ignored", "--exclude-standard", "--directory", "-z")
    ignored_dirs = {path.rstrip(b"/") for path in ignored.split(b"\0") if path.endswith(b"/")}
    count = 0

    def visit(directory_fd: int, prefix: bytes, depth: int) -> None:
        nonlocal count
        if depth > 64:
            raise EvidenceError("Directory depth exceeds the inspection limit")
        with os.scandir(directory_fd) as entries:
            for entry in entries:
                name = os.fsencode(entry.name)
                if not prefix and name == b".git":
                    continue
                path = prefix + name
                count += 1
                if count > MAX_ENTRIES:
                    raise EvidenceError("Workspace exceeds the directory-entry inspection limit")
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    if path in ignored_dirs:
                        continue
                    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd)
                    try:
                        visit(fd, path + b"/", depth + 1)
                    finally:
                        os.close(fd)
                elif not (stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)):
                    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
                    result = subprocess.run(
                        ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-C", str(root),
                         "check-ignore", "--quiet", "--", os.fsdecode(path)], env=env,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, check=False,
                    )
                    if result.returncode != 0:
                        raise EvidenceError("Non-ignored special files are outside the supported scope")

    visit(root_fd, b"", 0)


def repository() -> tuple[Path, Path, Path]:
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
        raise EvidenceError("This version requires POSIX no-follow file access")
    cwd = Path.cwd().resolve()
    root = Path(os.fsdecode(git(cwd, "rev-parse", "--show-toplevel").rstrip(b"\n"))).resolve()
    metadata = Path(os.fsdecode(git(cwd, "rev-parse", "--absolute-git-dir").rstrip(b"\n"))).resolve()
    if not cwd.is_relative_to(root) or not metadata.is_dir():
        raise EvidenceError("Working directory is outside its repository")
    return root, metadata, cwd


def identity(root: Path, metadata: Path) -> dict[str, str]:
    return {"root": str(root), "git_dir": str(metadata), "identity": hashlib.sha256(
        os.fsencode(root) + b"\0" + os.fsencode(metadata)
    ).hexdigest()}


def storage(metadata: Path, *, create: bool = False) -> Path:
    path = metadata / "verification-receipts"
    if create:
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise EvidenceError("Receipt storage must be a private directory owned by the current user")
    return path


def parts(path: bytes) -> list[bytes]:
    values = path.split(b"/")
    if not path or any(value in (b"", b".", b"..") for value in values):
        raise EvidenceError("Invalid repository-relative path")
    return values


def field(digest: object, value: bytes) -> None:
    digest.update(len(value).to_bytes(8, "big"))
    digest.update(value)


def stable_stat(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def file_state(root_fd: int, path: bytes, digest: object) -> None:
    components = parts(path)
    parent = os.dup(root_fd)
    try:
        try:
            for component in components[:-1]:
                new_parent = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                os.close(parent)
                parent = new_parent
            info = os.stat(components[-1], dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            field(digest, b"missing")
            return
        field(digest, str(stat.S_IMODE(info.st_mode)).encode())
        if stat.S_ISLNK(info.st_mode):
            field(digest, b"symlink")
            field(digest, os.readlink(components[-1], dir_fd=parent))
        elif stat.S_ISREG(info.st_mode):
            if info.st_size > MAX_FILE:
                raise EvidenceError("A source file exceeds the 64 MiB inspection limit")
            field(digest, b"regular")
            fd = os.open(components[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            with os.fdopen(fd, "rb") as handle:
                opened = os.fstat(handle.fileno())
                if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                    raise EvidenceError("A workspace file changed during inspection")
                contents = hashlib.sha256()
                total = 0
                while block := handle.read(1024 * 1024):
                    total += len(block)
                    if total > MAX_FILE:
                        raise EvidenceError("A source file exceeds the 64 MiB inspection limit")
                    contents.update(block)
                ended = os.fstat(handle.fileno())
                if stable_stat(opened) != stable_stat(ended):
                    raise EvidenceError("A workspace file changed during inspection")
                field(digest, contents.digest())
        else:
            raise EvidenceError("Special files and directory replacements are outside the supported scope")
        ended = os.stat(components[-1], dir_fd=parent, follow_symlinks=False)
        if stable_stat(info) != stable_stat(ended):
            raise EvidenceError("A workspace path changed during inspection")
    finally:
        os.close(parent)


def fingerprint(root: Path) -> dict[str, object]:
    index = git(root, "ls-files", "--stage", "-z")
    others = git(root, "ls-files", "--others", "--exclude-standard", "-z")
    def current_head() -> str | None:
        try:
            return git(root, "rev-parse", "--verify", "HEAD").strip().decode("ascii")
        except EvidenceError:
            if git(root, "rev-list", "--all", "--max-count=1").strip():
                raise EvidenceError("HEAD cannot be resolved") from None
            return None

    head = current_head()
    paths: set[bytes] = set()
    digest = hashlib.sha256()
    field(digest, index)
    field(digest, b"unborn" if head is None else head.encode())
    for entry in index.split(b"\0"):
        if not entry:
            continue
        prefix, path = entry.split(b"\t", 1)
        mode, _object_id, stage = prefix.split(b" ")
        if stage != b"0" or mode not in (b"100644", b"100755", b"120000"):
            raise EvidenceError("Unmerged entries and submodules are outside the supported scope")
        paths.add(path)
    paths.update(path for path in others.split(b"\0") if path)
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        check_specials(root, root_fd)
        for path in sorted(paths):
            field(digest, path)
            file_state(root_fd, path, digest)
    finally:
        os.close(root_fd)
    if head != current_head() or index != git(root, "ls-files", "--stage", "-z") or others != git(root, "ls-files", "--others", "--exclude-standard", "-z"):
        raise EvidenceError("Repository inventory changed during inspection")
    return {"head": head, "digest": digest.hexdigest(), "entries": len(paths)}


def timestamp() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def atomic_json(path: Path, data: dict[str, object]) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".receipt-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def stop_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def run_command(argv: list[str], timeout: float, log: object) -> tuple[int | None, bool, str | None]:
    try:
        process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   shell=False, start_new_session=True)
    except OSError:
        return None, False, "Command could not be started"
    timed_out = False
    error = None
    total = 0
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    stop_group(process)
                    break
                for key, _ in selector.select(min(remaining, 0.1)):
                    block = os.read(key.fd, 65536)
                    if not block:
                        selector.unregister(key.fileobj)
                        continue
                    total += len(block)
                    if total > MAX_LOG:
                        error = "Output exceeded the 16 MiB capture limit"
                        stop_group(process)
                        break
                    log.write(block)
                if error:
                    break
            if not timed_out and not error:
                try:
                    process.wait(timeout=max(0.001, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    timed_out = True
                    stop_group(process)
    finally:
        stop_group(process)
        process.stdout.close()
    return process.returncode, timed_out, error


def private_bytes(path: Path, maximum: int) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise EvidenceError("Evidence files must be private regular files owned by the current user")
        if info.st_size > maximum:
            raise EvidenceError("Evidence file exceeds its size limit")
        data = handle.read(maximum + 1)
        if len(data) > maximum or stable_stat(info) != stable_stat(os.fstat(handle.fileno())):
            raise EvidenceError("Evidence file changed during inspection")
        return data


def state_shape(value: object) -> bool:
    return isinstance(value, dict) and set(value) == {"head", "digest", "entries"} and (
        value["head"] is None or isinstance(value["head"], str) and re.fullmatch(r"[0-9a-f]{40,64}", value["head"])
    ) and isinstance(value["digest"], str) and bool(HEX.fullmatch(value["digest"])) and type(value["entries"]) is int and value["entries"] >= 0


def validate(data: object, token: str, repo: dict[str, str]) -> dict[str, object]:
    required = {"schema", "id", "repository", "cwd", "argv", "started_at", "ended_at", "before", "after",
                "returncode", "timed_out", "capture_error", "log", "log_sha256", "exclusions"}
    if not isinstance(data, dict) or set(data) != required:
        raise EvidenceError("Invalid receipt structure")
    if type(data["schema"]) is not int or data["schema"] != SCHEMA or data["id"] != token or data["repository"] != repo:
        raise EvidenceError("Receipt schema, identity, or repository does not match")
    if data["log"] != token + ".log" or not isinstance(data["log_sha256"], str) or not HEX.fullmatch(data["log_sha256"]):
        raise EvidenceError("Invalid log reference")
    if not state_shape(data["before"]) or (data["after"] is not None and not state_shape(data["after"])):
        raise EvidenceError("Invalid workspace state")
    if not isinstance(data["cwd"], str) or data["cwd"] != "." and not parts(os.fsencode(data["cwd"])):
        raise EvidenceError("Invalid working directory")
    if not isinstance(data["argv"], list) or not data["argv"] or not all(isinstance(item, str) for item in data["argv"]):
        raise EvidenceError("Invalid stored command")
    if data["returncode"] is not None and type(data["returncode"]) is not int:
        raise EvidenceError("Invalid command result")
    if type(data["timed_out"]) is not bool or data["capture_error"] is not None and not isinstance(data["capture_error"], str):
        raise EvidenceError("Invalid capture result")
    if data["exclusions"] != EXCLUSIONS or not all(isinstance(data[key], str) for key in ("started_at", "ended_at")):
        raise EvidenceError("Invalid capture metadata")
    for key in ("started_at", "ended_at"):
        dt.datetime.fromisoformat(data[key])
    return data


def inspect_receipt(argument: str) -> tuple[str, str, Path, dict[str, object] | None]:
    root, metadata, _cwd = repository()
    store = storage(metadata)
    path = Path(os.path.abspath(argument))
    token = path.stem
    if path.parent != store or path.suffix != ".json" or not TOKEN.fullmatch(token):
        raise EvidenceError("Receipt must be a JSON file directly in this repository's receipt directory")
    data = validate(json.loads(private_bytes(path, MAX_RECEIPT)), token, identity(root, metadata))
    log = private_bytes(store / data["log"], MAX_LOG)
    if hashlib.sha256(log).hexdigest() != data["log_sha256"]:
        return "INVALID", "Log digest does not match", path, data
    if data["after"] is not None and data["before"] != data["after"]:
        return "INVALID", "Workspace changed during capture", path, data
    if data["capture_error"] or data["after"] is None or data["returncode"] is None:
        return "UNKNOWN", "Capture did not produce complete evidence", path, data
    try:
        current = fingerprint(root)
    except (EvidenceError, OSError, subprocess.SubprocessError):
        return "UNKNOWN", "Current workspace cannot be safely inspected", path, data
    if current != data["after"]:
        return "STALE", "Workspace no longer matches the verified state", path, data
    if data["timed_out"] or data["returncode"] != 0:
        return "FAIL_CURRENT", "Command failed or timed out against the current state", path, data
    return "PASS_CURRENT", "Command passed against the current scoped workspace state", path, data


def capture(argv: list[str], timeout: float) -> Path:
    root, metadata, cwd = repository()
    store = storage(metadata, create=True)
    before = fingerprint(root)
    token = uuid.uuid4().hex
    receipt = store / (token + ".json")
    fd, temporary = tempfile.mkstemp(prefix=".log-", dir=store)
    started = timestamp()
    try:
        with os.fdopen(fd, "wb") as handle:
            returncode, timed_out, error = run_command(argv, timeout, handle)
            handle.flush()
            os.fsync(handle.fileno())
        ended = timestamp()
        try:
            after = fingerprint(root)
        except (EvidenceError, OSError, subprocess.SubprocessError) as exception:
            after = None
            error = type(exception).__name__ + ": workspace could not be inspected after capture"
        log_path = store / (token + ".log")
        os.replace(temporary, log_path)
        data = {"schema": SCHEMA, "id": token, "repository": identity(root, metadata),
                "cwd": str(cwd.relative_to(root)), "argv": argv, "started_at": started, "ended_at": ended,
                "before": before, "after": after, "returncode": returncode, "timed_out": timed_out,
                "capture_error": error, "log": log_path.name,
                "log_sha256": hashlib.sha256(private_bytes(log_path, MAX_LOG)).hexdigest(), "exclusions": EXCLUSIONS}
        atomic_json(receipt, data)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    run = commands.add_parser("capture", help="Run a newly authorized command and record its result")
    run.add_argument("--timeout", type=float, default=300, help="Seconds, greater than zero and at most 3600")
    run.add_argument("argv", nargs=argparse.REMAINDER)
    for name in ("status", "show"):
        commands.add_parser(name, help="Inspect local evidence without executing stored commands").add_argument("receipt")
    args = parser.parse_args()
    receipt = None
    try:
        if args.operation == "capture":
            argv = args.argv[1:] if args.argv and args.argv[0] == "--" else args.argv
            if not argv or len(json.dumps(argv)) > 65536 or not math.isfinite(args.timeout) or not 0 < args.timeout <= 3600:
                parser.error("provide --timeout in (0, 3600] and a command after --")
            receipt = capture(argv, args.timeout)
        status, reason, receipt, data = inspect_receipt(str(receipt or args.receipt))
        result = {"status": status, "reason": reason, "receipt": str(receipt), "scope_exclusions": EXCLUSIONS}
        if args.operation == "show":
            result["evidence"] = data
    except (EvidenceError, OSError, ValueError, KeyError, subprocess.SubprocessError) as exception:
        status = "UNKNOWN" if args.operation == "capture" else "INVALID"
        result = {"status": status, "reason": str(exception), "receipt": str(receipt) if receipt else None,
                  "scope_exclusions": EXCLUSIONS}
    print(json.dumps(result, ensure_ascii=True))
    return CODES[status]


if __name__ == "__main__":
    raise SystemExit(main())
