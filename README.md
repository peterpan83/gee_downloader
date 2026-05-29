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

```bash
pip install -r requirements.txt
```

**GEE / GCLD backends** — also install and authenticate the gcloud CLI:
```bash
gcloud auth login
gcloud auth application-default login
```
Register your Google Cloud project for Earth Engine: https://code.earthengine.google.com/register

**GCLD / CDSE backends** — require [ACOLITE](https://github.com/acolite/acolite) for TOA reflectance generation. Set `acolite_dir` in the config to your local ACOLITE clone.

**CDSE backend (cloud masking)** — IdePix via ESA SNAP is used when available, with automatic fallback to native OLCI quality flags. To enable IdePix:
1. Install [ESA SNAP](https://step.esa.int/main/download/snap-download/)
2. `pip install esa_snappy`
3. `<SNAP_dir>/bin/snappy-conf <python_executable>`
4. Install the IdePix plugin: `<SNAP_dir>/bin/snap --modules --install org.esa.snap.idepix.core org.esa.snap.idepix.olci --nogui --nosplash`

## Usage

```bash
python main.py -c download.yaml
```

Edit `download.yaml` to set the backend, AOI path, date range, assets, and output directory. A timestamped copy of the config is saved to `save_dir` on each run.

## Supported assets

`S2_L1TOA`, `S2_L2SURF`, `S2_L2RGB`, `LC08_L1TOA`, `LC09_L1TOA`, `S1_L1C`, `S3_L1TOA`, `ALPHAEARTH_V1`

## Output structure

```
<save_dir>/<project_name>/<asset_savedir>/<sensor>/<aoi_name>/<year>/
    <ASSET>_<acquisition_time>_<aoi_name>_<resolution>m.tif
```

S3 OLCI output GeoTIFFs contain 21 TOA reflectance bands (`rhot_*`), observing geometry angles (SZA/VZA/RAA), and a classification band (`0`=clear land, `1`=clear water, `2`=cloud land, `3`=cloud water, `255`=invalid).
