"""Exercise real QGIS encryption in a separate process and temporary auth DB."""
import os
from pathlib import Path
import subprocess
import sys
import unittest


class AuthStorageTests(unittest.TestCase):
    def test_real_encryption_and_headless_unlock(self):
        env = dict(os.environ)
        # Ignore user auth overrides; the helper supplies its own isolated database.
        for key in ('QGIS_AUTH_DB_URI', 'QGIS_AUTH_DB_DIR_PATH', 'QGIS_AUTH_PASSWORD_FILE'):
            env.pop(key, None)
        result = subprocess.run(
            [sys.executable, '-B', str(Path(__file__).with_name('auth_storage_check.py'))],
            env=env, capture_output=True, text=True, timeout=45)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_native_password_notices_and_cancel_retry(self):
        env = dict(os.environ)
        for key in ('QGIS_AUTH_DB_URI', 'QGIS_AUTH_DB_DIR_PATH', 'QGIS_AUTH_PASSWORD_FILE'):
            env.pop(key, None)
        result = subprocess.run(
            [sys.executable, '-B', str(Path(__file__).with_name('auth_dialog_check.py'))],
            env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
