
import glob, os,pickle
import pendulum
import ee

from .exceptions import *

def get_cloudmask_from_bitmask_l2(img_bitmask):
    '''
    cloud include Dilated Cloud (bit 1), Cirrus (bit 2), Cloud(bit 3), Cloud Shadow(bit 4)
    '''
    qa_bits = 0
    for bit_index in [1,2,3,4]:
        qa_bits += 2**bit_index

    img = img_bitmask.select([0],['cloud_mask']).bitwiseAnd(qa_bits).rightShift(1)
    return img.gt(0)


def get_cloudmask_from_bitmask_l1():
    pass


def add_cloudpixelsnumber_image(images: ee.ImageCollection, roi_rect, resolution=30, water_mask=None):
    # images = images.updateMask(water_mask)
    def __f(image:ee.Image):
        if water_mask is not None:
            image = image.updateMask(water_mask)

        QA = image.select('QA_PIXEL')
        cloud = get_cloudmask_from_bitmask_l2(QA)
        total = QA.gt(0)
        total = total.rename('total_mask')
        image = image.addBands(cloud).addBands(total)

        cloudpixels = image.reduceRegion(
            reducer=ee.Reducer.sum(),
            geometry=roi_rect,
            scale=resolution,
            maxPixels=1e11
        ).get('cloud_mask')

        totalpixels = image.reduceRegion(
            reducer=ee.Reducer.sum(),
            geometry=roi_rect,
            scale=resolution,
            maxPixels=1e11
        ).get('total_mask')
        return image.set('cloud_num', cloudpixels).set('total_num', totalpixels)
    images = images.map(__f)
    return images


def get_cloudpercentage(date, aoi_rect_ee, satellite='LC08', cloud_prob_threshold=60, water_mask=None,resolution=20):
    source = f'LANDSAT/{satellite}/C02/T1_L2'
    s_d, e_d = date.format('YYYY-MM-DD'), (date + pendulum.duration(days=1)).format('YYYY-MM-DD')
    surf_collection = ee.ImageCollection(source).filterDate(s_d, e_d).filterBounds(aoi_rect_ee)

    # images = add_surfacewater_cloudprob(images_cloudprob,roi_rect=self.roi_rect)

    img_count_cloudprob = len(surf_collection.getInfo()['features'])
    if img_count_cloudprob == 0:
        raise NoEEImageFoundError('LANDSAT/LC08/C02/T1_L2', date=s_d)

    images_cloud = add_cloudpixelsnumber_image(surf_collection, roi_rect=aoi_rect_ee,
                                               resolution=resolution,
                                               water_mask=water_mask)

    images_cloud_list = images_cloud.toList(images_cloud.size())

    total_pixels, cloud_pixels = 0, 0
    for i in range(img_count_cloudprob):
        img_1 = ee.Image(images_cloud_list.get(i)).clip(aoi_rect_ee)
        cloudnum = ee.Number(img_1.get('cloud_num')).getInfo()  ##90:30 60:986
        totalnum = ee.Number(img_1.get('total_num')).getInfo()
        cloud_pixels += cloudnum
        total_pixels += totalnum

    if total_pixels == 0:
        raise EEImageOverlayError(ee_source='LANDSAT/LC08/C02/T1_L2', date=s_d)
    return cloud_pixels / total_pixels * 100


def add_snowicepixelsnumber_image_landsat(images: ee.ImageCollection, roi_rect, resolution=20, water_mask=None):
    '''
    Landsat Collection 2 QA_PIXEL bits: 0 Fill, 1 Dilated Cloud, 2 Cirrus, 3 Cloud,
    4 Cloud Shadow, 5 Snow, 6 Clear, 7 Water
    '''
    def __f(image: ee.Image):
        if water_mask is not None:
            image = image.updateMask(water_mask)

        qa = image.select('QA_PIXEL')
        fill_mask = qa.bitwiseAnd(1 << 0).neq(0)
        cloud_mask = get_cloudmask_from_bitmask_l2(qa)
        snowice_mask = qa.bitwiseAnd(1 << 5).neq(0)
        water_band_mask = qa.bitwiseAnd(1 << 7).neq(0)
        total_mask = fill_mask.Not()
        other_mask = total_mask.And(cloud_mask.Not()).And(snowice_mask.Not()).And(water_band_mask.Not())

        total = total_mask.rename('total')
        cloud = cloud_mask.rename('cloud')
        snowice = snowice_mask.rename('snowice')
        water = water_band_mask.rename('water')
        other = other_mask.rename('other')

        image = image.addBands(snowice).addBands(total).addBands(cloud).addBands(water).addBands(other)

        reducer = image.reduceRegion(
            reducer=ee.Reducer.sum(),
            geometry=roi_rect,
            scale=resolution,
            maxPixels=1e11
        )

        return image.set('snowice_num', reducer.get('snowice')) \
                     .set('total_num', reducer.get('total')) \
                     .set('water_num', reducer.get('water')) \
                     .set('other_num', reducer.get('other')) \
                     .set('cloud_num', reducer.get('cloud'))
    images = images.map(__f)
    return images


def get_snowicepercentage(date, aoi_rect_ee, satellite='LC08', water_mask=None, resolution=20):
    source = f'LANDSAT/{satellite}/C02/T1_TOA'
    s_d, e_d = date.format('YYYY-MM-DD'), (date + pendulum.duration(days=1)).format('YYYY-MM-DD')
    images = ee.ImageCollection(source).filterDate(s_d, e_d).filterBounds(aoi_rect_ee)

    features = images.getInfo()['features']
    img_count = len(features)
    if img_count == 0:
        raise NoEEImageFoundError(source, date=s_d)

    date_acquired = features[0]['properties']['DATE_ACQUIRED'].replace('-', '')
    scene_center_time = features[0]['properties']['SCENE_CENTER_TIME'].replace(':', '')[:6]
    acq_time = f'{date_acquired}T{scene_center_time}'
    product_ids = ','.join([feat['properties']['LANDSAT_PRODUCT_ID'] for feat in features])

    images_snowice = add_snowicepixelsnumber_image_landsat(images, roi_rect=aoi_rect_ee,
                                                            resolution=resolution,
                                                            water_mask=water_mask)
    images_snowice_list = images_snowice.toList(images_snowice.size())

    total_pixels, snowice_pixels, cloud_pixels, water_pixels, other_pixels = 0, 0, 0, 0, 0
    for i in range(img_count):
        img_1 = ee.Image(images_snowice_list.get(i)).clip(aoi_rect_ee)
        snowice_pixels += ee.Number(img_1.get('snowice_num')).getInfo()
        cloud_pixels += ee.Number(img_1.get('cloud_num')).getInfo()
        water_pixels += ee.Number(img_1.get('water_num')).getInfo()
        other_pixels += ee.Number(img_1.get('other_num')).getInfo()
        total_pixels += ee.Number(img_1.get('total_num')).getInfo()

    if total_pixels == 0:
        raise EEImageOverlayError(ee_source=source, date=s_d)

    return (acq_time,
            snowice_pixels / total_pixels * 100,
            cloud_pixels / total_pixels * 100,
            water_pixels / total_pixels * 100,
            other_pixels / total_pixels * 100,
            product_ids)



#################### l8 ######################################
def get_l8_cloudpercentage(date, aoi_rect_ee, cloud_prob_threshold=60, water_mask=None,resolution=20):
    return get_cloudpercentage(date=date,
                               aoi_rect_ee=aoi_rect_ee,
                               satellite='LC08',
                               cloud_prob_threshold=cloud_prob_threshold,
                               water_mask=water_mask,
                               resolution=resolution)
    # COPERNICUS / S2_CLOUD_PROBABILITY

def get_l8_snowicepercentage(date, aoi_rect_ee, water_mask=None,resolution=20):
    return get_snowicepercentage(date=date,
                                 aoi_rect_ee=aoi_rect_ee,
                                 satellite='LC08',
                                 water_mask=water_mask,
                                 resolution=resolution)




def get_descriptions_l8_l1toa(download_dir):
    info_pickels = glob.glob(os.path.join(download_dir, "*.pickle"))
    descriptions = []
    descriptions_meta = 'product_id,theta_s,azimuth_s'
    acquisition_time = ''
    prefix = ''
    for _pf in info_pickels:
        with open(_pf, 'rb') as f:
            info = pickle.load(f)
        _id = info['id'].split('/')[-1]

        theta_s = 90-float(info['properties']['SUN_ELEVATION'])
        phi_s = float(info['properties']['SUN_AZIMUTH'])
        if acquisition_time == '':
            acquisition_time = _id.split('_')[2][:13]
        if prefix == '':
            prefix = '_'.join(_id.split('_')[:2])
        descriptions.append(','.join([_id, str(theta_s), str(phi_s)]))

        # descriptions.append(self.__extract_id_from_info(_pf))
        #  descriptions_meta = 'product_id'
    return prefix, acquisition_time, descriptions, descriptions_meta

def get_descriptions_l8_l2rgb(download_dir):
    return get_descriptions_l8_l1toa(download_dir)

##############################l9############################
def get_descriptions_l9_l2rgb(download_dir):
    return get_descriptions_l8_l2rgb(download_dir)

def get_descriptions_l9_l1toa(download_dir):
    return get_descriptions_l8_l2rgb(download_dir)

def get_l9_cloudpercentage(date, aoi_rect_ee, cloud_prob_threshold=60, water_mask=None,resolution=20):
    return get_cloudpercentage(date=date,
                               aoi_rect_ee=aoi_rect_ee,
                               satellite='LC09',
                               cloud_prob_threshold=cloud_prob_threshold,
                               water_mask=water_mask,
                               resolution=resolution)

def get_l9_snowicepercentage(date, aoi_rect_ee, water_mask=None,resolution=20):
    return get_snowicepercentage(date=date,
                                 aoi_rect_ee=aoi_rect_ee,
                                 satellite='LC09',
                                 water_mask=water_mask,
                                 resolution=resolution)





