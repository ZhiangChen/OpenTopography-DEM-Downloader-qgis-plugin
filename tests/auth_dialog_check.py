"""Check native password notices and cancellation using isolated credentials."""
import os, tempfile, sys
from pathlib import Path
from unittest.mock import patch
from qgis.core import QgsApplication
from qgis.PyQt.QtCore import QTimer, QSettings
from qgis.gui import QgsCredentialDialog, QgsMessageBar
from qgis.PyQt.QtWidgets import QLabel, QWidget, QTabWidget, QApplication, QMessageBox
import importlib
with tempfile.TemporaryDirectory() as tmp:
    for key in ('QGIS_AUTH_DB_URI', 'QGIS_AUTH_PASSWORD_FILE'):
        os.environ.pop(key, None)
    os.environ['QGIS_AUTH_DB_DIR_PATH'] = tmp
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, tmp)
    app = QgsApplication([], True, tmp)
    app.setOrganizationName('OTIsolatedCancelTest')
    app.setApplicationName('OTIsolatedCancelTest')
    QgsApplication.setAuthDatabaseDirPath(tmp)
    app.initQgis()
    auth = QgsApplication.authManager()
    assert Path(auth.authenticationDatabasePath()).parent.resolve() == Path(tmp).resolve()
    auth.setPasswordHelperEnabled(False)
    native = QgsCredentialDialog()
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root.parent))
    creds = importlib.import_module(root.name + '.OpenTopography_DEMDownloader_credentials')
    ui = importlib.import_module(root.name + '.OpenTopography_DEMDownloader_credentials_ui')
    for existing in (False, True):
        if existing:
            assert auth.setMasterPassword('synthetic-test-master', True)
            auth.clearMasterPassword()
        for retry in range(2):
            shown = []
            timer = QTimer()
            def cancel():
                if native.isVisible():
                    shown.append(all(native.findChild(QLabel, name).isHidden() for name in ('lblDontForget', 'lblSavedForSession')))
                    native.reject()
            timer.timeout.connect(cancel)
            timer.start(50)
            with ui._plugin_password_prompt():
                assert not creds.unlock()
            timer.stop()
            assert shown and all(shown), 'Native notices were visible or prompt did not reopen'
            assert not native.findChild(QLabel, 'lblSavedForSession').isHidden(), 'Notice not restored'
            assert not auth.masterPasswordIsSet()
            assert not auth.isDisabled()
            print('existing=%s attempt=%s: dialog shown, canceled, remains locked' % (existing, retry+1))
    class Host(QWidget):
        def __init__(self):
            super().__init__()
            self.credentialStatus = QLabel(self)
            self.tabs = QTabWidget(self)
            self.bar = QgsMessageBar(self)
        def tabWidget(self):
            return self.tabs
        def messageBar(self):
            return self.bar
    host = Host()
    with patch.object(creds, 'QgsSettings') as settings:
        settings.return_value.value.return_value = creds.DEFAULT_ID
        panel = ui.CredentialsPanel(host)
        host.tabs.addTab(panel, 'Credentials')
        host.show()
        app.processEvents()
        assert auth.setMasterPassword('synthetic-test-master', True)
        creds.save_key('synthetic-OT-key-A123')
        # Cancel native management dialogs. Never confirm wallet deletion:
        # even this test must not touch the user's operating-system Keychain.
        for action in (panel.changeMasterPassword, panel.removeMasterPasswordFromWallet,
                       panel.eraseQgisCredentials):
            shown = []
            confirmation_texts = []
            timer = QTimer()
            def cancel_management():
                dialog = QApplication.activeModalWidget()
                if dialog is not None:
                    shown.append(dialog.metaObject().className())
                    if isinstance(dialog, QMessageBox):
                        confirmation_texts.append(' '.join(label.text() for label in dialog.findChildren(QLabel)))
                        for button in dialog.buttons():
                            if dialog.buttonRole(button) in (QMessageBox.ButtonRole.RejectRole, QMessageBox.ButtonRole.NoRole):
                                button.click()
                                break
                    else:
                        dialog.reject()
            timer.timeout.connect(cancel_management)
            timer.start(50)
            if action == panel.removeMasterPasswordFromWallet:
                auth.setPasswordHelperEnabled(True)
            if action == panel.eraseQgisCredentials:
                auth.clearMasterPassword()
                panel.refresh()
                assert panel.eraseCredentialsButton.isEnabled()
            action()
            if action == panel.eraseQgisCredentials:
                assert not auth.masterPasswordIsSet()
                assert confirmation_texts
                assert all('Current database will be backed up' not in text for text in confirmation_texts)
                assert auth.setMasterPassword('synthetic-test-master', True)
            auth.setPasswordHelperEnabled(False)
            timer.stop()
            assert shown, 'Native management dialog did not appear'
            assert auth.masterPasswordHashInDatabase()
            assert creds.read_key() == 'synthetic-OT-key-A123'
            assert panel.timer.isActive()
            assert panel.changeMasterButton.isEnabled()
            print('Native management dialog canceled safely:', shown)
        panel.refresh()
        assert panel.storageStatus.text() == 'Unlocked'
        assert 'A123' in panel.status.text()
        panel.lockStorageButton.click()
        assert not auth.masterPasswordIsSet()
        assert panel.storageStatus.text() == 'Locked'
        assert panel.status.text() == 'Saved key — locked'
        assert not panel.lockStorageButton.isEnabled()
        assert creds.existing_config() is not None
        assert auth.setMasterPassword('synthetic-test-master', True)
        assert creds.read_key() == 'synthetic-OT-key-A123'
        panel.refresh()
        assert panel.lockStorageButton.isEnabled()
        assert not hasattr(panel, 'securityMenu')
        assert panel.changeMasterButton.menu() is None
        assert panel.forgetMasterButton.menu() is None
        print('Lock clears memory, preserves the encrypted key, and refreshes status')
        # A prompt started later by QGIS must still be simplified while this page
        # is visible, without relying on the synchronous plugin call wrapper.
        panel.setParent(None)
        panel.show()
        app.processEvents()
        auth.clearMasterPassword()
        seen = []
        timer = QTimer()
        def cancel_deferred():
            if native.isVisible():
                seen.append(all(native.findChild(QLabel, name).isHidden()
                                for name in ('lblDontForget', 'lblSavedForSession')))
                native.reject()
        timer.timeout.connect(cancel_deferred)
        timer.start(50)
        assert not auth.setMasterPassword(True)
        timer.stop()
        assert seen and all(seen), 'Deferred prompt notices were visible'
        panel.hide()
        assert not native.findChild(QLabel, 'lblSavedForSession').isHidden()
        panel.timer.stop()
    host.deleteLater()
    native.deleteLater()
