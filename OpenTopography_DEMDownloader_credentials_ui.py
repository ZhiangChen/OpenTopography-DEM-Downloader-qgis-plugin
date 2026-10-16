# SPDX-License-Identifier: GPL-2.0-or-later
"""Credential controls outside QGIS's serializable processing parameter registry."""
from contextlib import contextmanager

from qgis.PyQt.QtCore import Qt, QTimer, QObject, QEvent, QMetaObject, QCoreApplication
from qgis.PyQt.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QApplication, QGroupBox, QFrame, QTextBrowser
from qgis.core import QgsProcessingException
from qgis.gui import QgsAuthEditorWidgets, QgsMessageBar
from . import OpenTopography_DEMDownloader_credentials as credentials


class _PasswordNoticeFilter(QObject):
    """Simplify native prompt text temporarily, without touching password fields."""
    def __init__(self, owner=None):
        super().__init__(owner)
        self.owner = owner
        self.labels = {}
        self.texts = {}

    def restore(self):
        from qgis.PyQt import sip
        labels, self.labels = self.labels, {}
        texts, self.texts = self.texts, {}
        for label, hidden in labels.items():
            if not sip.isdeleted(label):
                label.setHidden(hidden)
        for label, text in texts.items():
            if not sip.isdeleted(label):
                label.setText(text)

    def enabled(self):
        from qgis.PyQt import sip
        if self.owner is None:
            return True
        return (not sip.isdeleted(self.owner) and
                (self.owner.isVisible() or self.owner.dialog.isVisible()))

    def simplifyWindow(self, window):
        from qgis.PyQt import sip
        if sip.isdeleted(window) or not self.enabled() or not window.isVisible():
            return
        # QGIS-created C++ dialogs can reach Python as generic QDialog wrappers.
        # Use Qt's runtime inheritance, rather than Python isinstance checks.
        if window.inherits('QgsCredentialDialog'):
            for name in ('lblDontForget', 'lblSavedForSession'):
                label = window.findChild(QLabel, name)
                if label is not None:
                    if label not in self.labels:
                        self.labels[label] = label.isHidden()
                    label.hide()
        elif window.inherits('QMessageBox'):
            phrase = '(Current database will be backed up and new one created.)'
            translated = QCoreApplication.translate('QgsAuthGuiUtils', phrase)
            for label in window.findChildren(QLabel):
                text = label.text()
                shorter = text.replace(phrase, '').replace(translated, '').strip()
                if shorter != text:
                    if label not in self.texts:
                        self.texts[label] = text
                    label.setText(shorter)

    def eventFilter(self, watched, event):
        from qgis.PyQt import sip
        if self.owner is not None and sip.isdeleted(self.owner):
            QApplication.instance().removeEventFilter(self)
            self.restore()
            return False
        native_dialog = watched.inherits('QgsCredentialDialog') or watched.inherits('QMessageBox')
        if event.type() == QEvent.Type.Hide and (watched is self.owner or native_dialog):
            self.restore()
        if event.type() == QEvent.Type.Show and self.enabled():
            if native_dialog:
                # Also run after QGIS finishes showing/updating the reused dialog.
                QTimer.singleShot(0, lambda window=watched: self.simplifyWindow(window))
            elif isinstance(watched, QLabel):
                self.simplifyWindow(watched.window())
        return False


@contextmanager
def _plugin_password_prompt():
    app = QApplication.instance()
    notice_filter = _PasswordNoticeFilter()
    app.installEventFilter(notice_filter)
    try:
        yield
    finally:
        # QGIS reuses this dialog: restore notices for other plugins/settings.
        app.removeEventFilter(notice_filter)
        notice_filter.restore()


class CredentialsPanel(QWidget):
    def __init__(self, dialog):
        super().__init__(dialog)
        self.dialog = dialog
        # QGIS may open a password prompt after a queued database reset, after
        # the initiating slot returned. Observe prompts for this visible page’s
        # lifetime, not just the synchronous Save/Remove call. Restore native
        # notices when the prompt or page closes; other QGIS pages keep them.
        self._password_notices = _PasswordNoticeFilter(self)
        QApplication.instance().installEventFilter(self._password_notices)
        self._wallet_failed = False
        # Observe QGIS results without probing the OS store or reading credentials.
        # Signals may arrive inside auth-manager operations: only record state here;
        # the existing UI timer refreshes safely after control returns.
        auth = credentials.manager()
        if hasattr(auth, 'passwordHelperFailure'):
            auth.passwordHelperFailure.connect(self._walletFailure)
        if hasattr(auth, 'passwordHelperSuccess'):
            auth.passwordHelperSuccess.connect(self._walletSuccess)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)
        # Explain the three distinct credentials/storage concepts on every OS.
        self.credentialGuide = QTextBrowser(self)
        self.credentialGuide.setHtml(self.tr(
            '<p><b>OpenTopography API key:</b> Your personal key from OpenTopography '
            'that authorizes DEM downloads. Save it once to reuse it in this plugin. '
            '<a href="https://portal.opentopography.org/myopentopo">Get an API key from OpenTopography</a>.</p>'
            '<p><b>QGIS master password:</b> A password that protects credentials stored '
            'by QGIS, including your OT API key. It may also protect credentials used '
            'by other plugins.</p>'
            '<p><b>System password storage:</b> Your operating system’s password manager, '
            'such as macOS Keychain, Windows Credential Manager, or a supported Linux '
            'wallet/keyring. When available and enabled, it lets QGIS remember your '
            'master password and unlock saved credentials automatically.</p>'))
        # Bound only the introduction; scrolling it leaves credential controls visible.
        self.credentialGuide.setReadOnly(True)
        self.credentialGuide.setAccessibleName(self.tr('About credentials'))
        self.credentialGuide.setMinimumHeight(100)
        self.credentialGuide.setMaximumHeight(160)
        self.credentialGuide.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.credentialGuide.setOpenExternalLinks(True)
        layout.addWidget(self.credentialGuide)
        key_group = QGroupBox(self.tr('OpenTopography API key'), self)
        key_layout = QVBoxLayout(key_group)
        key_layout.setContentsMargins(12, 16, 12, 12)
        key_layout.setSpacing(10)
        self.status = QLabel(key_group)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        key_layout.addWidget(self.status)
        key_layout.addWidget(QLabel(self.tr('New API key'), key_group))
        self.keyEdit = QLineEdit(key_group)
        self.keyEdit.setEchoMode(QLineEdit.EchoMode.Password)
        self.keyEdit.setPlaceholderText(self.tr('Enter a key to save or replace the saved key'))
        key_layout.addWidget(self.keyEdit)
        row = QHBoxLayout()
        self.saveButton = QPushButton(self.tr('Save key'), key_group)
        self.removeButton = QPushButton(self.tr('Remove OT key'), key_group)
        # Authenticate within each action instead of requiring a separate unlock step.
        for button in (self.saveButton, self.removeButton):
            row.addWidget(button)
        key_layout.addLayout(row)
        layout.addWidget(key_group)

        storage_group = QGroupBox(self.tr('QGIS secure storage'), self)
        storage_layout = QVBoxLayout(storage_group)
        storage_layout.setContentsMargins(12, 16, 12, 12)
        storage_layout.setSpacing(10)
        self.storageStatus = QLabel(storage_group)
        self.storageStatus.setTextFormat(Qt.TextFormat.PlainText)
        self.storageStatus.setWordWrap(True)
        status_row = QHBoxLayout()
        status_row.setSpacing(8)
        status_row.addWidget(QLabel(self.tr('Status:'), storage_group))
        status_row.addWidget(self.storageStatus, 1)
        storage_layout.addLayout(status_row)
        self.walletHint = QLabel(storage_group)
        self.walletHint.setTextFormat(Qt.TextFormat.PlainText)
        self.walletHint.setWordWrap(True)
        storage_layout.addWidget(self.walletHint)
        helper_name = getattr(credentials.manager(), 'passwordHelperDisplayName', None)
        self.walletName = helper_name(True) if helper_name else self.tr('system wallet')
        # Keep locking beside the current state. Full-width password controls
        # below it have consistent sizing and fit longer platform-specific labels.
        self.changeMasterButton = QPushButton(self.tr('Change master password…'), storage_group)
        self.changeMasterButton.setToolTip(self.tr(
            'Requires the current password and changes protection for all credentials in this QGIS database.'))
        self.forgetMasterButton = QPushButton(
            self.tr('Forget password in {0}…').format(self.walletName), storage_group)
        self.forgetMasterButton.setToolTip(self.tr(
            'Removes only the remembered copy. Your existing master password still protects '
            'saved credentials, and QGIS may remain unlocked in this session.'))
        self.lockStorageButton = QPushButton(self.tr('Lock storage'), storage_group)
        self.lockStorageButton.setToolTip(self.tr(
            'Clears the master password cached in memory. Keychain or another automatic '
            'unlock method may unlock storage again. Existing network connections may stay authenticated.'))
        status_row.addWidget(self.lockStorageButton)
        storage_layout.addWidget(self.changeMasterButton)
        storage_layout.addWidget(self.forgetMasterButton)
        # Separate recovery from routine actions; QGIS still owns confirmation.
        divider = QFrame(storage_group)
        divider.setFrameShape(QFrame.Shape.HLine)
        divider.setFrameShadow(QFrame.Shadow.Sunken)
        storage_layout.addWidget(divider)
        recovery = QLabel(self.tr('Recovery'), storage_group)
        font = recovery.font()
        font.setBold(True)
        recovery.setFont(font)
        storage_layout.addWidget(recovery)
        self.eraseCredentialsButton = QPushButton(self.tr('Erase all QGIS credentials…'), storage_group)
        self.eraseCredentialsButton.setToolTip(self.tr(
            'Start over when the master password is forgotten. Opens QGIS confirmation to '
            'erase the authentication database, including credentials used by other plugins. '
            'This is not just removal of the OT key or its Keychain copy.'))
        storage_layout.addWidget(self.eraseCredentialsButton)
        self.recoveryWarning = QLabel(self.tr(
            'Removes saved credentials for all QGIS plugins.'), storage_group)
        self.recoveryWarning.setWordWrap(True)
        storage_layout.addWidget(self.recoveryWarning)
        layout.addWidget(storage_group)
        self.changeMasterButton.clicked.connect(self.changeMasterPassword)
        self.forgetMasterButton.clicked.connect(self.removeMasterPasswordFromWallet)
        self.lockStorageButton.clicked.connect(self.lockSecureStorage)
        self.eraseCredentialsButton.clicked.connect(self.eraseQgisCredentials)
        # Native styling respects QGIS themes; emphasis does not rely on color.
        for label in (self.status, self.storageStatus):
            font = label.font()
            font.setBold(True)
            label.setFont(font)
        for button in (self.saveButton, self.removeButton, self.changeMasterButton,
                       self.forgetMasterButton, self.lockStorageButton, self.eraseCredentialsButton):
            button.setMinimumHeight(30)
            button.setAutoDefault(False)
        self._auth_action_running = False
        layout.addStretch()
        self.saveButton.clicked.connect(self.save)
        self.removeButton.clicked.connect(self.remove)
        self.keyEdit.textChanged.connect(self.updateControls)
        # Other dialogs/batch jobs share the key; disable mutation while any request
        # holds it. The backend enforces the same rule to avoid UI races.
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.pollState)
        self.timer.start(500)
        self.refresh()

    def state(self):
        auth = credentials.manager()
        # Check disabled first: an unavailable backend must not trigger config reads.
        if auth.isDisabled():
            return ('unavailable', None)
        if not auth.masterPasswordHashInDatabase():
            return ('setup', None)
        config = credentials.existing_config()
        if config is not None and not credentials.owns(config):
            return ('conflict', config.id())
        if not auth.masterPasswordIsSet():
            return ('locked', config.id() if config else None)
        return ('saved' if config else 'empty', config.id() if config else None)

    def _walletFailure(self, *args):
        self._wallet_failed = True

    def _walletSuccess(self, *args):
        self._wallet_failed = False

    def storageState(self):
        # Storage status is independent of whether an OT key/configuration exists.
        auth = credentials.manager()
        if auth.isDisabled():
            return ('unavailable', False, self._wallet_failed)
        if not auth.masterPasswordHashInDatabase():
            state = 'setup'
        else:
            state = 'unlocked' if auth.masterPasswordIsSet() else 'locked'
        return state, bool(auth.passwordHelperEnabled()), self._wallet_failed

    def pollState(self):
        # Detect locking/removal in other QGIS windows without decrypting on every tick.
        if self.state() != self._state or self.storageState() != self._storage_state:
            self.refresh()
        else:
            self.updateControls()

    def updateControls(self):
        busy = (credentials.busy() or bool(getattr(self.dialog, '_download_running', False))
                or self._auth_action_running)
        state, config_id = self.state()
        available = state not in ('unavailable', 'conflict')
        self.keyEdit.setEnabled(not busy and available)
        self.saveButton.setEnabled(not busy and available and bool(self.keyEdit.text().strip()))
        self.saveButton.setText(self.tr('Replace key') if config_id else self.tr('Save key'))
        self.removeButton.setEnabled(not busy and available and config_id is not None)
        storage, helper_enabled, helper_failed = self.storageState()
        self.changeMasterButton.setEnabled(not busy and storage in ('locked', 'unlocked'))
        self.forgetMasterButton.setEnabled(
            not busy and storage != 'unavailable' and helper_enabled and not helper_failed)
        self.lockStorageButton.setEnabled(not busy and storage == 'unlocked')
        self.eraseCredentialsButton.setEnabled(not busy and storage != 'unavailable')

    def refresh(self):
        self._state = self.state()
        state, config_id = self._state
        if state == 'unavailable':
            text = self.tr('Saved key unavailable')
        elif state == 'conflict':
            text = self.tr('Key configuration conflict — check QGIS Authentication settings.')
        elif state == 'locked' and config_id:
            text = self.tr('Saved key — locked')
        elif state in ('setup', 'empty', 'locked'):
            text = self.tr('No key saved')
        else:
            try:
                # Only the suffix is retained by widgets, never saved to settings/logs.
                text = self.tr('Saved key: ••••••••{0}').format(credentials.read_key()[-4:])
            except QgsProcessingException:
                text = self.tr('Saved key unavailable — replace it')
        self.status.setText(text)
        self.dialog.credentialStatus.setText(text)
        self._storage_state = self.storageState()
        storage, helper_enabled, helper_failed = self._storage_state
        statuses = {
            'setup': (self.tr('Not configured'), self.tr(
                'Saving your API key will ask you to create a master password.')),
            'locked': (self.tr('Locked'), self.tr(
                'Your next protected action will request unlocking with the existing master password.')),
            'unlocked': (self.tr('Unlocked'), self.tr(
                'QGIS can access your saved key during this session.')),
            'unavailable': (self.tr('Unavailable'), self.tr(
                'Check QGIS Authentication settings. If authentication is disabled, restart QGIS.')),
        }
        title, explanation = statuses[storage]
        self.storageStatus.setText(title)
        # Enabled is a preference, not a capability check. Do not claim that an
        # OS backend works (or contains a password) until QGIS actually uses it.
        if helper_failed:
            wallet_explanation = self.tr(
                'System password storage could not be used. Enter your QGIS master password when prompted.')
        elif not helper_enabled:
            wallet_explanation = self.tr(
                'System password storage is disabled. Enter your QGIS master password when prompted.')
        else:
            wallet_explanation = self.tr(
                '{0} integration is enabled. If a valid password is saved there, QGIS can unlock automatically.'
            ).format(self.walletName)
        self.walletHint.setText(wallet_explanation)
        self.walletHint.setVisible(storage != 'unavailable' and (helper_failed or not helper_enabled))
        if helper_failed or not helper_enabled:
            self.forgetMasterButton.setToolTip(wallet_explanation + '\n' + self.tr(
                'Check QGIS Authentication settings to configure or manage system password storage. '
                'This does not prevent saving your OT key in QGIS’s encrypted database.'))
        else:
            self.forgetMasterButton.setToolTip(self.tr(
                'Removes only the remembered copy. Your existing master password still protects '
                'saved credentials, and QGIS may remain unlocked in this session.'))
        # Keep the page compact; details remain available on hover.
        self.storageStatus.setToolTip(explanation if storage == 'unavailable' else
                                      explanation + '\n' + wallet_explanation)
        self.updateControls()

    def unlock(self):
        # Bring the explanation into view before QGIS opens its own password prompt.
        self.dialog.tabWidget().setCurrentWidget(self)
        result = credentials.unlock()
        self.refresh()
        return result

    def changeMasterPassword(self):
        self._runAuthUtility('resetMasterPassword')

    def removeMasterPasswordFromWallet(self):
        self._runAuthUtility('passwordHelperDelete')

    def lockSecureStorage(self):
        self._runAuthUtility('clearCachedMasterPassword')

    def eraseQgisCredentials(self):
        # Do not require unlocking: this is also the forgotten-password recovery path.
        self._runAuthUtility('eraseAuthenticationDatabase')

    def _runAuthUtility(self, action):
        # Use the same native slots as QGIS Settings. QgsAuthGuiUtils is not
        # Python-bound; check the Qt slot before invoking it for version safety.
        # No master password or Keychain contents pass through plugin code.
        if (self._auth_action_running or credentials.busy()
                or getattr(self.dialog, '_download_running', False)):
            self.dialog.messageBar().pushWarning(
                self.tr('Credentials'), self.tr('Wait for active operations to finish.'))
            return
        auth = credentials.manager()
        if auth.isDisabled():
            self.refresh()
            return
        if action == 'passwordHelperDelete' and (not auth.passwordHelperEnabled() or self._wallet_failed):
            self.refresh()
            return
        if action == 'resetMasterPassword' and not auth.masterPasswordHashInDatabase():
            self.refresh()
            return
        if action == 'clearCachedMasterPassword' and not auth.masterPasswordIsSet():
            self.refresh()
            return
        editor = QgsAuthEditorWidgets(self)
        editor.hide()
        if editor.metaObject().indexOfMethod((action + '()').encode()) < 0:
            editor.deleteLater()
            self.dialog.messageBar().pushWarning(self.tr('Credentials'), self.tr(
                'This QGIS version does not expose this action. Use QGIS Authentication settings.'))
            return
        # Native actions report success, cancellation or errors themselves. Relay
        # their messages from the hidden settings widget into the plugin dialog.
        for bar in editor.findChildren(QgsMessageBar):
            bar.widgetAdded.connect(self._relayAuthMessage)
        self._auth_action_running = True
        self.timer.stop()
        self.updateControls()
        try:
            QMetaObject.invokeMethod(editor, action, Qt.ConnectionType.DirectConnection)
        finally:
            editor.deleteLater()
            self._auth_action_running = False
            self.refresh()
            self.timer.start(500)

    def _relayAuthMessage(self, item):
        self.dialog.messageBar().pushMessage(
            item.title(), item.text(), level=item.level(), duration=item.duration())

    def save(self):
        key = self.keyEdit.text().strip()
        try:
            if self.unlock():
                credentials.save_key(key)
                # Keep masked draft on cancellation/failure, clear only after success.
                self.keyEdit.setText('')
        except QgsProcessingException as error:
            self.dialog.messageBar().pushWarning(self.tr('Credentials'), str(error))
        finally:
            key = None
            self.refresh()

    def remove(self):
        self.keyEdit.setText('')
        try:
            if self.unlock():
                credentials.remove_key()
        except QgsProcessingException as error:
            self.dialog.messageBar().pushWarning(self.tr('Credentials'), str(error))
        self.refresh()

    def clear(self):
        self.keyEdit.setText('')
