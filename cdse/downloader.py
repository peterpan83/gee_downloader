import os, glob, sys, zipfile, shutil
from collections import defaultdict

import pendulum
import requests

from downloader import Downloader

CDSE_TOKEN_URL = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"
CDSE_CATALOG_URL = "https://catalogue.dataspace.copernicus.eu/odata/v1/Products"
CDSE_DOWNLOAD_URL = "https://zipper.dataspace.copernicus.eu/odata/v1/Products({id})/$value"

S3_PRODUCT_TYPE_MAP = {
    "ol_1_efr": "OL_1_EFR___",
    "ol_1_err": "OL_1_ERR___",
    "sl_1_rbt": "SL_1_RBT___",
    "sy_2_syn": "SY_2_SYN___",
}

PAGE_SIZE = 100


def _find_complete_toa(save_dir: str, sat: str, aoi_name: str, t0) -> str | None:
    """
    Return the path of an existing TOA GeoTIFF for this pass if it is complete
    (i.e. contains a classification band), otherwise None.

    Matches files named  {sat}_L1TOA_*_{aoi_name}_*m.tif  whose acquisition
    timestamp is within 5 minutes of t0 (to tolerate isodate vs ContentDate
    minor differences).
    """
    import rasterio as _rio

    pattern = os.path.join(save_dir, f"{sat}_L1TOA_*_{aoi_name}_*m.tif")
    for candidate in glob.glob(pattern):
        try:
            with _rio.open(candidate) as src:
                if 'classification' in (src.descriptions or []):
                    return candidate
        except Exception:
            continue
    return None


class CDSEDownloader(Downloader):

    def __init__(self, **config):
        super().__init__(**config)

        cdse_cfg = config.get("cdse") or {}
        self.username = config["global"].get("cdse_username") or cdse_cfg.get("username")
        self.password = config["global"].get("cdse_password") or cdse_cfg.get("password")

        if not self.username or not self.password:
            raise ValueError(
                "CDSE credentials required: set cdse_username / cdse_password in [GLOBAL] or [CDSE]"
            )

        self.remove_downloaded = config["global"].get("remove_downloaded", False)

        # Cloud masking (IdePix via esa_snappy + JRC GSW water mask)
        self.cloud_masking = str(config["global"].get("cloud_masking", "false")).lower() == "true"
        self.cloud_buffer   = int(config["global"].get("cloud_buffer_size", 2))

        self._access_token = None
        self._token_expiry = None
        print("CDSEDownloader initialized")

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def _authenticate(self):
        resp = requests.post(
            CDSE_TOKEN_URL,
            data={
                "grant_type": "password",
                "username": self.username,
                "password": self.password,
                "client_id": "cdse-public",
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        self._access_token = data["access_token"]
        self._token_expiry = pendulum.now("UTC").add(
            seconds=int(data.get("expires_in", 600)) - 30
        )

    def _get_token(self) -> str:
        if not self._access_token or pendulum.now("UTC") >= self._token_expiry:
            self._authenticate()
        return self._access_token

    # ------------------------------------------------------------------
    # Catalog search (paginated, accepts YYYYMMDD or ISO date strings)
    # ------------------------------------------------------------------

    def search(self, product_type: str, start_date: str, end_date: str, aoi_wkt: str) -> list[dict]:
        """
        Query CDSE catalog for Sentinel-3 products intersecting the AOI in [start_date, end_date).

        Parameters
        ----------
        product_type : str   CDSE product type, e.g. 'OL_1_EFR___'
        start_date   : str   YYYYMMDD or ISO8601
        end_date     : str   YYYYMMDD or ISO8601 (exclusive upper bound)
        aoi_wkt      : str   WKT geometry in EPSG:4326
        """

        def to_iso(d: str) -> str:
            if len(d) == 8 and d.isdigit():
                return pendulum.from_format(d, "YYYYMMDD", tz="UTC").isoformat()
            return pendulum.parse(d).isoformat()

        filter_str = " and ".join([
            "Collection/Name eq 'SENTINEL-3'",
            f"Attributes/OData.CSC.StringAttribute/any(att:att/Name eq 'productType'"
            f" and att/OData.CSC.StringAttribute/Value eq '{product_type}')",
            f"ContentDate/Start ge {to_iso(start_date)}",
            f"ContentDate/Start lt {to_iso(end_date)}",
            f"OData.CSC.Intersects(area=geography'SRID=4326;{aoi_wkt}')",
        ])

        results = []
        skip = 0
        while True:
            resp = requests.get(
                CDSE_CATALOG_URL,
                params={
                    "$filter": filter_str,
                    "$top": PAGE_SIZE,
                    "$skip": skip,
                    "$orderby": "ContentDate/Start asc",
                },
                timeout=60,
            )
            resp.raise_for_status()
            page = resp.json().get("value", [])
            results.extend(page)
            if len(page) < PAGE_SIZE:
                break
            skip += PAGE_SIZE

        print(f"  Found {len(results)} products [{product_type}] {start_date} -> {end_date}")
        return results

    # ------------------------------------------------------------------
    # Download + extract
    # ------------------------------------------------------------------

    def download(self, product_id: str, product_name: str) -> str:
        """Stream-download a product zip into self._temp_dir, return zip path."""
        url = CDSE_DOWNLOAD_URL.format(id=product_id)
        headers = {"Authorization": f"Bearer {self._get_token()}"}
        zip_path = os.path.join(self._temp_dir, f"{product_name}.zip")
        print(f"  Downloading {product_name} ...")
        with requests.get(url, headers=headers, stream=True, timeout=300) as resp:
            resp.raise_for_status()
            with open(zip_path, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=1 << 20):
                    fh.write(chunk)
        return zip_path

    def _download_products(self, products: list[dict]) -> list[str]:
        """Download + extract a list of product dicts; return extracted folder paths."""
        input_files = []
        for product in products:
            pid = product["Id"]
            pname = product.get("Name", pid)

            # Only count a previously extracted .SEN3 directory as existing;
            # leftover .zip files from failed downloads are stale and must be re-fetched.
            existing_dirs = [p for p in glob.glob(os.path.join(self._temp_dir, f"{pname}*"))
                             if os.path.isdir(p)]
            if existing_dirs:
                input_files.extend(existing_dirs)
                continue

            # Remove any leftover corrupt zip from a previous failed attempt.
            stale_zip = os.path.join(self._temp_dir, f"{pname}.zip")
            if os.path.exists(stale_zip):
                print(f"  Removing stale zip: {stale_zip}")
                os.remove(stale_zip)

            try:
                zip_path = self.download(pid, pname)
                before = set(glob.glob(os.path.join(self._temp_dir, "*")))
                with zipfile.ZipFile(zip_path, "r") as zf:
                    zf.extractall(self._temp_dir)
                os.remove(zip_path)
                after = set(glob.glob(os.path.join(self._temp_dir, "*")))
                input_files.extend(list(after - before))
            except Exception as e:
                print(f"  Error downloading {pname}: {e}")

        return input_files

    # ------------------------------------------------------------------
    # TOA generation — local s3_toa module, no external project dependency
    # Returns the output GeoTIFF path (str).
    # ------------------------------------------------------------------

    def gen_toa(self, input_files: list[str], output_dir: str) -> str:
        acolite_dir = self._config_dic["global"].get("acolite_dir")
        if acolite_dir and acolite_dir not in sys.path:
            sys.path.insert(0, acolite_dir)

        from .s3_toa import gen_s3_toa

        bounds = self.aoi_geo.bounds  # (minx, miny, maxx, maxy) in source CRS
        return gen_s3_toa(
            input_files=input_files,
            output_dir=output_dir,
            # acolite limit: [lat_min, lon_min, lat_max, lon_max]
            limit=[bounds[1], bounds[0], bounds[3], bounds[2]],
            aoi_name=self.aoi_name,
            remove_temp=True,
            cloud_masking=self.cloud_masking,
            cloud_buffer=self.cloud_buffer,
            buffer_size=self.cloud_buffer,
        )

    # ------------------------------------------------------------------
    # Pass grouping
    # ------------------------------------------------------------------

    @staticmethod
    def _group_by_pass(products: list, gap_minutes: int = 10) -> list:
        """
        Split a list of catalog products into mergeable groups (orbit passes).

        Two granules belong to the same pass when they share the same satellite
        (S3A / S3B) AND their acquisition start times are within `gap_minutes`
        of each other.  Consecutive granules within one pass are ~3 min apart;
        the next orbit is ~100 min later, so 10 min is a safe threshold.

        Returns a list of groups, each group being a list of product dicts.
        Each group is safe to pass to acolite with merge_tiles=True.
        """
        by_sat = defaultdict(list)
        for p in products:
            sat = p["Name"][:3]          # 'S3A' or 'S3B'
            by_sat[sat].append(p)

        passes = []
        for sat, prods in by_sat.items():
            prods_sorted = sorted(prods, key=lambda p: p["ContentDate"]["Start"])
            group = [prods_sorted[0]]
            for prev, curr in zip(prods_sorted, prods_sorted[1:]):
                t_prev = pendulum.parse(prev["ContentDate"]["Start"])
                t_curr = pendulum.parse(curr["ContentDate"]["Start"])
                if (t_curr - t_prev).in_minutes() <= gap_minutes:
                    group.append(curr)
                else:
                    passes.append(group)
                    group = [curr]
            passes.append(group)

        return passes

    # ------------------------------------------------------------------
    # CSV helpers (used in CSV mode)
    # ------------------------------------------------------------------

    def _build_name_to_productids(self, df) -> dict:
        name_map = defaultdict(set)
        for product_ids, name in zip(df["product_ids"], df["name"]):
            if not product_ids:
                continue
            for pid in str(product_ids).split(","):
                pid = pid.strip()
                if pid:
                    name_map[name].add(pid)
        return dict(name_map)

    # ------------------------------------------------------------------
    # Run modes
    # ------------------------------------------------------------------

    def _run_from_search(self, sensor: str, anynom: str, asset_savedir: str, cdse_product_type: str):
        """Auto-discover products from CDSE catalog using AOI + date range."""
        start_str = self.start_date.format("YYYYMMDD")
        end_str = self.end_date.format("YYYYMMDD")

        for _, row in self.proj_gdf.iterrows():
            name = str(row["name"])
            geo = row["geometry"]

            if geo.geom_type not in ("Polygon", "MultiPolygon"):
                raise ValueError("Only Polygon or MultiPolygon AOI geometries are supported")

            # Ensure WKT is in EPSG:4326 for the OData filter
            if self.proj_gdf.crs and self.proj_gdf.crs.to_epsg() != 4326:
                import geopandas as gpd
                from shapely.geometry import shape
                geo_4326 = (
                    gpd.GeoSeries([geo], crs=self.proj_gdf.crs)
                    .to_crs("EPSG:4326")
                    .iloc[0]
                )
            else:
                geo_4326 = geo

            print(f"Searching products for AOI '{name}' ({start_str} -> {end_str}) ...")
            results = self.search(cdse_product_type, start_str, end_str, geo_4326.wkt)

            if not results:
                print(f"No products found for {name}. Skipping.")
                continue

            # Group by acquisition date (UTC)
            by_date = defaultdict(list)
            for r in results:
                date = pendulum.parse(r["ContentDate"]["Start"]).format("YYYYMMDD")
                by_date[date].append(r)

            self.aoi_name = name
            self.aoi_geo = geo

            for date in sorted(by_date):
                year = pendulum.from_format(date, "YYYYMMDD").year

                self._temp_dir = os.path.join(self.save_dir, "temp_dir", str(date))
                os.makedirs(self._temp_dir, exist_ok=True)

                save_dir = os.path.join(self.save_dir, asset_savedir, anynom, name, str(year))
                os.makedirs(save_dir, exist_ok=True)

                passes = self._group_by_pass(by_date[date])
                print(f"  {date}: {len(by_date[date])} products → {len(passes)} pass(es)")

                for pass_products in passes:
                    sat = pass_products[0]["Name"][:3]   # S3A or S3B
                    t0 = pendulum.parse(pass_products[0]["ContentDate"]["Start"])
                    pass_id = f"{sat}_{t0.format('YYYYMMDDTHHmmss')}"

                    existing = _find_complete_toa(save_dir, sat, name, t0)
                    if existing:
                        print(f"  Already exists, skipping: {existing}")
                        continue

                    input_files = self._download_products(pass_products)
                    if not input_files:
                        print(f"  No files for pass {pass_id}. Skipping.")
                        continue

                    try:
                        toa_f_out = self.gen_toa(input_files, save_dir)
                        print(f"  TOA written: {toa_f_out}")
                    except Exception as e:
                        print(f"  gen_toa failed for pass {pass_id}: {e}")
                        continue

                if self.remove_downloaded and os.path.isdir(self._temp_dir):
                    shutil.rmtree(self._temp_dir)

    def _run_from_csv(self, sensor: str, anynom: str, asset_savedir: str, cdse_product_type: str):
        """Process pre-selected product UUIDs from date_df (CSV mode)."""
        dates = self.date_df[self.date_df["sensor"] == sensor]["date"].unique()

        for date in dates:
            year = pendulum.from_format(str(date), "YYYYMMDD").year

            self._temp_dir = os.path.join(self.save_dir, "temp_dir", str(date))
            os.makedirs(self._temp_dir, exist_ok=True)

            df_filter = self.date_df[
                (self.date_df["date"] == date) & (self.date_df["sensor"] == sensor)
            ]
            name_to_pids = self._build_name_to_productids(df_filter)

            for name, product_ids in name_to_pids.items():
                self.aoi_name = name
                self.aoi_geo = self.proj_gdf[self.proj_gdf["name"] == name]["geometry"].values[0]

                if self.aoi_geo.geom_type not in ("Polygon", "MultiPolygon"):
                    raise ValueError("Only Polygon or MultiPolygon AOI geometries are supported")

                save_dir = os.path.join(self.save_dir, asset_savedir, anynom, name, str(year))
                os.makedirs(save_dir, exist_ok=True)

                toa_f = os.path.join(
                    save_dir, f"{str.upper(sensor)}_L1TOA_{date}_{name}_300m.tif"
                )
                if os.path.exists(toa_f):
                    print(f"Already exists, skipping: {toa_f}")
                    continue

                # UUID-only entries — Name will be resolved during download via the API
                products = [{"Id": pid, "Name": pid} for pid in product_ids]
                input_files = self._download_products(products)

                if not input_files:
                    print(f"No files for {name} on {date}. Skipping.")
                    continue

                try:
                    toa_f_out = self.gen_toa(input_files, save_dir)
                    print(f"TOA written: {toa_f_out}")
                except Exception as e:
                    print(f"gen_toa failed for {name} on {date}: {e}")
                    continue

            if self.remove_downloaded and os.path.isdir(self._temp_dir):
                shutil.rmtree(self._temp_dir)

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def run(self):
        image_collection_dic = self.asset_dic["image_collection"]

        for sensor, sensor_info in image_collection_dic.items():
            config = sensor_info["config"]

            asset = f"{sensor}_l1toa"
            if asset not in config:
                print(f"Warning: No config found for {asset}. Skipping.")
                continue

            asset_cfg = config[asset]
            anynom = asset_cfg.get("anonym", sensor)
            asset_savedir = asset_cfg.get("save_dir", "L1")

            cdse_product_type_key = asset_cfg.get("cdse_product_type", "ol_1_efr")
            cdse_product_type = S3_PRODUCT_TYPE_MAP.get(
                str(cdse_product_type_key).lower(), str(cdse_product_type_key).upper()
            )

            if self.date_df is not None:
                self._run_from_csv(sensor, anynom, asset_savedir, cdse_product_type)
            else:
                self._run_from_search(sensor, anynom, asset_savedir, cdse_product_type)
