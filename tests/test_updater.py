import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import struct
import tempfile
import unittest
from unittest.mock import patch

from byhuav_updater.common import StopUpdate, atomic_json, digest
from byhuav_updater.engine import Updater, ESP_SIZE, flash_sequence, validate_layout
from byhuav_updater import hardware, VERSION
from byhuav_updater.runtime import file_lock

ROOT = Path(__file__).resolve().parents[1]


def flash_fixture(app_size=0x180000, overlap=False):
    content = bytearray(b'\xff' * ESP_SIZE)
    content[0], content[12:14] = 0xE9, b'\x09\x00'
    table = b''
    for kind, subtype, offset, size, label in [
        (1, 2, 0x9000, 0x5000, b'nvs'),
        (1, 0, 0xE000, 0x2000, b'otadata'),
        (0, 0x10, 0x10000, app_size, b'app0'),
    ]:
        if overlap and label == b'nvs':
            size = 0x6000
        table += struct.pack('<HBBII16sI', 0x50AA, kind, subtype, offset, size, label, 0)
    table += b'\xeb\xeb' + b'\xff' * 14 + hashlib.md5(table).digest()
    content[0x8000:0x8000 + len(table)] = table
    return content


class FirmwareTests(unittest.TestCase):
    def test_shipped_sources(self):
        self.assertEqual(set(Updater(ROOT, ROOT / 'unused').sources()), {'core', 'bootloader', 'esp32'})

    def test_compatible_layout(self):
        self.assertEqual(len(validate_layout(flash_fixture(), 885520)), 3)

    def test_truncated_backup(self):
        with self.assertRaises(StopUpdate):
            validate_layout(b'x' * 64, 885520)

    def test_corrupt_table(self):
        content = flash_fixture()
        content[0x8008] ^= 1
        with self.assertRaises(StopUpdate):
            validate_layout(content, 885520)

    def test_small_app(self):
        with self.assertRaises(StopUpdate):
            validate_layout(flash_fixture(0x1000), 885520)

    def test_overlapping_partitions(self):
        with self.assertRaises(StopUpdate):
            validate_layout(flash_fixture(overlap=True), 885520)

    def test_wrong_chip(self):
        content = flash_fixture()
        content[12] = 0
        with self.assertRaises(StopUpdate):
            validate_layout(content, 885520)

    def test_flash_sequence_order(self):
        calls = []
        flash_sequence(lambda *a, **kw: calls.append((a, kw)), Path('firmware.bin'))
        self.assertEqual([c[0][0] for c in calls], ['write_flash', 'verify_flash', 'erase_region'])
        self.assertEqual(calls[-1][0][1], ('0xe000', '0x2000'))
        self.assertEqual(calls[-1][1], {'after': 'hard_reset'})

    def test_failed_write_or_verify_does_not_erase_ota(self):
        for failure in ('write_flash', 'verify_flash'):
            with self.subTest(failure=failure):
                calls = []

                def run(action, *args, **kw):
                    calls.append(action)
                    if action == failure:
                        raise StopUpdate('simulated')

                with self.assertRaises(StopUpdate):
                    flash_sequence(run, Path('firmware.bin'))
                self.assertNotIn('erase_region', calls)


class HardwareTests(unittest.TestCase):
    def test_linux_usb_disk_and_vendor_model(self):
        data = {'blockdevices': [
            {'name': '/dev/sda', 'type': 'disk', 'tran': 'sata', 'model': hardware.SD_MODEL,
             'vendor': '', 'size': 32000000000, 'serial': 'internal'},
            {'name': '/dev/sdb', 'type': 'disk', 'tran': 'usb', 'model': 'SD_Flash_Disk',
             'vendor': 'BYHUAV', 'size': 32000000000, 'serial': 'sd',
             'children': [{'mountpoint': '/media/user/BYHUAV'}]},
        ]}
        disks = hardware.linux_disk_records(data)
        self.assertEqual(len(disks), 1)
        self.assertEqual(disks[0]['model'], hardware.SD_MODEL)
        self.assertEqual(disks[0]['roots'], ['/media/user/BYHUAV'])

    def test_mac_child_disk_keeps_nearest_usb_identity(self):
        tree = [{'USB Serial Number': 'hub', 'USB Product Name': 'hub', 'IORegistryEntryChildren': [
            {'USB Serial Number': 'sd', 'USB Product Name': hardware.SD_MODEL,
             'IORegistryEntryChildren': [{'IORegistryEntryChildren': [{'BSD Name': 'disk4'}]}]},
            {'USB Serial Number': 'esp', 'USB Product Name': hardware.ESP_MODEL,
             'IORegistryEntryChildren': [{'BSD Name': 'disk5'}]},
        ]}]
        result = hardware.mac_usb_identity(tree)
        self.assertEqual(result['disk4']['serial'], 'sd')
        self.assertEqual(result['disk5']['serial'], 'esp')

    def test_mac_inventory_parses_plist(self):
        tree = [{'USB Serial Number': 'SD123', 'USB Product Name': hardware.SD_MODEL,
                 'IORegistryEntryChildren': [{'BSD Name': 'disk8'}]}]
        listing = {'AllDisksAndPartitions': [{'DeviceIdentifier': 'disk8',
                   'Partitions': [{'DeviceIdentifier': 'disk8s1'}]}]}

        def capture(command, binary=False):
            if command[0] == 'ioreg':
                obj = tree
            elif command[1] == 'list':
                obj = listing
            elif command[-1] == 'disk8':
                obj = {'Internal': False, 'BusProtocol': 'USB', 'TotalSize': 32000000000}
            else:
                obj = {'MountPoint': '/Volumes/BYHUAV'}
            return plistlib.dumps(obj)

        with patch.object(hardware, 'capture', side_effect=capture):
            result = hardware.mac_disks()
        self.assertEqual(result[0]['serial'], 'SD123')
        self.assertEqual(result[0]['roots'], ['/Volumes/BYHUAV'])

    def test_windows_empty_and_single_disks(self):
        item = {'node': 2, 'model': hardware.SD_MODEL, 'serial': ' sd ',
                'size': 32000000000, 'roots': ['E:\\']}
        with patch.object(hardware, 'capture', return_value=json.dumps([item])):
            self.assertEqual(hardware.windows_disks()[0]['serial'], 'sd')
        with patch.object(hardware, 'capture', return_value='[]'):
            self.assertEqual(hardware.windows_disks(), [])

    def test_identity_rejects_changed_components(self):
        identity = {'mac': 'aabbccddeeff', 'stm_serial': 'stm', 'sd_serial': 'sd'}
        self.assertTrue(hardware.same_device(identity, identity))
        for key in identity:
            self.assertFalse(hardware.same_device(identity, dict(identity, **{key: 'other'})))

    def test_model_name_normalization(self):
        self.assertEqual(hardware.model_name(' BYHUAV SD_Flash Disk Media '), hardware.SD_MODEL)


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.updater = Updater(ROOT, self.root / 'data')
        self.folder = self.root / 'SD' / 'BYHUAV' / 'FirmwareUpdate'
        self.folder.mkdir(parents=True)
        self.session = {'id': 'aabbccddeeff/test', 'phase': 'esp32_verified',
                        'package_id': self.updater.package_id, 'tool_version': VERSION,
                        'mac': 'aabbccddeeff', 'stm_serial': 'stm', 'sd_serial': 'sd'}
        self.device = dict(self.session, update_dir=self.folder, sd_root=str(self.folder.parents[1]))
        self.patch = patch.object(self.updater, 'require_device', return_value=self.device)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.updater.save(self.session)
        atomic_json(self.updater.pointer, {'id': self.session['id']})

    def test_session_round_trip(self):
        self.assertEqual(self.updater.current()['phase'], 'esp32_verified')

    def test_mixed_package_rejected(self):
        self.session['package_id'] = 'another'
        self.updater.save(self.session)
        with self.assertRaises(StopUpdate):
            self.updater.current()

    def test_other_tool_version_rejected(self):
        self.session['tool_version'] = 'other'
        self.updater.save(self.session)
        with self.assertRaises(StopUpdate):
            self.updater.current()

    def test_record_path_escape_rejected(self):
        with self.assertRaises(StopUpdate):
            self.updater.record_dir({'id': '../../elsewhere'})

    def test_select_sd_requires_existing_directory(self):
        disk = hardware.disk_record('/dev/test', hardware.SD_MODEL, 'sd', 32000000000,
                                    [self.folder.parents[1]])
        self.assertEqual(hardware.select_sd([disk])[1], self.folder)
        self.folder.rmdir()
        with self.assertRaises(StopUpdate):
            hardware.select_sd([disk])

    def test_select_sd_rejects_ambiguous_disk(self):
        disk = hardware.disk_record('test', hardware.SD_MODEL, 'sd', 32000000000, [self.folder.parents[1]])
        with self.assertRaises(StopUpdate):
            hardware.select_sd([disk, disk])

    def test_select_sd_rejects_no_identity_or_mount(self):
        for serial, roots in [('', [self.folder.parents[1]]), ('sd', [])]:
            with self.subTest(serial=serial, roots=roots), self.assertRaises(StopUpdate):
                hardware.select_sd([hardware.disk_record('test', hardware.SD_MODEL, serial, 32000000000, roots)])

    def test_stage_preserves_unknown_files(self):
        unknown = self.folder / 'other.bin'
        unknown.write_bytes(b'user file')
        with self.assertRaises(StopUpdate):
            self.updater.stage(self.session, 'core')
        self.assertEqual(unknown.read_bytes(), b'user file')

    def test_stage_and_hash(self):
        self.updater.stage(self.session, 'core')
        self.assertEqual(self.session['phase'], 'core_staged')
        self.assertEqual(digest(self.folder / 'BYHUAV.bin'), self.updater.manifest['core']['sha256'])
        self.assertEqual(len(list(self.folder.iterdir())), 1)

    def test_interrupted_pending_copy_recovered(self):
        self.session.update(phase='core_copying', pending_name='.byhuav-test.pending')
        (self.folder / self.session['pending_name']).write_bytes(b'incomplete')
        self.updater.stage(self.session, 'core')
        self.assertEqual(self.session['phase'], 'core_staged')
        self.assertFalse((self.folder / self.session['pending_name']).exists())

    def test_crash_after_rename_recovered(self):
        self.session.update(phase='core_copying', pending_name='.byhuav-test.pending')
        shutil.copyfile(self.updater.sources()['core'], self.folder / 'BYHUAV.bin')
        self.updater.stage(self.session, 'core')
        self.assertEqual(self.session['phase'], 'core_staged')

    def test_corrupt_renamed_target_is_never_overwritten(self):
        self.session.update(phase='core_copying', pending_name='.byhuav-test.pending')
        target = self.folder / 'BYHUAV.bin'
        target.write_bytes(b'damaged')
        with self.assertRaises(StopUpdate):
            self.updater.stage(self.session, 'core')
        self.assertEqual(target.read_bytes(), b'damaged')

    def test_boot_before_core_verification_rejected(self):
        with self.assertRaises(StopUpdate):
            self.updater.stage(self.session, 'bootloader')

    def test_boot_confirmation_required(self):
        self.session['phase'] = 'core_verified'
        with patch('builtins.input', return_value='no'), self.assertRaises(StopUpdate):
            self.updater.stage(self.session, 'bootloader')
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_accept_rejects_file_not_consumed(self):
        self.updater.stage(self.session, 'core')
        with patch('builtins.input', return_value='CORE-OK'), self.assertRaises(StopUpdate):
            self.updater.accept(self.session)
        self.assertEqual(self.session['phase'], 'core_staged')

    def test_consumption_alone_does_not_accept(self):
        self.session['phase'] = 'core_staged'
        with patch('builtins.input', return_value=''), self.assertRaises(StopUpdate):
            self.updater.accept(self.session)
        self.assertEqual(self.session['phase'], 'core_staged')

    def test_core_acceptance_requires_user_and_empty_dir(self):
        self.session['phase'] = 'core_staged'
        with patch('builtins.input', return_value='CORE-OK'):
            self.updater.accept(self.session)
        self.assertEqual(self.session['phase'], 'core_verified')

    def test_boot_acceptance_records_limits(self):
        self.session['phase'] = 'boot_staged'
        with patch('builtins.input', return_value='BOOT-OK'):
            self.updater.accept(self.session, boot=True)
        self.assertEqual(self.session['phase'], 'complete')
        self.assertFalse(self.session['bootloader_version_readback_verified'])
        self.assertTrue((self.updater.record_dir(self.session) / '更新结果.txt').is_file())

    def test_interrupted_flash_retries_write_verify_erase(self):
        path = self.updater.record_dir(self.session) / 'original.bin'
        path.write_bytes(flash_fixture())
        self.session.update(phase='esp32_flash_running', backup_name=path.name, backup_sha256=digest(path))
        runtime = self.root / 'firmware-disk'
        runtime.mkdir()
        shutil.copyfile(self.updater.sources()['esp32'], runtime / 'FIRMWARE.BIN')
        self.device['firmware_roots'] = [runtime]
        with patch.object(self.updater, 'run_esptool') as run:
            self.updater.flash(self.session)
        self.assertEqual([call.args[1] for call in run.call_args_list], ['write_flash', 'verify_flash', 'erase_region'])
        self.assertEqual(self.session['phase'], 'esp32_verified')

    def test_corrupt_backup_blocks_writes(self):
        path = self.updater.record_dir(self.session) / 'original.bin'
        path.write_bytes(b'partial')
        self.session.update(phase='backup_verified', backup_name=path.name, backup_sha256=digest(path))
        with patch.object(self.updater, 'run_esptool') as run, self.assertRaises(StopUpdate):
            self.updater.flash(self.session)
        run.assert_not_called()

    def test_complete_wizard_with_simulated_board(self):
        self.updater.pointer.unlink()
        firmware = self.root / 'esp-firmware-disk'
        firmware.mkdir()
        shutil.copyfile(self.updater.sources()['esp32'], firmware / 'FIRMWARE.BIN')
        self.device.update(esp_port='ESP', stm_port='STM', firmware_roots=[firmware])
        calls = []

        def tool(session, action, args=(), **kw):
            calls.append(action)
            if action == 'flash_id':
                return 'Detected flash size: 8MB'
            if action == 'get_security_info':
                return 'Secure Boot: Disabled\nFlash Encryption: Disabled'
            if action == 'read_flash':
                Path(args[-1]).write_bytes(flash_fixture())
            return ''

        def answer(prompt):
            if 'MAC 后 4 位' in prompt:
                return 'eeff'
            if '输入 UPDATE 开始' in prompt:
                return 'UPDATE'
            if '输入 UPDATE-BOOT' in prompt:
                return 'UPDATE-BOOT'
            if 'CORE-OK' in prompt:
                (self.folder / 'BYHUAV.bin').unlink()
                return 'CORE-OK'
            if 'BOOT-OK' in prompt:
                (self.folder / 'BYHUAV_Bootloader.bin').unlink()
                return 'BOOT-OK'
            raise AssertionError('Unexpected prompt: ' + prompt)

        with patch.object(hardware, 'inventory', return_value=self.device), \
                patch.object(self.updater, 'run_esptool', side_effect=tool), \
                patch('builtins.input', side_effect=answer):
            self.updater.run()
        self.assertEqual(calls, ['flash_id', 'get_security_info', 'read_flash', 'write_flash',
                                 'verify_flash', 'erase_region'])
        session = self.updater.current()
        self.assertEqual(session['phase'], 'complete')
        self.assertTrue((self.updater.record_dir(session) / session['backup_name']).is_file())
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_lock_prevents_concurrent_updates(self):
        with file_lock(self.root / 'test.lock'):
            with self.assertRaises(StopUpdate):
                with file_lock(self.root / 'test.lock'):
                    pass
        with file_lock(self.root / 'test.lock'):
            pass


if __name__ == '__main__':
    unittest.main()
