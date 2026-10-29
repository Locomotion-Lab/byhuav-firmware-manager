#!/usr/bin/env bash
set -euo pipefail
task_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
task_pio_root="${PLATFORMIO_CORE_DIR:-$HOME/.platformio}"
task_python="${BYHUAV_PIO_PYTHON:-$task_pio_root/penv/bin/python}"
if [[ ! -x "$task_python" ]]; then
  echo '请先在 VS Code 安装 PlatformIO IDE，并等待初始化完成。' >&2
  exit 1
fi
export PYTHONUTF8=1
exec "$task_python" -u "$task_root/update.py" "$@"
