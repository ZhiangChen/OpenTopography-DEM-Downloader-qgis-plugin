"""QGIS integration tests with synthetic credentials; no remote requests."""
import importlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock
from urllib.parse import parse_qs, urlsplit

from osgeo import gdal
from qgis.core import (
    QgsApplication, QgsCoordinateReferenceSystem, QgsProcessingContext,
    QgsProcessingException, QgsProcessingFeedback, QgsProcessingAlgRunnerTask, QgsProject,
)

# Import as a package so the same relative imports as the installed plugin are tested.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
plugin = importlib.import_module(ROOT.name + '.OpenTopography_DEMDownloader_algorithm')


def write_raster(path):
    raster = gdal.GetDriverByName('GTiff').Create(str(path), 2, 2, 1)
    raster.SetGeoTransform((-105.01, 0.005, 0, 40.01, 0, -0.005))
    raster.SetProjection(QgsCoordinateReferenceSystem('EPSG:4326').toWkt())
    raster.GetRasterBand(1).Fill(10)
    raster = None


class DownloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.profile = tempfile.TemporaryDirectory()
        cls.app = QgsApplication.instance() or QgsApplication([], True, cls.profile.name)
        cls.app.initQgis()
        from qgis.analysis import QgsNativeAlgorithms
        if not QgsApplication.processingRegistry().providerById('native'):
            QgsApplication.processingRegistry().addProvider(QgsNativeAlgorithms())

    @classmethod
    def tearDownClass(cls):
        cls.profile.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # Tests never read/remove the developer's actual saved key.
        self.settings_patch = patch.object(plugin, 'QgsSettings')
        self.settings = self.settings_patch.start().return_value
        self.addCleanup(self.settings_patch.stop)
        self.settings.value.return_value = ""
        self.algorithm = plugin.OpenTopographyDEMDownloaderAlgorithm()
        self.algorithm.initAlgorithm({})
        self.context = QgsProcessingContext()
        self.context.setProject(QgsProject.instance())
        self.feedback = QgsProcessingFeedback()
        self.key = 'synthetic-key+&secret'
        creds = plugin.credentials
        self.auth_patch = patch.object(creds, 'manager')
        self.auth = self.auth_patch.start().return_value
        self.addCleanup(self.auth_patch.stop)
        self.auth.isDisabled.return_value = False
        self.auth.masterPasswordIsSet.return_value = True
        self.auth.masterPasswordHashInDatabase.return_value = True
        self.auth.passwordHelperEnabled.return_value = False
        self.auth.passwordHelperDisplayName.return_value = 'Keychain'
        from qgis.core import QgsAuthMethodConfig
        config = QgsAuthMethodConfig()
        config.setId(creds.DEFAULT_ID)
        config.setName(creds.CONFIG_NAME)
        config.setMethod('Basic')
        config.setConfig('password', self.key)
        self.auth.availableAuthMethodConfigs.return_value = {creds.DEFAULT_ID: config}
        self.auth.loadAuthenticationConfig.return_value = (True, config)
        self.credential_settings_patch = patch.object(creds, 'QgsSettings', return_value=self.settings)
        self.credential_settings_patch.start()
        self.addCleanup(self.credential_settings_patch.stop)
        self.params = {
            'DEMs': 15, 'Extent': '-105.01,-105,40,40.01 [EPSG:4326]',
            'OUTPUT': str(Path(self.temp.name) / 'OUTPUT.tif'),
        }
        self.calls = []

    def run_algorithm(self, failure=None, body=None, cancel=False):
        from qgis.PyQt.QtCore import QObject, QTimer, pyqtSignal
        outer = self
        class Downloader(QObject):
            downloadError = pyqtSignal(list)
            downloadCompleted = pyqtSignal(object)
            downloadExited = pyqtSignal()
            downloadProgress = pyqtSignal(int, int)
            def __init__(self, url, output, authcfg, delay):
                super().__init__()
                self.url, self.output = url, output
                outer.calls.append(('download', {'URL': bytes(url.toEncoded()).decode(), 'OUTPUT': output}))
            def cancelDownload(self):
                self.downloadExited.emit()
            def startDownload(self):
                def respond():
                    if cancel:
                        outer.feedback.cancel()
                    elif failure:
                        self.downloadError.emit([failure + ' ' + self.url.toString()])
                    elif body is not None:
                        Path(self.output).write_text(body)
                        self.downloadCompleted.emit(self.url)
                    else:
                        write_raster(self.output)
                        self.downloadCompleted.emit(self.url)
                    self.downloadExited.emit()
                QTimer.singleShot(0, respond)
        def load(name, params, **kwargs):
            self.calls.append((name, params))
            self.assertEqual(name, 'native:loadlayer')
            self.assertNotIn(self.key, str(params))
            return {'OUTPUT': params['INPUT']}
        with patch.object(plugin, 'QgsFileDownloader', Downloader), patch.object(plugin.processing, 'run', side_effect=load):
            result = self.algorithm.processAlgorithm(self.params, self.context, self.feedback)
        self.assertFalse(plugin.credentials.busy())
        return result

    def test_existing_indices_and_default(self):
        self.assertEqual([d.code for d in plugin.DATASETS[:15]], [
            'ANADEM', 'AW3D30', 'AW3D30_E', 'COP30', 'COP90', 'EU_DTM',
            'GEBCOIceTopo', 'GEBCOSubIceTopo', 'GEDI_L3', 'SRTM15Plus',
            'GEDTM30', 'NASADEM', 'SRTMGL1', 'SRTMGL3', 'SRTMGL1_E'])
        self.assertEqual(len(self.algorithm.parameterDefinition('DEMs').options()), 18)
        self.assertEqual(self.algorithm.parameterDefinition('DEMs').defaultValue(), 3)
        self.assertEqual(plugin.DATASETS[3].code, 'COP30')

    def test_usgs_routes_and_outputs(self):
        for index, code in [(15, 'USGS30m'), (16, 'USGS10m'), (17, 'USGS1m')]:
            with self.subTest(code=code):
                self.calls.clear()
                self.params['DEMs'] = index
                self.assertEqual(self.run_algorithm()['OUTPUT'], self.params['OUTPUT'])
                url = urlsplit(self.calls[0][1]['URL'])
                query = parse_qs(url.query)
                self.assertEqual(url.path, '/API/usgsdem')
                self.assertEqual(query['datasetName'], [code])
                self.assertNotIn('demtype', query)
                self.assertEqual(query['API_Key'], [self.key])
                self.assertEqual(query['outputFormat'], ['GTiff'])
                self.assertAlmostEqual(float(query['south'][0]), 40)
                self.assertEqual(self.calls[1][1]['NAME'], code + '[Memory]')

    def test_global_route(self):
        self.params['DEMs'] = 13
        self.run_algorithm()
        url = urlsplit(self.calls[0][1]['URL'])
        self.assertEqual(url.path, '/API/globaldem')
        self.assertEqual(parse_qs(url.query)['demtype'], ['SRTMGL3'])
        self.assertNotIn('datasetName', parse_qs(url.query))

    def test_dialog_defaults_to_copernicus_30m(self):
        from processing.gui.AlgorithmDialog import AlgorithmDialog
        dialog = AlgorithmDialog(self.algorithm)
        try:
            # Check the actual widget: the parameter's stored default alone does
            # not reveal whether QGIS can apply it to a single-selection dropdown.
            self.assertEqual(dialog.mainWidget().wrappers['DEMs'].parameterValue(), 3)
        finally:
            dialog.deleteLater()

    def test_named_output(self):
        self.params['OUTPUT'] = str(Path(self.temp.name) / 'terrain.tif')
        self.run_algorithm()
        self.assertEqual(self.calls[1][1]['NAME'], 'terrain.tif')

    def test_projected_extent(self):
        self.params['Extent'] = '-11689661,-11688548,4865942,4867396 [EPSG:3857]'
        self.run_algorithm()
        query = parse_qs(urlsplit(self.calls[0][1]['URL']).query)
        self.assertAlmostEqual(float(query['west'][0]), -105.01, places=3)
        self.assertAlmostEqual(float(query['south'][0]), 40, places=3)

    def test_invalid_extent(self):
        for extent in ['-105,-105,40,41 [EPSG:4326]', '-105,-104,91,92 [EPSG:4326]',
                       '181,182,40,41 [EPSG:4326]']:
            self.params['Extent'] = extent
            with self.assertRaises(QgsProcessingException):
                self.run_algorithm()
            self.assertEqual(self.calls, [])

    def test_missing_crs(self):
        with patch.object(self.algorithm, 'parameterAsExtentCrs', return_value=QgsCoordinateReferenceSystem()):
            with self.assertRaisesRegex(QgsProcessingException, 'valid CRS'):
                self.run_algorithm()
        self.assertEqual(self.calls, [])

    def test_invalid_index(self):
        for index in [-1, 18]:
            self.params['DEMs'] = index
            with self.assertRaises(QgsProcessingException):
                self.run_algorithm()
        self.assertEqual(self.calls, [])

    def test_failure_diagnostics(self):
        for raw, expected in [('Connection timed out', 'network request timed out'),
                              ('Forbidden', 'service denied access'),
                              ('Too many requests', 'rate limit'),
                              ('Unknown', 'Check API-key authorization')]:
            with self.subTest(raw=raw):
                self.calls.clear()
                with self.assertRaises(QgsProcessingException) as error:
                    self.run_algorithm(failure=raw)
                self.assertIn(expected, str(error.exception))
                self.assertNotIn(self.key, str(error.exception))
                self.assertNotIn('https://', str(error.exception))
                self.assertEqual(len(self.calls), 1)

    def test_empty_or_error_response(self):
        for body in ['', '<error>Unauthorized</error>', '<html>Service unavailable</html>']:
            self.calls.clear()
            with self.assertRaisesRegex(QgsProcessingException, 'No readable raster'):
                self.run_algorithm(body=body)
            self.assertEqual(len(self.calls), 1)

    def test_cancel_before_download(self):
        self.feedback.cancel()
        self.assertEqual(self.run_algorithm(), {})
        self.assertEqual(self.calls, [])

    def test_cancel_during_download(self):
        self.assertEqual(self.run_algorithm(cancel=True), {})
        self.assertEqual(len(self.calls), 1)

    def test_no_key_in_parameters_exports_or_settings(self):
        self.assertIsNone(self.algorithm.parameterDefinition('OT_AUTH_TOKEN'))
        self.settings.remove.assert_called_with(plugin.credentials.LEGACY_KEY)
        self.run_algorithm()
        self.settings.setValue.assert_not_called()
        serialized = self.algorithm.asPythonCommand(self.params, self.context) + str(self.algorithm.asMap(self.params, self.context))
        self.assertNotIn('OT_AUTH_TOKEN', serialized)
        self.assertNotIn(self.key, serialized)
        self.assertTrue(self.algorithm.flags() & plugin.QgsProcessingAlgorithm.FlagSupportsBatch)
        self.assertFalse(self.algorithm.flags() & plugin.QgsProcessingAlgorithm.FlagHideFromModeler)

    def test_processing_run_with_native_download_and_load(self):
        source = Path(self.temp.name) / 'source.tif'
        write_raster(source)
        def local_download(dataset, bounds, key, output, feedback):
            self.algorithm.downloadFile(source.as_uri(), output, feedback, key)
        with patch.object(plugin.OpenTopographyDEMDownloaderAlgorithm, 'requestRaster', side_effect=local_download):
            result = plugin.processing.run(self.algorithm, self.params, context=self.context, feedback=self.feedback)
        self.assertTrue(Path(result['OUTPUT']).is_file())
        self.assertTrue(any(d.name == 'USGS30m[Memory]' for d in self.context.layersToLoadOnCompletion().values()))
        self.assertNotIn(self.key, self.feedback.textLog())
        self.assertNotIn('API_Key', self.feedback.textLog())

    def test_locked_or_missing_fails_without_prompt(self):
        self.auth.masterPasswordIsSet.return_value = False
        with self.assertRaisesRegex(QgsProcessingException, 'locked'):
            self.algorithm.prepareAlgorithm(self.params, self.context, self.feedback)
        self.auth.availableAuthMethodConfigs.return_value = {}
        with self.assertRaisesRegex(QgsProcessingException, 'No OpenTopography key'):
            self.algorithm.prepareAlgorithm(self.params, self.context, self.feedback)
        self.auth.setMasterPassword.assert_not_called()

    def test_old_parameter_rejected(self):
        with self.assertRaisesRegex(QgsProcessingException, 'no longer accepted'):
            self.algorithm.prepareAlgorithm(dict(self.params, OT_AUTH_TOKEN='old-key'), self.context, self.feedback)

    def test_active_request_blocks_key_mutation_and_releases_after_error(self):
        with self.assertRaises(RuntimeError):
            with plugin.credentials.request_key():
                self.assertTrue(plugin.credentials.busy())
                with self.assertRaisesRegex(QgsProcessingException, 'active downloads'):
                    plugin.credentials.remove_key()
                with self.assertRaisesRegex(QgsProcessingException, 'active downloads'):
                    plugin.credentials.save_key('replacement-key')
                raise RuntimeError('simulated failure')
        self.assertFalse(plugin.credentials.busy())

    def coverage_dialog(self):
        from qgis.PyQt.QtWidgets import QMainWindow
        parent = QMainWindow()
        self.addCleanup(parent.deleteLater)
        dialog = self.algorithm.createCustomParametersWidget(parent)
        self.addCleanup(dialog.deleteLater)
        self.assertIs(dialog.parent(), parent)
        self.assertFalse(dialog.in_place)
        return dialog

    def test_coverage_dialog_preserves_parameters_and_batch(self):
        dialog = self.coverage_dialog()
        self.assertEqual(dialog.mainWidget().wrappers['DEMs'].parameterValue(), 3)
        self.assertEqual(set(dialog.mainWidget().wrappers), {'DEMs', 'Extent', 'OUTPUT'})
        self.assertFalse(dialog.runAsBatchButton.isHidden())
        self.assertEqual(dialog.mainWidget().coverageButton.text(), 'Show coverage')

    def test_coverage_mappings_and_validation(self):
        import json
        coverage = importlib.import_module(ROOT.name + '.OpenTopography_DEMDownloader_coverage')
        self.assertEqual(set(coverage.OPENTOPO_IDS), {d.code for d in plugin.DATASETS})
        for dataset in plugin.DATASETS:
            self.assertNotIn('API_Key', coverage.coverage_url(dataset.code))
        polygon = {'type': 'Polygon', 'coordinates': [[[-105,40],[-104,40],[-104,41],[-105,40]]]}
        for document in [polygon, {'type': 'FeatureCollection', 'features': [
                {'type': 'Feature', 'properties': {}, 'geometry': polygon}]}]:
            layer = coverage.coverage_layer(json.dumps(document), plugin.DATASETS[3])
            self.assertTrue(layer.isValid())
            self.assertEqual(layer.crs().authid(), 'EPSG:4326')
        for invalid in ['<html>Error</html>', '{}', '{"type":"Point","coordinates":[0,0]}',
                        '{"type":"FeatureCollection","features":[]}']:
            with self.assertRaises((ValueError, TypeError)):
                coverage.coverage_layer(invalid, plugin.DATASETS[3])

    def test_coverage_async_load_reuse_and_close(self):
        from qgis.PyQt.QtCore import QObject, pyqtSignal
        from qgis.PyQt.QtNetwork import QNetworkReply
        from unittest.mock import MagicMock
        coverage = importlib.import_module(ROOT.name + '.OpenTopography_DEMDownloader_coverage')
        class Reply(QObject):
            finished = pyqtSignal()
            aborted = False
            def error(self):
                return QNetworkReply.NetworkError.NoError
            def readAll(self):
                return b'{"type":"Polygon","coordinates":[[[-105,40],[-104,40],[-104,41],[-105,40]]]}'
            def abort(self):
                self.aborted = True
                self.finished.emit()
        dialog = self.coverage_dialog()
        reply = Reply()
        network = MagicMock()
        network.instance.return_value.get.return_value = reply
        project = QgsProject.instance()
        before = set(project.mapLayers())
        def cleanup():
            project.removeMapLayers(list(set(project.mapLayers()) - before))
        self.addCleanup(cleanup)
        with patch.object(coverage, 'QgsNetworkAccessManager', network):
            dialog.showCoverage()
            self.assertFalse(dialog.mainWidget().coverageButton.isEnabled())
            self.assertEqual(set(project.mapLayers()), before)
            reply.finished.emit()
            self.assertTrue(dialog.mainWidget().coverageButton.isEnabled())
            added = set(project.mapLayers()) - before
            self.assertEqual(len(added), 1)
            layer = project.mapLayer(added.pop())
            node = project.layerTreeRoot().findLayer(layer.id())
            node.setItemVisibilityChecked(False)
            dialog.showCoverage()
            self.assertTrue(node.isVisible())
            self.assertEqual(network.instance.return_value.get.call_count, 1)
            # Start another dataset request then close: no layer should arrive late.
            dialog.setParameters({'DEMs': 17})
            pending = Reply()
            network.instance.return_value.get.return_value = pending
            dialog.showCoverage()
            dialog.reject()
            self.assertTrue(pending.aborted)
            self.assertIsNone(dialog._reply)
            self.assertEqual(len(set(project.mapLayers()) - before), 1)

    def test_coverage_network_failure_recovers_button(self):
        from unittest.mock import MagicMock
        from qgis.PyQt.QtNetwork import QNetworkReply
        dialog = self.coverage_dialog()
        reply = MagicMock()
        reply.error.return_value = QNetworkReply.NetworkError.TimeoutError
        dialog._reply = reply
        before = set(QgsProject.instance().mapLayers())
        dialog.coverageFinished(reply, plugin.DATASETS[3])
        self.assertIsNone(dialog._reply)
        self.assertTrue(dialog.mainWidget().coverageButton.isEnabled())
        self.assertEqual(set(QgsProject.instance().mapLayers()), before)
        reply.deleteLater.assert_called_once()


    def test_credentials_tab_suffix_draft_clear_and_legacy_history(self):
        dialog = self.coverage_dialog()
        panel = dialog.credentialsPanel
        self.assertIn(self.key[-4:], panel.status.text())
        self.assertNotIn(self.key, panel.status.text())
        self.assertEqual(panel.keyEdit.text(), '')
        self.assertEqual(dialog.tabWidget().tabText(2), 'Credentials')
        dialog.setParameters({'DEMs': 17, 'OT_AUTH_TOKEN': 'legacy-secret'})
        self.assertNotIn('OT_AUTH_TOKEN', dialog.mainWidget().wrappers)
        self.auth.masterPasswordIsSet.return_value = False
        panel.refresh()
        self.assertIn('locked', panel.status.text())
        self.assertNotIn(self.key[-4:], panel.status.text())
        panel.keyEdit.setText('unsaved-secret')
        dialog.reject()
        self.assertEqual(panel.keyEdit.text(), '')
        panel.keyEdit.undo()
        self.assertEqual(panel.keyEdit.text(), '')

    def test_credentials_ui_save_and_remove(self):
        dialog = self.coverage_dialog()
        panel = dialog.credentialsPanel
        panel.keyEdit.setText('replacement-key')
        with patch.object(plugin.credentials, 'save_key') as save:
            panel.save()
            save.assert_called_once_with('replacement-key')
        self.assertEqual(panel.keyEdit.text(), '')
        with patch.object(plugin.credentials, 'remove_key') as remove:
            panel.remove()
            remove.assert_called_once()
        self.auth.availableAuthMethodConfigs.return_value = {}
        panel.refresh()
        self.assertIn('No key saved', panel.status.text())
        self.assertFalse(panel.removeButton.isEnabled())
        with patch.object(importlib.import_module('processing.gui.AlgorithmDialog').AlgorithmDialog, 'runAlgorithm') as run:
            dialog.runAlgorithm()
            run.assert_not_called()
        self.assertIs(dialog.tabWidget().currentWidget(), panel)

    def test_real_batch_runs_two_rows_without_key_parameters(self):
        from processing.gui.BatchAlgorithmDialog import BatchAlgorithmDialog
        from qgis.PyQt.QtCore import QEventLoop, QTimer, Qt
        from qgis.PyQt.QtWidgets import QTextBrowser
        dialog = BatchAlgorithmDialog(self.algorithm)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        from qgis.PyQt import sip
        self.addCleanup(lambda: dialog.deleteLater() if not sip.isdeleted(dialog) else None)
        logs = []
        self.assertEqual(dialog.mainWidget().tblParameters.columnCount(), 3)
        rows = [dict(self.params, DEMs=index, OUTPUT=str(Path(self.temp.name) / ('batch'+str(index)+'.tif')))
                for index in (15, 17)]
        requests = []
        def request(dataset, bounds, key, output, feedback):
            self.assertEqual(key, self.key)
            requests.append(dataset.code)
            logs.append(feedback.textLog())
            write_raster(output)
        loop = QEventLoop()
        timer = QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(loop.quit)
        poll = QTimer()
        poll.timeout.connect(lambda: loop.quit() if sip.isdeleted(dialog) or (len(requests) == 2 and dialog.runButton().isEnabled()) else None)
        with patch.object(plugin.OpenTopographyDEMDownloaderAlgorithm, 'requestRaster', side_effect=request), \
             patch.object(plugin.processing, 'run', side_effect=lambda name, params, **kw: {'OUTPUT': params['INPUT']}), \
             patch.object(dialog, 'handleAlgorithmResults', return_value=None):
            timer.start(5000)
            poll.start(10)
            dialog.execute(rows)
            loop.exec()
            poll.stop()
            timer.stop()
        self.assertEqual(requests, ['USGS30m', 'USGS1m'])
        self.assertFalse(plugin.credentials.busy())
        for row in rows:
            self.assertTrue(Path(row['OUTPUT']).is_file())
        for log in logs:
            self.assertNotIn(self.key, log)
            self.assertNotIn('OT_AUTH_TOKEN', log)

    def test_authentication_states_and_actions(self):
        dialog = self.coverage_dialog()
        panel = dialog.credentialsPanel
        self.auth.availableAuthMethodConfigs.return_value = {}
        self.auth.masterPasswordIsSet.return_value = False
        self.auth.masterPasswordHashInDatabase.return_value = False
        panel.refresh()
        self.assertEqual(panel.status.text(), 'No key saved')
        self.assertEqual(panel.storageStatus.text(), 'Not configured')
        self.assertIn('create a master password', panel.storageStatus.toolTip())
        self.assertFalse(hasattr(panel, 'unlockButton'))
        panel.keyEdit.setText('new-key-A123')
        self.assertTrue(panel.saveButton.isEnabled())
        self.auth.masterPasswordHashInDatabase.return_value = True
        panel.pollState()
        self.assertEqual(panel.storageStatus.text(), 'Locked')
        self.assertEqual(panel.status.text(), 'No key saved')
        self.assertTrue(panel.saveButton.isEnabled())
        self.auth.masterPasswordIsSet.return_value = True
        panel.pollState()
        self.assertEqual(panel.storageStatus.text(), 'Unlocked')
        self.assertTrue(panel.lockStorageButton.isEnabled())
        self.auth.passwordHelperEnabled.return_value = True
        panel.pollState()
        self.assertIn('If a valid password is saved', panel.storageStatus.toolTip())
        self.auth.isDisabled.return_value = True
        panel.pollState()
        self.assertEqual(panel.storageStatus.text(), 'Unavailable')
        self.assertFalse(panel.lockStorageButton.isEnabled())
        self.assertFalse(panel.saveButton.isEnabled())
        self.assertFalse(panel.removeButton.isEnabled())

    def test_master_password_actions_respect_busy_and_unavailable_states(self):
        panel = self.coverage_dialog().credentialsPanel
        ui = importlib.import_module(ROOT.name + '.OpenTopography_DEMDownloader_credentials_ui')
        with patch.object(ui, 'QgsAuthEditorWidgets') as editor:
            self.auth.masterPasswordHashInDatabase.return_value = False
            panel.refresh()
            self.assertFalse(panel.changeMasterButton.isEnabled())
            panel.changeMasterPassword()
            editor.assert_not_called()
            self.auth.masterPasswordHashInDatabase.return_value = True
            with plugin.credentials.request_key():
                panel.refresh()
                self.assertFalse(panel.changeMasterButton.isEnabled())
                self.assertFalse(panel.forgetMasterButton.isEnabled())
                panel.changeMasterPassword()
                panel.removeMasterPasswordFromWallet()
                panel.lockSecureStorage()
                panel.eraseQgisCredentials()
                self.assertFalse(panel.eraseCredentialsButton.isEnabled())
                self.assertFalse(panel.lockStorageButton.isEnabled())
                editor.assert_not_called()
            self.auth.isDisabled.return_value = True
            panel.refresh()
            self.assertFalse(panel.changeMasterButton.isEnabled())
            self.assertFalse(panel.forgetMasterButton.isEnabled())
            panel.removeMasterPasswordFromWallet()
            panel.eraseQgisCredentials()
            self.assertFalse(panel.eraseCredentialsButton.isEnabled())
            editor.assert_not_called()

    def test_password_store_disabled_failure_and_recovery(self):
        panel = self.coverage_dialog().credentialsPanel
        ui = importlib.import_module(ROOT.name + '.OpenTopography_DEMDownloader_credentials_ui')
        self.assertFalse(panel.forgetMasterButton.isEnabled())
        self.assertIn('disabled', panel.walletHint.text())
        self.assertFalse(panel.walletHint.isHidden())
        panel.keyEdit.setText('synthetic-new-key')
        self.assertTrue(panel.saveButton.isEnabled())
        self.assertTrue(panel.removeButton.isEnabled())
        with patch.object(ui, 'QgsAuthEditorWidgets') as editor:
            panel.removeMasterPasswordFromWallet()
            editor.assert_not_called()
            self.auth.passwordHelperEnabled.return_value = True
            panel.pollState()
            self.assertTrue(panel.forgetMasterButton.isEnabled())
            self.assertTrue(panel.walletHint.isHidden())
            panel._walletFailure()
            panel.pollState()
            self.assertFalse(panel.forgetMasterButton.isEnabled())
            self.assertIn('could not be used', panel.walletHint.text())
            self.assertTrue(panel.saveButton.isEnabled())
            self.assertTrue(panel.changeMasterButton.isEnabled())
            panel.removeMasterPasswordFromWallet()
            editor.assert_not_called()
            panel._walletSuccess()
            panel.pollState()
            self.assertTrue(panel.forgetMasterButton.isEnabled())
            self.assertTrue(panel.walletHint.isHidden())

    def test_remove_requests_unlock_and_does_not_remove_on_cancel(self):
        panel = self.coverage_dialog().credentialsPanel
        self.auth.masterPasswordIsSet.return_value = False
        panel.refresh()
        self.assertTrue(panel.removeButton.isEnabled())
        with patch.object(plugin.credentials, 'unlock', return_value=False) as unlock, \
             patch.object(plugin.credentials, 'remove_key') as remove:
            panel.removeButton.click()
            unlock.assert_called_once()
            remove.assert_not_called()
        with patch.object(plugin.credentials, 'unlock', return_value=True) as unlock, \
             patch.object(plugin.credentials, 'remove_key') as remove:
            panel.removeButton.click()
            unlock.assert_called_once()
            remove.assert_called_once()

    def test_save_cancel_and_failure_preserve_masked_draft(self):
        dialog = self.coverage_dialog()
        panel = dialog.credentialsPanel
        panel.keyEdit.setText('draft-secret-A123')
        with patch.object(plugin.credentials, 'unlock', return_value=False), \
             patch.object(plugin.credentials, 'save_key') as save:
            panel.save()
            save.assert_not_called()
        self.assertEqual(panel.keyEdit.text(), 'draft-secret-A123')
        self.assertIs(dialog.tabWidget().currentWidget(), panel)
        with patch.object(plugin.credentials, 'unlock', return_value=True), \
             patch.object(plugin.credentials, 'save_key', side_effect=QgsProcessingException('Storage unavailable')):
            panel.save()
        self.assertEqual(panel.keyEdit.text(), 'draft-secret-A123')
        with patch.object(plugin.credentials, 'unlock', return_value=True), \
             patch.object(plugin.credentials, 'save_key'):
            panel.save()
        self.assertEqual(panel.keyEdit.text(), '')
        panel.keyEdit.undo()
        self.assertEqual(panel.keyEdit.text(), '')


if __name__ == '__main__':
    unittest.main()
