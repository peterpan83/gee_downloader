import os, glob, sys
import shutil
import subprocess

import pandas as pd
import rasterio
import pendulum
from collections import defaultdict
import gcld
from downloader import Downloader


class GCLDDownloader(Downloader):

    def __init__(self, **config):
        super().__init__(**config)

        self.remove_downloaded = self._config_dic['global'].get('remove_downloaded', False)
        print("GCLDDownloader initialized")

    def build_name_to_productids(self, df):
        """
        Build a mapping: name -> set of product_ids

        Expects columns:
          - 'product_ids' (comma-separated string)
          - 'name'
        """
        name_map = defaultdict(set)

        for product_ids, name in zip(df["product_ids"], df["name"]):
            if not product_ids:
                continue

            for pid in str(product_ids).split(","):
                pid = pid.strip()
                if pid:
                    name_map[name].add(pid)

        return dict(name_map)

    def build_productid_to_names(self, df):
        """
        Build a mapping: product_id -> set of names

        Expects columns:
          - 'product_ids' (comma-separated string)
          - 'name'
        """
        product_map = defaultdict(set)
        for product_ids, name in zip(df["product_ids"], df["name"]):
            if not product_ids:
                continue

            for pid in str(product_ids).split(","):
                pid = pid.strip()
                if pid:
                    product_map[pid].add(name)

        return dict(product_map)


    def download(self, url):
        try:
            subprocess.run(["gsutil", "-m", "cp", "-r", url, self._temp_dir], check=True)
        except Exception as e:
            print(f"Error downloading from {url}: {e}")
            raise e


    def gen_toa(self, input_files, output_dir, datestr):
        try:
            import acolite as ac
        except ImportError:
            sys.path.append(self._config_dic['global']['acolite_dir'])
            import acolite as ac

        from .gen_toa import gen_toa

        bounds = self.aoi_geo.bounds

        # (-78.91280942751852, 51.35812443259449, -78.74999872091318, 51.46007284993028)

        ##limit: south, west,north,east
        config_dic = {'limit': [bounds[1], bounds[0], bounds[3], bounds[2]], 's2_target_res': 10, 'remove_temp': True, 'aoi_name':self.aoi_name}

        toa_f, rgb_f = gen_toa(proc='acolite', input_files=input_files,
                        output_dir=output_dir,
                               datestr=datestr,
                        **config_dic
                        )
        return toa_f, rgb_f


    def run(self):
        sensors = self.date_df['sensor'].unique()
        sensor_dates = {sensor: self.date_df[self.date_df['sensor'] == sensor]['date'].unique() for sensor in sensors}

        image_collection_dic = self.asset_dic['image_collection']

        for sensor, dates in sensor_dates.items():
            print(f"Processing sensor {sensor} with dates: {dates}")

            config = image_collection_dic[sensor]['config']

            asset = f'{sensor}_l1toa'

            if asset not in config:
                print(f"Warning: No config found for {sensor}_l1toa. Skipping.")
                continue
            search_url_func = getattr(gcld, f'search_url_{asset}')
            get_prodid_essentials = getattr(gcld, f'get_{asset}_prodid_essential')

            anynom = config[asset]['anonym']
            asset_savedir = config[asset]['save_dir']

            for date in dates:
                year = pendulum.from_format(str(date), "YYYYMMDD").year

                self._temp_dir = self._config_dic.get('temp_download_dir', os.path.join(self.save_dir, 'temp_dir', str(date)))
                os.makedirs(self._temp_dir, exist_ok=True)
                print(f"Downloading data for sensor {sensor} and date {date}")
                date_str = pendulum.from_format(str(date), "YYYYMMDD").to_date_string()

                df_filter = self.date_df[(self.date_df['date'] == date) & (self.date_df['sensor'] == sensor)]
                self._productid_to_names_dic = self.build_productid_to_names(df_filter)
                self._name_to_productids_dic = self.build_name_to_productids(df_filter)

                print(f"Data for sensor {sensor} and date {date_str} downloaded successfully.")
                for name, product_ids in self._name_to_productids_dic.items():
                    self.aoi_name = name
                    self.aoi_geo = self.proj_gdf[self.proj_gdf['name'] == name]['geometry'].values[0]

                    if self.aoi_geo.type not in ['Polygon', 'MultiPolygon']:
                        raise ValueError('only Polygon or MultiPolygon is supported for the aoi')

                    _sensor = str.upper(list(product_ids)[0].split('_')[0])
                    save_dir = os.path.join(self.save_dir, asset_savedir, anynom, str(self.aoi_name), str(year))
                    toa_f = os.path.join(save_dir, f'{_sensor}_L1TOA_{date}_{self.aoi_name}_10m.tif')
                    if os.path.exists(toa_f):
                        print(f"TOA file {toa_f} already exists. Skipping generation.")
                        continue

                    input_files = []
                    for product_id in product_ids:
                        product_id = get_prodid_essentials(product_id)

                        ### check if files already exist before downloading
                        l1safe = glob.glob(os.path.join(self._temp_dir, f'{product_id}*'))
                        if len(l1safe) < 1:
                            try:
                                url = search_url_func(product_id)
                                self.download(url)
                            except Exception as e:
                                print(f"Error searching url or downloading product {product_id}: {e}")
                                continue
                            l1safe = glob.glob(os.path.join(self._temp_dir, f'{product_id}*'))

                        input_files.extend(l1safe)

                    if len(input_files) < 1:
                        print(f"No files found for name {name} with product IDs {product_ids}. Skipping.")
                        continue

                    toa_f, rgb_f = self.gen_toa(input_files, save_dir, datestr = date)

                    if rgb_f is not None:
                        save_l1rgb_dir = os.path.join(self.save_dir, 'L1RGB', anynom, str(self.aoi_name), str(year))
                        os.makedirs(save_l1rgb_dir, exist_ok=True)
                        l1rgb_f = os.path.join(save_l1rgb_dir, os.path.basename(rgb_f).replace('_L1TOA_', '_L1RGB_'))
                        shutil.move(rgb_f, l1rgb_f)


                ## remove the temp dir after processing all AOIs for the date
                if self.remove_downloaded:
                    shutil.rmtree(self._temp_dir)
