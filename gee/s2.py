import os,sys, glob
import subprocess
import json
import re
import numpy as np
import pickle


import ee
import pendulum

from .exceptions import NoEEImageFoundError, \
    EEImageOverlayError, \
    BigQueryError, \
    OldSentinelFormat,\
    GsutilError

def extract_geometry_from_info(pickle_file):
    #     print(pickle_file)
    with open(pickle_file, 'rb') as f:
        info = pickle.load(f)
    theta_v, theta_s, azimuth_v, azimuth_s = [], [], [], []
    #     print(info['bands'][0].keys())
    properties = info['properties']
    for key in properties.keys():
        if 'MEAN_INCIDENCE_ZENITH_ANGLE' in key:
            theta_v.append(properties[key])
        if 'MEAN_SOLAR_ZENITH_ANGLE' in key:
                #             print(key, properties[key])
            theta_s.append(properties[key])
        if 'INCIDENCE_AZIMUTH_ANGLE' in key:
            azimuth_v.append(properties[key])
        if 'MEAN_SOLAR_AZIMUTH_ANGLE' in key:
            azimuth_s.append(properties[key])
    theta_v, theta_s, azimuth_v, azimuth_s = np.asarray(theta_v), np.asarray(theta_s), np.asarray(
            azimuth_v), np.asarray(azimuth_s)
    return (properties['PRODUCT_ID'], theta_v.mean(), theta_s.mean(), azimuth_v.mean(), azimuth_s.mean())


def get_descriptions_l1toa(download_dir):
    info_pickels = glob.glob(os.path.join(download_dir, "*.pickle"))
    descriptions = []
    descriptions_meta = 'product_id,theta_v,theta_s,azimuth_v,azimuth_s'
    acquisition_time = ''
    prefix = ''
    for _pf in info_pickels:
        _id, theta_v, theta_s, azimuth_v, azimuth_s = extract_geometry_from_info(_pf)
        if acquisition_time == '':
            acquisition_time = _id.split('_')[2][:13]
        if prefix == '':
            prefix = '_'.join(_id.split('_')[:2])
        descriptions.append(','.join([_id, str(theta_v), str(theta_s), str(azimuth_v), str(azimuth_s)]))

        # descriptions.append(self.__extract_id_from_info(_pf))
        #  descriptions_meta = 'product_id'
    return prefix, acquisition_time, descriptions, descriptions_meta

def get_descriptions_l2rgb(download_dir):
    return get_descriptions_l1toa(download_dir)

def get_descriptions_l2surf(download_dir):
    return get_descriptions_l1toa(download_dir)




def add_cloudpixelsnumber_image(images: ee.ImageCollection, roi_rect, resolution=10, threshold_prob=50, water_mask=None):
    # images = images.updateMask(water_mask)
    # dataset = ee.Image('JRC/GSW1_4/GlobalSurfaceWater').clip(roi_rect)
    # water_mask = dataset.select('occurrence').gt(0)
    def __f(image:ee.Image):
        if water_mask is not None:
            image = image.updateMask(water_mask)
        prob = image.select('probability')
        cloud_mask = prob.gt(threshold_prob)
        total_mask = prob.gt(0)

        cloud = cloud_mask.rename('cloud')
        total = total_mask.rename('total')

        image = image.addBands(cloud).addBands(total)

        cloudpixels = image.reduceRegion(
            reducer=ee.Reducer.sum(),
            geometry=roi_rect,
            scale=resolution,
            maxPixels=1e11
        ).get('cloud')

        totalpixels = image.reduceRegion(
            reducer=ee.Reducer.sum(),
            geometry=roi_rect,
            scale=resolution,
            maxPixels=1e11
        ).get('total')

        return image.set('cloud_num', cloudpixels).set('total_num', totalpixels)
    images = images.map(__f)
    return images


def add_snowicepixelsnumber_image(images: ee.ImageCollection, roi_rect, resolution=10, water_mask=None):
    # images = images.updateMask(water_mask)
    # dataset = ee.Image('JRC/GSW1_4/GlobalSurfaceWater').clip(roi_rect)
    # water_mask = dataset.select('occurrence').gt(0)
    def __f(image:ee.Image):
        if water_mask is not None:
            image = image.updateMask(water_mask)
        prob = image.select('SCL')
        snow_mask = prob.eq(11) ## 11 snow/ice  , some L2A image has very high reflectance > 10000 for snowy areas
        _water_mask = prob.eq(6)
        cloud_mask = prob.eq(7).Or(prob.eq(8)).Or(prob.eq(9)).Or(prob.eq(10)).Or(prob.eq(3))
        other_mask = prob.eq(1).Or(prob.eq(2)).Or(prob.eq(4)).Or(prob.eq(5))

        total_mask = prob.gt(0)

        water = _water_mask.rename('water')
        cloud = cloud_mask.rename('cloud')
        other = other_mask.rename('other')
        snowice = snow_mask.rename('snowice')
        total = total_mask.rename('total')

        image = image.addBands(snowice).addBands(total).addBands(cloud).addBands(water).addBands(other)

        reducer = image.reduceRegion(
            reducer=ee.Reducer.sum(),
            geometry=roi_rect,
            scale=resolution,
            maxPixels=1e11
        )

        snowicepixels = reducer.get('snowice')
        waterpixels = reducer.get('water')
        cloudpixels = reducer.get('cloud')
        otherpixels = reducer.get('other')
        totalpixels = reducer.get('total')

        return image.set('snowice_num', snowicepixels).set('total_num', totalpixels).set('water_num', waterpixels).set('other_num', otherpixels).set('cloud_num', cloudpixels)
    images = images.map(__f)
    return images

# def get_s2_cloudpercentage():
#     print('get_s2_cloudpercentage')

def get_s2_snowicepercentage(date, aoi_rect_ee, water_mask=None,resolution=20):
    s_d, e_d = date.format('YYYY-MM-DD'), (date + pendulum.duration(days=1)).format('YYYY-MM-DD')
    # COPERNICUS / S2_CLOUD_PROBABILITY
    images_snowiceprob = ee.ImageCollection('COPERNICUS/S2_SR_HARMONIZED').filterDate(s_d, e_d).filterBounds(aoi_rect_ee)

    # images = add_surfacewater_cloudprob(images_cloudprob,roi_rect=self.roi_rect)
    features = images_snowiceprob.getInfo()['features']
    img_count_snowiceprob = len(features)
    images_snowice = add_snowicepixelsnumber_image(images_snowiceprob,roi_rect=aoi_rect_ee,
                                               resolution=resolution,
                                               water_mask=water_mask)

    images_snowice_list = images_snowice.toList(images_snowice.size())

    total_pixels, snowice_pixels, cloud_pixels,water_pixels, other_pixels = 0, 0, 0, 0, 0

    if img_count_snowiceprob == 0:
        raise NoEEImageFoundError('COPERNICUS/S2_SR_HARMONIZED',date=s_d)

    acq_time = features[0]['properties']['PRODUCT_ID'].split('_')[2]


    prodids = ','.join([feat['properties']['PRODUCT_ID'] for feat in features])


    for i in range(img_count_snowiceprob):
        img_1 = ee.Image(images_snowice_list.get(i)).clip(aoi_rect_ee)
        snowicenum = ee.Number(img_1.get('snowice_num')).getInfo() ##90:30 60:986
        cloudnum = ee.Number(img_1.get('cloud_num')).getInfo()
        waternum = ee.Number(img_1.get('water_num')).getInfo()
        othernum = ee.Number(img_1.get('other_num')).getInfo()
        totalnum = ee.Number(img_1.get('total_num')).getInfo()
        snowice_pixels += snowicenum
        cloud_pixels += cloudnum
        water_pixels += waternum
        other_pixels += othernum
        total_pixels += totalnum

    if total_pixels == 0:
        raise EEImageOverlayError(ee_source='S2_SNOW_ICE_PROBABILITY',date=s_d)
    return acq_time, snowice_pixels/total_pixels*100, cloud_pixels/total_pixels*100, water_pixels/total_pixels*100, other_pixels/total_pixels*100, prodids

def get_s2_acquistion(date, aoi_rect_ee):
    s_d, e_d = date.format('YYYY-MM-DD'), (date + pendulum.duration(days=1)).format('YYYY-MM-DD')
    # COPERNICUS / S2_CLOUD_PROBABILITY
    images = ee.ImageCollection('COPERNICUS/S2_SR_HARMONIZED').filterDate(s_d, e_d).filterBounds(aoi_rect_ee)

    # images = add_surfacewater_cloudprob(images_cloudprob,roi_rect=self.roi_rect)

    features = images.getInfo()['features']
    img_count_snowiceprob = len(features)
    if img_count_snowiceprob == 0:
        raise NoEEImageFoundError('COPERNICUS/S2_SR_HARMONIZED',date=s_d)
    # id = features[0]['id']
    acq_time = features[0]['properties']['PRODUCT_ID'].split('_')[2]
    return acq_time

def get_s2_cloudpercentage(date, aoi_rect_ee, cloud_prob_threshold=60, water_mask=None,resolution=20):
    s_d, e_d = date.format('YYYY-MM-DD'), (date + pendulum.duration(days=1)).format('YYYY-MM-DD')
    # COPERNICUS / S2_CLOUD_PROBABILITY
    images_cloudprob = ee.ImageCollection('COPERNICUS/S2_CLOUD_PROBABILITY').filterDate(s_d, e_d).filterBounds(aoi_rect_ee)

    # images = add_surfacewater_cloudprob(images_cloudprob,roi_rect=self.roi_rect)

    img_count_cloudprob = len(images_cloudprob.getInfo()['features'])
    images_cloud = add_cloudpixelsnumber_image(images_cloudprob,roi_rect=aoi_rect_ee,
                                               threshold_prob=cloud_prob_threshold,
                                               resolution=resolution,
                                               water_mask=water_mask)

    images_cloud_list = images_cloud.toList(images_cloud.size())

    total_pixels, cloud_pixels = 0, 0

    if img_count_cloudprob == 0:
        raise NoEEImageFoundError('COPERNICUS/S2_CLOUD_PROBABILITY',date=s_d)

    for i in range(img_count_cloudprob):
        img_1 = ee.Image(images_cloud_list.get(i)).clip(aoi_rect_ee)
        cloudnum = ee.Number(img_1.get('cloud_num')).getInfo() ##90:30 60:986
        totalnum = ee.Number(img_1.get('total_num')).getInfo()
        cloud_pixels += cloudnum
        total_pixels += totalnum

    if total_pixels == 0:
        raise EEImageOverlayError(ee_source='S2_CLOUD_PROBABILITY',date=s_d)
    return cloud_pixels/total_pixels*100



def grid_geom(dom):
    '''
    this function was copied from acolite
    '''
    from numpy import zeros

    ## get column and row step
    col_step = dom.getElementsByTagName('COL_STEP')[0].firstChild.nodeValue
    row_step = dom.getElementsByTagName('ROW_STEP')[0].firstChild.nodeValue

    ## get grid values
    values = []
    for val in dom.getElementsByTagName('Values_List')[0].getElementsByTagName('VALUES'):
        values.append([float(i) for i in val.firstChild.nodeValue.split(' ')])

    ## make array of values
    nx, ny = len(values), len(values[0])
    arr =  zeros((nx,ny))
    for i in range(nx):
        for j in range(ny):
            arr[i,j] = values[i][j]
    return(arr)

def fillnan(data):
    from scipy.ndimage import distance_transform_edt
    import numpy as np

    ## fill nans with closest value
    ind = distance_transform_edt(np.isnan(data), return_distances=False, return_indices=True)
    return(data[tuple(ind)])

def tiles_interp(data, xnew, ynew, smooth=False, kern_size=2, method='nearest', mask=None,
                 target_mask=None, target_mask_full=False, fill_nan=True, dtype='float32'):
    import numpy as np
    from scipy.interpolate import griddata
    from scipy.ndimage import uniform_filter, percentile_filter, distance_transform_edt

    if mask is not None: data[mask] = np.nan

    ## fill nans with closest value
    if fill_nan:
        # ind = distance_transform_edt(np.isnan(data), return_distances=False, return_indices=True)
        # cur_data = data[tuple(ind)]
        cur_data = fillnan(data)
    else:
        cur_data = data * 1.0

    ## smooth dataset
    if smooth:
        z = uniform_filter(cur_data, size=kern_size)
        zv = list(z.ravel())
    else:
        zv = list(cur_data.ravel())
    dim = data.shape

    ### tile centers
    # x = arange(0.5, dim[1], 1)
    # y = arange(0.5, dim[0], 1)

    ## tile edges
    x = np.arange(0., dim[1], 1)
    y = np.arange(0., dim[0], 1)

    xv, yv = np.meshgrid(x, y, sparse=False)
    ci = (list(xv.ravel()), list(yv.ravel()))

    ## interpolate
    if target_mask is None:
        ## full dataset
        znew = griddata(ci, zv, (xnew[None, :], ynew[:, None]), method=method)
    else:
        ## limit to target mask
        vd = np.where(target_mask)
        if target_mask_full:
            ## return a dataset with the proper dimensions
            znew = np.zeros((len(ynew), len(xnew))) + np.nan
            znew[vd] = griddata(ci, zv, (xnew[vd[1]], ynew[vd[0]]), method=method)
        else:
            ## return only target_mask data
            znew = griddata(ci, zv, (xnew[vd[1]], ynew[vd[0]]), method=method)

    if dtype is None:
        return (znew)
    else:
        return (znew.astype(np.dtype(dtype)))

def metadata_granule(metafile, fillnan=False):
    '''
    this function was copied from acolite
    '''
    import dateutil.parser
    from xml.dom import minidom
    import numpy as np
    import copy

    try:
        xmldoc = minidom.parse(metafile)
    except:
        print('Error opening metadata file.')
        sys.exit()

    xml_main = xmldoc.firstChild

    metadata = {}
    tags = ['TILE_ID','DATASTRIP_ID', 'SENSING_TIME']
    for tag in tags:
        tdom = xmldoc.getElementsByTagName(tag)
        if len(tdom) > 0: metadata[tag] = tdom[0].firstChild.nodeValue

    #Geometric_Info
    grids = {'10':{}, '20':{}, '60':{}}
    Geometric_Info = xmldoc.getElementsByTagName('n1:Geometric_Info')
    if len(Geometric_Info) > 0:
        for tg in Geometric_Info[0].getElementsByTagName('Tile_Geocoding'):
            tags = ['HORIZONTAL_CS_NAME','HORIZONTAL_CS_CODE']
            for tag in tags:
                tdom = tg.getElementsByTagName(tag)
                if len(tdom) > 0: metadata[tag] = tdom[0].firstChild.nodeValue

            for sub in  tg.getElementsByTagName('Size'):
                res = sub.getAttribute('resolution')
                grids[res]['RESOLUTION'] = float(res)
                tags = ['NROWS','NCOLS']
                for tag in tags:
                    tdom = sub.getElementsByTagName(tag)
                    if len(tdom) > 0: grids[res][tag] = int(tdom[0].firstChild.nodeValue)

            for sub in  tg.getElementsByTagName('Geoposition'):
                res = sub.getAttribute('resolution')
                tags = ['ULX','ULY','XDIM','YDIM']
                for tag in tags:
                    tdom = sub.getElementsByTagName(tag)
                    if len(tdom) > 0: grids[res][tag] = int(tdom[0].firstChild.nodeValue)

        for ta in Geometric_Info[0].getElementsByTagName('Tile_Angles'):
            ## sun angles
            sun_angles={}
            for tag in ['Zenith','Azimuth']:
                for sub in ta.getElementsByTagName('Sun_Angles_Grid')[0].getElementsByTagName(tag):
                    sun_angles[tag] = grid_geom(sub)

            for sub in ta.getElementsByTagName('Mean_Sun_Angle'):
                sun_angles['Mean_Zenith'] = float(sub.getElementsByTagName('ZENITH_ANGLE')[0].firstChild.nodeValue)
                sun_angles['Mean_Azimuth'] = float(sub.getElementsByTagName('AZIMUTH_ANGLE')[0].firstChild.nodeValue)

            ## view angles
            view_angles={} # merged detectors
            view_angles_det={} # separate detectors
            for sub in ta.getElementsByTagName('Viewing_Incidence_Angles_Grids'):
                band = sub.getAttribute('bandId')
                detector = sub.getAttribute('detectorId')
                if band not in view_angles_det: view_angles_det[band]={}
                if detector not in view_angles_det[band]: view_angles_det[band][detector]={}
                band_view = {}
                for tag in ['Zenith','Azimuth']:
                    ret = grid_geom(sub.getElementsByTagName(tag)[0])
                    band_view[tag] = copy.copy(ret)
                    view_angles_det[band][detector][tag] = copy.copy(ret)
                if band not in view_angles.keys():
                    view_angles[band] = band_view
                else:
                    for tag in ['Zenith','Azimuth']:
                        mask = np.isfinite(band_view[tag]) & np.isnan(view_angles[band][tag])
                        view_angles[band][tag][mask] = band_view[tag][mask]

            if fillnan:
                for b,band in enumerate(view_angles.keys()):
                    for tag in ['Zenith','Azimuth']:
                        view_angles[band][tag] = ac.shared.fillnan(view_angles[band][tag])

            ## average view angle grid
            ave = {}
            for b,band in enumerate(view_angles.keys()):
                for tag in ['Zenith','Azimuth']:
                    data = view_angles[band][tag]
                    count = np.isfinite(data)*1
                    if b == 0:
                        ave[tag] = data
                        ave['{}_Count'.format(tag)] = count
                    else:
                        ave[tag] += data
                        ave['{}_Count'.format(tag)] += count
            for tag in ['Zenith','Azimuth']: view_angles['Average_View_{}'.format(tag)] = ave[tag] / ave['{}_Count'.format(tag)]

    metadata["GRIDS"] = grids
    metadata["VIEW"] = view_angles
    metadata["VIEW_DET"] = view_angles_det
    metadata["SUN"] = sun_angles

    ## some interpretation
    #metadata['TIME'] = dateutil.parser.parse(metadata['SENSING_TIME'])
    #metadata["DOY"] = metadata["TIME"].strftime('%j')
    #metadata["SE_DISTANCE"] = ac.shared.distance_se(metadata['DOY'])

    return(metadata)


def get_prodid(pickle_file):
    with open(pickle_file, 'rb') as f:
        info = pickle.load(f)
    proid = info['properties']['PRODUCT_ID']

    ## invalid
    if proid.find('OPER') > -1:
        return None
    return proid

def get_obsgeo_from_MTD_TL(MTD_TL_XML):
    '''
    extract project and transform from MTD_TL.xml file
    '''
    from rasterio.transform import Affine
    meta = metadata_granule(MTD_TL_XML)
    epsg_crs = meta['HORIZONTAL_CS_CODE']
    # print(meta)
    nrows, ncols, ulx, uly = meta['GRIDS']['60']['NROWS'], meta['GRIDS']['60']['NCOLS'], meta['GRIDS']['60']['ULX'],meta['GRIDS']['60']['ULY']

    transform = Affine(60.0, 0.0, ulx, 0.0, -60.0, uly)

    vza = meta['VIEW']['Average_View_Zenith']
    vaa = meta['VIEW']['Average_View_Azimuth']
    sza = meta['SUN']['Zenith']
    saa = meta['SUN']['Azimuth']

    return {'epsg_crs':epsg_crs,
            'nrows_60':nrows,
            'ncols_60':ncols,
            'ulx_60':ulx,
            'uly_60':uly,
            'transform_60':transform,
            'vza':vza,
            'vaa':vaa,
            'sza':sza,
            'saa':saa}


S2_GCS_BUCKET = 'gs://gcp-public-data-sentinel-2'
S2_INDEX_TABLE = '`bigquery-public-data.cloud_storage_geo_index.sentinel_2_index`'
## the tile token is last in an 'essential' product id, so do not require a trailing _
_S2_TILE_RE = re.compile(r'_T(\d{2})([A-Z])([A-Z]{2})(?:_|$)')


def derive_mtd_tl_url(product_id):
    '''
    Where a granule's MTD_TL.xml lives, worked out from the product id alone: the tile
    token T17UNA maps to tiles/17/U/NA. The granule directory is left as a wildcard for
    gsutil to expand, which is the only thing the BigQuery index was ever consulted for.

    Returns None for product ids with no tile token (the old OPER format).
    '''
    m = _S2_TILE_RE.search(product_id)
    if m is None:
        return None
    zone, band, square = m.groups()
    return (f'{S2_GCS_BUCKET}/tiles/{zone}/{band}/{square}/'
            f'{product_id}.SAFE/GRANULE/*/MTD_TL.xml')


def _gsutil_cp(url, dst_file):
    try:
        subprocess.run(['gsutil', '-q', 'cp', url, dst_file],
                       capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError as e:
        print(f'gsutil cp {url}: {e.stderr.strip() or e}')
        return False
    except Exception as e:
        print(f'gsutil cp {url}: {e}')
        return False
    return os.path.exists(dst_file)


def _index_cache_file():
    cache_dir = os.path.join(os.path.expanduser('~'), '.cache', 'gee_downloader')
    os.makedirs(cache_dir, exist_ok=True)
    return os.path.join(cache_dir, 'sentinel2_index.json')


def _first_present(row, *columns):
    for c in columns:
        v = row[c]
        if v is not None and not (isinstance(v, float) and v != v):  ## v != v catches NaN
            return v
    return None


def query_mtd_tl_url(product_id):
    '''
    Fallback lookup in the BigQuery public index, for the rare product whose derived path
    does not resolve.

    Every lookup is metered against the 1TiB monthly free allowance, and measured with
    `bq query --dry_run` it scans 0.5-4GB depending on the product id - a few hundred
    lookups is enough to hit "Quota exceeded: free query bytes scanned". So only the three
    columns actually used are selected, and answers are cached on disk, misses included,
    so no product is ever charged for twice.
    '''
    cache_f = _index_cache_file()
    cache = {}
    if os.path.exists(cache_f):
        try:
            with open(cache_f) as f:
                cache = json.load(f)
        except Exception as e:
            print(f'ignoring unreadable index cache {cache_f}: {e}')
    if product_id in cache:
        return cache[product_id]

    from google.cloud import bigquery
    client = bigquery.Client()
    query_str = (f'SELECT granule_id,base_url,source_url FROM {S2_INDEX_TABLE} '
                 f'WHERE product_id = "{product_id}"')
    try:
        rows_df = client.query(query_str).result().to_dataframe()
    except Exception as e:
        ## letting this fall through left rows_df unbound and raised an UnboundLocalError
        ## further down, hiding the real cause (expired ADC, or the scan quota above)
        print(e)
        print("try---> gcloud auth application-default login; pip install db_dtypes")
        raise BigQueryError(query_str=query_str)

    url = None
    if rows_df.shape[0] > 0:
        ## for some products the location is under source_url instead of base_url
        base_url = _first_present(rows_df.iloc[0], 'base_url', 'source_url')
        granule_id = _first_present(rows_df.iloc[0], 'granule_id')
        if base_url is not None and granule_id is not None:
            url = f'{base_url}/GRANULE/{granule_id}/MTD_TL.xml'

    cache[product_id] = url
    try:
        with open(cache_f, 'w') as f:
            json.dump(cache, f, indent=1)
    except Exception as e:
        print(f'could not update index cache {cache_f}: {e}')
    return url


def get_obsgeo(pickle_file):
    mtd_tl_xml_file = pickle_file.replace('_info.pickle', '_MTD_TL.xml')
    if not os.path.exists(mtd_tl_xml_file):
        proid = get_prodid(pickle_file)
        if proid is None:
            raise OldSentinelFormat(message=pickle_file)

        ## the product id already says where the file is, so go straight to GCS and keep
        ## the metered index query as a fallback for the products that path misses
        mtd_tl_xml_url = derive_mtd_tl_url(proid)
        fetched = _gsutil_cp(mtd_tl_xml_url, mtd_tl_xml_file) if mtd_tl_xml_url else False

        if not fetched:
            print(f'{proid}: not at the derived path, asking the BigQuery index')
            mtd_tl_xml_url = query_mtd_tl_url(proid)
            if mtd_tl_xml_url is None:
                raise NoEEImageFoundError(ee_source=f'sentinel_2_index: {proid}', date='')
            fetched = _gsutil_cp(mtd_tl_xml_url, mtd_tl_xml_file)

        if not fetched:
            raise GsutilError(f'failed: gsutil cp {mtd_tl_xml_url} {mtd_tl_xml_file}')



    obsgeo_dic = get_obsgeo_from_MTD_TL(mtd_tl_xml_file)
    ###
    xnew = np.linspace(0, obsgeo_dic['sza'].shape[1], obsgeo_dic['ncols_60'])
    ynew = np.linspace(0, obsgeo_dic['sza'].shape[0], obsgeo_dic['nrows_60'])

    sza = tiles_interp(data=obsgeo_dic['sza'], xnew=xnew, ynew=ynew, method='nearest')
    saa = tiles_interp(data=obsgeo_dic['saa'], xnew=xnew, ynew=ynew, method='nearest')
    vza = tiles_interp(data=obsgeo_dic['vza'], xnew=xnew, ynew=ynew, method='nearest')
    vaa = tiles_interp(data=obsgeo_dic['vaa'], xnew=xnew, ynew=ynew, method='nearest')
    phi = np.abs(saa - vaa)
    ## raa along 180 degree symmetry
    tmp = np.where(phi > 180)
    phi[tmp] = np.abs(phi[tmp] - 360)
    return sza, vza, phi, obsgeo_dic['transform_60'], obsgeo_dic['epsg_crs']



def get_obsgeo_fromdir(download_dir):
    info_pickels = glob.glob(os.path.join(download_dir, "*.pickle"))
    for pickle_file in info_pickels:
        sza, vza, phi,transform_60, eosg_crs = get_obsgeo(pickle_file)
        print('ok')





