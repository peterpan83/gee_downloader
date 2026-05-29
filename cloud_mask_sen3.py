"""
Generate an IdePix cloud mask for a single Sentinel-3 OLCI .SEN3 granule.

Usage
-----
    python cloud_mask_sen3.py <sen3_folder> <toa_tif> <output_tif>

    sen3_folder  : path to the .SEN3 directory (L1B granule)
    toa_tif      : TOA GeoTIFF whose UTM grid the cloud mask is projected onto
    output_tif   : path for the output cloud mask GeoTIFF (uint8)

Output band encoding
--------------------
    0   clear
    1   cloud / cloud-shadow (IdePix)
    255 outside swath / invalid

Dependencies
------------
    esa_snappy + ESA SNAP with IdePix OLCI plugin installed.
    See README for setup instructions.

    netCDF4 must be importable BEFORE esa_snappy initialises its JVM to avoid
    an HDF5 shared-library conflict (H5Pset_fapl_ros3 undefined symbol).
    This script guarantees correct import order.
"""

import sys
import os

# ---- Force netCDF4 to load before esa_snappy's JVM to avoid HDF5 conflict ----
import netCDF4  # noqa: F401

import numpy as np
import rasterio
from rasterio.enums import Resampling

sys.path.insert(0, os.path.dirname(__file__))
from cdse.s3_cloud_mask import run_idepix_olci, project_to_utm


def plot_swath_mask(swath_mask: np.ndarray, out_png: str):
    """Save swath_mask (bool, h×w) as a greyscale PNG: white=cloud, black=clear."""
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(10, 8), dpi=150)
    ax.imshow(swath_mask, cmap='gray_r', vmin=0, vmax=1, interpolation='none',
              aspect='auto')
    ax.set_title('IdePix cloud mask — swath coordinates\n'
                 f'cloud pixels: {int(swath_mask.sum())} / {swath_mask.size} '
                 f'({100 * swath_mask.mean():.2f}%)')
    ax.set_xlabel('sample')
    ax.set_ylabel('line')
    fig.tight_layout()
    fig.savefig(out_png, dpi=150)
    plt.close(fig)
    print(f'Swath mask PNG written: {out_png}')


def main(sen3: str, toa_f: str, out_f: str):
    if not os.path.isdir(sen3):
        sys.exit(f'ERROR: .SEN3 folder not found: {sen3}')
    if not os.path.exists(toa_f):
        sys.exit(f'ERROR: TOA GeoTIFF not found: {toa_f}')

    with rasterio.open(toa_f) as src:
        crs       = src.crs
        transform = src.transform
        height    = src.height
        width     = src.width
        meta      = src.meta.copy()

    print(f'Running IdePix on {os.path.basename(sen3)} ...')
    swath_mask = run_idepix_olci(sen3, cloud_buffer=True, buffer_size=2)
    print(f'  Swath shape: {swath_mask.shape}, cloud pixels: {int(swath_mask.sum())}')

    swath_png = os.path.splitext(out_f)[0] + '_swath.png'
    plot_swath_mask(swath_mask, swath_png)

    print('Projecting to UTM ...')
    utm_mask = project_to_utm(swath_mask, sen3, crs, transform, height, width)
    cloud_pct = 100.0 * utm_mask.sum() / utm_mask.size
    print(f'  Cloud pixels: {int(utm_mask.sum())} / {utm_mask.size} ({cloud_pct:.2f}%)')

    meta.update(dtype='uint8', count=1, nodata=255)
    os.makedirs(os.path.dirname(os.path.abspath(out_f)), exist_ok=True)
    with rasterio.open(out_f, 'w', **meta) as dst:
        dst.write(utm_mask.astype(np.uint8), 1)
        dst.set_band_description(1, 'cloud_mask: 0=clear,1=cloud,255=invalid')
        dst.update_tags(
            source_granule=os.path.basename(sen3),
            reference_toa=os.path.basename(toa_f),
        )
        dst.build_overviews([2, 4, 8, 16], Resampling.nearest)
        dst.update_tags(ns='rio_overview', resampling='nearest')

    print(f'Cloud mask written: {out_f}')


if __name__ == '__main__':
    if len(sys.argv) != 4:
        print(__doc__)
        sys.exit(1)
    main(sys.argv[1], sys.argv[2], sys.argv[3])
