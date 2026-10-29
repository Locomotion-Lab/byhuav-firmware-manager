"""OS inventories and serial control lines; no firmware, disk or GPIO writes."""
import json
import os
from pathlib import Path
import plistlib
import re
import sys

from .common import StopUpdate, capture, check

SD_MODEL = 'BYHUAV SD Flash Disk'
ESP_MODEL = 'ESP32 Firmware MSC'


def norm_mac(value):
    return re.sub('[^0-9a-f]', '', str(value or '').lower())


def model_name(value):
    return ' '.join(str(value or '').replace('_', ' ').split()).removesuffix(' Media')


def disk_record(node, model, serial, size, roots):
    return {'node': node, 'model': model_name(model), 'serial': str(serial or '').strip(),
            'size': int(size or 0), 'roots': list(dict.fromkeys(str(r) for r in roots if r))}


def windows_disks():
    script = r"""$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false)
$items=@(foreach($disk in (Get-Disk | Where-Object {$_.BusType -eq 'USB'})) {
  $roots=@($disk | Get-Partition | Where-Object {$_.DriveLetter} | ForEach-Object {$_.DriveLetter.ToString()+':\'})
  [pscustomobject]@{node=$disk.Number;model=$disk.FriendlyName;serial=[string]$disk.SerialNumber;size=$disk.Size;roots=$roots}
})
ConvertTo-Json -InputObject $items -Depth 5 -Compress
"""
    data = json.loads(capture(['powershell.exe', '-NoProfile', '-Command', script]))
    return [disk_record(**item) for item in data]


def linux_disk_records(data):
    result = []

    def mounts(item):
        found = [item.get('mountpoint')]
        for child in item.get('children', []):
            found.extend(mounts(child))
        return found

    for item in data['blockdevices']:
        if item.get('type') != 'disk' or item.get('tran') != 'usb':
            continue
        model = model_name(item.get('model'))
        vendor = model_name(item.get('vendor'))
        if model not in (SD_MODEL, ESP_MODEL):
            model = model_name(vendor + ' ' + model)
        result.append(disk_record(item['name'], model, item.get('serial'), item['size'], mounts(item)))
    return result


def linux_disks():
    data = json.loads(capture(['lsblk', '--json', '--bytes', '--paths', '--output',
                              'NAME,TYPE,TRAN,VENDOR,MODEL,SERIAL,SIZE,MOUNTPOINT']))
    return linux_disk_records(data)


def mac_usb_identity(tree):
    """Associate BSD disk names with their nearest USB device's identity."""
    disks = {}

    def visit(item, identity=None):
        if isinstance(item, list):
            for child in item:
                visit(child, identity)
        elif isinstance(item, dict):
            # Storage driver/interface nodes repeat the USB product name but
            # often omit the serial number. Keep the physical parent's serial.
            # A new physical USB device always starts a new identity, even if
            # it has no serial, so a hub's identity is never reused for it.
            if item.get('IOObjectClass') == 'IOUSBHostDevice' or 'USB Serial Number' in item:
                identity = {'serial': item.get('USB Serial Number', ''),
                            'model': item.get('USB Product Name', '')}
            if identity and 'Vendor Identification' in item and 'Product Identification' in item:
                identity = dict(identity, media_model=model_name(
                    item['Vendor Identification'] + ' ' + item['Product Identification']))
            characteristics = item.get('Device Characteristics', {})
            if identity and characteristics.get('Vendor Name') and characteristics.get('Product Name'):
                identity = dict(identity, media_model=model_name(
                    characteristics['Vendor Name'] + ' ' + characteristics['Product Name']))
            if item.get('BSD Name') and identity:
                disks[item['BSD Name']] = identity
            for child in item.values():
                if isinstance(child, (dict, list)):
                    visit(child, identity)

    visit(tree)
    return disks


def mac_disks():
    usb = mac_usb_identity(plistlib.loads(capture(
        ['ioreg', '-a', '-l', '-p', 'IOService', '-r', '-c', 'IOUSBHostDevice'], binary=True)))
    listing = plistlib.loads(capture(['diskutil', 'list', '-plist', 'external', 'physical'], binary=True))
    result = []
    for item in listing.get('AllDisksAndPartitions', []):
        name = item['DeviceIdentifier']
        info = plistlib.loads(capture(['diskutil', 'info', '-plist', name], binary=True))
        if info.get('Internal') is not False or info.get('BusProtocol') != 'USB':
            continue
        identity = usb.get(name, {})
        roots = [info.get('MountPoint')]
        for partition in item.get('Partitions', []):
            detail = plistlib.loads(capture(
                ['diskutil', 'info', '-plist', partition['DeviceIdentifier']], binary=True))
            roots.append(detail.get('MountPoint'))
        # The USB composite product (e.g. STM32 Mass Storage) is different
        # from the SCSI media product (BYHUAV SD Flash Disk).
        result.append(disk_record('/dev/' + name, identity.get('media_model') or info.get('MediaName') or identity.get('model'),
                                  identity.get('serial'), info.get('TotalSize') or info.get('DiskSize'), roots))
    return result


def disks():
    check(sys.platform in ('win32', 'linux', 'darwin'), '只支持 Windows、Linux 和 macOS。')
    return {'win32': windows_disks, 'linux': linux_disks, 'darwin': mac_disks}[sys.platform]()


def select_sd(items):
    candidates = [d for d in items if d['model'] == SD_MODEL
                  and 28_000_000_000 <= d['size'] <= 35_000_000_000]
    check(len(candidates) == 1, '必须识别到唯一一张约 32 GB 的 BYHUAV SD 卡；请检查 U29 数据线。')
    disk = candidates[0]
    check(disk['serial'], '无法读取板卡 SD 设备序列号，停止；请保留检查结果联系维护人员。')
    check(len(disk['roots']) == 1, '板卡 SD 卡未挂载或挂载点不唯一；请用系统文件管理器打开它后重试。')
    root = Path(disk['roots'][0])
    update = root / 'BYHUAV' / 'FirmwareUpdate'
    check(root.is_dir() and update.is_dir(), 'SD 卡缺少原有 BYHUAV/FirmwareUpdate 目录，停止；不要手工新建。')
    check(not root.is_symlink() and not (root / 'BYHUAV').is_symlink() and not update.is_symlink(),
          '板卡更新目录不能是符号链接。')
    check(os.access(update, os.W_OK), '板卡 SD 更新目录不可写，请检查挂载权限或只读状态。')
    return disk, update


def serial_ports():
    from serial.tools import list_ports
    return list(list_ports.comports())


def inventory():
    ports = serial_ports()
    esp = [p for p in ports if (p.vid, p.pid) == (0x303A, 0x1001)]
    stm = [p for p in ports if (p.vid, p.pid) == (0x0483, 0x572A)]
    check(len(esp) == 1 and len(stm) == 1,
          '请只连接一块板卡，并确认 U29 已同时提供 ESP32、STM32 串口。Linux 用户还需检查串口权限。')
    mac = norm_mac(esp[0].serial_number)
    check(len(mac) == 12 and stm[0].serial_number, '无法读取 ESP32/STM32 序列号，请检查连接。')
    if sys.platform != 'win32':
        check(all(os.access(p.device, os.R_OK | os.W_OK) for p in (esp[0], stm[0])),
              '没有串口读写权限。Linux 请按教程配置设备权限并重新登录；不要 sudo 运行更新工具。')
    items = disks()
    sd, update = select_sd(items)
    firmware = [d for d in items if d['model'] == ESP_MODEL]
    check(len(firmware) <= 1, '识别到多块 ESP32 固件盘，请只连接一块板卡。')
    return {'mac': mac, 'esp_port': esp[0].device, 'stm_port': stm[0].device,
            'stm_serial': stm[0].serial_number, 'sd_serial': sd['serial'],
            'sd_root': sd['roots'][0], 'sd_node': sd['node'], 'update_dir': update,
            'firmware_roots': [Path(r) for d in firmware for r in d['roots']]}


def same_device(session, device):
    return all(session[key] == device[key] for key in ('mac', 'stm_serial', 'sd_serial'))


def matching_port(mac):
    ports = [p for p in serial_ports() if p.vid == 0x303A and norm_mac(p.serial_number) == mac]
    check(len(ports) == 1, '目标 ESP32 串口未恢复；请检查连接，必要时按 BOOT + RESET 进入下载模式后重试。')
    return ports[0].device


def release_port_controls(mac):
    """Release native USB DTR/RTS after esptool, without sending serial data."""
    import serial
    port = serial.Serial(port=None, baudrate=115200, timeout=0.2)
    port.dtr = False
    port.rts = False
    try:
        port.port = matching_port(mac)
        port.open()
        return True
    except (serial.SerialException, OSError, StopUpdate):
        # Re-enumeration can disconnect this handle. Only the subsequent
        # firmware readback is authoritative about successful startup.
        return False
    finally:
        if port.is_open:
            port.close()
