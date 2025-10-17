import os, glob, sys

import pandas as pd
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
        project_id = config['global']['project_id']
        try:
            ee.Initialize(project=project_id)
        except Exception as e:
            print('try authenticate')
            ee.Authenticate()
            ee.Initialize(project=project_id)
            sys.exit(-1)
        super(GEEDownloader, self).__init__(**config)


    def __create_cells_single(self, resolution, bandnumber):
        # resolution = int(config['resolution'])
        ## the cell size varies with the resolution
        ## it is 0.1 when the resolution is 10 based on the previous experience

        ## why 16? because it is tested ok for senteinel-2 l1c with 16 bands including obs geo
        step = 0.1 * (resolution / 10) * 16 / bandnumber

        x_step, y_step = step, step
        print(f'GEE cell size for res:{resolution}, band num: {bandnumber}:', x_step, y_step)
        xs, ys = gen_subcells(self.aoi_geo, x_step=x_step, y_step=x_step)
        ee_small_cells = [ee.Geometry.Rectangle([x[0], y[0], x[1], y[1]]) for x in xs for y in ys]
        ee_small_cells_box = [([x[0], y[0], x[1], y[1]]) for x in xs for y in ys]
        return (ee_small_cells, ee_small_cells_box)


    def __create_cells(self):
        # print(self.asset_dic)
        self.cells_dic = {}
        for prefix in self.asset_dic['image_collection']:
            sensor_type = self.asset_dic['image_collection'][prefix]['sensor_type']
            config = self.asset_dic['image_collection'][prefix]['config']
            for asset in config:
                res = int(config[asset]['resolution'])
                band_num = len(config[asset]['include_bands'].split(','))
                if f'{band_num}_{res}' not in self.cells_dic:
                    self.cells_dic[f'{band_num}_{res}'] = self.__create_cells_single(resolution=res, bandnumber=band_num)


        # print(self._config_dic)


    def run(self):
        ## 1. generate small cells
        # x_step, y_step = float(self._config_dic['global']['grid_x']), float(self._config_dic['global']['grid_y'])
        # print('GEE cell size:',x_step, y_step)
        # xs, ys = gen_subcells(self.aoi_geo, x_step=x_step, y_step=x_step)
        # self.ee_small_cells = [ee.Geometry.Rectangle([x[0], y[0], x[1], y[1]]) for x in xs for y in ys]
        # self.ee_small_cells_box = [([x[0], y[0], x[1], y[1]]) for x in xs for y in ys]
        for i, row in self.proj_gdf.iterrows():
            self.aoi_geo = row['geometry']
            if self.aoi_geo.type not in ['Polygon', 'MultiPolygon']:
                raise ValueError('only Polygon or MultiPolygon is supported for the aoi')
            self.aoi_name = row['name'] if 'name' in row else str(i)
            # if self.aoi_name != '295826':
            #     continue
            if i<=800 or i>1000:
                continue
            print('Start for AOI:', self.aoi_name)

            # print(self.info_df.columns[1])


            self.aoi_bounds = self.aoi_geo.bounds

            if self.aoi_bounds[3]>77.2 or self.aoi_bounds[1]<-55.8:
                self.water_mask = False
            else:
                self.water_mask = True



            self.aoi_rect_ee = ee.Geometry.Rectangle(self.aoi_bounds)

            self.__create_cells()

            self.download_imagecollection()

    def download_imagecollection(self):
        '''
        start_date='2021-08-01'
        '''
        image_collection_dic = self.asset_dic['image_collection']

        for prefix in image_collection_dic:
            sensor_type = image_collection_dic[prefix]['sensor_type']

            self.info_csv = self.save_dir + '/' + f'{self.aoi_name}_imagecollection_{sensor_type}_info.csv'  ## [date, sensor, type, acquisition_time, cloud_percentage, snow_ice_percentage]
            self.info_cols = image_collection_dic[prefix]['info_col']

            if not os.path.exists(self.info_csv):
                with open(self.info_csv, 'w') as f:
                    # f.write(
                    #     'date,sensor,type,acquisition_time,cloud_percentage,snow_ice_percentage,water_percentage,other_percentage\n')
                    f.write(','.join(self.info_cols) + '\n')


            self.info_df = pd.read_csv(self.info_csv)


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

            grids = self.cells_dic.get(f'{len(bands)}_{resolution}', None)
            if grids is None:
                raise NoEEIntersectionBandsError(f'no grid found for {asset} with {len(bands)} bands and {resolution}m resolution')

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

            temp_dir = os.path.join(save_dir, f'{self.aoi_name}_{s_d}')
            try:
                res, bands = download_images_roi(images=images, grids=grids,
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

        func_cld = getattr(gee, f'get_{prefix}cld')
        func_snowice = getattr(gee, f'get_{prefix}snowice')
        for _date in (self.end_date - self.start_date):
            # print(_date.month)
            # if _date.month < 6 or _date.month>10:
            #     print(_date.month, '-------skip')
            #     continue
            ## 1. obtain cloud percentage

            # cld_percentage = self.get_s2_cloudpercentage(s_d='2018-06-10',e_d='2018-06-11')
            try:
                record = self.info_df[(self.info_df['date']==_date.format('YYYY-MM-DD')) & (self.info_df['sensor']==prefix)]
                if record.shape[0]>0:
                    cld_percentage = float(record['cloud_percentage'].values[0])
                    snowice_percentage = float(record['snow_ice_percentage'].values[0])
                    # acquisition_time = record['acquisition_time'].values[0]

                else:
                    # acquisition_time = getattr(gee, f'get_{prefix}acq')(_date, self.aoi_rect_ee)
                    # cld_percentage = func_cld(_date, self.aoi_rect_ee,
                    #                   cloud_prob_threshold=60,
                    #                   water_mask=water_mask,
                    #                   resolution=20)

                    # acq_time, snowice_pixels / total_pixels * 100, cloud_pixels / total_pixels * 100, water_pixels / total_pixels * 100, other_pixels / total_pixels * 100

                    acquisition_time, snowice_percentage, cld_percentage, water_p,other_p = func_snowice(_date, self.aoi_rect_ee,
                                      water_mask=water_mask,
                                      resolution=20)

                    # date, sensor, type, acquisition_time, cloud_percentage, snow_ice_percentage

                    info = {"date": _date.format('YYYY-MM-DD'), "sensor": prefix, "type": "optical", "acquisition_time": acquisition_time,"cloud_percentage": round(cld_percentage,1), "snow_ice_percentage": round(snowice_percentage,1),"water_percentage": round(water_p,1),"other_percentage": round(other_p,1)}
                    print(f'{_date}, {prefix}, "optical", {acquisition_time}, {round(cld_percentage,1)},{round(snowice_percentage,1)}, {round(water_p,1)},{round(other_p,1)}')

                    info = pd.DataFrame([info])
                    info.to_csv(self.info_csv, mode='a', header=False, index=False)


            except NoEEImageFoundError as e:
                print(e)
                continue
            except EEImageOverlayError as e:
                print(e)
                continue

            if self.mode == 'info':
                continue

            if cld_percentage > self.cloud_percentage_threshold:
                print(f'{_date}, {prefix}, cloud percentage = {round(cld_percentage,1)} > {self.cloud_percentage_threshold}. skip!')
                continue

            if snowice_percentage > self.snow_ice_percentage_threshold:
                print(f'{_date}, {prefix}, snowice percentage = {round(snowice_percentage,1)} > {self.snow_ice_percentage_threshold}. skip!')
                continue

            config.update({'extral_info':{'cloud_percentage': round(cld_percentage,1)}})
            self.__download_imgcoll_assets(date=_date, **config)


    def _download_radar(self, prefix, **config):
        '''
        does not require cloud percentage
        '''
        func_info = getattr(gee, f'get_{prefix}info')

        for _date in (self.end_date - self.start_date):
            info_dic = {"date": _date.format('YYYY-MM-DD'), "sensor": prefix, "type": "radar",
                        "acquisition_time": '', 'bands': ''}
            # print(_date.month)
            record = self.info_df[
                (self.info_df['date'] == _date.format('YYYY-MM-DD')) & (self.info_df['sensor'] == prefix)]
            if record.shape[0] > 0:
                acquisition_time, bands  = record[['acquisition_time', 'bands']].values[0]
            else:
                acquisition_time, bands = func_info(_date, self.aoi_rect_ee)

                info_dic.update({'acquisition_time': acquisition_time, 'bands': bands})

                info_df = pd.DataFrame([info_dic])
                info_df.to_csv(self.info_csv, mode='a', header=False, index=False)

            print(
                f'{_date}, {prefix}, "radar", {acquisition_time}, {bands}')

            if self.mode == 'info':
                continue

            self.__download_imgcoll_assets(date=_date, **config)

    def _download_embeding(self, prefix, **config):
        for _year in range(self.end_date.year, self.start_date.year+1):

            s_d, e_d = f'{_year}-01-01', f'{_year+1}-01-01'
            _s_d = str(_year)

            dst_crs = None
            for asset in config:
                config_asset = config[asset]
                if asset in ['extral_info']:
                    continue
                rgb = False
                vmin, vmax = None, None
                if asset.find('rgb') > -1:
                    vmin = float(config[asset]['vmin'])
                    vmax = float(config[asset]['vmax'])
                    rgb = True

                data_source = config[asset]['source']
                bands = [str.strip(_) for _ in config[asset]['include_bands'].split(',')]
                resolution = int(config[asset]['resolution'])
                grids = self.cells_dic.get(f'{len(bands)}_{resolution}', None)
                if grids is None:
                    raise NoEEIntersectionBandsError(
                        f'no grid found for {asset} with {len(bands)} bands and {resolution}m resolution')

                anynom = config[asset]['anonym']
                asset_savedir = config[asset]['save_dir']

                extral_info_dic = config['extral_info'] if 'extral_info' in config else {}

                save_dir = os.path.join(self.save_dir, asset_savedir, anynom)

                ofs = glob.glob(os.path.join(save_dir, f'{str.upper(asset)}_{_s_d}*{self.aoi_name}_{resolution}m.tif'))
                if len(ofs) == 1:
                    print(f'{s_d}, {asset} exist, skip downloading')
                    continue

                images = ee.ImageCollection(data_source).filterDate(s_d, e_d).filterBounds(self.aoi_rect_ee)
                if len(images.getInfo()['features']) == 0:
                    print(f'{_s_d}, {asset},No image found!')
                    continue
                print(f'{_s_d}, {asset},Start downloading ')

                temp_dir = os.path.join(save_dir, f'{self.aoi_name}_{_s_d}')
                try:
                    res, bands = download_images_roi(images=images, grids=grids,
                                                     save_dir=temp_dir,
                                                     bands=bands,
                                                     resolution=resolution)
                except Exception as e:
                    res = -1
                    print(f'{s_d},{asset}:{str(e)}')

                if res != 1:
                    continue
                    # prefix, acquisition_time, des, des_meta = getattr(gee, f'get_descriptions_{asset}')(temp_dir) if hasattr(gee, f'get_descriptions_{asset}') else None
                # else:
                prefix, acquisition_time, des, des_meta = '', str(_year), [], ''
                #
                #
                output_f = os.path.join(save_dir,
                                        f'{str.upper(asset)}_{acquisition_time}_{self.aoi_name}_{resolution}m.tif')
                try:
                    dst_crs = merge_download_dir(download_dir=temp_dir,
                                                     output_f=output_f,
                                                     dst_crs=dst_crs,
                                                     descriptions=des,
                                                     descriptions_meta=des_meta,
                                                     bandnames=bands,
                                                     remove_temp=True,
                                                     RGB=rgb,
                                                     min_max=(vmin, vmax),
                                                     **extral_info_dic)
                except Exception as e:
                    print(e)
                    continue