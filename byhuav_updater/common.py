import hashlib
import json
import os
from pathlib import Path
import subprocess


class StopUpdate(RuntimeError):
    pass


def check(condition, message):
    if not condition:
        raise StopUpdate(message)


def digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def capture(command, binary=False):
    try:
        result = subprocess.run(command, capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise StopUpdate('无法读取设备信息：' + str(exc)) from exc
    check(result.returncode == 0,
          '读取设备信息失败：' + result.stderr.decode('utf-8', errors='replace')[-800:])
    return result.stdout if binary else result.stdout.decode('utf-8-sig', errors='replace')
