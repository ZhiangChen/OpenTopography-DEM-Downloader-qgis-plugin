# SPDX-License-Identifier: GPL-2.0-or-later
"""One encrypted OT key per authentication database; never a processing input."""
from contextlib import contextmanager
from threading import RLock

from qgis.core import QgsApplication, QgsAuthMethodConfig, QgsSettings, QgsProcessingException

REFERENCE = 'OpenTopographyDEMDownloader/auth_config_id'
LEGACY_KEY = 'OpenTopographyDEMDownloader/ot_api_key'
DEFAULT_ID = 'otdem01'
CONFIG_NAME = 'OpenTopography DEM Downloader API key'
_lock = RLock()
_active_requests = 0


def manager():
    return QgsApplication.authManager()


def config_id():
    # A stable fallback also lets qgis_process use the same auth DB without copying
    # desktop QSettings. Never overwrite an unrelated configuration with this ID.
    return QgsSettings().value(REFERENCE, DEFAULT_ID) or DEFAULT_ID


def existing_config():
    return manager().availableAuthMethodConfigs().get(config_id())


def owns(config):
    return config is not None and config.name() == CONFIG_NAME and config.method() == 'Basic'


def busy():
    with _lock:
        return _active_requests > 0


def unlock():
    """Only UI entry points call this: workers and unattended jobs never prompt."""
    auth = manager()
    return not auth.isDisabled() and (auth.masterPasswordIsSet() or auth.setMasterPassword(True))


def read_key():
    auth = manager()
    if auth.isDisabled():
        raise QgsProcessingException('QGIS authentication is unavailable.')
    config = existing_config()
    if not owns(config):
        raise QgsProcessingException('No OpenTopography key is saved. Open the Credentials tab to save one.')
    if not auth.masterPasswordIsSet():
        raise QgsProcessingException(
            'QGIS authentication is locked. Unlock it before running. For unattended jobs, '
            'configure the authentication database and its master-password provisioning before startup.')
    ok, config = auth.loadAuthenticationConfig(config_id(), QgsAuthMethodConfig(), True)
    if not ok or not owns(config) or not config.config('password'):
        raise QgsProcessingException('Could not read the saved OpenTopography key. Replace it in Credentials.')
    return config.config('password')


def save_key(key):
    key = key.strip()
    if len(key) < 5 or any(c.isspace() for c in key):
        raise QgsProcessingException('Enter an API key of at least five characters without whitespace.')
    with _lock:
        if busy():
            raise QgsProcessingException('Wait for active downloads to finish before replacing the key.')
        if not manager().masterPasswordIsSet():
            raise QgsProcessingException('Unlock QGIS authentication before saving the key.')
        old = existing_config()
        if old is not None and not owns(old):
            raise QgsProcessingException('The authentication ID is used by another configuration.')
        config = QgsAuthMethodConfig()
        config.setId(config_id())
        config.setName(CONFIG_NAME)
        # Use QGIS's supported encrypted password storage, not HTTP Basic on the wire.
        # OT needs API_Key in its query; this config is never attached to a request.
        config.setMethod('Basic')
        config.setConfig('username', 'OpenTopography')
        config.setConfig('password', key)
        if old is not None:
            ok = manager().updateAuthenticationConfig(config)
        else:
            ok, config = manager().storeAuthenticationConfig(config)
        if not ok:
            raise QgsProcessingException('Could not save the key in QGIS authentication.')
        QgsSettings().setValue(REFERENCE, config.id())
        QgsSettings().remove(LEGACY_KEY)


def remove_key():
    with _lock:
        if busy():
            raise QgsProcessingException('Wait for active downloads to finish before removing the key.')
        config = existing_config()
        if config is not None:
            if not owns(config):
                raise QgsProcessingException('Refusing to remove an unrelated authentication configuration.')
            if not manager().masterPasswordIsSet():
                raise QgsProcessingException('Unlock QGIS authentication before removing the key.')
            if not manager().removeAuthenticationConfig(config_id()):
                raise QgsProcessingException('Could not remove the saved key.')
        QgsSettings().remove(REFERENCE)
        QgsSettings().remove(LEGACY_KEY)


@contextmanager
def request_key():
    global _active_requests
    key = None
    with _lock:
        key = read_key()
        _active_requests += 1
    try:
        yield key
    finally:
        key = None  # Release references; Python cannot guarantee memory zeroing.
        with _lock:
            _active_requests -= 1
