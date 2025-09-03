import os, glob, sys
from os.path import exists as Pexists
from os.path import split as Psplit
from os.path import basename as Pbasename

import rasterio
import pendulum
import geopandas as gpd
import logging


CATALOG_DIC = {'image_collections':
                   {'optical':('s2','lc08','lc09'),
                    'radar':('s1')},
               'image':{}}

class Downloader():
    def __init__(self, **config):
        self._config_dic = config

        self.save_dir = config['global']['save_dir']
        self.cloud_percentage_threshold = float(config['global']['cloud_percentage'])
        self._assets = [str.lower(str.strip(_)) for _ in  config['global']['assets'].split(',')]

        self.assets_dic = {}


        ## water or all
        self._target = str.lower(config['global']['target'])
        self._aoi_f = config['global']['aoi']

        self.project_name = os.path.splitext(Pbasename(self._aoi_f))[0]
        self.save_dir = os.path.join(self.save_dir, self.project_name)

        self.proj_gdf = gpd.read_file(self._aoi_f)



        self.asset_dic = self.__categroy_assets()

        self.start_date = pendulum.from_format(self._config_dic['global']['start_date'], 'YYYY-MM-DD')
        self.end_date = pendulum.from_format(self._config_dic['global']['end_date'], 'YYYY-MM-DD')

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

                elif prefix in CATALOG_DIC['image']:
                    if prefix not in image_dic:
                        image_dic[prefix] = {'config':{_:_config_dic}}
                    else:
                        image_dic[prefix]['config'].update({_: _config_dic})

                imagecoll_dic[prefix] = {'sensor_type':sensor_type, 'config':{_:_config_dic}}

            else:
                imagecoll_dic[prefix]['config'].update({_:_config_dic})

        asset_dic['image_collection'] = imagecoll_dic
        asset_dic['image'] = image_dic
        return asset_dic


    def _insert_record(self):
        pass

    def run(self):
        pass






