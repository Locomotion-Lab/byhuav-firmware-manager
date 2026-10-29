"""Use PlatformIO's Python to create a separate updater environment."""
from contextlib import contextmanager
import importlib.metadata
import os
from pathlib import Path
import subprocess
import sys

from .common import StopUpdate, check

PINS = {'esptool': '4.11.0', 'pyserial': '3.5'}


@contextmanager
def file_lock(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as stream:
        stream.seek(0, 2)
        if not stream.tell():
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise StopUpdate('另一个更新进程正在运行，请先关闭它。') from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def versions_match():
    try:
        return all(importlib.metadata.version(name) == version for name, version in PINS.items())
    except importlib.metadata.PackageNotFoundError:
        return False


def prepare(root, data):
    check(sys.version_info >= (3, 9), '需要 Python 3.9 或更新版本，请更新并完成 PlatformIO 初始化。')
    python = data / 'runtime-esptool-4.11.0' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    with file_lock(data / 'runtime.lock'):
        if not python.is_file():
            print('首次准备独立烧录环境，请保持联网。', flush=True)
            result = subprocess.run([sys.executable, '-m', 'venv', str(python.parent.parent)])
            check(result.returncode == 0, '无法创建运行环境，请检查 PlatformIO 安装是否完整。')
        probe = subprocess.run([str(python), '-c',
                                'import importlib.metadata as m; '
                                'assert m.version("esptool")=="4.11.0"; '
                                'assert m.version("pyserial")=="3.5"; import esptool,serial'],
                               capture_output=True)
        if probe.returncode:
            result = subprocess.run([str(python), '-m', 'pip', 'install', '--disable-pip-version-check',
                                     '-r', str(root / 'requirements.txt')])
            check(result.returncode == 0, '烧录依赖下载失败。检查网络后重新运行；尚未开始更新板卡。')
        return python
