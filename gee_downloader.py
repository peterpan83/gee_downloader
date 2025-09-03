import os, glob, sys
import rasterio
import pendulum

import ee, gee
from gee.utils import (gen_subcells,
                       download_images_roi,
                       merge_download_dir,
                       merge_download_dir_obsgeo)
from gee.exceptions import NoEEImageFoundError, EEImageOverlayError, NoEEIntersectionBandsError
from downloader import Downloader

class GEEDownloader(Downloader):
    def __init__(self, **config):
        try:
            ee.Initialize()
        except Exception as e:
            print('try authenticate')
            ee.Authenticate()
            ee.Initialize()
            sys.exit(-1)
        super(GEEDownloader, self).__init__(**config)
        self.aoi_rect_ee = ee.Geometry.Rectangle(self.aoi_bounds)

    def __create_cells(self, resolution):
        # resolution = int(config['resolution'])
        ## the cell size varies with the resolution
        ## it is 0.1 when the resolution is 10 based on the previous experience
        step = 0.1 * (resolution / 10)

        x_step, y_step = step, step
        print('GEE cell size:', x_step, y_step)
        xs, ys = gen_subcells(self.aoi_geo, x_step=x_step, y_step=x_step)
        self.ee_small_cells = [ee.Geometry.Rectangle([x[0], y[0], x[1], y[1]]) for x in xs for y in ys]
        self.ee_small_cells_box = [([x[0], y[0], x[1], y[1]]) for x in xs for y in ys]

    def run(self):
        ## 1. generate small cells
        # x_step, y_step = float(self._config_dic['global']['grid_x']), float(self._config_dic['global']['grid_y'])
        # print('GEE cell size:',x_step, y_step)
        # xs, ys = gen_subcells(self.aoi_geo, x_step=x_step, y_step=x_step)
        # self.ee_small_cells = [ee.Geometry.Rectangle([x[0], y[0], x[1], y[1]]) for x in xs for y in ys]
        # self.ee_small_cells_box = [([x[0], y[0], x[1], y[1]]) for x in xs for y in ys]

        self.__create_cells(resolution=10)

        self.download_imagecollection()

    def download_imagecollection(self):
        '''
        start_date='2021-08-01'
        '''
        image_collection_dic = self.asset_dic['image_collection']

        for prefix in image_collection_dic:
            sensor_type = image_collection_dic[prefix]['sensor_type']
            download_func = getattr(self, f'_download_{sensor_type}')

            config =  image_collection_dic[prefix]['config']

            download_func(prefix, **config)
            # self.download_func = getattr(self, f'_download_{prefix}')
            # self.download_func(image_collection_dic[prefix])


    def download_image(self):
        pass


    def __download_imgcoll_assets(self, date, **config):
        s_d, e_d = date.format('YYYY-MM-DD'), (date + pendulum.duration(days=1)).format('YYYY-MM-DD')
        _s_d = date.format('YYYYMMDD')

        ## to keep CRS of all the assets for the same satellite the same
        ### for example, s2_l1toa, s2_l2rgb, s2_l2surf should have the same CRS
        dst_crs = None
        for asset in config:
            config_asset = config[asset]
            if asset in ['extral_info']:
                continue
            rgb = False
            vmin, vmax = None,None
            if asset.find('rgb')>-1:
                vmin = float(config[asset]['vmin'])
                vmax = float(config[asset]['vmax'])
                rgb = True

            data_source = config[asset]['source']
            obs_geo_pixel = False if 'obs_geo_pixel' not in config_asset else config_asset['obs_geo_pixel'] in ('True', 'true')

            bands = [str.strip(_) for _ in config[asset]['include_bands'].split(',')]
            resolution = int(config[asset]['resolution'])
            anynom = config[asset]['anonym']
            asset_savedir = config[asset]['save_dir']

            extral_info_dic = config['extral_info'] if 'extral_info' in config else {}
            cloud_per = 0 if 'cloud_percentage' not in extral_info_dic else extral_info_dic['cloud_percentage']

            save_dir = os.path.join(self.save_dir, asset_savedir, anynom)

            ofs = glob.glob(os.path.join(save_dir, f'{str.upper(asset)}_{_s_d}*{self.aoi_name}_{resolution}m.tif'))
            if len(ofs) == 1:
                if 'cloud_percentage' in extral_info_dic:

                    with rasterio.open(ofs[0],'r') as src:
                        tags = src.tags()
                        if 'cloud_percentage' in tags:
                            cloud_per_img = float(tags['cloud_percentage'])
                            if cloud_per_img == cloud_per:
                                print(f'{s_d}, {asset} exist, skip downloading')
                                continue
                else:
                    print(f'{s_d}, {asset} exist, skip downloading')
                    continue

            images = ee.ImageCollection(data_source).filterDate(s_d, e_d).filterBounds(self.aoi_rect_ee)
            if len(images.getInfo()['features']) == 0:
                print(f'{s_d}, {asset},cloud percentage: {cloud_per} No image found!')
                continue
            print(f'{s_d}, {asset}, cloud percentage: {cloud_per} Start downloading ')

            temp_dir = os.path.join(save_dir, s_d)
            try:
                res, bands = download_images_roi(images=images, grids=(self.ee_small_cells,
                                                                    self.ee_small_cells_box),
                                              save_dir=temp_dir,
                                              bands=bands,
                                              resolution=resolution)
            except Exception as e:
                res = -1
                print(f'{s_d},{asset}:{str(e)}')

            if res!=1:
                continue
                # prefix, acquisition_time, des, des_meta = getattr(gee, f'get_descriptions_{asset}')(temp_dir) if hasattr(gee, f'get_descriptions_{asset}') else None
            # else:
            prefix, acquisition_time, des, des_meta = getattr(gee, f'get_descriptions_{asset}')(temp_dir) if hasattr(gee,f'get_descriptions_{asset}') else None
            #
            #
            output_f = os.path.join(save_dir, f'{str.upper(asset)}_{acquisition_time}_{self.aoi_name}_{resolution}m.tif')
            try:
                # extract observing geometry pixel by pixel
                if obs_geo_pixel:
                    func_get_obsgeo = getattr(gee, f'get_obsgeo_{asset}')
                    dst_crs = merge_download_dir_obsgeo(
                        func_obsgeo = func_get_obsgeo,
                        download_dir=temp_dir,
                                   output_f=output_f,
                                   dst_crs=dst_crs,
                                   descriptions=des,
                                   descriptions_meta=des_meta,
                                   bandnames=bands,
                                   remove_temp=True,
                                **extral_info_dic)

                else:
                    dst_crs = merge_download_dir(download_dir=temp_dir,
                                   output_f=output_f,
                                   dst_crs=dst_crs,
                                   descriptions=des,
                                   descriptions_meta=des_meta,
                                   bandnames=bands,
                                   remove_temp=True,
                                   RGB = rgb,
                                   min_max = (vmin,vmax),
                                   **extral_info_dic)
            except Exception as e:
                print(e)
                continue

    def _download_optical(self, prefix, **config):
        '''
        download optical image
        requires cloud percentage
        '''
        # print(prefix, config)
        if self._target == 'water':
            dataset = ee.Image('JRC/GSW1_4/GlobalSurfaceWater').clip(self.aoi_rect_ee)
            water_mask = dataset.select('occurrence').gt(0)
        else:
            water_mask = None

        for _date in (self.end_date - self.start_date):
            # print(_date.month)
            # if _date.month < 6 or _date.month>10:
            #     print(_date.month, '-------skip')
            #     continue
            ## 1. obtain cloud percentage

            func_cld = getattr(gee, f'get_{prefix}cld')
            # cld_percentage = self.get_s2_cloudpercentage(s_d='2018-06-10',e_d='2018-06-11')
            try:
                cld_percentage = func_cld(_date, self.aoi_rect_ee,
                                      cloud_prob_threshold=60,
                                      water_mask=water_mask,
                                      resolution=20)
            except NoEEImageFoundError as e:
                print(e)
                continue
            except EEImageOverlayError as e:
                print(e)
                continue

            if cld_percentage > self.cloud_percentage_threshold:
                print(f'{_date}, {prefix}, cloud percentage = {round(cld_percentage,1)} > {self.cloud_percentage_threshold}. skip!')
                continue

            config.update({'extral_info':{'cloud_percentage': round(cld_percentage,1)}})
            self.__download_imgcoll_assets(date=_date, **config)


    def _download_radar(self, prefix, **config):
        '''
        does not require cloud percentage
        '''
        for _date in (self.end_date - self.start_date):
            self.__download_imgcoll_assets(date=_date, **config)