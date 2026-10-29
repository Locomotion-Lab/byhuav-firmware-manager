#!/usr/bin/env python3
"""Build an explicit, reproducible payload and hash-pinned online installers."""
import argparse
import hashlib
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from byhuav_updater import VERSION
from byhuav_updater.engine import Updater


def build(output, base_url):
    Updater(ROOT, output / 'unused').sources()
    output.mkdir(parents=True, exist_ok=True)
    files = [ROOT / name for name in ('update.py', 'update.sh', 'Update.ps1', 'update.cmd',
                                      'manifest.json', 'requirements.txt', 'README.md')]
    for folder in ('byhuav_updater', 'firmware', 'docs'):
        files.extend(p for p in (ROOT / folder).rglob('*') if p.is_file()
                     and '__pycache__' not in p.parts and p.name != '.DS_Store')
    archive = output / ('byhuav-firmware-manager-v' + VERSION + '.zip')
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED) as target:
        for path in sorted(files):
            name = path.relative_to(ROOT).as_posix()
            entry = zipfile.ZipInfo(name, (2026, 10, 29, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = (0o100755 if path.suffix == '.sh' else 0o100644) << 16
            target.writestr(entry, path.read_bytes())
    sha = hashlib.sha256(archive.read_bytes()).hexdigest()
    (output / 'SHA256SUMS').write_text(sha + '  ' + archive.name + '\n', encoding='utf-8')
    helper = (ROOT / 'tools/bootstrap.py').read_text(encoding='utf-8')
    url = base_url.rstrip('/') + '/' + archive.name
    if not url.startswith('https://') or any(c in url for c in "'\"`$\n\r"):
        raise ValueError('base-url must be a plain HTTPS URL')
    shell = '''#!/usr/bin/env bash
set -euo pipefail
task_pio_root="${PLATFORMIO_CORE_DIR:-$HOME/.platformio}"
task_python="${BYHUAV_PIO_PYTHON:-$task_pio_root/penv/bin/python}"
if [[ ! -x "$task_python" ]]; then
  echo '请先在 VS Code 安装 PlatformIO IDE，并等待初始化完成。' >&2
  exit 1
fi
export PYTHONUTF8=1
task_target="$HOME/.byhuav-firmware-manager/packages/@SHA@"
"$task_python" - '@URL@' '@SHA@' "$task_target" <<'BYHUAV_BOOTSTRAP'
@HELPER@
BYHUAV_BOOTSTRAP
case " $* " in
  *--verify-package*|*--prepare*|*--status*|*--check*) exec "$task_python" -u "$task_target/update.py" "$@" ;;
  *) exec "$task_python" -u "$task_target/update.py" "$@" </dev/tty ;;
esac
'''.replace('@URL@', url).replace('@SHA@', sha).replace('@HELPER@', helper)
    powershell = '''$ToolArguments = @($args)
$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
try {
  $pioRoot = if ($env:PLATFORMIO_CORE_DIR) { $env:PLATFORMIO_CORE_DIR } else { Join-Path $env:USERPROFILE '.platformio' }
  $pythonPath = if ($env:BYHUAV_PIO_PYTHON) { $env:BYHUAV_PIO_PYTHON } else { Join-Path $pioRoot 'penv\\Scripts\\python.exe' }
  if (-not (Test-Path -LiteralPath $pythonPath)) { throw '请先在 VS Code 安装 PlatformIO IDE，并等待初始化完成。' }
  $target = Join-Path $env:USERPROFILE '.byhuav-firmware-manager\\packages\\@SHA@'
  $code = @'
@HELPER@
'@
  $helperPath = Join-Path $env:TEMP ('byhuav-download-' + [guid]::NewGuid() + '.py')
  try {
    [System.IO.File]::WriteAllText($helperPath, $code, [System.Text.UTF8Encoding]::new($false))
    & $pythonPath $helperPath '@URL@' '@SHA@' $target
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
  } finally {
    Remove-Item -LiteralPath $helperPath -ErrorAction SilentlyContinue
  }
  & $pythonPath -u (Join-Path $target 'update.py') @ToolArguments
  exit $LASTEXITCODE
} catch {
  Write-Host ('[停止] ' + $_.Exception.Message) -ForegroundColor Red
  exit 1
}
'''.replace('@URL@', url).replace('@SHA@', sha).replace('@HELPER@', helper)
    with (output / 'install.sh').open('w', encoding='utf-8', newline='\n') as stream:
        stream.write(shell)
    with (output / 'Install.ps1').open('w', encoding='utf-8-sig', newline='\r\n') as stream:
        stream.write(powershell)
    print(str(archive) + '\nSHA-256: ' + sha)
    return archive


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=ROOT / 'dist')
    parser.add_argument('--base-url', default='https://github.com/Locomotion-Lab/byhuav-firmware-manager/releases/download/v' + VERSION)
    args = parser.parse_args()
    build(args.output, args.base_url)
