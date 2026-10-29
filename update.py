#!/usr/bin/env python3
"""Run the guided updater; verification and status modes never access hardware."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

from byhuav_updater import VERSION
from byhuav_updater.common import StopUpdate, check
from byhuav_updater.engine import Updater
from byhuav_updater.runtime import file_lock, prepare, versions_match

ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description='BYHUAV 外部电脑 1.x → 2.x 固件迁移向导')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--verify-package', action='store_true', help='离线检查随附固件，不读取设备')
    mode.add_argument('--prepare', action='store_true', help='仅准备烧录环境，不读取设备')
    mode.add_argument('--check', action='store_true', help='检查固件与设备，不烧录、不创建更新任务')
    mode.add_argument('--status', action='store_true', help='读取上次进度，不读取设备')
    parser.add_argument('--data-dir', type=Path, default=Path.home() / '.byhuav-firmware-manager', help='记录和运行环境目录')
    parser.add_argument('--runtime-ready', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    updater = Updater(ROOT, args.data_dir)
    if args.verify_package:
        updater.sources()
        print('[通过] 固件大小、SHA-256 和镜像类型检查通过。未读取设备。')
        return 0
    if args.status:
        session = updater.current()
        print(json.dumps(session, ensure_ascii=False, indent=2) if session else '尚无更新记录。')
        return 0
    check(not (hasattr(os, 'geteuid') and os.geteuid() == 0), '请使用普通用户运行，不要 sudo 启动更新工具。')
    updater.sources()
    if not args.runtime_ready:
        python = prepare(ROOT, args.data_dir.resolve())
        if args.prepare:
            print('[通过] 烧录环境准备完成。未读取设备。')
            return 0
        return subprocess.call([str(python), '-u', str(ROOT / 'update.py'), '--runtime-ready', *sys.argv[1:]])
    check(versions_match(), '烧录依赖版本不正确，请使用 update.sh 或 Update.ps1 启动。')
    updater.tool_python = sys.executable
    with file_lock(updater.data / 'updater.lock'):
        if args.check:
            from byhuav_updater.hardware import inventory
            device = inventory()
            print(json.dumps({k: str(v) for k, v in device.items() if k != 'firmware_roots'}, ensure_ascii=False, indent=2))
            print('[通过] 设备及固件检查通过，未执行备份或烧录。')
        else:
            updater.run()
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print('\n[停止] 已中断，请保留记录；重新运行同一更新包可继续。', file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        print('\n[停止] ' + str(exc), file=sys.stderr)
        print('请保留本次更新记录和报错，不要跳步或自行更换固件。', file=sys.stderr)
        sys.exit(1)
