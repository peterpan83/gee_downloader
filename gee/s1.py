import os, glob
import numpy as np
import pickle

import pendulum
import ee

from .exceptions import NoEEImageFoundError


def get_descriptions_l1c(download_dir):
    info_pickels = glob.glob(os.path.join(download_dir, "*.pickle"))
    descriptions = []
    descriptions_meta = 'product_id, transmitterReceiverPolarisation, instrumentMode'
    acquisition_time = ''
    prefix = ''
    for _pf in info_pickels:
        with open(_pf, 'rb') as f:
            info = pickle.load(f)

        _id = info['id'].split('/')[-1]
        if acquisition_time == '':
            acquisition_time = _id.split('_')[4][:13]
        if prefix == '':
            prefix = _id.split('_')[0]

        transmitterReceiverPolarisation = '_'.join(info['properties']['transmitterReceiverPolarisation'])
        instrumentMode = info['properties']['instrumentMode']
        descriptions.append(','.join([_id, transmitterReceiverPolarisation, instrumentMode]))

        # descriptions.append(self.__extract_id_from_info(_pf))
        #  descriptions_meta = 'product_id'
    return prefix, acquisition_time, descriptions, descriptions_meta

def get_s1_acquistion(date, aoi_rect_ee):
    s_d, e_d = date.format('YYYY-MM-DD'), (date + pendulum.duration(days=1)).format('YYYY-MM-DD')
    # COPERNICUS / S2_CLOUD_PROBABILITY
    images = ee.ImageCollection('COPERNICUS/S1_GRD').filterDate(s_d, e_d).filterBounds(aoi_rect_ee)

    # images = add_surfacewater_cloudprob(images_cloudprob,roi_rect=self.roi_rect)

    features = images.getInfo()['features']
    img_count_snowiceprob = len(features)
    if img_count_snowiceprob == 0:
        raise NoEEImageFoundError('COPERNICUS/S1_GRD',date=s_d)
    # id = features[0]['id']
    acq_time = features[0]['properties']['system:index'].split('_')[4]
    try:
        bands = list(set(np.asarray([f['properties']['transmitterReceiverPolarisation'] for f in features]).flatten().tolist()))
    except:
        bands = ['ERROR']
    return acq_time, ','.join(bands)


def get_s1_info(date, aoi_rect_ee):
    try:
        acq_time, bands = get_s1_acquistion(date, aoi_rect_ee)
    except NoEEImageFoundError as e:
        return '',''
    return acq_time, bands


