# USGS 3DEP download timeout: observations and troubleshooting

## Observed issue

During manual testing on October 14, 2026, the **USGS 3DEP 1 meter DEM**
download succeeded for one extent but timed out for a larger extent.

Environment: QGIS 3.44.9-Solothurn (a997df02), Qt 5.15.18,
Python 3.12.11, and GDAL 3.12.0 on macOS.

| Test | Result | Duration |
| --- | --- | --- |
| Smaller extent | GeoTIFF downloaded successfully; algorithm completed | 32.59 seconds |
| Larger extent | Network request timed out | 59.71 seconds |

Both requested areas were below OpenTopography's documented 250 km² limit
for USGS 1 m requests. An allowed request area does not guarantee completion
within the client's network timeout.

## Interpretation

The successful request provides live evidence that the new USGS 1 m endpoint
and parameter mapping work, and that the user's key had access to data for
that extent. It does not establish coverage or availability for every extent,
or validate the 10 m and 30 m products.

The second request explicitly reported a network timeout. Its duration is
consistent with QGIS's default network timeout of 60,000 milliseconds.
The logs do not establish whether the delay arose from OpenTopography
processing, the upstream USGS service, or network conditions. They do not
establish that the larger area alone caused the delay.

The original plugin and initial USGS extension used QGIS's native file
downloader processing algorithm. The credential update below now uses
`QgsFileDownloader` directly with the same QGIS network timeout. The first implementation of the extension replaced failures with
a generic message to avoid exposing credential-bearing URLs, which obscured
the timeout diagnosis. Updated error handling identifies timeout and
access-denied messages while avoiding raw URLs and credentials. It does not
automatically increase the timeout or retry the request.

## Suggested workaround

1. Reload the updated plugin using Plugin Reloader, or restart QGIS.
2. Open QGIS **Options → Network** (through Settings or Preferences, depending
   on the platform).
3. Set **Timeout for network requests (ms)** to `300000` (five minutes),
   then apply the setting.
4. Retry the download. If it still times out, try a smaller extent, such as
   approximately 1 km × 1 km, or retry later if the service is slow.

This timeout setting affects QGIS generally, not only this plugin. A longer
timeout allows more waiting but does not guarantee success or override
server-side limits. Automatic tiling is not implemented.

OpenTopography documents these request-area limits:

| Dataset | API identifier | Maximum area |
| --- | --- | --- |
| USGS 3DEP 1 arc-second DEM (~30 m) | `USGS30m` | 225,000 km² |
| USGS 3DEP 1/3 arc-second DEM (~10 m) | `USGS10m` | 25,000 km² |
| USGS 3DEP 1 meter DEM | `USGS1m` | 250 km² |

The 1 m product requires an appropriately authorized OpenTopography API key;
increasing the timeout does not resolve authorization or missing-coverage errors.

## Validation still needed

The reported logs confirm one successful live 1 m download and one timeout.
A successful retry of the larger extent after increasing the timeout has not
yet been reported. Record that outcome, including duration and extent size,
before claiming this workaround resolves the observed case.

## Sharing diagnostic logs

Old processing logs and exported Python commands can include `OT_AUTH_TOKEN`.
The credential update below removes that parameter from new runs. Old logs and
exports are not cleaned automatically; redact them and rotate any exposed key. No API keys
or credential-bearing request URLs are included in this note.

## References

- [QGIS 3.44 network settings](https://docs.qgis.org/3.44/en/docs/user_manual/introduction/qgis_configuration.html#network-settings)
- [OpenTopography API documentation](https://portal.opentopography.org/apidocs/)
- [OpenTopography API specification](https://portal.opentopography.org/apidocs/openapi.json)

## Dataset coverage button

The interactive downloader has a **Show coverage** button beside the DEM
selector. It fetches the selected dataset's footprint from OpenTopography's
`getGeoJson?host=OT&id=<OpenTopoID>&isRaster=true` endpoint and adds an orange
outline with a lightly transparent orange fill (~16% opacity) to QGIS. No API key
is sent for coverage requests.

Requests are asynchronous and use QGIS's network settings. Closing the dialog
cancels a pending request. Network errors or invalid/empty polygon responses show
a warning without adding a layer. Repeated clicks reuse an existing coverage
layer and make it visible, without changing the map extent or download extent.
The response is stored as inline GeoJSON in the layer source so it can be saved
with the QGIS project without depending on a temporary file.

The dataset IDs are mapped by API code in `OpenTopography_DEMDownloader_coverage.py`.
The custom panel only changes the layout around the existing DEM widget. The credential update below removes the API-key parameter while retaining
Python exports and batch/model support. Coverage display is an interactive convenience,
not an additional processing parameter.

On October 15, 2026, all 18 mapped public endpoint URLs returned polygon or
multipolygon GeoJSON. Coverage is a visual guide, not a guarantee of availability
at every location. Some global products return simple rectangular footprints;
USGS 1 m coverage may change as data is added.

Tests cover the default selection, dataset/extent/output parameter wrappers and the batch button,
ID mappings, polygon parsing, asynchronous loading, duplicate reuse, network
failure recovery, and cancellation when closing the dialog. Manual release check:
reload the plugin, open the downloader from the menu/toolbox, click Show coverage,
and check the outline and Layers panel for both COP30 and USGS1m.

## Encrypted single-key workflow

The Credentials tab saves one OpenTopography key per QGIS authentication database
(normally one per profile). It offers a masked new-key field, Save / Replace,
and Remove OT key. There is no separate Unlock button; saving/replacing and
removing a key request QGIS authentication when needed. When unlocked, the status displays only the last
four characters. The input is never populated with the stored key and is cleared
after a successful save, successful credential preflight for execution, or closing
the dialog. Canceling unlock or a failed save preserves the masked draft for retry. A locked database shows
“Key saved — locked”, without displaying a suffix.

The Parameters tab shows credential status and a Change credentials button.
Tab order is Parameters, Log, Credentials: the native QGIS processing dialog uses
index 1 internally for Log, so the new tab is appended to preserve that behavior.
Coverage display continues to work without credentials.

### Storage and removal

`OpenTopography_DEMDownloader_credentials.py` uses QGIS authentication storage.
The key is held in the encrypted password field of a dedicated Basic config;
this is a storage container, not HTTP Basic authentication sent to OT.
The non-secret configuration ID is stored in
`OpenTopographyDEMDownloader/auth_config_id`. The stable default ID `otdem01`
allows a separate process using the same authentication database to find it
without copying desktop settings. An unrelated config occupying this ID is
never overwritten or removed.

The old plaintext `OpenTopographyDEMDownloader/ot_api_key` setting is removed on
algorithm initialization without reading or automatically migrating its value.
Users must save their key once in Credentials. No plugin-specific master password
is introduced; QGIS handles creation and unlocking of its authentication database.
Save/replace/remove controls are unavailable while this dialog is downloading or
another plugin download holds the key. The backend checks this too. Removal
clears the plugin's reference and its own auth config, not other QGIS credentials.
It does not revoke the key at OT or clear historical logs/backups.

### Python and batch

`OT_AUTH_TOKEN` is no longer a processing parameter. Old history restoration
ignores it. Explicit legacy parameters are rejected, but callers can log input
arguments before the plugin runs: remove the old field from scripts and JSON.

```python
processing.run("OTDEMDownloader:OpenTopography DEM Downloader", {
    "DEMs": 17,
    "Extent": "-118.01,-118.00,34.00,34.01 [EPSG:4326]",
    "OUTPUT": "/path/to/dem.tif"
})
```

Batch processing retains dataset, extent, and output columns; every row uses the
saved key. Opening batch from this plugin's dialog prompts for unlocking first.
When entering batch directly from the toolbox, unlock authentication first through
QGIS authentication settings, or open batch through this plugin's dialog. A locked/missing credential fails with an actionable
error instead of prompting from a worker thread. Models use the same three inputs.

### Unattended command and JSON execution

Install/enable the plugin in the execution environment. Configure access to the
same QGIS authentication database and provision unlocking before starting QGIS:

```sh
export QGIS_AUTH_DB_DIR_PATH="/path/to/auth-database-directory"
export QGIS_AUTH_PASSWORD_FILE="/path/to/protected/master-password-file"
qgis_process run "OTDEMDownloader:OpenTopography DEM Downloader" -- \
  DEMs=17 \
  'Extent=-118.01,-118.00,34.00,34.01 [EPSG:4326]' \
  OUTPUT=/path/to/dem.tif
```

The master-password file contains the QGIS master password on its first line.
Provision it outside exported commands through the job environment, restrict
access to the job's OS account, and do not commit it. The file itself is a secret;
encryption is not a substitute for protecting unattended unlocking credentials.
Set these variables before process startup. A copied script or project does not
copy the saved key. Each machine/account needs its own setup.

For JSON execution, save this as `job.json`:

```json
{
  "inputs": {
    "DEMs": 17,
    "Extent": "-118.01,-118.00,34.00,34.01 [EPSG:4326]",
    "OUTPUT": "/path/to/dem.tif"
  }
}
```

```sh
qgis_process run "OTDEMDownloader:OpenTopography DEM Downloader" - < job.json
```

### Request handling and boundaries

The worker retrieves the saved key only for execution. No key, suffix, or full
request URL is placed in processing parameters or plugin feedback. The downloader
uses `QgsFileDownloader` directly because the native downloader processing
algorithm logs its URL. Errors are classified into fixed diagnostic messages.
Cancellation and exceptions release plugin-held references; there is no plugin
session-wide plaintext key cache. Python/Qt cannot guarantee memory zeroing, and
QGIS may maintain its own authentication caches.

This protects against plaintext profile storage and ordinary processing-log
exposure. It does not isolate secrets from malicious plugins running in the same
process. OT still requires the API key in an HTTPS query string, which may appear
in network diagnostics or server logs. No live API credential is used by tests.

### Verification

Tests cover real encrypted save/replace/remove in a temporary auth database,
locked/missing failures without prompting, preserving unrelated auth configs,
last-four display, legacy history filtering, secret-free exports, request cleanup,
Python execution, and two rows through the native QGIS batch executor.
A fresh process verifies QGIS password-file unlocking. Real `qgis_process` command
and JSON runs use a test algorithm subclass that redirects only the raster URL
to a local fixture. User plugins and user settings are excluded from these tests.
The tests require a QGIS Python runtime and `qgis_process` for CLI integration.

Manual release check: reload the plugin, save a key in Credentials, verify the
suffix, download a small authorized area, replace/remove the saved key, and verify
missing/locked status. Check master-password prompts and the batch entry points
in desktop QGIS. Older supported QGIS versions still need separate validation.

References: [QGIS authentication infrastructure](https://docs.qgis.org/3.40/en/docs/pyqgis_developer_cookbook/authentication.html)
and [authentication overview](https://docs.qgis.org/3.40/en/docs/user_manual/auth_system/auth_overview.html).

## Master-password guidance and retry behavior

The Credentials tab now distinguishes authentication not configured, database
locked, unlocked with no OT key, saved key available, and authentication
unavailable. It uses QGIS's password-hash and unlocked-state checks rather than
assuming that absence of an OT key means no master password exists.

There is no separate setup or unlock button. **Save key / Replace key** invokes
QGIS master-password setup or unlocking when required; **Remove OT key**
unlocks before deleting the saved OT key. Canceling authentication prevents the
requested save or removal. Already unlocked storage does not prompt again.
Run and the dialog’s batch entry retain their automatic unlock preflight.
An explanation is shown before QGIS prompts: its master password protects authentication credentials
for QGIS and other plugins, and is separate from the OT API key. The plugin never
creates its own master password or automatically resets QGIS authentication.

Unattended configuration is documented above in the command and JSON examples.
Unavailable authentication disables credential actions and points to QGIS authentication
settings; status is refreshed as the database is locked/unlocked or credentials
are removed in another window.

Canceling unlocking or encountering a save failure retains the masked input for
retry. Successful saving clears the field and its undo history; closing the
dialog also clears it. Tests cover these states, button availability, and retry
behavior alongside the existing encryption, command/JSON, and batch tests.


The plugin temporarily hides QGIS’s “Do not forget it: NOT retrievable!” and
“Saved for session, until app is quit.” notices in the native master-password
prompt while its Credentials page is visible, including a deferred prompt after
a QGIS database reset. Password validation, Keychain controls,
and session caching are unchanged. The notices are restored after the prompt
closes so QGIS settings and other plugins retain their standard dialog.


### Credentials page layout and security buttons

An introduction above the API-key section explains the OpenTopography API key,
QGIS master password, and system password storage using examples for macOS,
Windows, and Linux. The introduction is a read-only, scrollable area with a
bounded height so the key and security controls remain accessible.

The **OpenTopography API key** section has a masked New API key field, Save key /
Replace key, and Remove OT key. Its status is No key saved, Saved key with only
the last four characters, or Saved key — locked. It never fills the input with
the saved key.

A separate **QGIS secure storage** section shows Not configured, Locked, Unlocked,
or Unavailable. The next-step and automatic-unlock explanations are available
in the status tooltip, keeping the page compact. It distinguishes Keychain/wallet
integration being enabled from a password actually being stored there; enabling
integration alone is not proof that automatic unlocking will succeed.

Three visible buttons provide security actions, with explanations in their tooltips.
The status and Lock storage share the top row. Change master password and Forget
password use matching full-width buttons below. A divider and Recovery heading
separate Erase all QGIS credentials, with the visible warning “Removes saved
credentials for all QGIS plugins.” Compact spacing separates the API-key workflow
from secure-storage management.

- **Change master password…** opens QGIS’s native password-change workflow,
  including its current-password check, backup option, and Keychain update option.
- **Forget password in Keychain…** (wallet name varies by platform) opens QGIS’s
  native removal confirmation. Only the wallet copy is removed. The existing
  master password and encrypted OT key remain; QGIS may still be unlocked.
- **Lock storage** clears the master password cached in QGIS memory and
  updates the status immediately. It does not delete the wallet copy or OT key.
  Keychain or another configured unlock method may unlock again automatically;
  already authenticated network connections may remain cached.

These actions affect credentials used by other plugins in the same authentication
database. They are disabled during this plugin’s downloads or another security
action. Change password is unavailable before setup; Lock is available only when
unlocked. **Erase all QGIS credentials…** is separated below the other buttons. It opens
QGIS’s native confirmation and backup/erasure workflow, including when storage
is locked and the master password is forgotten. This affects every credential
in the authentication database, not only the OT key. The plugin does not erase
anything on button click without QGIS’s confirmation. Tests cancel this dialog
while locked and verify that the existing master password and saved key remain usable.

The plugin delegates to native Qt slots on `QgsAuthEditorWidgets`, checks their
availability, and relays native result messages. It never reads or stores the
master password. Versions without those slots receive a message directing users
to QGIS Authentication settings. Tests cover native dialog cancellation, locking
without deleting the encrypted OT key, separate key/storage states, button availability,
and operation guards.


#### Systems without usable password storage

The wallet button is disabled when QGIS integration is disabled or QGIS reports
that a password-helper operation failed. A short visible hint explains that users
can enter their QGIS master password when prompted. Saving/replacing/removing the
OT key, changing the master password, and locking storage continue to use QGIS’s
encrypted database independently of the OS wallet. A later QGIS helper success
clears the failure state. No raw helper error or password is displayed by the
plugin, and no active Keychain probe is performed.

QGIS’s Python API provides an enabled preference and success/failure signals,
not a reliable backend-availability query. Enabled integration is therefore not
presented as proof of support or a saved password. Previously unobserved backend
problems are handled when QGIS reports them. Users can configure integration in
QGIS Authentication settings; actual Windows/Linux backend testing remains a
release check. Tests simulate disabled integration, failure, and recovery.


Native prompt text filtering uses Qt runtime class detection so dialogs created
by QGIS itself are recognized even when Python exposes a generic dialog wrapper.
It applies after a dialog is shown and also covers delayed prompts while the
plugin dialog is visible. The erase confirmation omits the parenthetical backup
sentence; its destructive confirmation, backup behavior and erasure are unchanged.
Original native text is restored when the dialog closes.
