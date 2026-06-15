# Troubleshooting

## PROJ database version mismatch

### Symptom A — foreign PROJ installation takes priority

```
ERROR 1: PROJ: internal_proj_create_from_database:
/opt/seadas/ocssw/opt/share/proj/proj.db lacks
DATABASE.LAYOUT.VERSION.MAJOR / DATABASE.LAYOUT.VERSION.MINOR metadata.
It comes from another PROJ installation.
```

**Cause**: A third-party application (e.g. SeaDAS) sets `PROJ_LIB` or `PROJ_DATA`
to its own PROJ data directory in its environment scripts. That directory is picked
up before the Python environment's own `proj.db`, which is invalid.

**Fix**: Override `PROJ_DATA` to point to the `proj.db` that matches your Python
environment, **before** importing any geo library (`rasterio`, `geopandas`, etc.):

```python
import os
import pyproj
os.environ['PROJ_DATA'] = pyproj.datadir.get_data_dir()

import rasterio   # must come after
```

Check which environment variable is causing the conflict:

```bash
echo $PROJ_LIB
echo $PROJ_DATA
```

If either points to a third-party directory (e.g. `/opt/seadas/...`), unset it
before running:

```bash
unset PROJ_LIB
unset PROJ_DATA
python main.py -c download.yaml
```

---

### Symptom B — pyproj's own `proj.db` is too old

```
ERROR 1: PROJ: internal_proj_create_from_database:
/home/user/anaconda3/envs/myenv/lib/python3.9/site-packages/pyproj/proj_dir/share/proj/proj.db
contains DATABASE.LAYOUT.VERSION.MINOR = 2 whereas a number >= 3 is expected.
It comes from another PROJ installation.
```

**Cause**: The `proj` package in the conda environment is older than the PROJ shared
library being used by GDAL/rasterio. The two are version-mismatched.

**Fix — preferred**: Update `proj` and `pyproj` together via conda-forge so their
versions are resolved consistently:

```bash
conda activate <your-env>
conda install -c conda-forge proj pyproj --update-deps
```

**Fix — alternative** (if you cannot modify the environment): Find a `proj.db` on
the system with `DATABASE.LAYOUT.VERSION.MINOR >= 3` and point `PROJ_DATA` to it:

```bash
# Find candidate proj.db files
find /usr /opt/conda /home -name "proj.db" 2>/dev/null

# Check the minor version of each candidate
sqlite3 /path/to/proj.db \
  "SELECT value FROM metadata WHERE key='DATABASE.LAYOUT.VERSION.MINOR';"
```

Then set it before importing geo libraries:

```python
import os
os.environ['PROJ_DATA'] = '/path/to/dir/containing/proj.db'

import rasterio
```

---

### General rule

Always set `PROJ_DATA` **before** importing `rasterio`, `geopandas`, `pyproj`, or
any GDAL-dependent library. Once the GDAL/PROJ shared library is loaded, changing
the environment variable has no effect.
