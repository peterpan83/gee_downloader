"""
Cloud masking for S3 OLCI.

Primary method  : ESA SNAP IdePix (requires esa_snappy + SNAP installation).
Fallback method : native OLCI quality flags from qualityFlags.nc (no SNAP needed).

Workflow
--------
1. run_idepix_olci()        : IdePix on a single .SEN3 folder →
                              cloud flag array in native swath coordinates
2. run_native_flags_olci()  : read qualityFlags.nc cloud bits →
                              cloud flag array in native swath coordinates
3. project_to_utm()         : project swath flags to the UTM output grid
4. build_cloud_mask()       : run steps 1/2 for every .SEN3 in a pass,
                              mosaic with logical OR, return combined mask
"""

import os
import numpy as np
from rasterio.crs import CRS
from rasterio.transform import Affine
from scipy.ndimage import binary_dilation, distance_transform_edt

def _cpu_has_avx2() -> bool:
    """Return True if the host CPU advertises AVX2 support via /proc/cpuinfo."""
    try:
        with open('/proc/cpuinfo', 'r') as f:
            for line in f:
                if line.startswith('flags') and 'avx2' in line.split():
                    return True
    except OSError:
        pass
    return False


# IdePix pixel_classif_flags bit positions
_IDEPIX_CLOUD_SURE      = 1 << 1
_IDEPIX_CLOUD_AMBIGUOUS = 1 << 2
_IDEPIX_CLOUD_BUFFER    = 1 << 4
_IDEPIX_CLOUD_SHADOW    = 1 << 5

# Native OLCI quality_flags bit positions (ESA Product Data Format Spec)
_OLCI_CLOUD          = 1 << 27
_OLCI_CLOUD_AMBIGUOUS = 1 << 26
_OLCI_CLOUD_SHADOW   = 1 << 14


def run_idepix_olci(
    l1b_path: str,
    cloud_buffer: bool = True,
    buffer_size: int = 2,
    compute_ctp: bool = False,
) -> np.ndarray:
    """
    Run IdePix on an OLCI L1B .SEN3 product.

    Parameters
    ----------
    compute_ctp : bool
        Enable Cloud Top Pressure (CTP) estimation via TensorFlow neural network.
        Disable on machines with CPUs older than Haswell (pre-AVX2) to avoid a
        SIGILL crash caused by AVX2 instructions in the bundled TensorFlow library.

    Returns
    -------
    np.ndarray (h, w) bool — True where cloud / cloud-shadow is detected.
    """
    import esa_snappy as snappy

    ProductIO = snappy.jpy.get_type('org.esa.snap.core.dataio.ProductIO')
    GPF       = snappy.jpy.get_type('org.esa.snap.core.gpf.GPF')
    HashMap   = snappy.jpy.get_type('java.util.HashMap')
    JBoolean  = snappy.jpy.get_type('java.lang.Boolean')
    JInteger  = snappy.jpy.get_type('java.lang.Integer')

    product = ProductIO.readProduct(l1b_path)

    params = HashMap()
    params.put('computeCloudBuffer',      JBoolean(cloud_buffer))
    params.put('cloudBufferSize',         JInteger(buffer_size))
    params.put('computeCloudShadow',      JBoolean(True))
    params.put('computeMountainShadow',   JBoolean(False))
    params.put('computeCloudTopPressure', JBoolean(compute_ctp))

    idepix = GPF.createProduct('Idepix.Olci', params, product)

    w = idepix.getSceneRasterWidth()
    h = idepix.getSceneRasterHeight()

    flags_data = np.zeros(w * h, dtype=np.int32)
    idepix.getBand('pixel_classif_flags').readPixels(0, 0, w, h, flags_data)
    flags_data = flags_data.reshape(h, w)

    product.dispose()
    idepix.dispose()

    return (
        (flags_data & _IDEPIX_CLOUD_SURE)      |
        (flags_data & _IDEPIX_CLOUD_AMBIGUOUS) |
        (flags_data & _IDEPIX_CLOUD_BUFFER)    |
        (flags_data & _IDEPIX_CLOUD_SHADOW)
    ).astype(bool)


def run_native_flags_olci(l1b_path: str) -> np.ndarray:
    """
    Derive a cloud mask from the native OLCI quality flags in qualityFlags.nc.

    Uses CLOUD (bit 27), CLOUD_AMBIGUOUS (bit 26), and CLOUD_SHADOW (bit 14).
    No SNAP dependency — works directly on the .SEN3 folder.

    Returns
    -------
    np.ndarray (h, w) bool — True where cloud / cloud-shadow is detected.
    """
    import netCDF4 as nc_lib

    qf_path = os.path.join(l1b_path, 'qualityFlags.nc')
    if not os.path.exists(qf_path):
        raise FileNotFoundError(f'qualityFlags.nc not found in {l1b_path}')

    with nc_lib.Dataset(qf_path, 'r') as ds:
        flags = np.asarray(ds['quality_flags'][:], dtype=np.int32)

    return (
        (flags & _OLCI_CLOUD)           |
        (flags & _OLCI_CLOUD_AMBIGUOUS) |
        (flags & _OLCI_CLOUD_SHADOW)
    ).astype(bool)


def project_to_utm(
    cloud_swath: np.ndarray,
    sen3_folder: str,
    dst_crs: CRS,
    dst_transform: Affine,
    dst_height: int,
    dst_width: int,
) -> np.ndarray:
    """
    Project a swath-coordinate cloud mask to the UTM output grid.

    Reads lat/lon from geo_coordinates.nc inside the .SEN3 folder and
    applies the same nearest-neighbour paint as the TOA reflectance bands.

    Note: netCDF4 must be imported before esa_snappy initialises its JVM to
    avoid a HDF5 shared-library conflict (H5Pset_fapl_ros3 undefined symbol).
    In the main downloader flow this ordering is guaranteed because
    _nc_to_geotiff imports netCDF4 before build_cloud_mask is called.
    """
    import netCDF4 as nc_lib
    from pyproj import Transformer

    geo_nc = os.path.join(sen3_folder, 'geo_coordinates.nc')
    ds = nc_lib.Dataset(geo_nc, 'r')
    lat_arr = np.asarray(ds['latitude'][:],  dtype=np.float64)
    lon_arr = np.asarray(ds['longitude'][:], dtype=np.float64)
    ds.close()

    epsg = dst_crs.to_epsg()
    tf = Transformer.from_crs(4326, epsg, always_xy=True)
    east, north = tf.transform(lon_arr.ravel(), lat_arr.ravel())
    east  = east.reshape(lat_arr.shape)
    north = north.reshape(lat_arr.shape)

    # Recover grid origin and pixel size from the affine transform
    e_min = dst_transform.c
    n_max = dst_transform.f
    res_m = abs(dst_transform.a)

    valid = np.isfinite(east) & np.isfinite(north)
    col_out = np.round((east  - e_min) / res_m).astype(np.int32)
    row_out = np.round((n_max - north) / res_m).astype(np.int32)
    in_bounds = (
        valid &
        (col_out >= 0) & (col_out < dst_width) &
        (row_out >= 0) & (row_out < dst_height)
    )

    out = np.zeros((dst_height, dst_width), dtype=bool)
    out[row_out[in_bounds], col_out[in_bounds]] = cloud_swath.ravel()[in_bounds.ravel()]

    # Fill diagonal stripe gaps caused by OLCI camera-stitch geometry.
    # Identical to the gap-fill in _nc_to_geotiff: dilate the coverage footprint
    # and propagate values from the nearest painted cell into uncovered gaps.
    coverage = np.zeros((dst_height, dst_width), dtype=bool)
    coverage[row_out[in_bounds], col_out[in_bounds]] = True
    fill_region = binary_dilation(coverage, iterations=3)
    gap_mask = fill_region & ~coverage
    if gap_mask.any():
        _, nn_idx = distance_transform_edt(~coverage, return_indices=True)
        out[gap_mask] = out[nn_idx[0][gap_mask], nn_idx[1][gap_mask]]

    return out


def build_cloud_mask(
    sen3_folders: list,
    dst_crs: CRS,
    dst_transform: Affine,
    dst_height: int,
    dst_width: int,
    cloud_buffer: bool = True,
    buffer_size: int = 2,
    compute_ctp: bool = False,
    force_native: bool = False,
):
    """
    Run IdePix on each .SEN3 granule, project to UTM, mosaic with logical OR.

    Returns
    -------
    np.ndarray (h, w) bool, or None if esa_snappy is not installed.
    """
    use_idepix = False
    if force_native:
        import warnings
        warnings.warn(
            'cloud_mask_method=native: using native OLCI quality flags '
            '(CLOUD bit 27, CLOUD_AMBIGUOUS bit 26, CLOUD_SHADOW bit 14).',
            RuntimeWarning,
            stacklevel=2,
        )
    elif not _cpu_has_avx2():
        import warnings
        warnings.warn(
            'CPU does not support AVX2 — IdePix skipped to avoid a fatal JVM crash '
            '(libtensorflow_framework.so requires AVX2). '
            'Falling back to native OLCI quality flags.',
            RuntimeWarning,
            stacklevel=2,
        )
    else:
        try:
            import esa_snappy
            GPF = esa_snappy.jpy.get_type('org.esa.snap.core.gpf.GPF')
            registry = GPF.getDefaultInstance().getOperatorSpiRegistry()
            if registry.getOperatorSpi('Idepix.Olci') is not None:
                use_idepix = True
            else:
                import warnings
                warnings.warn(
                    'Idepix.Olci operator not found in SNAP (plugin not installed) — '
                    'falling back to native OLCI quality flags. '
                    'Install the IdePix plugin via SNAP > Tools > Plugins.',
                    RuntimeWarning,
                    stacklevel=2,
                )
        except ImportError:
            import warnings
            warnings.warn(
                'esa_snappy not available — falling back to native OLCI quality flags '
                '(CLOUD bit 27, CLOUD_AMBIGUOUS bit 26, CLOUD_SHADOW bit 14). '
                'Install SNAP and run snappy-conf for IdePix-quality masking.',
                RuntimeWarning,
                stacklevel=2,
            )

    combined = np.zeros((dst_height, dst_width), dtype=bool)
    for folder in sen3_folders:
        try:
            if use_idepix:
                print(f'  IdePix: {os.path.basename(folder)} ...')
                swath_mask = run_idepix_olci(
                    folder, cloud_buffer=cloud_buffer,
                    buffer_size=buffer_size, compute_ctp=compute_ctp,
                )
            else:
                print(f'  Native flags: {os.path.basename(folder)} ...')
                swath_mask = run_native_flags_olci(folder)

            utm_mask = project_to_utm(
                swath_mask, folder, dst_crs, dst_transform, dst_height, dst_width
            )
            combined |= utm_mask
        except Exception as e:
            print(f'  Cloud mask failed for {os.path.basename(folder)}: {e}')

    return combined
