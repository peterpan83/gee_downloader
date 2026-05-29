"""
Self-contained S3 OLCI L1 TOA processing.

Pipeline:
  1. Run acolite to produce a L1R NetCDF with rhot_* / lat / lon / geometry.
  2. Project the swath pixels to a regular 300 m UTM grid (pyproj + nearest-neighbour).
  3. Gap-fill diagonal stripe artefacts (scipy binary_dilation + distance_transform_edt).
  4. Write a tiled, compressed GeoTIFF with band names and geometry tags.
"""

import glob
import os
from os.path import basename, join as pjoin, split as psplit

import numpy as np
import rasterio
from rasterio.crs import CRS
from rasterio.transform import Affine
from scipy.ndimage import binary_dilation, distance_transform_edt
from tqdm import tqdm


# S3A / S3B OLCI band → centre wavelength (nm)
_S3_BANDS = {
    'S3A': {
        'Oa01': 400, 'Oa02': 412, 'Oa03': 443, 'Oa04': 490,
        'Oa05': 510, 'Oa06': 560, 'Oa07': 620, 'Oa08': 665,
        'Oa09': 674, 'Oa10': 682, 'Oa11': 709, 'Oa12': 754,
        'Oa13': 762, 'Oa14': 765, 'Oa15': 768, 'Oa16': 779,
        'Oa17': 865, 'Oa18': 884, 'Oa19': 899, 'Oa20': 939,
        'Oa21': 1016,
    },
    'S3B': {
        'Oa01': 401, 'Oa02': 412, 'Oa03': 443, 'Oa04': 490,
        'Oa05': 510, 'Oa06': 560, 'Oa07': 620, 'Oa08': 665,
        'Oa09': 674, 'Oa10': 681, 'Oa11': 709, 'Oa12': 754,
        'Oa13': 762, 'Oa14': 765, 'Oa15': 768, 'Oa16': 779,
        'Oa17': 865, 'Oa18': 884, 'Oa19': 899, 'Oa20': 939,
        'Oa21': 1016,
    },
}

_RES_M = 300   # OLCI EFR native resolution


def _utm_epsg(lon: float, lat: float) -> int:
    zone = int((lon + 180) / 6) + 1
    return 32600 + zone if lat >= 0 else 32700 + zone


def _wavelength_match(wl: int, candidates: np.ndarray, tol: int = 10):
    diff = np.abs(candidates - wl)
    return int(candidates[diff.argmin()]) if diff.min() <= tol else None


def _run_acolite(input_files: list, output_dir: str, limit=None) -> tuple:
    """
    Call acolite acolite_l1r to produce L1R NetCDF files.

    Returns
    -------
    l1r_files : list[str]
    sat_key   : 'S3A' or 'S3B'
    """
    import acolite as ac

    settings = {
        'inputfile': input_files,
        'output': output_dir,
        'atmospheric_correction': False,
        'ancillary_data': True,
        'map_raster': False,
        'output_geometry': True,
        'l1r_export_geotiff': False,
        'l1r_export_geotiff_rgb': False,
        'merge_tiles': len(input_files) > 1,
    }
    if limit is not None:
        settings['limit'] = limit

    # Reset acolite settings to avoid bleed-over from previous runs
    ac.settings = {}
    ac.settings['defaults'] = ac.acolite.settings.parse(
        None, settings=ac.acolite.settings.load(None), merge=False
    )
    ac.settings['run'] = dict(ac.settings['defaults'])
    ac.settings['user'] = {}

    setu = ac.acolite.settings.merge(settings)
    ac.settings['run'] = setu
    ac.settings['user'] = settings

    l1r_files, _, _ = ac.acolite.acolite_l1r(input_files, setu)

    sat_key = 'S3A' if basename(input_files[0]).startswith('S3A') else 'S3B'
    return l1r_files, sat_key


def _nc_to_geotiff(nc_file: str, sat_key: str, aoi_name: str, remove_temp: bool, limit=None) -> str:
    """
    Convert an acolite L1R NetCDF (OLCI swath) to a projected GeoTIFF.

    Steps
    -----
    - Read rhot_* variables and lat/lon arrays from the NC.
    - Project swath pixels to a 300 m UTM grid using pyproj.
    - Paint pixels with nearest-neighbour; gap-fill diagonal stripe artefacts.
    - Write tiled + compressed GeoTIFF with band-name / geometry tags.
    """
    import netCDF4 as nc_lib
    from pyproj import Transformer

    bands_dic = _S3_BANDS[sat_key]
    expected_wavelengths = list(bands_dic.values())
    wav_to_band = {v: k for k, v in bands_dic.items()}

    ds = nc_lib.Dataset(nc_file, 'r')

    # Match NC rhot_ variables to sensor wavelengths
    wave_var_dic = {}
    for var in ds.variables:
        if not var.startswith('rhot_'):
            continue
        wl = int(var.split('_')[-1])
        ref = _wavelength_match(wl, np.asarray(expected_wavelengths))
        if ref is not None:
            expected_wavelengths.remove(ref)
            wave_var_dic[ref] = var

    if not wave_var_dic:
        ds.close()
        raise RuntimeError(f'No rhot_ variables found in {nc_file}')

    # NC global attributes
    oname = ds.getncattr('oname') if 'oname' in ds.ncattrs() else psplit(nc_file)[-1].replace('_L1R.nc', '')
    isodate = ds.getncattr('isodate') if 'isodate' in ds.ncattrs() else ''
    acolite_sensor = ds.getncattr('sensor') if 'sensor' in ds.ncattrs() else ''

    def _scalar_or_mean(name):
        try:
            return float(ds.getncattr(name))
        except Exception:
            if name in ds.variables:
                return float(np.nanmean(np.asarray(ds[name][:], dtype=np.float64)))
            return ''

    sza = _scalar_or_mean('sza')
    vza = _scalar_or_mean('vza')
    raa = _scalar_or_mean('raa')

    # ---- Swath → UTM projection ----
    lat_arr = np.asarray(ds['lat'][:], dtype=np.float64)
    lon_arr = np.asarray(ds['lon'][:], dtype=np.float64)
    for arr, var in ((lat_arr, 'lat'), (lon_arr, 'lon')):
        if '_FillValue' in ds[var].ncattrs():
            arr[arr == float(ds[var].getncattr('_FillValue'))] = np.nan

    height, width = lat_arr.shape
    center_lat = float(np.nanmean(lat_arr))
    center_lon = float(np.nanmean(lon_arr))
    utm_epsg = _utm_epsg(center_lon, center_lat)
    dst_crs = CRS.from_epsg(utm_epsg)

    print(f'Projecting OLCI swath ({height}x{width}) to UTM EPSG:{utm_epsg} ...')
    tf = Transformer.from_crs(4326, utm_epsg, always_xy=True)
    east, north = tf.transform(lon_arr.ravel(), lat_arr.ravel())
    east = east.reshape(height, width)
    north = north.reshape(height, width)

    valid = np.isfinite(east) & np.isfinite(north)
    e_min = float(np.nanmin(east[valid]))
    e_max = float(np.nanmax(east[valid]))
    n_min = float(np.nanmin(north[valid]))
    n_max = float(np.nanmax(north[valid]))

    dst_width  = int(np.ceil((e_max - e_min) / _RES_M)) + 1
    dst_height = int(np.ceil((n_max - n_min) / _RES_M)) + 1

    col_out = np.round((east  - e_min) / _RES_M).astype(np.int32)
    row_out = np.round((n_max - north) / _RES_M).astype(np.int32)
    in_bounds = (
        valid &
        (col_out >= 0) & (col_out < dst_width) &
        (row_out >= 0) & (row_out < dst_height)
    )
    col_idx = col_out[in_bounds]
    row_idx = row_out[in_bounds]

    # ---- Gap-fill map (computed once, reused per band) ----
    print('Computing gap-fill map ...')
    coverage = np.zeros((dst_height, dst_width), dtype=bool)
    coverage[row_idx, col_idx] = True
    fill_region = binary_dilation(coverage, iterations=3)
    gap_mask = fill_region & ~coverage
    if gap_mask.any():
        _, nn_idx = distance_transform_edt(~coverage, return_indices=True)
        gap_tgt_r, gap_tgt_c = np.where(gap_mask)
        gap_src_r = nn_idx[0][gap_mask]
        gap_src_c = nn_idx[1][gap_mask]
    else:
        gap_tgt_r = gap_tgt_c = gap_src_r = gap_src_c = None

    def _paint(src_data: np.ndarray) -> np.ndarray:
        out = np.full((dst_height, dst_width), np.nan, dtype=np.float32)
        out[row_idx, col_idx] = src_data.ravel()[in_bounds.ravel()]
        out[out < 0] = np.nan
        if gap_tgt_r is not None:
            out[gap_tgt_r, gap_tgt_c] = out[gap_src_r, gap_src_c]
        return out

    # ---- Build output filename ----
    dst_transform = Affine.translation(e_min, n_max) * Affine.scale(_RES_M, -_RES_M)
    geo_vars = [g for g in ('sza', 'vza', 'raa') if g in ds.variables]
    wavelengths = sorted(wave_var_dic.keys())
    n_bands = len(wavelengths) + len(geo_vars)

    nc_dir = psplit(nc_file)[0]
    datestr = isodate.replace('-', '').replace(':', '').replace('.', '_') if isodate else 'unknown'
    out_f = pjoin(nc_dir, f'{sat_key}_L1TOA_{datestr}_{aoi_name}_{_RES_M}m.tif')

    meta = {
        'driver': 'GTiff',
        'dtype': 'float32',
        'width': dst_width,
        'height': dst_height,
        'count': n_bands,
        'crs': dst_crs,
        'transform': dst_transform,
        'compress': 'lzw',
        'tiled': True,
        'blockxsize': 512,
        'blockysize': 512,
        'BIGTIFF': 'YES',
        'nodata': np.nan,
    }

    bandnames = []
    with rasterio.open(out_f, 'w', **meta) as dst:
        for i, wl in tqdm(enumerate(wavelengths, 1), total=len(wavelengths), desc='write rhot'):
            src = np.asarray(ds[wave_var_dic[wl]][:], dtype=np.float32)
            dst.write(_paint(src), i)
            dst.set_band_description(i, f'rhot_{wl}')
            bandnames.append(wav_to_band[wl])

        for i, g in tqdm(
            enumerate(geo_vars, len(wavelengths) + 1), total=len(geo_vars), desc='write geometry'
        ):
            dst.write(_paint(np.asarray(ds[g][:], dtype=np.float32)), i)
            dst.set_band_description(i, g)
            bandnames.append(g)

        dst.update_tags(
            ns='band_names',
            bandnames=','.join(bandnames),
            wavelengths=','.join(str(w) for w in wavelengths),
        )
        dst.update_tags(ns='geometry', solz=str(sza), senz=str(vza), phi=str(raa))
        dst.update_tags(descriptions='S3 OLCI L1TOA reflectance')
        dst.update_tags(ns='sensor', sensor_name=sat_key, sensing_time=isodate,
                        acolite_sensor=acolite_sensor)

    ds.close()

    if limit is not None:
        print('Cropping to AOI bounding box ...')
        out_f = _crop_to_bbox(out_f, limit, dst_crs)

    # Build overviews on the (possibly cropped) file
    with rasterio.open(out_f, 'r+') as dst:
        dst.build_overviews([2, 4, 8, 16], rasterio.enums.Resampling.nearest)
        dst.update_tags(ns='rio_overview', resampling='nearest')

    if remove_temp:
        for f in glob.glob(os.path.join(nc_dir, f'{oname}*')):
            if f != out_f:
                os.remove(f)

    return out_f


def _crop_to_bbox(tif_path: str, limit: list, dst_crs: CRS) -> str:
    """
    Crop a GeoTIFF to the AOI bounding box and overwrite it in place.

    limit   : [lat_min, lon_min, lat_max, lon_max] in WGS84 (acolite convention)
    dst_crs : CRS of the GeoTIFF (UTM); the limit is reprojected to match it.
    """
    from pyproj import Transformer
    from rasterio.windows import from_bounds, Window

    lat_min, lon_min, lat_max, lon_max = limit

    # Reproject the four corners to UTM and take the axis-aligned envelope
    tf = Transformer.from_crs(4326, dst_crs, always_xy=True)
    xs, ys = tf.transform(
        [lon_min, lon_max, lon_min, lon_max],
        [lat_min, lat_min, lat_max, lat_max],
    )
    x_min, x_max = min(xs), max(xs)
    y_min, y_max = min(ys), max(ys)

    tmp_path = tif_path + '.crop.tif'

    with rasterio.open(tif_path) as src:
        win = from_bounds(x_min, y_min, x_max, y_max, src.transform).intersection(
            Window(0, 0, src.width, src.height)
        )
        out_transform = src.window_transform(win)
        data = src.read(window=win)
        meta = src.meta.copy()
        meta.update({
            'height': data.shape[1],
            'width': data.shape[2],
            'transform': out_transform,
        })
        descriptions = src.descriptions
        # Collect all tag namespaces to preserve them
        all_tags = {'': src.tags()}
        for ns in ('band_names', 'geometry', 'sensor'):
            t = src.tags(ns=ns)
            if t:
                all_tags[ns] = t

    with rasterio.open(tmp_path, 'w', **meta) as dst:
        dst.write(data)
        dst.descriptions = descriptions
        for ns, tags in all_tags.items():
            if ns:
                dst.update_tags(ns=ns, **tags)
            else:
                dst.update_tags(**tags)

    os.replace(tmp_path, tif_path)
    return tif_path


def _append_classification_band(
    toa_f: str,
    sen3_folders: list,
    aoi_name: str,
    aoi_dir: str,
    cloud_masking: bool,
    cloud_buffer: bool,
    buffer_size: int,
) -> dict:
    """
    Append a classification band to an existing TOA GeoTIFF and return scene stats.

    Band encoding (uint8)
    ---------------------
    0  clear land
    1  clear permanent water
    2  cloud / shadow over land
    3  cloud / shadow over permanent water
    255 invalid (NaN in reflectance)

    Returns a dict with cloud_pct_total, cloud_pct_water, water_pct.
    """
    from .s3_water_mask import get_or_create_water_mask
    from .s3_cloud_mask import build_cloud_mask

    with rasterio.open(toa_f) as src:
        crs       = src.crs
        transform = src.transform
        height    = src.height
        width     = src.width
        n_existing = src.count
        meta       = src.meta.copy()
        descriptions = list(src.descriptions)
        all_tags = {'': src.tags()}
        for ns in ('band_names', 'geometry', 'sensor'):
            t = src.tags(ns=ns)
            if t:
                all_tags[ns] = t
        data = src.read()

    # Valid pixel mask (any non-NaN reflectance band)
    valid = np.isfinite(data[0])

    # ---- Water mask ----
    water_mask = get_or_create_water_mask(aoi_name, aoi_dir, crs, transform, width, height)

    # ---- Cloud mask ----
    cloud_mask = None
    if cloud_masking:
        cloud_mask = build_cloud_mask(
            sen3_folders, crs, transform, height, width,
            cloud_buffer=cloud_buffer, buffer_size=buffer_size,
        )

    # ---- Build classification band ----
    classif = np.full((height, width), 255, dtype=np.uint8)   # default: invalid
    classif[valid & (water_mask == 0)] = 0    # clear land
    classif[valid & (water_mask == 1)] = 1    # clear water
    if cloud_mask is not None:
        classif[valid & cloud_mask & (water_mask == 0)] = 2   # cloud land
        classif[valid & cloud_mask & (water_mask == 1)] = 3   # cloud water

    # ---- Rewrite GeoTIFF with extra band ----
    meta['count'] = n_existing + 1
    tmp_f = toa_f + '.cls.tif'
    with rasterio.open(tmp_f, 'w', **meta) as dst:
        for i in range(n_existing):
            dst.write(data[i], i + 1)
        dst.write(classif, n_existing + 1)
        dst.descriptions = tuple(
            list(descriptions) + ['classification']
        )
        dst.set_band_description(
            n_existing + 1,
            'classification: 0=clear_land,1=clear_water,2=cloud_land,3=cloud_water,255=invalid'
        )
        for ns, tags in all_tags.items():
            kw = {'ns': ns} if ns else {}
            dst.update_tags(**kw, **tags)
        dst.update_tags(
            ns='classification',
            cloud_masking_applied=str(cloud_mask is not None),
            water_mask_source='JRC/GSW1_4/GlobalSurfaceWater',
        )

    os.replace(tmp_f, toa_f)

    # Rebuild overviews on the updated file
    with rasterio.open(toa_f, 'r+') as dst:
        dst.build_overviews([2, 4, 8, 16], rasterio.enums.Resampling.nearest)
        dst.update_tags(ns='rio_overview', resampling='nearest')

    # ---- Compute stats ----
    total_valid = int(valid.sum())
    water_pixels = (water_mask == 1) & valid
    water_count  = int(water_pixels.sum())

    stats = {
        'water_pct':       round(water_count / total_valid * 100, 2) if total_valid else float('nan'),
        'cloud_pct_total': float('nan'),
        'cloud_pct_water': float('nan'),
    }
    if cloud_mask is not None:
        cloud_total = int(cloud_mask[valid].sum())
        stats['cloud_pct_total'] = round(cloud_total / total_valid * 100, 2) if total_valid else float('nan')
        stats['cloud_pct_water'] = round(int(cloud_mask[water_pixels].sum()) / water_count * 100, 2) if water_count else float('nan')

    return stats


def _write_scene_csv(csv_path: str, row: dict):
    """Append one row to the scene-info CSV, creating it with a header if needed."""
    import csv
    file_exists = os.path.exists(csv_path)
    with open(csv_path, 'a', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=list(row.keys()))
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)


def gen_s3_toa(
    input_files: list,
    output_dir: str,
    limit=None,
    aoi_name: str = 'none',
    remove_temp: bool = True,
    cloud_masking: bool = False,
    cloud_buffer: bool = True,
    buffer_size: int = 2,
) -> str:
    """
    End-to-end: downloaded .SEN3 folders → projected, AOI-cropped GeoTIFF.

    Parameters
    ----------
    input_files   : list of .SEN3 folder paths
    output_dir    : directory where the GeoTIFF is written
    limit         : [lat_min, lon_min, lat_max, lon_max] in WGS84 —
                    passed to acolite for scene subsetting AND used to
                    crop the final GeoTIFF to the AOI bounding box
    aoi_name      : label embedded in the output filename
    remove_temp   : delete intermediate acolite NC files after writing GeoTIFF
    cloud_masking : if True, run IdePix and append classification band
    cloud_buffer  : IdePix cloud buffer flag
    buffer_size   : IdePix cloud buffer radius in pixels

    Returns
    -------
    str  path to the output GeoTIFF
    """
    import datetime

    os.makedirs(output_dir, exist_ok=True)
    l1r_files, sat_key = _run_acolite(input_files, output_dir, limit=limit)

    if not l1r_files:
        raise RuntimeError(f'acolite produced no L1R files for: {input_files}')

    toa_f = _nc_to_geotiff(
        nc_file=l1r_files[0],
        sat_key=sat_key,
        aoi_name=aoi_name,
        remove_temp=remove_temp,
        limit=limit,
    )

    # aoi_dir = parent of output_dir (year dir → AOI dir) for water-mask caching
    aoi_dir = os.path.dirname(output_dir)

    stats = _append_classification_band(
        toa_f=toa_f,
        sen3_folders=input_files,
        aoi_name=aoi_name,
        aoi_dir=aoi_dir,
        cloud_masking=cloud_masking,
        cloud_buffer=cloud_buffer,
        buffer_size=buffer_size,
    )

    # ---- Write scene CSV ----
    with rasterio.open(toa_f) as src:
        sensing_time = src.tags(ns='sensor').get('sensing_time', '')

    csv_row = {
        'filename':        os.path.basename(toa_f),
        'satellite':       sat_key,
        'sensing_time':    sensing_time,
        'scenes':          ';'.join(os.path.basename(f) for f in input_files),
        'cloud_pct_total': stats['cloud_pct_total'],
        'cloud_pct_water': stats['cloud_pct_water'],
        'water_pct':       stats['water_pct'],
        'processed_utc':   datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S'),
    }
    _write_scene_csv(os.path.join(output_dir, 'download_info.csv'), csv_row)

    return toa_f
