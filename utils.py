import os, configparser
import numpy as np
import rasterio
from rasterio.warp import reproject,calculate_default_transform,Resampling
from rasterio.io import MemoryFile
from rasterio.merge import merge

def covert_config_to_dic(config:configparser.ConfigParser):
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
        reproj_src = reproject_raster_dataset(src, dst_crs=dst_crs_ret)
        src_files_to_mosaic.append(reproj_src)
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
        reproj_src = reproject_raster_dataset(src, dst_crs=dst_crs_ret)
        src_files_to_mosaic.append(reproj_src)

    badfile = True
    while badfile and len(src_files_to_mosaic)>0:
        try:
            mosaic, out_trans = merge(src_files_to_mosaic)
        except Exception as error:
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


