import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
import ci
import input_config as inputs


class InputTests(unittest.TestCase):
    def test_configuration_errors_identify_missing_or_invalid_json_without_exposing_input(self):
        for value, message in (('', 'prepared_input is empty'),
                               (' \n ', 'prepared_input is empty'),
                               ('{"url":"sensitive-token",}', 'prepared_input is not valid JSON'),
                               ('[]', 'prepared_input must be a JSON object')):
            with self.subTest(value=value), patch.dict(os.environ, PREPARED_INPUT=value):
                with self.assertRaisesRegex(ValueError, message) as error:
                    inputs.settings()
                self.assertNotIn('sensitive-token', str(error.exception))

    def test_json_url_checksum_and_identity(self):
        url = 'https://drive.google.com/file/d/file_123/view'
        for digest in ('a' * 64, '', None):
            with self.subTest(digest=digest), patch.dict(os.environ, PREPARED_INPUT=json.dumps({'url': url, 'sha256': digest})):
                self.assertEqual(inputs.settings(), (url, digest or ''))
        with tempfile.TemporaryDirectory() as temporary:
            checksum = Path(temporary) / 'SHA256SUMS.txt'
            checksum.write_text('b' * 64 + '  D2RMobile-Prepared.zip\n')
            def fetch(url, destination, **kwargs):
                destination.write_bytes(checksum.read_bytes())
            with patch.object(inputs, 'download_file', side_effect=fetch), patch.dict(os.environ, PREPARED_INPUT=json.dumps({'url': url, 'sha256_url': 'https://storage.invalid/checksums.txt'})):
                self.assertEqual(inputs.settings(), (url, 'b' * 64))
        for value in ('not JSON', {'url': 'http://storage.invalid/input.zip'}, {'url': url, 'sha256': 'wrong'}):
            with patch.dict(os.environ, PREPARED_INPUT=value if isinstance(value, str) else json.dumps(value)):
                with self.assertRaises(ValueError):
                    inputs.settings()
        with patch.dict(os.environ, PREPARED_INPUT=json.dumps({'url': url, 'sha256': 'a' * 64}), INPUT_REVISION='different'):
            with self.assertRaisesRegex(ValueError, 'checksum changed'):
                inputs.settings()
        self.assertEqual(inputs.input_identity(url, 'a' * 64), inputs.input_identity('https://other.invalid/same.zip', 'a' * 64))

    def test_drive_links(self):
        for url in ('https://drive.google.com/file/d/file_123/view', 'https://docs.google.com/uc?id=file_123'):
            self.assertEqual(inputs.drive_file_id(url), 'file_123')
        self.assertIsNone(inputs.drive_file_id('https://storage.invalid/file.zip'))
        with self.assertRaisesRegex(ValueError, 'folder'):
            inputs.drive_file_id('https://drive.google.com/drive/folders/file_123')

    def test_drive_download_verifies_bytes_and_preserves_completed_archive(self):
        body = b'fixture archive'
        def fetch(**kwargs):
            kwargs['hasher'].update(body)
            Path(kwargs['output']).write_bytes(body)
            return kwargs['output']
        with tempfile.TemporaryDirectory() as temporary, patch.dict(sys.modules, gdown=types.SimpleNamespace(download=fetch)):
            path = Path(temporary) / 'prepared.zip'
            inputs.download_file('https://drive.google.com/file/d/file_123/view', path, hashlib.sha256(body).hexdigest())
            with self.assertRaisesRegex(ValueError, 'SHA256 mismatch'):
                inputs.download_file('https://drive.google.com/file/d/file_123/view', path, '0' * 64)
            self.assertEqual(path.read_bytes(), body)

    def test_download_errors_are_bounded_and_do_not_expose_signed_urls(self):
        response = io.BytesIO(b'data')
        response.headers = {'Content-Length': '1000000000', 'Content-Type': 'application/octet-stream'}
        with tempfile.TemporaryDirectory() as temporary, patch.object(ci.urllib.request, 'urlopen', return_value=response):
            with self.assertRaisesRegex(ValueError, 'supported size'):
                inputs.download_file('https://storage.invalid/input.zip', Path(temporary) / 'input', max_bytes=64)
        secret_url = 'https://drive.google.com/file/d/file_123/view?resourcekey=secret'
        def fail(**kwargs):
            raise OSError(secret_url)
        with tempfile.TemporaryDirectory() as temporary, patch.dict(sys.modules, gdown=types.SimpleNamespace(download=fail)):
            with self.assertRaises(RuntimeError) as error:
                inputs.download_file(secret_url, Path(temporary) / 'input')
            self.assertNotIn('resourcekey', str(error.exception))
        def quota(**kwargs):
            raise RuntimeError('Too many users have viewed or downloaded this file recently. ' + secret_url)
        with tempfile.TemporaryDirectory() as temporary, patch.dict(sys.modules, gdown=types.SimpleNamespace(download=quota)):
            with self.assertRaisesRegex(RuntimeError, 'Google Drive download quota exceeded') as error:
                inputs.download_file(secret_url, Path(temporary) / 'input')
            self.assertNotIn('resourcekey', str(error.exception))
