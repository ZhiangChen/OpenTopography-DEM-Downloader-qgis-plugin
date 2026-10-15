"""QGIS integration tests with synthetic credentials; no remote requests."""
import importlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
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
        self.params = {
            'DEMs': 15, 'Extent': '-105.01,-105,40,40.01 [EPSG:4326]',
            'OUTPUT': str(Path(self.temp.name) / 'OUTPUT.tif'),
            'OT_AUTH_TOKEN': self.key,
        }
        self.calls = []

    def run_algorithm(self, failure=None, body=None, cancel=False):
        def run(name, params, **kwargs):
            self.calls.append((name, params))
            if name == 'native:filedownloader':
                if cancel:
                    self.feedback.cancel()
                    return {'OUTPUT': params['OUTPUT']}
                if failure:
                    raise QgsProcessingException(failure + ' ' + params['URL'])
                if body is not None:
                    Path(params['OUTPUT']).write_text(body)
                else:
                    write_raster(params['OUTPUT'])
                return {'OUTPUT': params['OUTPUT']}
            return {'OUTPUT': params['INPUT']}
        with patch.object(plugin.processing, 'run', side_effect=run):
            return self.algorithm.processAlgorithm(self.params, self.context, self.feedback)

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

    def test_original_key_parameter_and_settings(self):
        self.settings.value.return_value = 'synthetic-saved-key'
        algorithm = plugin.OpenTopographyDEMDownloaderAlgorithm()
        algorithm.initAlgorithm({})
        self.assertEqual(algorithm.parameterDefinition('OT_AUTH_TOKEN').defaultValue(), 'synthetic-saved-key')
        self.settings.remove.assert_not_called()
        self.run_algorithm()
        self.settings.setValue.assert_called_with('OpenTopographyDEMDownloader/ot_api_key', self.key)
        self.assertIn('OT_AUTH_TOKEN', self.algorithm.asPythonCommand(self.params, self.context))
        self.assertTrue(self.algorithm.flags() & plugin.QgsProcessingAlgorithm.FlagSupportsBatch)
        self.assertFalse(self.algorithm.flags() & plugin.QgsProcessingAlgorithm.FlagHideFromModeler)

    def test_processing_run_with_native_download_and_load(self):
        # Exercise the automation entry point and real native downloader with a local
        # fixture. Settings are mocked so tests never read/write the user's real key.
        source = Path(self.temp.name) / 'source.tif'
        write_raster(source)
        real_run = plugin.processing.run
        def local_download(name, params, **kwargs):
            if name == 'native:filedownloader':
                params = dict(params, URL=source.as_uri())
            return real_run(name, params, **kwargs)
        with patch.object(plugin.processing, 'run', side_effect=local_download):
            result = real_run(self.algorithm, self.params, context=self.context, feedback=self.feedback)
        self.assertTrue(Path(result['OUTPUT']).is_file())
        self.assertTrue(any(d.name == 'USGS30m[Memory]' for d in self.context.layersToLoadOnCompletion().values()))


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
        self.assertEqual(set(dialog.mainWidget().wrappers), {'DEMs', 'Extent', 'OT_AUTH_TOKEN', 'OUTPUT'})
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


if __name__ == '__main__':
    unittest.main()
