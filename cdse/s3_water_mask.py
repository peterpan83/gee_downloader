"""
Permanent water mask for S3 OLCI output grids.

Source: CIESIN GPWv4.11 Water Mask (CIESIN/GPWv411/GPW_Water_Mask) via Google Earth Engine.
Already a binary mask (1 = water, 0 = land/non-water).

The mask is cached per AOI directory; subsequent scenes reuse it without hitting GEE.
"""

import os
import numpy as np
import rasterio
from rasterio.crs import CRS
from rasterio.transform import Affine

_MASK_SUFFIX = '_water_mask_300m.tif'


def get_or_create_water_mask(
    aoi_name: str,
    aoi_dir: str,
    crs: CRS,
    transform: Affine,
    width: int,
    height: int,
) -> np.ndarray:
    """
    Return a water mask (uint8, 1=water, 0=land) aligned to the S3 output grid.

    Cached in `aoi_dir` on first call; subsequent calls read the file directly.

    Parameters
    ----------
    aoi_name  : used for the cache filename
    aoi_dir   : directory where the cache file is stored (typically the AOI-level dir)
    crs       : CRS of the S3 output GeoTIFF
    transform : affine transform of the S3 output GeoTIFF
    width, height : pixel dimensions of the S3 output GeoTIFF
    """
    mask_path = os.path.join(aoi_dir, f'{aoi_name}{_MASK_SUFFIX}')

    if os.path.exists(mask_path):
        with rasterio.open(mask_path) as src:
            if src.width == width and src.height == height:
                print(f'Water mask found, reusing: {mask_path}')
                return src.read(1)
        print(f'Water mask grid mismatch, regenerating: {mask_path}')

    print(f'Downloading GPW water mask for {aoi_name} ...')
    mask_arr = _fetch_from_gee(crs, transform, width, height)

    os.makedirs(aoi_dir, exist_ok=True)
    meta = {
        'driver': 'GTiff',
        'dtype': 'uint8',
        'width': width,
        'height': height,
        'count': 1,
        'crs': crs,
        'transform': transform,
        'compress': 'lzw',
        'tiled': True,
        'blockxsize': 512,
        'blockysize': 512,
        'nodata': 255,
    }
    tmp_path = mask_path + '.tmp'
    with rasterio.open(tmp_path, 'w', **meta) as dst:
        dst.write(mask_arr, 1)
        dst.set_band_description(1, 'water_mask_gpw_v411')
    os.replace(tmp_path, mask_path)
    print(f'Water mask cached: {mask_path}')
    return mask_arr


def _fetch_from_gee(crs: CRS, transform: Affine, width: int, height: int) -> np.ndarray:
    """Download CIESIN GPWv4.11 water mask from GEE and resample to the exact target grid."""
    import ee
    import requests
    from io import BytesIO
    from pyproj import Transformer as ProjTF
    from rasterio.io import MemoryFile
    from rasterio.warp import reproject, Resampling

    try:
        ee.Initialize()
    except Exception:
        ee.Authenticate()
        ee.Initialize()

    # Compute WGS84 bounding box from the UTM grid corners
    epsg = crs.to_epsg()
    tf = ProjTF.from_crs(epsg, 4326, always_xy=True)
    x0, y0 = transform.c, transform.f
    x1, y1 = transform.c + transform.a * width, transform.f + transform.e * height
    lons, lats = tf.transform([x0, x1, x0, x1], [y0, y0, y1, y1])
    bbox = [min(lons), min(lats), max(lons), max(lats)]

    region = ee.Geometry.Rectangle(bbox)
    water_img = (
        ee.ImageCollection('CIESIN/GPWv411/GPW_Water_Mask')
        .first()
        .select('water_mask')
        .unmask(0)
        .toByte()
    )

    url = water_img.getDownloadURL({
        'region': region,
        'scale': 300,
        'crs': f'EPSG:{epsg}',
        'format': 'GEO_TIFF',
    })

    resp = requests.get(url, timeout=180)
    resp.raise_for_status()

    raw = np.zeros((height, width), dtype=np.uint8)
    with MemoryFile(resp.content) as mf:
        with mf.open() as src:
            reproject(
                source=rasterio.band(src, 1),
                destination=raw,
                src_transform=src.transform,
                src_crs=src.crs,
                dst_transform=transform,
                dst_crs=crs,
                resampling=Resampling.nearest,
            )
    # GPW values: 0=total water, 1=partial water+land, 2=total land, 3=ocean
    # Treat 0 (permanent water/ice) and 3 (ocean) as water; 1 and 2 as land.
    return np.where((raw == 0) | (raw == 3), 1, 0).astype(np.uint8)
