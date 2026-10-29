"""Embedded by build_release.py into the two online installers. Stdlib only."""
import hashlib
import io
from pathlib import Path, PurePosixPath
import shutil
import stat
import sys
import tempfile
import urllib.parse
import urllib.request
import zipfile

MAX_BYTES = 32 * 1024 * 1024


def install(url, expected, destination):
    if urllib.parse.urlsplit(url).scheme != 'https':
        raise RuntimeError('更新包地址必须使用 HTTPS。')
    destination = Path(destination)
    print('正在获取固定版本更新包……', flush=True)
    with urllib.request.urlopen(url, timeout=90) as response:
        if urllib.parse.urlsplit(response.geturl()).scheme != 'https':
            raise RuntimeError('下载被重定向到非 HTTPS 地址。')
        content = response.read(MAX_BYTES + 1)
    if len(content) > MAX_BYTES or hashlib.sha256(content).hexdigest() != expected:
        raise RuntimeError('更新包 SHA-256 校验失败，请重新下载。')
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        names, total = set(), 0
        for entry in archive.infolist():
            name = PurePosixPath(entry.filename)
            if (name.is_absolute() or '..' in name.parts or '\\' in entry.filename
                    or ':' in entry.filename or not name.parts or name.parts[0].startswith('.')):
                raise RuntimeError('更新包包含无效路径。')
            if entry.filename in names or stat.S_ISLNK(entry.external_attr >> 16):
                raise RuntimeError('更新包包含重复路径或符号链接。')
            names.add(entry.filename)
            total += entry.file_size
        if total > 64 * 1024 * 1024 or not {'update.py', 'manifest.json', 'requirements.txt'} <= names:
            raise RuntimeError('更新包结构或大小无效。')
        if destination.exists():
            # Compare cache to the authenticated archive rather than trusting
            # a writable local checksum file.
            for entry in archive.infolist():
                path = destination / entry.filename
                if entry.is_dir():
                    continue
                if not path.is_file() or path.is_symlink() or path.read_bytes() != archive.read(entry):
                    raise RuntimeError('本地工具缓存被更改，请联系维护人员；板卡备份不受影响。')
            return destination
        destination.parent.mkdir(parents=True, exist_ok=True)
        scratch = Path(tempfile.mkdtemp(prefix='.download-', dir=destination.parent))
        try:
            for entry in archive.infolist():
                path = scratch / entry.filename
                if entry.is_dir():
                    path.mkdir(parents=True, exist_ok=True)
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with path.open('xb') as stream:
                        stream.write(archive.read(entry))
            try:
                scratch.rename(destination)
            except OSError:
                if not destination.exists():
                    raise
                # A second installer may have finished in parallel. Validate
                # that destination on the next run rather than executing it.
                raise RuntimeError('另一个安装进程已完成，请重新运行启动命令。')
        finally:
            if scratch.exists():
                shutil.rmtree(scratch)
    return destination


if __name__ == '__main__':
    try:
        install(*sys.argv[1:])
    except Exception as exc:
        print('[停止] ' + str(exc), file=sys.stderr)
        sys.exit(1)
