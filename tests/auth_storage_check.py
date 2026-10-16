"""Run in its own process: never initialize auth against the user's profile."""
import importlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

from qgis.core import QgsApplication, QgsAuthMethodConfig

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))


def start(database):
    os.environ['QGIS_AUTH_DB_DIR_PATH'] = str(database)
    app = QgsApplication([], False, str(database))
    QgsApplication.setAuthDatabaseDirPath(str(database))
    app.initQgis()
    auth = QgsApplication.authManager()
    assert Path(auth.authenticationDatabasePath()).parent.resolve() == database.resolve()
    assert not auth.isDisabled()
    return app, auth


if len(sys.argv) > 1:
    database = Path(sys.argv[1])
    app, auth = start(database)
    creds = importlib.import_module(ROOT.name + '.OpenTopography_DEMDownloader_credentials')
    with patch.object(creds, 'QgsSettings') as settings:
        settings.return_value.value.return_value = ''  # stable ID fallback without desktop settings
        assert auth.masterPasswordIsSet(), 'Password-file initialization did not unlock auth'
        assert creds.read_key() == 'replacement-ot-secret-B8y0'
    print('Fresh unattended process: passed')
else:
    with tempfile.TemporaryDirectory() as tmp:
        database = Path(tmp)
        app, auth = start(database)
        assert auth.setMasterPassword('synthetic-master-test', True)
        creds = importlib.import_module(ROOT.name + '.OpenTopography_DEMDownloader_credentials')
        with patch.object(creds, 'QgsSettings') as settings:
            settings.return_value.value.return_value = creds.DEFAULT_ID
            creds.save_key('synthetic-ot-secret-A7x9')
            assert creds.read_key() == 'synthetic-ot-secret-A7x9'
            assert b'synthetic-ot-secret-A7x9' not in Path(auth.authenticationDatabasePath()).read_bytes()
            creds.save_key('replacement-ot-secret-B8y0')
            assert creds.read_key() == 'replacement-ot-secret-B8y0'
            auth.clearMasterPassword()
            try:
                creds.read_key()
            except Exception as error:
                assert 'locked' in str(error)
            else:
                raise AssertionError('Locked read unexpectedly worked')
            assert auth.setMasterPassword('synthetic-master-test', True)
            password_file = database / 'master-password.txt'
            password_file.write_text('synthetic-master-test\n')
            password_file.chmod(0o600)
            env = dict(os.environ, QGIS_AUTH_PASSWORD_FILE=str(password_file))
            subprocess.run([sys.executable, '-B', __file__, str(database)], env=env, check=True, timeout=30)
            # Exercise actual qgis_process command and JSON entry points without
            # loading user plugins or touching user QSettings. A test subclass only
            # redirects the raster URL to a local fixture; auth uses the real DB.
            from osgeo import gdal
            source = database / 'fixture.tif'
            raster = gdal.GetDriverByName('GTiff').Create(str(source), 2, 2, 1)
            raster.SetGeoTransform((-105, .01, 0, 40, 0, -.01))
            raster.GetRasterBand(1).Fill(10)
            raster = None
            script = database / 'credential_download.py'
            script.write_text("""import sys, importlib
from qgis.core import QgsApplication
from processing.script.ScriptAlgorithmProvider import ScriptAlgorithmProvider
class IsolatedScriptProvider(ScriptAlgorithmProvider):
    def loadAlgorithms(self):
        for cls in self.additional_algorithm_classes:
            self.addAlgorithm(cls())
if not QgsApplication.processingRegistry().providerById('script'):
    isolated_provider = IsolatedScriptProvider()
    QgsApplication.processingRegistry().addProvider(isolated_provider)
from unittest.mock import MagicMock
sys.path.insert(0, ROOT_PARENT)
try:
    m = importlib.import_module(PACKAGE + '.OpenTopography_DEMDownloader_algorithm')
except Exception:
    import traceback
    traceback.print_exc()
    raise
settings = MagicMock()
settings.value.return_value = ''
m.QgsSettings = lambda: settings
m.credentials.QgsSettings = lambda: settings
class CredentialDownload(m.OpenTopographyDEMDownloaderAlgorithm):
    def initAlgorithm(self, config=None):
        super().initAlgorithm(config or {})
    def createInstance(self):
        return CredentialDownload()
    def requestRaster(self, dataset, bounds, key, output, feedback):
        assert key == 'replacement-ot-secret-B8y0'
        self.downloadFile(SOURCE_URI, output, feedback, key)
""".replace('ROOT_PARENT', repr(str(ROOT.parent))).replace('PACKAGE', repr(ROOT.name)).replace('SOURCE_URI', repr(source.as_uri())))
            import shutil, json
            executable = Path(sys.executable).with_name('qgis_process')
            if not executable.exists():
                executable = Path(shutil.which('qgis_process') or '')
            assert executable.is_file(), 'qgis_process executable required for integration test'
            base = [str(executable), '--skip-loading-plugins', 'run', str(script)]
            inputs = {'DEMs': 17, 'Extent': '-105,-104,39,40 [EPSG:4326]', 'OUTPUT': str(database / 'cli.tif')}
            command = subprocess.run(base + ['--'] + [k+'='+str(v) for k,v in inputs.items()],
                                     env=env, capture_output=True, text=True, timeout=30)
            assert command.returncode == 0, command.stdout + command.stderr
            assert Path(inputs['OUTPUT']).is_file()
            inputs['OUTPUT'] = str(database / 'json.tif')
            command = subprocess.run(base + ['-'], input=json.dumps({'inputs': inputs}),
                                     env=env, capture_output=True, text=True, timeout=30)
            assert command.returncode == 0, command.stdout + command.stderr
            assert Path(inputs['OUTPUT']).is_file()
            assert 'replacement-ot-secret-B8y0' not in command.stdout + command.stderr
            assert 'OT_AUTH_TOKEN' not in command.stdout
            print('qgis_process command and JSON runs: passed')
            # An unrelated credential must survive removal.
            other = QgsAuthMethodConfig()
            other.setName('Unrelated test credential')
            other.setMethod('Basic')
            other.setConfig('username', 'test')
            other.setConfig('password', 'unrelated-secret')
            ok, other = auth.storeAuthenticationConfig(other)
            assert ok
            creds.remove_key()
            assert creds.existing_config() is None
            assert other.id() in auth.availableAuthMethodConfigs()
            settings.return_value.remove.assert_any_call(creds.LEGACY_KEY)
        print('Encrypted save/replace/lock/unlock/remove and isolation: passed')
