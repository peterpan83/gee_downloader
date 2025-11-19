import os, glob, sys
from os.path import exists as Pexists
from os.path import split as Psplit
from os.path import basename as Pbasename

import pandas as pd
import rasterio
import pendulum
import geopandas as gpd
import logging


CATALOG_DIC = {'image_collections':
                   {'optical':('s2','lc08','lc09'),
                    'radar':('s1'),
                    'embeding':('alphaearth')},
               'image':{}}

INFO_COL_DIC = {
    'optical':['date','sensor','type','acquisition_time','cloud_percentage','snow_ice_percentage','water_percentage','other_percentage'],
    'radar':['date','sensor','type','acquisition_time','bands'],
    'embeding':['date','sensor']
}

class Downloader():
    def __init__(self, **config):
        self._config_dic = config

        self.save_dir = config['global']['save_dir']
        self.cloud_percentage_threshold = float(config['global']['cloud_percentage'])
        self.snow_ice_percentage_threshold = float(config['global']['snowice_percentage'])
        self.mode = str.lower(config['global']['mode'])  ## download or info

        self._assets = [str.lower(str.strip(_)) for _ in  config['global']['assets'].split(',')]

        ## water or all
        self._target = str.lower(config['global']['target'])
        self._aoi_f = config['global']['aoi']

        self.project_name = os.path.splitext(Pbasename(self._aoi_f))[0]
        self.save_dir = os.path.join(self.save_dir, self.project_name)

        self.proj_gdf = gpd.read_file(self._aoi_f)
        self.water_mask = True



        self.asset_dic = self.__categroy_assets()

        ### should at least contain aoi name and date YYYYMMDD
        self.date_csv = self._config_dic['global']['date_csv'] if 'date_csv' in self._config_dic['global'] else None
        self.date_df = None
        if self.date_csv is not None:
            self.date_df = pd.read_csv(self.date_csv) if self.date_csv is not None else None
            # print(self.date_df)
            # self.date_df["date"] = self.date_df.apply(lambda x: pendulum.from_format(str(x['date']), "YYYYMMDD"),axis=1)
            self.date_df['name'] = self.date_df['name'].astype(str)


        self.start_date = pendulum.from_format(self._config_dic['global']['start_date'], 'YYYY-MM-DD') if 'start_date' in self._config_dic['global'] else None
        self.end_date = pendulum.from_format(self._config_dic['global']['end_date'], 'YYYY-MM-DD') if 'end_date' in self._config_dic['global'] else None

        if (self.date_df is None) & ((self.start_date is None) or (self.end_date is None)):
            raise Exception('either date_csv or start_date/end_date for downloading is required')

        if not Pexists(self.save_dir):
            os.makedirs(self.save_dir)

    def __categroy_assets(self):
        asset_dic = {}
        imagecoll_dic = {}
        image_dic = {}
        for _ in self._assets:
            if _ not in self._config_dic:
                logging.warning(f'config for {str.upper(_)} not found!')
                continue

            sensor_type = None
            prefix = str.lower(_.split('_')[0])
            _config_dic = self._config_dic[_]
            if prefix not in imagecoll_dic:
                if prefix in CATALOG_DIC['image_collections']['optical']:
                    sensor_type = 'optical'
                elif prefix in CATALOG_DIC['image_collections']['radar']:
                    sensor_type = 'radar'
                elif prefix in CATALOG_DIC['image_collections']['embeding']:
                    sensor_type = 'embeding'

                elif prefix in CATALOG_DIC['image']:
                    if prefix not in image_dic:
                        image_dic[prefix] = {'config':{_:_config_dic}}
                    else:
                        image_dic[prefix]['config'].update({_: _config_dic})

                imagecoll_dic[prefix] = {'sensor_type':sensor_type, 'config':{_:_config_dic}, 'info_col':INFO_COL_DIC[sensor_type]}

            else:
                imagecoll_dic[prefix]['config'].update({_:_config_dic})

        asset_dic['image_collection'] = imagecoll_dic
        asset_dic['image'] = image_dic
        return asset_dic


    def _insert_record(self):
        pass

    def run(self):
        pass






