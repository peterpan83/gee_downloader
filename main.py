import os,sys,shutil
import datetime
import configparser
from plumbum import cli
from plumbum import colors

from utils import load_config_file, colorstr
try:
    import gdal
except:
    from osgeo import gdal

gdal.PushErrorHandler('CPLQuietErrorHandler')

prefix = colorstr('red', 'bold', 'CONFIG DOES NOT EXIST:')

class App(cli.Application):
    PROGNAME = colors.green
    VERSION = colors.blue

    @cli.switch(["-c"], str, mandatory=True, help="a .ini file describing the data to be downloaded")
    def config_file(self, config_f):
        self._config_f = config_f
        if not os.path.exists(config_f):
            print(f"{prefix}: {config_f}")
            sys.exit(-1)
        config_dic = load_config_file(config_f)
        self._config_dic = config_dic


    def main(self, *args):
        proj_data = self._config_dic.get('global', {}).get('proj_data')
        if proj_data:
            os.environ['PROJ_DATA'] = proj_data
            os.environ['PROJ_LIB'] = proj_data
            print(f"PROJ_DATA set to: {proj_data}")

        backend = str.lower(self._config_dic.get('global', {}).get('backend', 'gee') or 'gee')
        if backend == 'stac':
            from stac import STACDownloader
            downloader = STACDownloader(config_path=self._config_f, **self._config_dic)
        elif backend == 'gcld':
            from gcld import GCLDDownloader
            downloader = GCLDDownloader(**self._config_dic)
        elif backend == 'cdse':
            from cdse import CDSEDownloader
            downloader = CDSEDownloader(**self._config_dic)
        else:
            from gee import GEEDownloader
            downloader = GEEDownloader(**self._config_dic)

        ext = os.path.splitext(self._config_f)[-1]
        now = datetime.datetime.now()

        shutil.copy(self._config_f, os.path.join(downloader.save_dir,
                                                 os.path.basename(self._config_f).
                                                 replace(ext,
                                                         f'_{now.year}{str.zfill(str(now.month), 2)}'
                                                         f'{str.zfill(str(now.day),2)}-{str.zfill(str(now.hour),2)}'
                                                         f'{str.zfill(str(now.minute),2)}{ext}')))
        downloader.run()


if __name__ == '__main__':
    App.run()



