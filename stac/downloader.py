import os
import tempfile
import configparser
import datetime
from pathlib import Path

from downloader import Downloader
from .geoanalytics_downloader import GeoanalyticsDownloader

def _upper_section(s: str) -> str:
    return s.upper()

class STACDownloader(Downloader):
    """Adapter that lets gee_downloader configs run through the Geoanalytics STAC engine.

    Keeps your existing gee_downloader.ini format (lowercase sections/keys produced by covert_config_to_dic),
    but executes downloads via the vendored GeoanalyticsDownloader.
    """

    def __init__(self, config_path: str, **config):
        self._config_path = config_path
        super().__init__(**config)

    def _build_geoanalytics_ini(self, start_date: str = None, end_date: str = None) -> str:
        cfg = configparser.ConfigParser()
        # GLOBAL section (Geoanalytics expects 'GLOBAL')
        g = self._config_dic.get('global', {})
        cfg['GLOBAL'] = {}
        G = cfg['GLOBAL']

        # Required / common fields
        G['aoi'] = g.get('aoi', '')
        sd = start_date or (g.get('start_date') if not self._multi_year_mode else None)
        ed = end_date   or (g.get('end_date')   if not self._multi_year_mode else None)
        if sd: G['start_date'] = sd
        if ed: G['end_date'] = ed
        if g.get('assets'): G['assets'] = g['assets']
        if g.get('target'): G['target'] = g['target']
        if g.get('cloud_percentage'): G['cloud_percentage'] = str(g['cloud_percentage'])
        if g.get('save_dir'):
            # mimic gee_downloader convention: save_dir/<aoi_file_stem>
            project_name = Path(G['aoi']).stem
            G['save_dir'] = os.path.join(g['save_dir'], project_name)
        # Optional STAC-specific knobs (pass-through)
        for k in ['stac_api','clip_to_aoi','merge_outputs','output_format','temp_download_dir']:
            if k in g and g[k] is not None:
                G[k] = str(g[k])

        # Per-asset sections: in gee_downloader config they are lowercased section names
        assets = [a.strip() for a in (g.get('assets','') or '').split(',') if a.strip()]
        for asset in assets:
            sec_key = asset.lower()
            if sec_key not in self._config_dic:
                continue
            sec = {}
            # copy all options as-is
            for opt, val in self._config_dic[sec_key].items():
                if val is None:
                    continue
                sec[opt] = str(val)
            cfg[_upper_section(asset)] = sec

        # write to temp file
        fd, tmp_path = tempfile.mkstemp(prefix='gee_downloader_stac_', suffix='.ini')
        os.close(fd)
        with open(tmp_path, 'w', encoding='utf-8') as f:
            cfg.write(f)
        return tmp_path

    def run(self):
        for _s, _e in self.date_ranges:
            tmp_ini = self._build_geoanalytics_ini(
                start_date=_s.format('YYYY-MM-DD'),
                end_date=_e.format('YYYY-MM-DD'),
            )
            try:
                downloader = GeoanalyticsDownloader(config_path=tmp_ini)
                downloader.run()
            finally:
                try:
                    os.remove(tmp_ini)
                except OSError:
                    pass
