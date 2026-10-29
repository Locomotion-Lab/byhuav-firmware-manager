import hashlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from tools.bootstrap import install


def archive_bytes(extra=None):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as target:
        for name in ('update.py', 'manifest.json', 'requirements.txt'):
            target.writestr(name, b'payload')
        if extra:
            target.writestr(extra.replace('\\', '/'), b'bad')
    content = stream.getvalue()
    if extra and '\\' in extra:
        # Windows ZipInfo normalizes backslashes when creating a ZIP. Patch
        # the two name fields so every host tests the same malformed archive.
        content = content.replace(extra.replace('\\', '/').encode(), extra.encode())
    return content


class Response(io.BytesIO):
    def geturl(self):
        return 'https://example.com/firmware.zip'


class DistributionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.target = Path(self.tmp.name) / 'package'

    def install(self, content, checksum=None):
        checksum = checksum or hashlib.sha256(content).hexdigest()
        with patch('urllib.request.urlopen', return_value=Response(content)):
            return install('https://example.com/firmware.zip', checksum, self.target)

    def test_verified_archive_and_cache(self):
        data = archive_bytes()
        self.install(data)
        self.assertEqual((self.target / 'update.py').read_bytes(), b'payload')
        self.install(data)

    def test_corrupt_archive_rejected_before_extraction(self):
        with self.assertRaises(RuntimeError):
            self.install(archive_bytes(), '0' * 64)
        self.assertFalse(self.target.exists())

    def test_traversal_rejected(self):
        with self.assertRaises(RuntimeError):
            self.install(archive_bytes('../escape.py'))
        self.assertFalse(self.target.exists())

    def test_windows_paths_rejected_on_all_hosts(self):
        for name in ('C:/escape', 'folder\\escape'):
            with self.subTest(name=name), self.assertRaises(RuntimeError):
                self.install(archive_bytes(name))

    def test_changed_cache_rejected(self):
        data = archive_bytes()
        self.install(data)
        (self.target / 'update.py').write_bytes(b'changed')
        with self.assertRaises(RuntimeError):
            self.install(data)

    def test_original_name_checked_before_windows_normalization(self):
        data = archive_bytes('folder\\escape')

        class WindowsZip(zipfile.ZipFile):
            def infolist(self):
                entries = super().infolist()
                for entry in entries:
                    entry.filename = entry.filename.replace('\\', '/')
                return entries

        with patch('tools.bootstrap.zipfile.ZipFile', WindowsZip), self.assertRaises(RuntimeError):
            self.install(data)

    def test_http_rejected(self):
        with self.assertRaises(RuntimeError):
            install('http://example.com/file.zip', '0' * 64, self.target)
