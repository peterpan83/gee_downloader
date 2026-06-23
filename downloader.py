import os, glob, sys

import pandas as pd
import rasterio
import pendulum
import geopandas as gpd
import logging


CATALOG_DIC = {'image_collections':
                   {'optical':('s2','lc08','lc09','s3'),
                    'radar':('s1',),
                    'embedding':('alphaearth',)},
               'image':{}}

INFO_COL_DIC = {
    'optical':['date','sensor','type','acquisition_time','cloud_percentage','snow_ice_percentage','water_percentage','other_percentage', 'product_ids'],
    'radar':['date','sensor','type','acquisition_time','bands'],
    'embedding':['date','sensor']
}

def _build_date_ranges(g: dict):
    """
    Parse date config and return (ranges, multi_year_mode).

    Two formats:
      Old: start_date='YYYY-MM-DD', end_date='YYYY-MM-DD'
           → single (start, end) pair; multi_year_mode=False
      New: start_year=YYYY, end_year=YYYY,
           start_date='MM-DD', end_date='MM-DD'
           → one (start, end) pair per year; multi_year_mode=True
           Cross-year windows (e.g. 12-01 to 02-28) are handled: end falls in year+1.
    """
    if 'start_year' in g and 'end_year' in g:
        start_md = str(g['start_date'])
        end_md   = str(g['end_date'])
        sm, sd = map(int, start_md.split('-'))
        em, ed = map(int, end_md.split('-'))
        cross_year = (sm, sd) > (em, ed)
        ranges = []
        for year in range(int(g['start_year']), int(g['end_year']) + 1):
            s = pendulum.from_format(f'{year}-{start_md}', 'YYYY-MM-DD')
            e_year = year + 1 if cross_year else year
            e = pendulum.from_format(f'{e_year}-{end_md}', 'YYYY-MM-DD')
            ranges.append((s, e))
        return ranges, True
    elif 'start_date' in g and 'end_date' in g:
        s = pendulum.from_format(str(g['start_date']), 'YYYY-MM-DD')
        e = pendulum.from_format(str(g['end_date']), 'YYYY-MM-DD')
        return [(s, e)], False
    return [], False


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

        self.project_name = os.path.splitext(os.path.basename(self._aoi_f))[0]
        self.save_dir = os.path.join(self.save_dir, self.project_name)

        self.proj_gdf = gpd.read_file(self._aoi_f)
        if 'name' in self.proj_gdf.columns:
            self.proj_gdf['name'] = self.proj_gdf['name'].astype(str)
        self.water_mask = True


        self.asset_dic = self.__categorize_assets()

        ### should at least contain aoi name and date YYYYMMDD
        self.date_csv = self._config_dic['global']['date_csv'] if 'date_csv' in self._config_dic['global'] else None
        self.date_df = None
        if self.date_csv is not None:
            self.date_df = pd.read_csv(self.date_csv) if self.date_csv is not None else None
            # print(self.date_df)
            # self.date_df["date"] = self.date_df.apply(lambda x: pendulum.from_format(str(x['date']), "YYYYMMDD"),axis=1)
            self.date_df['name'] = self.date_df['name'].astype(str)
            self.date_df['date'] = self.date_df['date'].astype(str)

        self.date_ranges, self._multi_year_mode = _build_date_ranges(self._config_dic['global'])
        self.start_date = self.date_ranges[0][0] if self.date_ranges else None
        self.end_date   = self.date_ranges[-1][1] if self.date_ranges else None

        if (self.date_df is None) and not self.date_ranges:
            raise Exception('either date_csv or start_date/end_date for downloading is required')

        if not os.path.exists(self.save_dir):
            os.makedirs(self.save_dir)

    def __categorize_assets(self):
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
                elif prefix in CATALOG_DIC['image_collections']['embedding']:
                    sensor_type = 'embedding'

                elif prefix in CATALOG_DIC['image']:
                    if prefix not in image_dic:
                        image_dic[prefix] = {'config':{_:_config_dic}}
                    else:
                        image_dic[prefix]['config'].update({_: _config_dic})
                    continue

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






