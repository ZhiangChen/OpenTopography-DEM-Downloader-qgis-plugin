# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-2.0-or-later
"""Interactive coverage display and integration of the separate Credentials tab."""
import json

from qgis.PyQt.QtCore import QUrl, Qt
from qgis.PyQt.QtNetwork import QNetworkRequest, QNetworkReply
from qgis.PyQt.QtWidgets import QWidget, QHBoxLayout, QPushButton, QLabel
from qgis.core import QgsNetworkAccessManager, QgsProject, QgsVectorLayer, QgsFillSymbol, QgsWkbTypes
from processing.gui.AlgorithmDialog import AlgorithmDialog
from processing.gui.ParametersPanel import ParametersPanel

from .OpenTopography_DEMDownloader_algorithm import DATASETS
from . import OpenTopography_DEMDownloader_credentials as credentials
from .OpenTopography_DEMDownloader_credentials_ui import CredentialsPanel

# Product IDs from OT's dataset metadata, distinct from collection IDs/API codes.
# Keep these keyed by API code so changing a dropdown label cannot change routing.
OPENTOPO_IDS = {
    'ANADEM': 'OTSDEM.082025.4674.1',
    'AW3D30': 'OTALOS.112016.4326.2',
    'AW3D30_E': 'OTALOS.082017.4326.1',
    'COP30': 'OTSDEM.032021.4326.3',
    'COP90': 'OTSDEM.032021.4326.1',
    'EU_DTM': 'OTSDEM.092022.3035.1',
    'GEBCOIceTopo': 'OTSDEM.122023.4326.1',
    'GEBCOSubIceTopo': 'OTSDEM.122023.4326.2',
    'GEDI_L3': 'OTSDEM.032022.4326.1',
    'SRTM15Plus': 'OTSRTM.122019.4326.1',
    'GEDTM30': 'OTSDEM.082025.4326.1',
    'NASADEM': 'OTSDEM.032021.4326.2',
    'SRTMGL1': 'OTSRTM.082015.4326.1',
    'SRTMGL3': 'OTSRTM.042013.4326.1',
    'SRTMGL1_E': 'OTSRTM.082016.4326.1',
    'USGS30m': 'OTNED.012021.4269.2',
    'USGS10m': 'OTNED.012021.4269.1',
    'USGS1m': 'OTNED.012021.4269.3',
}
COVERAGE_PROPERTY = 'OpenTopographyDEMDownloader/coverage_id'


def coverage_url(code):
    return ('https://portal.opentopography.org/getGeoJson?host=OT&id='
            + OPENTOPO_IDS[code] + '&isRaster=true')


def coverage_layer(payload, dataset):
    """Validate the response before adding anything to the user's project."""
    document = json.loads(payload)
    # getGeoJson returns a bare MultiPolygon for some products. Wrap it for OGR.
    if document.get('type') in ('Polygon', 'MultiPolygon'):
        document = {'type': 'Feature', 'properties': {}, 'geometry': document}
    options = QgsVectorLayer.LayerOptions()
    # Inline GeoJSON is a read-only source; avoid OGR attempting to open for update.
    if hasattr(options, 'forceReadOnly'):
        options.forceReadOnly = True
    layer = QgsVectorLayer(json.dumps(document), dataset.label + ' — coverage', 'ogr', options)
    if (not layer.isValid() or layer.geometryType() != QgsWkbTypes.PolygonGeometry
            or layer.featureCount() == 0):
        raise ValueError('No polygon coverage returned')
    if not any(f.hasGeometry() and not f.geometry().isEmpty() for f in layer.getFeatures()):
        raise ValueError('Empty coverage geometry')
    # The inline GeoJSON source travels with the project, without a temporary file.
    layer.renderer().setSymbol(QgsFillSymbol.createSimple({
        'style': 'solid', 'color': '230,100,20,40',  # Light fill (~16% opacity).
        'outline_color': '230,100,20,255', 'outline_width': '0.6'}))
    layer.setCustomProperty(COVERAGE_PROPERTY, OPENTOPO_IDS[dataset.code])
    metadata = layer.metadata()
    metadata.setAbstract('OpenTopography dataset footprint. Coverage is a visual guide, '
                         'not a guarantee of downloadable data at every location.\n'
                         + coverage_url(dataset.code))
    layer.setMetadata(metadata)
    return layer


class CoverageParametersPanel(ParametersPanel):
    def addParameterWidget(self, parameter, widget, stretch=0):
        if parameter.name() == 'DEMs':
            # Preserve the original wrapper and parameter serialization. Only its
            # layout changes; batch/model/script execution uses the same inputs.
            row = QWidget(self)
            layout = QHBoxLayout(row)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.addWidget(widget, 1)
            self.coverageButton = QPushButton(self.tr('Show coverage'), row)
            self.coverageButton.setToolTip(self.tr(
                'Add the selected dataset footprint to the map. No API key required.'))
            layout.addWidget(self.coverageButton)
            widget = row
        super().addParameterWidget(parameter, widget, stretch)


class CoverageDialog(AlgorithmDialog):
    def __init__(self, algorithm, parent=None):
        # The second positional argument is in_place, NOT parent.
        super().__init__(algorithm, in_place=False, parent=parent)
        self._reply = None
        self._download_running = False
        self.credentialStatus = QLabel(self)
        self.credentialStatus.setTextFormat(Qt.TextFormat.PlainText)
        self.credentialStatus.setWordWrap(True)
        change = QPushButton(self.tr('Change credentials…'), self)
        row = QWidget(self)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.credentialStatus, 1)
        layout.addWidget(change)
        self.mainWidget().addOutputWidget(row, 0)
        self.credentialsPanel = CredentialsPanel(self)
        # QGIS native showLog() assumes tab index 1. Append instead of shifting it.
        self.tabWidget().addTab(self.credentialsPanel, self.tr('Credentials'))
        change.clicked.connect(lambda: self.tabWidget().setCurrentWidget(self.credentialsPanel))
        self.tabWidget().currentChanged.connect(lambda _: self.credentialsPanel.refresh())
        self.mainWidget().coverageButton.clicked.connect(self.showCoverage)

    def setParameters(self, parameters):
        # Ignore old history's plaintext key rather than restoring or exporting it.
        allowed = {p.name() for p in self.algorithm().parameterDefinitions()}
        super().setParameters({k: v for k, v in parameters.items() if k in allowed})

    def ensureCredentials(self):
        if not credentials.owns(credentials.existing_config()):
            self.tabWidget().setCurrentWidget(self.credentialsPanel)
            self.messageBar().pushWarning(self.tr('Credentials'), self.tr('Save an OpenTopography API key before running.'))
            return False
        if not credentials.manager().masterPasswordIsSet() and not self.credentialsPanel.unlock():
            self.tabWidget().setCurrentWidget(self.credentialsPanel)
            self.credentialsPanel.refresh()
            return False
        self.credentialsPanel.clear()
        self.credentialsPanel.refresh()
        return True

    def runAlgorithm(self):
        if self.ensureCredentials():
            super().runAlgorithm()

    def runAsBatch(self):
        if self.ensureCredentials():
            super().runAsBatch()

    def blockAdditionalControlsWhileRunning(self):
        super().blockAdditionalControlsWhileRunning()
        self._download_running = True
        self.credentialsPanel.updateControls()

    def resetAdditionalGui(self):
        super().resetAdditionalGui()
        self._download_running = False
        self.credentialsPanel.refresh()

    def getParametersPanel(self, algorithm, parent):
        return CoverageParametersPanel(parent, algorithm)

    def showCoverage(self):
        if self._reply is not None:
            return
        wrapper = self.mainWidget().wrappers['DEMs']
        index = wrapper.parameterValue() if hasattr(wrapper, 'parameterValue') else wrapper.value()
        if not isinstance(index, int) or not 0 <= index < len(DATASETS):
            return
        dataset = DATASETS[index]
        project = QgsProject.instance()
        for layer in project.mapLayers().values():
            if layer.customProperty(COVERAGE_PROPERTY) == OPENTOPO_IDS[dataset.code] and layer.isValid():
                self.revealLayer(layer)
                return
        button = self.mainWidget().coverageButton
        button.setEnabled(False)
        button.setText(self.tr('Loading coverage…'))
        # QGIS's network manager honors its proxy/timeout configuration. Async Qt
        # replies keep the UI responsive; no key or download parameter is sent.
        reply = QgsNetworkAccessManager.instance().get(QNetworkRequest(QUrl(coverage_url(dataset.code))))
        self._reply = reply
        reply.finished.connect(lambda: self.coverageFinished(reply, dataset))

    def coverageFinished(self, reply, dataset):
        if reply is not self._reply:
            return
        self._reply = None
        self.resetCoverageButton()
        try:
            if reply.error() != QNetworkReply.NetworkError.NoError:
                raise ValueError('Network error')
            layer = coverage_layer(bytes(reply.readAll()).decode('utf-8'), dataset)
            # Another open dialog may have finished loading this footprint first.
            for existing in QgsProject.instance().mapLayers().values():
                if existing.customProperty(COVERAGE_PROPERTY) == OPENTOPO_IDS[dataset.code] and existing.isValid():
                    self.revealLayer(existing)
                    return
            QgsProject.instance().addMapLayer(layer)
            self.revealLayer(layer)
        except (ValueError, TypeError, AttributeError, RuntimeError):
            self.messageBar().pushWarning(self.tr('Coverage unavailable'), self.tr(
                'Could not load the footprint for {0}. Check your connection or retry later.').format(dataset.label))
        finally:
            reply.deleteLater()

    def revealLayer(self, layer):
        node = QgsProject.instance().layerTreeRoot().findLayer(layer.id())
        if node:
            node.setItemVisibilityCheckedParentRecursive(True)
        layer.triggerRepaint()
        self.messageBar().pushInfo(self.tr('Coverage'), self.tr(
            '{0} is visible in the Layers panel. Footprints do not guarantee data availability.').format(layer.name()))

    def resetCoverageButton(self):
        self.mainWidget().coverageButton.setEnabled(True)
        self.mainWidget().coverageButton.setText(self.tr('Show coverage'))

    def cancelCoverage(self):
        # Disconnect before abort: finished can fire synchronously during abort.
        reply, self._reply = self._reply, None
        if reply is not None:
            reply.finished.disconnect()
            reply.abort()
            reply.deleteLater()
        self.resetCoverageButton()

    def done(self, result):
        self.cancelCoverage()
        self.credentialsPanel.clear()
        super().done(result)

    def closeEvent(self, event):
        self.cancelCoverage()
        self.credentialsPanel.clear()
        super().closeEvent(event)
