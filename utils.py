import os, configparser
import numpy as np
import rasterio
from rasterio.warp import reproject,calculate_default_transform,Resampling
from rasterio.io import MemoryFile
from rasterio.merge import merge

def convert_config_to_dict(config:configparser.ConfigParser):
    '''
    convert ConfigParser to dict
    :param config:
    :return:
    '''
    sections_dict = {}
    sections = config.sections()
    for section in sections:
        options = config.options(section)
        temp_dict = {}
        for option in options:
            value = None if str.upper(config.get(section,option)) == 'NONE' else config.get(section,option)
            temp_dict[str.lower(option)] = value

        sections_dict[str.lower(section)] = temp_dict

    return sections_dict


def colorstr(*input):
    # Colors a string https://en.wikipedia.org/wiki/ANSI_escape_code, i.e.  colorstr('blue', 'hello world')
    *args, string = input if len(input) > 1 else ('blue', 'bold', input[0])  # color arguments, string
    colors = {
        'black': '\033[30m',  # basic colors
        'red': '\033[31m',
        'green': '\033[32m',
        'yellow': '\033[33m',
        'blue': '\033[34m',
        'magenta': '\033[35m',
        'cyan': '\033[36m',
        'white': '\033[37m',
        'bright_black': '\033[90m',  # bright colors
        'bright_red': '\033[91m',
        'bright_green': '\033[92m',
        'bright_yellow': '\033[93m',
        'bright_blue': '\033[94m',
        'bright_magenta': '\033[95m',
        'bright_cyan': '\033[96m',
        'bright_white': '\033[97m',
        'end': '\033[0m',  # misc
        'bold': '\033[1m',
        'underline': '\033[4m'}
    return ''.join(colors[x] for x in args) + f'{string}' + colors['end']

def reproject_raster_dataset(src, dst_crs):
    # reproject raster to project crs
    # return a dataset in memory
    # with rio.open(in_path) as src:
    src_crs = src.crs
    transform, width, height = calculate_default_transform(src_crs, dst_crs, src.width, src.height, *src.bounds)
    kwargs = src.meta.copy()
    kwargs.update({
        'crs': dst_crs,
        'transform': transform,
        'width': width,
        'height': height})
    with MemoryFile() as memfile:
        with memfile.open(**kwargs) as dst:
            for i in range(1, src.count + 1):
                reproject(
                    source=rasterio.band(src, i),
                    destination=rasterio.band(dst, i),
                    src_transform=src.transform,
                    src_crs=src.crs,
                    dst_transform=transform,
                    dst_crs=dst_crs,
                    resampling=Resampling.nearest)
        # with memfile.open() as dataset:  # Reopen as DatasetReader
        #     yield dataset  # Note yield not return as we're a contextmanager
        return memfile.open()


def reproject_raster(src, dst_crs):
    src_meta = src.meta.copy()

    transform, width, height = calculate_default_transform(src.crs, dst_crs, src.width, src.height, *src.bounds)

    src_meta.update({
        'crs': dst_crs,
        'transform': transform,
        'width': width,
        'height': height
    })

    reprojected_array = np.empty((src.count, height, width), dtype=np.float32)

    for i in range(1, src.count + 1):
        reproject(
            source=rasterio.band(src, i),
            destination=reprojected_array[i - 1],
            src_transform=src.transform,
            src_crs=src.crs,
            dst_transform=transform,
            dst_crs=dst_crs,
            resampling=Resampling.nearest
        )

    memfile = MemoryFile()
    with memfile.open(**src_meta) as dest:
        dest.write(reprojected_array)

    return memfile


def mosaic_tifs(tif_files, dst_crs=None):
    '''
    mosia tifs , only return the array, transform and crs
    '''
    tif_files = sorted(tif_files, key=lambda x: os.path.getsize(x))
    src_files_to_mosaic = []
    dst_crs_ret = dst_crs

    for i, tif in enumerate(tif_files[::-1]):
        src = rasterio.open(tif, 'r')
        crs = src.meta['crs']
        if dst_crs_ret is None:
            dst_crs_ret = crs
            src_files_to_mosaic.append(src)
            continue
        if crs == dst_crs_ret:
            src_files_to_mosaic.append(src)
            continue
        reproj_src = reproject_raster(src, dst_crs=dst_crs_ret)
        src_files_to_mosaic.append(reproj_src.open())
    mosaic, out_trans = merge(src_files_to_mosaic)
    return mosaic, out_trans,dst_crs_ret

def merge_tifs(tif_files,
               out_file,
               descriptions,
               descriptions_meta,
               obs_geo=None,
               bandnames=None,
               dst_crs=None,
               RGB = False,
               min_max = (None,None),
               output_cog = True,
               **extra_info):
    '''
    @obs_geo, None or (theta_z, theta_s, phi)
    @bandnames: None or a list

    '''
    tif_files = sorted(tif_files, key=lambda x: os.path.getsize(x))
    if bandnames is not None:
        bandnames_c = bandnames.copy()
    src_files_to_mosaic = []
    dst_crs_ret = dst_crs
    for i, tif in enumerate(tif_files[::-1]):
        src = rasterio.open(tif, 'r')
        crs = src.meta['crs']
        if dst_crs_ret is None:
            dst_crs_ret = crs
            src_files_to_mosaic.append(src)
            continue
        if crs == dst_crs_ret:
            src_files_to_mosaic.append(src)
            continue
        reproj_src = reproject_raster(src, dst_crs=dst_crs_ret)
        src_files_to_mosaic.append(reproj_src.open())

    badfile = True
    while badfile and len(src_files_to_mosaic)>0:
        try:
            mosaic, out_trans = merge(src_files_to_mosaic)
        except Exception as error:
            print(error)
            src_files_to_mosaic.pop(-1)  ## remove the files with the smallest size
        else:
            badfile = False
    if len(src_files_to_mosaic) == 0:
        return -1, None

    out_meta = src.meta.copy()
    # print(out_meta)
    if RGB:
        invalid_mask = mosaic == 0
        mosaic = (np.clip(mosaic, min_max[0], min_max[1])/min_max[1])*255
        mosaic[invalid_mask] = 0
        mosaic = mosaic.astype('uint8')
        out_meta['dtype'] = rasterio.uint8

    # print(obs_geo)
    if obs_geo is not None:
        obs_geo_arr = np.full((3,) + mosaic[0].shape, 0, dtype=out_meta['dtype'])
        valid_mask = mosaic[2] > 0
        obs_geo_arr[0, valid_mask] = obs_geo[0]
        obs_geo_arr[1, valid_mask] = obs_geo[1]
        obs_geo_arr[2, valid_mask] = obs_geo[2]
        mosaic = np.vstack([mosaic, obs_geo_arr])
        if bandnames is not None:
            bandnames_c += ['theta_z', 'theta_s', 'phi']
    out_meta.update(
        {"driver": "GTiff",
         "height": mosaic.shape[1],
         "width": mosaic.shape[2],
         "transform": out_trans,
         "count": mosaic.shape[0],
         "crs": dst_crs_ret
         }
    )

    if output_cog:
        out_meta.update(
            {'driver':"COG",
            'compress': "DEFLATE",
            'predictor': 2,
            'overview_resampling': "average"
             }
        )

    with rasterio.open(out_file, 'w', **out_meta) as dst:
        # if description is not None:
        #     f.descriptions = description
        dst.update_tags(info=descriptions)
        dst.update_tags(info_item=descriptions_meta)

        for key in extra_info:
            if key == 'cloud_percentage':
                dst.update_tags(cloud_percentage=extra_info[key])

        dst.write(mosaic)
        if bandnames is not None:
            # print(len(bandnames_c),mosaic.shape)
            dst.descriptions = tuple(bandnames_c)
    return 1, dst_crs_ret


# ---------------- YAML config support ----------------
def load_config_file(config_path: str) -> dict:
    """Load a .ini or .yml/.yaml config file and return the internal config dict
    expected by Downloader / GEEDownloader (lower-cased section keys).

    YAML schema supported (as proposed by user):
      GLOBAL: {backend, aoi, start_date, end_date, assets, save_dir, ...}
      STAC: {stac_api, stac_best_item, stac_max_items, <optional per-asset sources>}
      GEE: {project_id, target, mode, cloud_percentage, snowice_percentage, ...}
      ASSETS: [ {ASSET_NAME: {stac_source, gee_source, include_bands, ...}}, ... ]
    """
    ext = os.path.splitext(config_path)[1].lower()
    if ext == '.ini':
        import configparser
        cfg = configparser.ConfigParser()
        cfg.read(config_path)
        return convert_config_to_dict(cfg)

    if ext in ('.yml', '.yaml'):
        try:
            import yaml  # type: ignore
        except Exception as e:
            raise ImportError("PyYAML is required for .yml configs. Install with: pip install pyyaml") from e

        with open(config_path, 'r') as f:
            y = yaml.safe_load(f)
        return convert_yaml_to_internal_config(y)

    raise ValueError(f"Unsupported config extension: {ext}. Use .ini, .yml, or .yaml")


def _as_list(x):
    if x is None:
        return []
    if isinstance(x, (list, tuple)):
        return list(x)
    return [x]


def _comma_join(x):
    # keep existing code expectations (comma-separated strings)
    if x is None:
        return None
    if isinstance(x, str):
        return x
    if isinstance(x, (list, tuple)):
        return ','.join(str(v) for v in x)
    return str(x)


def convert_yaml_to_internal_config(y: dict) -> dict:
    """Convert the new YAML structure into the legacy dict structure.

    Returns dict with keys: 'global', '<asset_name_lower>', ...

    - 'global' contains merged GLOBAL + backend-specific settings

    - each asset section contains 'source' chosen from stac_source/gee_source based on backend
    """
    if not isinstance(y, dict):
        raise ValueError('YAML root must be a mapping/dict')

    # required blocks
    global_y = y.get('GLOBAL') or y.get('global')
    if not isinstance(global_y, dict):
        raise ValueError('YAML must contain GLOBAL: {...}')

    backend = str(global_y.get('backend', 'gee')).strip().lower()
    if backend not in ('gee', 'stac', 'gcld', 'cdse'):
        raise ValueError(f"GLOBAL.backend must be one of 'gee', 'stac', 'gcld', 'cdse' (got: {backend})")

    # stac_y = y.get('STAC') or y.get('stac') or {}
    # gee_y = y.get('GEE') or y.get('gee') or {}

    backend_y = y.get(backend.upper()) or y.get(backend) or {}


    assets_y = y.get('ASSETS') or y.get('assets_config') or y.get('Assets') or None
    if assets_y is None:
        # allow old-style: asset sections at top-level
        assets_y = []
    if not isinstance(assets_y, list):
        raise ValueError('ASSETS must be a list of {ASSET_NAME: {...}} entries')

    # build legacy global section
    global_out = {}
    # keep keys from GLOBAL (except backend)
    for k, v in global_y.items():
        if str(k).lower() == 'backend':
            continue
        global_out[str(k).lower()] = v

    global_out['backend'] = backend

    # merge backend-specific knobs into global so existing Downloader doesn't break
    if isinstance(backend_y, dict):
        for k, v in backend_y.items():
            lk = str(k).lower()
            # only merge if not already set in GLOBAL
            if lk not in global_out:
                global_out[lk] = v

    # normalize required fields for legacy code
    # assets can be 'S2_L1TOA' or ['S2_L1TOA', ...]
    global_out['assets'] = _comma_join(global_out.get('assets'))
    # legacy Downloader expects these; set safe defaults if missing (useful for stac backend)
    global_out.setdefault('mode', 'download')
    global_out.setdefault('target', 'all')
    global_out.setdefault('cloud_percentage', 100)
    global_out.setdefault('snowice_percentage', 100)

    # Build asset sections
    cfg = {'global': global_out}

    for entry in assets_y:
        if not isinstance(entry, dict) or len(entry) != 1:
            raise ValueError('Each ASSETS entry must be a single-key mapping like: - S2_L1TOA: {...}')
        asset_name, asset_conf = next(iter(entry.items()))
        if not isinstance(asset_conf, dict):
            raise ValueError(f'ASSETS.{asset_name} must map to a dict')
        asset_key = str(asset_name).strip().lower()

        # pick source based on backend
        src_key = f"{backend}_source"
        source = asset_conf.get(src_key)


        # if backend source missing, keep as NONE and let downstream warn
        out_asset = {}
        for k, v in asset_conf.items():
            lk = str(k).lower()
            if lk in ('stac_source', 'gee_source'):
                continue
            out_asset[lk] = v

        out_asset['source'] = None if (source is None or str(source).upper() == 'NONE') else source

        # normalize include_bands to legacy comma string
        if 'include_bands' in out_asset:
            out_asset['include_bands'] = _comma_join(out_asset['include_bands'])

        cfg[asset_key] = out_asset

    return cfg





