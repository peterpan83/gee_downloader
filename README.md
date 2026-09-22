# EO Downloader

Download satellite images as GeoTIFFs for a given AOI (shapefile) and date range.
Supports four backends selectable via `GLOBAL.backend` in the config file.

## Backends

| Backend | Key | Satellites | Notes |
|---------|-----|------------|-------|
| Google Earth Engine | `gee` | S2, S1, Landsat 8/9, AlphaEarth | Tiles AOI into sub-cells to stay within GEE pixel limits |
| Google Cloud Storage | `gcld` | S2 L1 (SAFE) | Downloads via `gsutil`, generates TOA with ACOLITE |
| STAC API | `stac` | S2 L1/L2 | Uses Element84 AWS Earth Search or any STAC endpoint |
| Copernicus Data Space | `cdse` | S3 OLCI L1 | Downloads via CDSE OData API, generates TOA with ACOLITE; cloud masking via IdePix or native OLCI flags |

## Installation

Install [uv](https://docs.astral.sh/uv/getting-started/installation/).
Dependencies are defined in `pyproject.toml` and locked in `uv.lock`.
The project currently requires Python 3.11.

For local installation, first install GDAL's native libraries and matching
Python bindings for your operating system and Python 3.11 interpreter. Verify
that this interpreter can run `from osgeo import gdal, gdal_array`.
Then create the uv environment using that interpreter (replace the example
path below with its actual location):

```bash
uv venv --python /path/to/python3.11 --system-site-packages
uv sync --locked
```

`--system-site-packages` lets the environment access the GDAL bindings installed
for the selected interpreter. GDAL is not installed by `uv sync`.

Alternatively, use the [Docker](#docker) instructions below, which include GDAL setup.

To add or update dependencies, use `uv add <package>` or `uv lock --upgrade`
and commit both `pyproject.toml` and `uv.lock` as appropriate.

**GEE / GCLD backends** — also install and authenticate the gcloud CLI:
```bash
gcloud auth login
gcloud auth application-default login
```
Register your Google Cloud project for Earth Engine: https://code.earthengine.google.com/register

**GCLD / CDSE backends** — require [ACOLITE](https://github.com/acolite/acolite) for TOA reflectance generation. Set `acolite_dir` in the config to your local ACOLITE clone.

**CDSE backend (cloud masking)** — controlled by `cloud_mask_method` in the `CDSE` config section:

| Value | Behaviour |
|-------|-----------|
| `idepix` (default) | ESA SNAP IdePix; auto-falls back to native flags on pre-AVX2 CPUs or if SNAP is unavailable |
| `native` | Native OLCI `qualityFlags.nc` bits (CLOUD, CLOUD_AMBIGUOUS, CLOUD_SHADOW); no SNAP required |

To enable IdePix:
1. Install [ESA SNAP](https://step.esa.int/main/download/snap-download/)
2. `uv sync --locked` (includes `esa-snappy`)
3. `<SNAP_dir>/bin/snappy-conf "$PWD/.venv/bin/python"`
4. Install the IdePix plugin: `<SNAP_dir>/bin/snap --modules --install org.esa.snap.idepix.core org.esa.snap.idepix.olci --nogui --nosplash`

**PROJ conflicts** — if another application (e.g. SeaDAS) sets `PROJ_LIB`/`PROJ_DATA` in the environment, set `proj_data` in the `GLOBAL` config section to override it before any geo library is loaded:
```yaml
GLOBAL:
  proj_data: /path/to/proj/data   # directory containing proj.db
```
To find the right path: `python -c "import pyproj; print(pyproj.datadir.get_data_dir())"`. See `docs/troubleshooting.md` for details.

## Usage

### Docker

Build and test run it:

```bash
docker build -t gee-downloader .
docker run --rm gee-downloader --help
```

Copy `download.yaml` to `docker-download.yaml` and edit it for your job. For
Earth Engine, set `GLOBAL.backend: gee`, `GLOBAL.aoi: /data/aoi.shp`,
`GLOBAL.save_dir: /output`, and `GEE.project_id` to your registered project.
Use container paths for any other input files and leave `proj_data` unset.
Mount the whole AOI directory so shapefile companion files are available.

Authenticate on the host using the Google commands above, then run:

```bash
mkdir -p output
docker run --rm \
  -e GOOGLE_APPLICATION_CREDENTIALS=/credentials/application_default_credentials.json \
  -v "$HOME/.config/gcloud:/credentials:ro" \
  -v "$PWD/docker-download.yaml:/config/download.yaml:ro" \
  -v "$PWD/data:/data:ro" \
  -v "$PWD/output:/output" \
  gee-downloader -c /config/download.yaml
```

The image includes GDAL and `gsutil`. Configuration and credentials are supplied
at runtime. ACOLITE and the SNAP runtime/plugins are not included: GCLD/CDSE
TOA processing requires an additional ACOLITE installation and its dependencies;
CDSE IdePix masking also requires SNAP configuration as described above.

### Local Python

```bash
uv run --locked main.py -c download.yaml
```

Edit `download.yaml` to set the backend, AOI path, date range, assets, and output directory. A timestamped copy of the config is saved to `save_dir` on each run.

### Date range

Two formats are supported in `GLOBAL`:

**Single range** — one continuous window:
```yaml
start_date: '2025-09-20'
end_date:   '2025-10-20'
```

**Multi-year seasonal window** — repeat the same window for each year in `[start_year, end_year]`:
```yaml
start_year: 2020
end_year:   2025
start_date: '09-20'   # MM-DD
end_date:   '10-20'   # MM-DD
```

Cross-year windows (e.g. `start_date: '12-01'`, `end_date: '02-28'`) are handled correctly — the end falls in year+1.

## Supported assets

`S2_L1TOA`, `S2_L2SURF`, `S2_L2RGB`, `LC08_L1TOA`, `LC09_L1TOA`, `S1_L1C`, `S3_L1TOA`, `ALPHAEARTH_V1`

## Output structure

```
<save_dir>/<project_name>/<asset_savedir>/<sensor>/<aoi_name>/<year>/
    <ASSET>_<acquisition_time>_<aoi_name>_<resolution>m.tif
```

S3 OLCI output GeoTIFFs contain 21 TOA reflectance bands (`rhot_*`), observing geometry angles (SZA/VZA/RAA), and a classification band (`0`=clear land, `1`=clear water, `2`=cloud land, `3`=cloud water, `255`=invalid).
