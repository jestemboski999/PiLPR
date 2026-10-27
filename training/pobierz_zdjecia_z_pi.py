#!/usr/bin/env python3
"""Resume a read-only Raspberry Pi image snapshot over an unstable SSH link."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tarfile
import threading
import time


REMOTE_ROOT = "/media/lpr/lpr"


def ssh_base(host: str, user: str) -> list[str]:
    return [
        "ssh.exe",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "ServerAliveInterval=10",
        "-o",
        "ServerAliveCountMax=2",
        f"{user}@{host}",
    ]


def subprocess_flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def fetch_manifest(
    host: str, user: str, env: dict[str, str], log
) -> list[tuple[str, int]]:
    command = f"find '{REMOTE_ROOT}' -type f -printf '%P\\0%s\\0'"
    result = subprocess.run(
        [*ssh_base(host, user), command],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=log,
        env=env,
        creationflags=subprocess_flags(),
        check=False,
    )
    if result.returncode != 0:
        raise ConnectionError(f"manifest ssh exit code {result.returncode}")

    fields = result.stdout.split(b"\0")
    items: list[tuple[str, int]] = []
    for index in range(0, len(fields) - 1, 2):
        if not fields[index]:
            continue
        relative = fields[index].decode("utf-8", errors="strict")
        size = int(fields[index + 1])
        items.append((relative, size))
    if not items:
        raise ConnectionError("remote manifest is empty")
    return items


def local_target(root: Path, relative: str) -> Path:
    parts = PurePosixPath(relative).parts
    if not parts or ".." in parts or PurePosixPath(relative).is_absolute():
        raise ValueError(f"unsafe remote path: {relative!r}")
    target = (root.joinpath(*parts)).resolve()
    if os.path.commonpath((root, target)) != str(root):
        raise ValueError(f"path escapes destination: {relative!r}")
    return target


def write_status(path: Path, **values: object) -> None:
    values["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(values, ensure_ascii=True, indent=2), encoding="utf-8"
    )
    os.replace(temporary, path)


def feed_file_list(stream, items: list[tuple[str, int]], errors: list[Exception]) -> None:
    try:
        for relative, _ in items:
            stream.write(relative.encode("utf-8") + b"\0")
        stream.close()
    except Exception as exc:  # The SSH process can close stdin on a link drop.
        errors.append(exc)


def transfer_pass(
    items: list[tuple[str, int]],
    root: Path,
    host: str,
    user: str,
    env: dict[str, str],
    log,
    status_path: Path,
    total_files: int,
    total_bytes: int,
) -> int:
    expected = dict(items)
    command = f"tar -C '{REMOTE_ROOT}' --null --files-from=- -cf -"
    process = subprocess.Popen(
        [*ssh_base(host, user), command],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=log,
        env=env,
        creationflags=subprocess_flags(),
    )
    assert process.stdin is not None
    assert process.stdout is not None

    feed_errors: list[Exception] = []
    feeder = threading.Thread(
        target=feed_file_list,
        args=(process.stdin, items, feed_errors),
        daemon=True,
    )
    feeder.start()

    completed = 0
    try:
        with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
            for member in archive:
                relative = member.name.removeprefix("./")
                if not member.isfile() or relative not in expected:
                    continue

                target = local_target(root, relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_name(target.name + ".part")
                source = archive.extractfile(member)
                if source is None:
                    continue

                try:
                    with temporary.open("wb") as destination:
                        shutil.copyfileobj(source, destination, length=1024 * 1024)
                    if temporary.stat().st_size != expected[relative]:
                        temporary.unlink(missing_ok=True)
                        continue
                    os.replace(temporary, target)
                    os.utime(target, (member.mtime, member.mtime))
                    completed += 1
                except Exception:
                    temporary.unlink(missing_ok=True)
                    raise

                if completed % 25 == 0:
                    remaining_files = len(items) - completed
                    write_status(
                        status_path,
                        state="transferring",
                        total_files=total_files,
                        total_bytes=total_bytes,
                        pass_files=len(items),
                        completed_in_pass=completed,
                        remaining_files_estimate=remaining_files,
                        current_file=relative,
                    )
    except (tarfile.TarError, EOFError, OSError) as exc:
        log.write(
            f"[{time.strftime('%Y-%m-%dT%H:%M:%S')}] tar stream interrupted: {exc}\n"
        )
        log.flush()
    finally:
        process.stdout.close()
        feeder.join(timeout=2)
        if process.poll() is None:
            process.kill()
        process.wait()

    return completed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--destination", required=True, type=Path)
    parser.add_argument("--askpass", required=True, type=Path)
    parser.add_argument("--host", required=True)
    parser.add_argument("--user", default="admin")
    parser.add_argument("--max-minutes", type=int, default=30)
    args = parser.parse_args()

    destination = args.destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    image_root = (destination / "lpr").resolve()
    image_root.mkdir(parents=True, exist_ok=True)
    if not args.askpass.is_file():
        raise FileNotFoundError(args.askpass)

    env = os.environ.copy()
    env["SSH_ASKPASS"] = str(args.askpass.resolve())
    env["SSH_ASKPASS_REQUIRE"] = "force"
    env["DISPLAY"] = "codex"

    status_path = destination / "tar_transfer_status.json"
    log_path = destination / "tar_transfer.log"
    deadline = time.monotonic() + args.max_minutes * 60
    attempt = 0

    with log_path.open("a", encoding="utf-8", buffering=1) as log:
        while time.monotonic() < deadline:
            attempt += 1
            log.write(
                f"[{time.strftime('%Y-%m-%dT%H:%M:%S')}] attempt {attempt}\n"
            )
            try:
                manifest = fetch_manifest(args.host, args.user, env, log)
            except (ConnectionError, OSError, ValueError) as exc:
                write_status(
                    status_path,
                    state="waiting_for_connection",
                    attempt=attempt,
                    error=str(exc),
                )
                time.sleep(3)
                continue

            total_bytes = sum(size for _, size in manifest)
            missing: list[tuple[str, int]] = []
            for relative, size in manifest:
                target = local_target(image_root, relative)
                if not target.is_file() or target.stat().st_size != size:
                    missing.append((relative, size))

            # Recent camera data is the most valuable training input.
            missing.sort(key=lambda item: item[0], reverse=True)
            if not missing:
                write_status(
                    status_path,
                    state="complete",
                    attempt=attempt,
                    total_files=len(manifest),
                    total_bytes=total_bytes,
                    remaining_files=0,
                )
                log.write(
                    f"[{time.strftime('%Y-%m-%dT%H:%M:%S')}] transfer complete\n"
                )
                return 0

            write_status(
                status_path,
                state="transferring",
                attempt=attempt,
                total_files=len(manifest),
                total_bytes=total_bytes,
                remaining_files=len(missing),
                remaining_bytes=sum(size for _, size in missing),
                first_file=missing[0][0],
            )
            completed = transfer_pass(
                missing,
                image_root,
                args.host,
                args.user,
                env,
                log,
                status_path,
                len(manifest),
                total_bytes,
            )
            log.write(
                f"[{time.strftime('%Y-%m-%dT%H:%M:%S')}] "
                f"attempt {attempt} completed {completed} files\n"
            )
            time.sleep(2)

    write_status(status_path, state="deadline_reached", attempt=attempt)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
