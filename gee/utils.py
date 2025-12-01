import os
import pickle
import requests
import glob
import shutil
import numpy as np
from scipy.ndimage import zoom

import ee
import rasterio
from rasterio.transform import Affine
from shapely.geometry import MultiPolygon

from utils import merge_tifs, mosaic_tifs
from .exceptions import DownloadDirIncompleteError,NoGoodTifsError, NoEEIntersectionBandsError, OldFormat, GsutilError

import logging

def gen_subcells(cell_geometry: MultiPolygon, x_step=0.1, y_step=0.1, x_overlap=None, y_overlap=None):
    '''
    because the maximum number of pixels and dimensions of extent that can be downloaded from
    GEE are limited, the big extent has be grided by the x_step (longitude) and y_step (latitude) parameters.
    '''
    x_overlap = x_overlap if x_overlap is not None else x_step * 0.2
    y_overlap = y_overlap if y_overlap is not None else y_step * 0.2
    bounds = cell_geometry.bounds
    # print(bounds)
    _x = (bounds[0],)
    x = []
    while True:
        if (_x[0] + x_step) > bounds[2]:
            _x = _x + (bounds[2],)
            x.append(_x)
            break
        else:
            _x = _x + (_x[0] + x_step,)
            x.append(_x)
            _x = (_x[1] - x_overlap,)
    _y = (bounds[1],)
    y = []
    while True:
        if (_y[0] + y_step) >= bounds[3]:
            _y = _y + (bounds[3],)
            y.append(_y)
            break
        else:
            _y = _y + (_y[0] + y_step,)
            y.append(_y)
            _y = (_y[1] - y_overlap,)
    # print(x, y)
    return x, y


def download_images_roi(images: ee.ImageCollection, grids, save_dir, bands=None, resolution=10):
    '''
    @grids ee_small_cells and ee_small_cells_box
    @bands, for s2 ['B1', 'B2', 'B3', 'B4', 'B5', 'B6', 'B7', 'B8', 'B8A', 'B9', 'B10', 'B11', 'QA60']
    '''
    # xs, ys = gen_subcells(roi_geo, x_step=0.1, y_step=0.1)
    # ee_small_cells = [ee.Geometry.Rectangle([x[0], y[0], x[1], y[1]]) for x in xs for y in ys]
    # ee_small_cells_box = [([x[0], y[0], x[1], y[1]]) for x in xs for y in ys]
    ee_small_cells, ee_small_cells_box = grids

    # print(images)
    img_count = len(images.getInfo()['features'])
    info_s = glob.glob(os.path.join(save_dir, '*.pickle'))
    bands_all = None
    download  = True
    if len(info_s) == img_count:
        for _ in info_s:
            with open(_, 'rb') as f:
                info = pickle.load(f)
            bands_all = set([_['id'] for _ in info['bands']]) if bands_all is None \
                else bands_all.intersection(set([_['id'] for _ in info['bands']]))
        download = False

    else:
        ## redownload
        for i in range(img_count):
            _id = images.getInfo()['features'][i]['id']
            img = ee.Image(_id)
            info = img.getInfo()
            bands_all = set([_['id'] for _ in info['bands']]) if bands_all is None \
                else bands_all.intersection(set([_['id'] for _ in info['bands']]))
    if bands is None:
        bands = sorted(list(bands_all))
        bands_c = bands.copy()
    else:
        bands_c = bands.copy()
        for _ in bands:
            if _ not in bands_all:
                bands_c.remove(_)
    # bands_c = ['VV', 'VH', 'angle']
    if len(bands_c) == 0 or bands_c==['angle']:
        raise NoEEIntersectionBandsError()

    if not download:
        return 1, bands_c

    ### download

    for i in range(img_count):
        # if i==0:
        #     continue
        _id = images.getInfo()['features'][i]['id']
        img = ee.Image(_id)
        info = img.getInfo()
        _id_name = _id.split('/')[-1]
        if not os.path.exists(save_dir):
            os.makedirs(save_dir)
        with open(os.path.join(save_dir, f'{_id_name}_info.pickle'), 'wb') as f:
            pickle.dump(info, f, pickle.HIGHEST_PROTOCOL)
        # print(_id.split('/')[-1][:8] + '_' + _id.split('_')[-1])

        # name = _id_name[:8] + '_' + _id.split('_')[-1]
        name = _id_name
        if name.find('_') < 0:
            name = '00_'+name
        for j, (ee_c, ee_c_b) in enumerate(zip(ee_small_cells, ee_small_cells_box)):
            try:
                url = img.getDownloadURL({
                    'name': 'multi_band',
                    'bands': bands_c,
                    'region': ee_c,
                    'scale': resolution,
                    'filePerBand': False,
                    'format': 'GEO_TIFF'})

            except Exception as e:
                print(ee_c_b, e)
                continue
            # print(url)
            print(f'downloading {url}')
            response = requests.get(url)
            o_f = os.path.join(save_dir, name + '_' + str(j) + '.tif')
            with open(o_f, 'wb') as f:
                f.write(response.content)

            fix_raster_upsidedown(o_f,output_tif=o_f)


    return 1, bands_c


def extract_id_from_info(self, pickle_file):
    with open(pickle_file,'rb') as f:
        info = pickle.load(f)
    return info['id'].split('/')[-1]


def merge_download_dir(download_dir,
                       output_f,
                       descriptions_meta,
                       descriptions,
                       dst_crs=None,
                       bandnames=None,
                       remove_temp = True,
                       RGB = False,
                       min_max = (None, None),
                       **extra_info):

    ### the minimum size should vary with the resolution and grid size,
    ### to be fixed
    tifs = [_ for _ in glob.glob(os.path.join(download_dir, f'*_*_*.tif')) if(os.path.getsize(_) / 1024.0) > (1.0*len(bandnames)) ]
    if len(tifs) < 1:
        raise DownloadDirIncompleteError(download_dir)

    ret, dst_crs = merge_tifs(tifs, output_f, descriptions=':'.join(descriptions),
                         descriptions_meta=descriptions_meta,
                              bandnames=bandnames,
                              dst_crs=dst_crs,
                              RGB = RGB,
                              min_max = min_max,
                              **extra_info)

    if ret == 1 and remove_temp:
        shutil.rmtree(download_dir)
    if ret == -1:  ## no good tifs found in the temp dir, and it casue the failure of merging
        raise NoGoodTifsError(download_dir)
    return dst_crs


def merge_download_dir_obsgeo(func_obsgeo, download_dir,
                       output_f,
                       descriptions_meta,
                       descriptions,
                       dst_crs=None,
                       bandnames=None,
                       remove_temp = True,
                       **extra_info):

    info_pickels = glob.glob(os.path.join(download_dir, "*.pickle"))

    output_f_ts = [] ## save the merged tif files for the same tiles, and these files will be further merged
    for pickle_file in info_pickels:
        try:
            sza, vza, phi, transform_60,epsg_crs = func_obsgeo(pickle_file)
        except OldFormat as e:
            print(e)
            continue
        except GsutilError as e:
            print(e)
            continue



        tilename = os.path.basename(pickle_file).split('_')[-2]

        tifs = [_ for _ in glob.glob(os.path.join(download_dir, f'*_{tilename}_*.tif')) if(os.path.getsize(_) / 1024.0) > (1.0*len(bandnames))]
        if len(tifs) < 1:
            raise DownloadDirIncompleteError(download_dir)

        output_f_t = '_'.join(tifs[0].split('_')[:-1])+'.tif'

        mosaic, transform_img, dst_crs = mosaic_tifs(tifs, dst_crs=dst_crs)

        ########## debug ################
        # meta = {"driver": "GTiff",
        #  "height": sza.shape[0],
        #  "width": sza.shape[1],
        #  "transform": transform_60,
        #  "count": 1,
        #  "crs": epsg_crs,
        # "dtype": rasterio.float32
        #  }
        # with rasterio.open(output_f_t.replace('.tif', '-GEO.tif'), 'w', **meta) as dst:
        #     dst.write(sza, 1)
        # meta = {"driver": "GTiff",
        #  "height": mosaic.shape[1],
        #  "width": mosaic.shape[2],
        #  "transform": transform_img,
        #  "count": 1,
        #  "crs": dst_crs,
        # "dtype": rasterio.float32
        #  }
        # with rasterio.open(output_f_t, 'w', **meta) as dst:
        #     dst.write(mosaic[2],1)
        ########## debug ################


        ulx_img, uly_img, resolution_img = transform_img[2], transform_img[5], transform_img[0]
        nrows_img, ncols_img = mosaic.shape[1], mosaic.shape[2]

        ## extend the extent by adding two pixels 120m for the interpolation after
        i_col, i_row = ~transform_60*(ulx_img-120, uly_img+120)
        i_col, i_row = max(int(np.round(i_col)), 0), max(int(np.round(i_row)), 0)

        ncols_img_60 = int(np.ceil(ncols_img * resolution_img /60.0))+2
        nrows_img_60 = int(np.ceil(nrows_img * resolution_img /60.0))+2
        e_col,e_row = min(ncols_img_60+i_col, sza.shape[1]-1),  min(nrows_img_60+i_row, sza.shape[0]-1)

        ### interpolate to the resolution of the image

        if e_row >= sza.shape[0]-2 or e_col >= sza.shape[1]-2:
            continue
        sza_img = zoom(sza[i_row: e_row, i_col:e_col], 60.0/resolution_img)
        vza_img = zoom(vza[i_row: e_row, i_col:e_col], 60.0 / resolution_img)
        phi_img = zoom(phi[i_row: e_row, i_col:e_col], 60.0 / resolution_img)

        ulx_new, uly_new = transform_60*(i_col, i_row)

        transform_new = Affine(resolution_img, 0.0, ulx_new, 0.0, -resolution_img, uly_new)

        i_col_img,  i_row_img = ~transform_new * (ulx_img, uly_img)
        i_col_img, i_row_img = int(np.round(i_col_img)), int(np.round(i_row_img))

        ## to avoid the extent of sza_img_new exceeds sza_img
        sza_img_new = np.pad(sza_img, pad_width=2, mode='edge')[i_row_img+2: i_row_img+nrows_img+2, i_col_img+2:i_col_img+ncols_img+2]
        vza_img_new = np.pad(vza_img, pad_width=2, mode='edge')[i_row_img+2: i_row_img + nrows_img+2, i_col_img+2:i_col_img + ncols_img+2]
        phi_img_new = np.pad(phi_img, pad_width=2, mode='edge')[i_row_img+2: i_row_img + nrows_img+2, i_col_img+2:i_col_img + ncols_img+2]

        if sza_img_new.shape != (mosaic[0].shape[0], mosaic[0].shape[1]):
            continue

        meta = {"driver": "GTiff",
         "height": nrows_img,
         "width": ncols_img,
         "transform": transform_img,
         "count": mosaic.shape[0] + 3,
         "crs": dst_crs,
        "dtype": rasterio.float32
         }

        obs_geo_arr = np.full((3,) + mosaic[0].shape, 0, dtype=np.float32)
        # valid_mask = mosaic[2] > 0
        # obs_geo_arr[0, valid_mask] = sza_img_new[valid_mask]
        # obs_geo_arr[1, valid_mask] = vza_img_new[valid_mask]
        # obs_geo_arr[2, valid_mask] = phi_img_new[valid_mask]

        obs_geo_arr[0] = sza_img_new
        obs_geo_arr[1] = vza_img_new
        obs_geo_arr[2] = phi_img_new


        mosaic = np.vstack([mosaic, obs_geo_arr])
        with rasterio.open(output_f_t, 'w', **meta) as dst:
            dst.write(mosaic)

        output_f_ts.append(output_f_t)
    if len(output_f_ts) > 0:
        bandnames += ['sza', 'vza', 'phi']
    else:
        logging.warning('OBSGEO is not available, using the mean values for the entire scene!')
        output_f_ts = [_ for _ in glob.glob(os.path.join(download_dir, f'*.tif')) if(os.path.getsize(_) / 1024.0) > (1.0*len(bandnames))]

    ret, dst_crs = merge_tifs(output_f_ts, output_f, descriptions=':'.join(descriptions),
                              descriptions_meta=descriptions_meta,
                              bandnames=bandnames,
                              dst_crs=dst_crs,
                              **extra_info)

    if ret == 1 and remove_temp:
        shutil.rmtree(download_dir)
        # print(meta_img)

    # if ret == 1 and remove_temp:
    #     shutil.rmtree(download_dir)
    return dst_crs


def fix_raster_upsidedown(input_tif, output_tif):
    import rasterio
    from rasterio.enums import Resampling
    from rasterio import Affine

    upsidedown = False

    with rasterio.open(input_tif) as src:
        profile = src.profile
        # Ensure transform has negative y pixel size
        transform = src.transform
        if transform.e > 0:  # y pixel size
            e = transform.e *(-1)
            f = transform.f + (src.height * transform.e)
            transform = Affine(a=transform.a, b=transform.b, c=transform.c,
                               d=transform.d, e=e, f=f)
            upsidedown = True
            data = src.read(
                out_shape=(src.count, src.height, src.width),
                resampling=Resampling.nearest
            )

    if upsidedown:
        profile.update(transform=transform)
        with rasterio.open(output_tif, "w", **profile) as dst:
            dst.write(data[:, ::-1, :])

