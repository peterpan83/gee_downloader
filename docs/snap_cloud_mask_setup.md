# SNAP IdePix Cloud Mask Setup Guide

This guide covers setting up ESA SNAP with the IdePix plugin and `esa_snappy` for Sentinel-3 OLCI cloud masking.

> **Fallback behaviour**: if `esa_snappy` or the IdePix plugin is not available, the tool automatically falls back to native OLCI quality flags (bits 27/26/14 in `qualityFlags.nc`). No SNAP installation is required for the fallback path.

---

## Step 1 — Install ESA SNAP

Download the **Sentinel Toolbox** installer from the ESA SNAP website. Choose the package that includes **S3TBX** (Sentinel-3 Toolbox), which provides OLCI support.

After installation, verify it works:

```bash
snap --version
```

Note the SNAP installation path — you will need it in the steps below (e.g. `/usr/local/snap` or `~/snap`).

---

## Step 2 — Install the IdePix Plugin

### Option A — Command line (headless machines)

```bash
/path/to/snap/bin/snap --nosplash --nogui \
  --modules --install org.esa.snap.idepix.olci
```

### Option B — SNAP GUI

Open SNAP desktop and go to:

**Tools → Plugins → Available Plugins → search "IdePix" → Install → Restart SNAP**

---

## Step 3 — Configure `esa_snappy`

`esa_snappy` is SNAP's Python bridge. Run the configuration script that ships with SNAP, pointing it at your Python executable:

```bash
/path/to/snap/bin/snappy-conf /path/to/your/python

# Example with a conda environment:
/usr/local/snap/bin/snappy-conf ~/anaconda3/envs/sme-chain/bin/python
```

This generates the `esa_snappy` package in `~/.snap/snap-python/esa_snappy` (or the path printed by `snappy-conf`). Then install it into your environment:

```bash
cd ~/.snap/snap-python/esa_snappy
pip install .
```

---

## Step 4 — Verify the Installation

Run the following in your Python environment to confirm both `esa_snappy` and the IdePix operator are available:

```python
import esa_snappy as snappy

GPF      = snappy.jpy.get_type('org.esa.snap.core.gpf.GPF')
registry = GPF.getDefaultInstance().getOperatorSpiRegistry()
op       = registry.getOperatorSpi('Idepix.Olci')

print("IdePix available:", op is not None)
```

If the output is `False`, the IdePix plugin was not installed correctly — repeat Step 2.

---

## Step 5 — HDF5 / netCDF4 Import Order

There is a known HDF5 shared-library conflict between `netCDF4` and `esa_snappy`'s JVM. **`netCDF4` must be imported before `esa_snappy`** initialises its JVM, otherwise you will see:

```
undefined symbol: H5Pset_fapl_ros3
```

This ordering is already enforced in the codebase:

- `cloud_mask_sen3.py` imports `netCDF4` at the top before any SNAP code.
- `cdse/s3_cloud_mask.py` documents this requirement in `project_to_utm()`.

If you call these modules from your own script, make sure to follow the same pattern:

```python
import netCDF4          # must come first
import esa_snappy       # JVM starts here
```

---

## Step 6 — Test End-to-End

Run the standalone cloud mask script against a single Sentinel-3 OLCI L1B granule:

```bash
python cloud_mask_sen3.py \
  /path/to/S3A_OL_1_EFR____*.SEN3 \
  /path/to/output_toa.tif \
  /path/to/cloud_mask_out.tif
```

### Arguments

| Argument | Description |
|----------|-------------|
| `sen3_folder` | Path to the `.SEN3` directory (L1B granule) |
| `toa_tif` | TOA GeoTIFF whose UTM grid the cloud mask is projected onto |
| `output_tif` | Output cloud mask GeoTIFF path (uint8) |

### Output encoding

| Value | Meaning |
|-------|---------|
| `0` | Clear |
| `1` | Cloud / cloud-shadow (IdePix) |
| `255` | Outside swath / invalid |

A swath-coordinate diagnostic PNG (`*_swath.png`) is also written alongside the output GeoTIFF.

---

## Troubleshooting

### `Idepix.Olci operator not found`

The IdePix plugin is not installed or was not picked up by SNAP. Re-run Step 2 and restart SNAP before re-running `snappy-conf`.

### `esa_snappy not available` (RuntimeWarning)

`esa_snappy` is not installed in the active Python environment. The tool will fall back to native OLCI flags automatically. Re-run Step 3 if IdePix quality masking is required.

### PROJ database version mismatch

If you see:

```
PROJ: internal_proj_create_from_database: ... contains DATABASE.LAYOUT.VERSION.MINOR = 2
whereas a number >= 3 is expected
```

Set `PROJ_DATA` to a compatible `proj.db` **before** importing any geo library:

```python
import os
import pyproj
os.environ['PROJ_DATA'] = pyproj.datadir.get_data_dir()

import rasterio  # import after setting PROJ_DATA
```

Or update PROJ in your conda environment:

```bash
conda install -c conda-forge proj pyproj --update-deps
```

### JVM crash: `SIGILL` in `libtensorflow_framework.so` (older CPUs)

**Symptom**: The JVM crashes with a fatal `SIGILL` (illegal instruction) signal and a `hs_err_pidXXXX.log` is written. The crash frame points to `libtensorflow_framework.so` and the call chain includes `TensorflowNNCalculator` / `CtpOp`.

**Cause**: The Cloud Top Pressure (CTP) sub-operator inside IdePix uses a TensorFlow neural network. The TensorFlow native library bundled with SNAP is compiled with **AVX2** instructions. CPUs older than Intel Haswell (pre-2013, e.g. Sandy Bridge Xeon E7-4870) support only AVX, not AVX2, so the JVM crashes immediately when TensorFlow is loaded.

**Fix**: The `IdepixOlciOp` operator calls CTP internally from `preProcess()` regardless of any GPF parameter — it cannot be disabled via the Python API. The only safe workaround is to skip IdePix entirely on pre-AVX2 machines and fall back to native OLCI quality flags.

This is handled automatically: `build_cloud_mask()` reads `/proc/cpuinfo` at runtime and bypasses IdePix (and therefore TensorFlow) when AVX2 is not listed in the CPU flags. No code change is needed on your end — just pull the latest version and re-run.

To confirm the fallback is active, look for this warning in the output:

```
RuntimeWarning: CPU does not support AVX2 — IdePix skipped to avoid a fatal JVM crash
(libtensorflow_framework.so requires AVX2). Falling back to native OLCI quality flags.
```

The native flag fallback uses OLCI `qualityFlags.nc` bits (CLOUD bit 27, CLOUD_AMBIGUOUS bit 26, CLOUD_SHADOW bit 14) and produces results slightly coarser than IdePix but requires no SNAP or TensorFlow.

### Multiple SNAP installations

If there are multiple SNAP installations (e.g. SeaDAS ships its own), ensure `snappy-conf` was run against the correct SNAP instance. Check which `snap` is on your `PATH`:

```bash
which snap
snap --version
```
