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

The original plugin and the USGS extension use QGIS's native file
downloader processing algorithm. The first implementation of the extension replaced failures with
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

QGIS processing logs and exported Python commands can include `OT_AUTH_TOKEN`.
API-key input and saved-key behavior remain unchanged from the original plugin.
Remove credentials before sharing logs, and rotate any exposed key. No API keys
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
The custom panel only changes the layout around the existing DEM widget. All
processing parameters, API-key storage, Python exports, and batch/model support
retain their existing behavior. Coverage display is an interactive convenience,
not an additional processing parameter.

On October 15, 2026, all 18 mapped public endpoint URLs returned polygon or
multipolygon GeoJSON. Coverage is a visual guide, not a guarantee of availability
at every location. Some global products return simple rectangular footprints;
USGS 1 m coverage may change as data is added.

Tests cover the default selection, unchanged parameter wrappers and batch button,
ID mappings, polygon parsing, asynchronous loading, duplicate reuse, network
failure recovery, and cancellation when closing the dialog. Manual release check:
reload the plugin, open the downloader from the menu/toolbox, click Show coverage,
and check the outline and Layers panel for both COP30 and USGS1m.
