import hashlib
import json
import os
from pathlib import Path
import queue
import re
import shutil
import struct
import subprocess
import threading
import time
from datetime import datetime, timezone
import uuid

from . import VERSION
from .common import StopUpdate, atomic_json, check, digest
from . import hardware

ESP_SIZE = 0x800000
PHASES = {'checked', 'backup_running', 'backup_verified', 'esp32_flash_running',
          'esp32_verified', 'core_copying', 'core_staged', 'core_verified',
          'boot_copying', 'boot_staged', 'complete'}


def now():
    return datetime.now(timezone.utc).isoformat()


def validate_layout(content, firmware_size):
    check(len(content) == ESP_SIZE, 'ESP32 完整备份必须为 8,388,608 字节。')
    check(content[0] == 0xE9 and int.from_bytes(content[12:14], 'little') == 9,
          'ESP32 Bootloader 不兼容，需要完整工厂包恢复。')
    table = content[0x8000:0x9000]
    parts, checksum = [], False
    for pos in range(0, len(table), 32):
        entry = table[pos:pos + 32]
        magic = struct.unpack_from('<H', entry)[0]
        if magic == 0xEBEB:
            checksum = hashlib.md5(table[:pos]).digest() == entry[16:32]
            break
        if magic != 0x50AA:
            break
        _, kind, subtype, offset, size, label, flags = struct.unpack('<HBBII16sI', entry)
        check(size > 0 and offset >= 0x9000 and offset + size <= ESP_SIZE and not flags & 1,
              '分区越界或启用了分区加密，停止。')
        check(all(offset + size <= p['offset'] or p['offset'] + p['size'] <= offset for p in parts),
              '分区存在重叠，停止。')
        parts.append({'type': kind, 'subtype': subtype, 'offset': offset, 'size': size,
                      'label': label.split(b'\0', 1)[0].decode('ascii', errors='replace')})
    check(checksum, 'ESP32 分区表 MD5 校验失败，需要完整工厂包恢复。')
    check(sum(p['type'] == 0 and p['subtype'] == 0x10 and p['offset'] == 0x10000
              and p['size'] >= firmware_size for p in parts) == 1, 'ESP32 app0 分区不兼容。')
    check(sum(p['type'] == 1 and p['subtype'] == 0 and p['offset'] == 0xE000
              and p['size'] == 0x2000 for p in parts) == 1, 'ESP32 OTA 选择区不兼容。')
    return parts


def flash_sequence(run, firmware):
    # Never erase otadata after a failed write or verify.
    run('write_flash', ('0x10000', str(firmware)))
    run('verify_flash', ('0x10000', str(firmware)))
    run('erase_region', ('0xe000', '0x2000'), after='hard_reset')


def sync_directory(directory):
    if os.name != 'nt':
        fd = os.open(str(directory), os.O_RDONLY)
        try:
            try:
                os.fsync(fd)
            except OSError as exc:
                # Some FAT implementations cannot fsync a directory. The wizard
                # still requires OS safe removal before the board is restarted.
                if exc.errno not in (22, 45, 95):
                    raise
        finally:
            os.close(fd)


class Updater:
    def __init__(self, root, data, tool_python=None):
        self.root, self.data = Path(root).resolve(), Path(data).resolve()
        self.manifest = json.loads((self.root / 'manifest.json').read_text(encoding='utf-8'))
        self.package_id = digest(self.root / 'manifest.json')
        self.records = self.data / 'records'
        self.pointer = self.data / 'current-session.json'
        self.tool_python = str(tool_python) if tool_python else None

    def sources(self):
        result = {}
        for name in ('esp32', 'core', 'bootloader'):
            item = self.manifest[name]
            path = (self.root / item['path']).resolve()
            check(path.is_relative_to(self.root) and path.is_file(), '固件路径无效：' + name)
            check(path.stat().st_size == item['size'] and digest(path) == item['sha256'],
                  '固件大小或 SHA-256 不符：' + name)
            check(0 < item['size'] <= item.get('max_size', item['size']), '固件超出大小限制：' + name)
            header = path.read_bytes()[:24]
            if name == 'esp32':
                check(header[0] == 0xE9 and int.from_bytes(header[12:14], 'little') == 9,
                      '固件不是 ESP32-S3 应用镜像。')
            else:
                stack, reset = struct.unpack('<II', header[:8])
                low, high = ((0x08100000, 0x081E0000) if name == 'core'
                             else (0x08000000, 0x08100000))
                check(0x20000000 <= stack < 0x40000000 and reset & 1 and low <= reset & ~1 < high,
                      'STM32 向量表与固件类型不符：' + name)
            result[name] = path
        return result

    def save(self, session, phase=None, **details):
        if phase:
            check(phase in PHASES, '更新阶段无效。')
            session['phase'] = phase
        session.update(details)
        session['updated'] = now()
        atomic_json(self.record_dir(session) / 'update-status.json', session)

    def record_dir(self, session):
        directory = (self.records / session['id']).resolve()
        check(directory.is_relative_to(self.records.resolve()) and directory != self.records.resolve(),
              '更新记录路径无效。')
        return directory

    def current(self):
        if not self.pointer.exists():
            return None
        pointer = json.loads(self.pointer.read_text(encoding='utf-8'))
        path = self.record_dir(pointer) / 'update-status.json'
        session = json.loads(path.read_text(encoding='utf-8'))
        check(session['id'] == pointer['id'] and session['phase'] in PHASES, '更新记录无效。')
        check(session['package_id'] == self.package_id and session['tool_version'] == VERSION,
              '这份记录属于另一套更新包。请使用原更新包恢复，不要混用固件或删除记录。')
        return session

    def require_device(self, session):
        device = hardware.inventory()
        check(hardware.same_device(session, device), '当前板卡与更新记录不一致，请接回原板卡。')
        return device

    def start(self):
        device = hardware.inventory()
        old = self.current()
        if old and old['phase'] != 'complete':
            check(hardware.same_device(old, device), '还有一块板卡尚未更新完成，请接回原板卡继续。')
            print('发现未完成任务，当前阶段：' + old['phase'])
            check(input('核对原板卡后输入 CONTINUE 继续：').strip() == 'CONTINUE', '未确认继续。')
            return old
        check(not list(device['update_dir'].iterdir()), 'SD 更新目录非空，先联系维护人员确认待处理文件。')
        print('ESP32 MAC：' + device['mac'] + '\nSTM32：' + device['stm_serial'])
        print('板卡 SD 卡：' + device['sd_root'])
        if old:
            check(input('上一块已完成。建立新任务请输入 NEW：').strip() == 'NEW', '未建立新任务。')
        check(input('对照板卡标签或 ESP32 热点，输入 MAC 后 4 位：').strip().lower() == device['mac'][-4:],
              'MAC 后缀不匹配。')
        check(input('确认本板卡按 1.x → 2.x 迁移流程维护，输入 UPDATE 开始：').strip() == 'UPDATE',
              '未确认开始更新。')
        session = {k: device[k] for k in ('mac', 'stm_serial', 'sd_serial')}
        session.update(id=device['mac'] + '/' + datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8],
                       created=now(), package_id=self.package_id, tool_version=VERSION,
                       package_version=self.manifest['package_version'])
        self.save(session, 'checked')
        atomic_json(self.pointer, {'id': session['id']})
        return session

    def run_esptool(self, session, action, args=(), before='no_reset', after='no_reset', baud='460800', timeout=180):
        check(self.tool_python, '缺少隔离烧录环境，请使用启动脚本。')
        deadline = time.monotonic() + 15
        while True:
            try:
                port = hardware.matching_port(session['mac'])
                break
            except StopUpdate:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.5)
        command = [self.tool_python, '-u', '-m', 'esptool', '--chip', 'esp32s3', '--port', port,
                   '--baud', baud, '--before', before, '--after', after, action] + list(args)
        log = self.record_dir(session) / (datetime.now().strftime('%H%M%S-%f') + '-' + action + '.log')
        events, output = queue.Queue(), []
        # Open log before touching hardware. Disk-full or permission errors must
        # not launch an unrecorded flash operation.
        with log.open('w', encoding='utf-8') as stream:
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       text=True, encoding='utf-8', errors='replace')

            def read():
                try:
                    for line in process.stdout:
                        events.put(line)
                finally:
                    events.put(None)

            reader = threading.Thread(target=read, daemon=True)
            reader.start()
            started, progress = time.monotonic(), -1
            print('\n[执行] ' + action + '，端口 ' + port, flush=True)
            try:
                while True:
                    check(time.monotonic() - started <= timeout, '烧录命令超时，请保留日志后重试。')
                    try:
                        line = events.get(timeout=0.5)
                    except queue.Empty:
                        continue
                    if line is None:
                        break
                    stream.write(line)
                    stream.flush()
                    output.append(line)
                    macs = re.findall(r'MAC:\s*([0-9a-f:]+)', line, re.I)
                    check(all(hardware.norm_mac(m) == session['mac'] for m in macs), '串口报告的 MAC 与目标不一致。')
                    match = re.search(r'\((\d+)\s*%\)', line)
                    if match:
                        value = int(match[1])
                        if value >= progress + 10 or value == 100:
                            print('  进度 ' + str(value) + '%', flush=True)
                            progress = value
                    else:
                        print('  ' + line.strip(), flush=True)
                process.wait(timeout=10)
                check(process.returncode == 0, '命令失败，后续未执行。日志：' + str(log))
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=10)
                reader.join(timeout=3)
                process.stdout.close()
        text = ''.join(output)
        check(any(hardware.norm_mac(m) == session['mac'] for m in re.findall(r'MAC:\s*([0-9a-f:]+)', text, re.I)),
              '无法从烧录日志确认设备 MAC，停止。日志：' + str(log))
        return text

    def backup(self, session):
        self.require_device(session)
        check(session['phase'] in ('checked', 'backup_running'), '当前不是备份阶段。')
        info = self.run_esptool(session, 'flash_id', before='default_reset', baud='115200')
        check('Detected flash size: 8MB' in info, 'ESP32 Flash 容量不是 8 MB，停止。')
        security = self.run_esptool(session, 'get_security_info', baud='115200')
        check('Secure Boot: Disabled' in security and 'Flash Encryption: Disabled' in security,
              'ESP32 安全启动或 Flash 加密未确认关闭，停止。')
        backup_name = 'flash-before-update-' + uuid.uuid4().hex + '.bin'
        self.save(session, 'backup_running', backup_name=backup_name)
        path = self.record_dir(session) / backup_name
        self.run_esptool(session, 'read_flash', ('0x0', '0x800000', str(path)), baud='921600', timeout=600)
        parts = validate_layout(path.read_bytes(), self.manifest['esp32']['size'])
        self.save(session, 'backup_verified', backup_sha256=digest(path), partitions=parts)
        print('[成功] ESP32 备份和分区检查通过。')

    def flash(self, session):
        self.require_device(session)
        check(session['phase'] in ('backup_verified', 'esp32_flash_running'), '请先完成备份。')
        path = (self.record_dir(session) / session['backup_name']).resolve()
        check(path.parent == self.record_dir(session) and path.is_file()
              and path.stat().st_size == ESP_SIZE and digest(path) == session['backup_sha256'],
              '原始 ESP32 备份校验失败，停止。')
        interrupted = session['phase'] == 'esp32_flash_running'
        self.save(session, 'esp32_flash_running')
        # Rewriting the same app is deliberate on recovery; never trust the
        # old backup as evidence of what a partially completed flash now holds.
        flash_sequence(lambda action, args, **kw: self.run_esptool(
            session, action, args, before='default_reset' if interrupted and action == 'write_flash' else 'no_reset', **kw),
                       self.sources()['esp32'])
        deadline = time.monotonic() + 45
        verified = False
        while time.monotonic() < deadline:
            try:
                device = self.require_device(session)
                verified = any((root / 'FIRMWARE.BIN').is_file()
                               and digest(root / 'FIRMWARE.BIN') == self.manifest['esp32']['sha256']
                               for root in device['firmware_roots'])
                if verified:
                    break
            except (StopUpdate, OSError):
                pass
            time.sleep(1)
        check(verified, 'ESP32 写入后启动读回尚未确认。请检查固件盘是否挂载，保留记录并重新运行。')
        self.save(session, 'esp32_verified', esp32_runtime_readback_verified=True)
        print('[成功] ESP32 写入、校验及启动后的固件读回通过。')

    def stage(self, session, name):
        boot = name == 'bootloader'
        prior = 'core_verified' if boot else 'esp32_verified'
        prefix = 'boot' if boot else 'core'
        check(session['phase'] in (prior, prefix + '_copying'), '更新顺序不正确。')
        device = self.require_device(session)
        source = self.sources()[name]
        folder = device['update_dir']
        filename = 'BYHUAV_Bootloader.bin' if boot else 'BYHUAV.bin'
        target = folder / filename
        if session['phase'] == prior:
            check(not list(folder.iterdir()), '更新目录不为空，停止；不要混放 Core 和 Bootloader。')
            if boot:
                check(input('即将更新 Bootloader，请确认持续稳定供电，输入 UPDATE-BOOT：').strip() == 'UPDATE-BOOT',
                      '未确认 Bootloader 更新。')
            self.save(session, prefix + '_copying', pending_name='.byhuav-' + uuid.uuid4().hex + '.pending')
        pending = folder / session['pending_name']
        check(pending.name.startswith('.byhuav-') and pending.suffix == '.pending', '暂存记录无效。')
        present = set(folder.iterdir())
        check(present <= {pending, target} and not (pending.exists() and target.exists()),
              '更新目录出现未知或冲突文件，请保留现场并联系维护人员。')
        if target.exists():
            check(not target.is_symlink() and digest(target) == self.manifest[name]['sha256'],
                  '已暂存固件不完整或被更改。不要重启板卡，请联系维护人员。')
        else:
            if pending.exists():
                check(not pending.is_symlink() and pending.is_file(), '暂存文件无效。')
                # Only this transaction's uniquely named incomplete file is
                # removed. Other files are never overwritten or deleted.
                if digest(pending) != self.manifest[name]['sha256']:
                    pending.unlink()
            if not pending.exists():
                with source.open('rb') as src, pending.open('xb') as dst:
                    shutil.copyfileobj(src, dst)
                    dst.flush()
                    os.fsync(dst.fileno())
            check(digest(pending) == self.manifest[name]['sha256'], '暂存固件校验失败，不要重启板卡。')
            check(not target.exists(), '目标文件已存在，停止。')
            pending.rename(target)
        sync_directory(folder)
        check(digest(target) == self.manifest[name]['sha256'], '复制后校验失败，不要重启板卡。')
        self.save(session, prefix + '_staged', **{prefix + '_copy_verified': True})
        print('[成功] ' + name + ' 已复制并校验；接下来按提示重启板卡。')

    def accept(self, session, boot=False):
        check(session['phase'] == ('boot_staged' if boot else 'core_staged'), '尚未完成固件暂存。')
        device = self.require_device(session)
        name = 'bootloader' if boot else 'core'
        filename = 'BYHUAV_Bootloader.bin' if boot else 'BYHUAV.bin'
        target = device['update_dir'] / filename
        contents = set(device['update_dir'].iterdir())
        check(contents <= {target}, '更新目录存在其他文件，不要重启板卡。')
        if target.exists():
            check(not target.is_symlink() and digest(target) == self.manifest[name]['sha256'],
                  '已暂存固件被更改或损坏，不要重启板卡。')
            print('\n请按顺序完成以下操作：')
            print('1. 确认板卡有独立稳定供电；在系统中安全弹出板卡 SD 卡：' + device['sd_root'])
            print('2. 拔掉电脑连接 U29 的维护 USB，再按设备流程重启 STM32 主控板。')
            print('3. 保持供电，至少等待 ' + ('30' if boot else '15') + ' 秒，期间不要插回 USB。')
        else:
            print('\n暂存文件已消失。请核对实际更新结果，不会仅凭文件消失认定更新成功。')
        if boot:
            print('Bootloader 灯光应为：紫 → 红 → 蓝闪 → 绿闪 → 紫。')
            print('停在黄色时停止；不要反复断电或重刷，联系维护人员。')
        item = self.manifest['core']
        print('用手机连接本板 ESP32 Wi-Fi（不是路由器 Wi-Fi），打开 http://192.168.4.1/')
        print('核对 STM32 已连接，MCU 为 ' + item['version'] + '（' + item['build'] + '），运行时间增加、传感器有数据。')
        print('验收通过后，再把 U29 USB 接回电脑，并等待板卡 SD 卡重新出现。')
        word = 'BOOT-OK' if boot else 'CORE-OK'
        check(input('全部完成后输入 ' + word + '：').strip() == word, '尚未确认验收，进度已保留。')
        device = self.require_device(session)
        check(not list(device['update_dir'].iterdir()), 'SD 更新目录仍有文件，本次更新尚未完成。')
        if not boot:
            self.save(session, 'core_verified', core_web_confirmed=True)
            return
        log = Path(device['sd_root']) / 'BYHUAV' / 'sysLog.txt'
        if log.is_file():
            shutil.copyfile(log, self.record_dir(session) / 'stm32-sysLog-final.txt')
        self.save(session, 'complete', bootloader_operational_acceptance=True,
                  bootloader_version_readback_verified=False)
        self.report(session)

    def report(self, session):
        esp, core = self.manifest['esp32'], self.manifest['core']
        text = ('BYHUAV 1.x → 2.x 固件迁移验收记录\n'
                + 'ESP32 MAC：' + session['mac'] + '\n'
                + 'ESP32：' + esp['version'] + '（' + esp['build'] + '），烧录及启动后读回校验通过\n'
                + 'Core：' + core['version'] + '（' + core['build'] + '），SD 复制校验及人工网页验收通过\n'
                + 'Bootloader：SD 复制校验、人工灯光及应用恢复验收通过；未查询版本\n'
                + '本工具处理 ESP32、STM32 固件。系统镜像和设备注册按各自教程完成。\n')
        (self.record_dir(session) / '更新结果.txt').write_text(text, encoding='utf-8')

    def run(self):
        self.sources()
        print('BYHUAV 板卡 1.x → 2.x 固件更新向导\n')
        print('请停止采集、拆除桨叶、保持独立供电；每次只连接一块板卡到 U29。')
        print('完整 Flash 备份、日志和更新记录保存在：' + str(self.records))
        session = self.start()
        actions = {'checked': self.backup, 'backup_running': self.backup,
                   'backup_verified': self.flash, 'esp32_flash_running': self.flash,
                   'esp32_verified': lambda s: self.stage(s, 'core'),
                   'core_copying': lambda s: self.stage(s, 'core'),
                   'core_staged': self.accept,
                   'core_verified': lambda s: self.stage(s, 'bootloader'),
                   'boot_copying': lambda s: self.stage(s, 'bootloader'),
                   'boot_staged': lambda s: self.accept(s, boot=True)}
        while session['phase'] != 'complete':
            actions[session['phase']](session)
        self.report(session)
        print('\n[完成] 本次固件迁移验收完成。记录：' + str(self.record_dir(session)))
        print('请安全弹出板卡 SD 卡，再拔掉维护 USB，恢复正常连接。')
