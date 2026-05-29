# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the downloader

```bash
python main.py -c download.yaml
```

The `-c` flag takes a `.yaml` or `.ini` config file. A copy of the config is saved to `save_dir` on each run for reproducibility.

## Authentication setup

Required before first use (GEE and GCLD backends):
```bash
gcloud auth login
gcloud auth application-default login
```

For GEE, also set `project_id` in the `GEE` section of the config. On first run, `ee.Authenticate()` is called automatically if initialization fails.

## Architecture

The tool downloads EO satellite imagery for a set of AOI polygons (from a shapefile) over a date range. The entry point is `main.py`, which reads the config and dispatches to the appropriate backend downloader.

### Backend selection

`download.yaml` → `GLOBAL.backend` selects one of four backends:

| Backend | Class | Source |
|---------|-------|--------|
| `gee` | `GEEDownloader` | Google Earth Engine API (direct pixel download) |
| `gcld` | `GCLDDownloader` | Google Cloud Storage (`gsutil`) → ACOLITE TOA processing |
| `stac` | `STACDownloader` | STAC API (e.g. Element84 AWS) via vendored `GeoanalyticsDownloader` |
| `cdse` | `CDSEDownloader` | Copernicus Data Space Ecosystem OData API |

### Class hierarchy

```
Downloader (downloader.py)          ← base class; parses config, loads AOI shapefile,
                                       categorizes assets, reads date_csv or start/end dates
    ├── GEEDownloader (gee/downloader.py)
    ├── GCLDDownloader (gcld/downloader.py)
    ├── STACDownloader (stac/downloader.py)
    └── CDSEDownloader (cdse/downloader.py)
```

`Downloader.__init__` sets `self.save_dir`, `self.proj_gdf`, `self.asset_dic`, and date configuration. All backends call `super().__init__(**config)` first and then implement `run()`.

### Asset configuration

Assets are defined in `download.yaml` under `ASSETS`. Each asset entry (e.g. `S2_L1TOA`) specifies:
- `gee_source` / `stac_source` / `gcld_source` / `cdse_source`: collection identifier per backend
- `include_bands`: comma-separated band list
- `resolution`: native resolution in meters
- `save_dir`: subdirectory under the project save directory
- `anonym`: generic sensor name used in output filenames

`Downloader.__categorize_assets()` groups assets by sensor prefix (`s2`, `lc08`, `lc09`, `s1`, `alphaearth`) into `optical`, `radar`, or `embedding` sensor types, which drives which download method is dispatched.

### Output file naming convention

```
<save_dir>/<asset_savedir>/<anonym>/<aoi_name>/<year>/<ASSET>_<acquisitiontime>_<aoi_name>_<resolution>m.tif
```

GEE downloads tile sub-cells first to a temp directory, then merges them into the final file via `merge_download_dir` or `merge_download_dir_obsgeo` (for observing geometry bands).

### Date-driven vs. range-driven downloads

- `date_csv` (CSV with columns `name`, `date` YYYYMMDD, optionally `product_ids`, `sensor`): overrides `start_date`/`end_date`; required for GCLD and CDSE backends
- `start_date` + `end_date`: used by GEE and STAC backends for full date-range iteration

### Config loading

`utils.load_config_file` reads both `.yaml` and `.ini` formats. All config keys are lowercased when loaded. The `ASSETS` list in YAML is transformed into per-asset dict entries keyed by lowercased asset name (e.g. `s2_l1toa`).

### Key utility modules

- `utils.py`: config loading, raster reprojection, mosaic merging, raster flip correction
- `gee/utils.py`: GEE-specific tiling, parallel tile download, band merging
- `gcld/gen_toa.py`: wraps ACOLITE for L1 TOA reflectance generation from SAFE files
- `shared/tools.py`: shared raster helpers

## Dependencies

Core: `rasterio`, `geopandas`, `earthengine-api`, `pendulum`, `plumbum`, `gdal/osgeo`  
GEE backend: `google-cloud-bigquery`, `db_dtypes`  
GCLD backend: `gsutil` CLI, `acolite` (external path, set `acolite_dir` in config)  
CDSE backend: `requests`, optional `esa_snappy` for cloud masking
