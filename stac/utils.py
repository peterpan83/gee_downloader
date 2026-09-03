#!/usr/bin/env python3
"""Utility functions for merging and processing raster files in the geoanalytics workflow."""

from __future__ import annotations

import glob
import logging
import os
import shutil
import tempfile
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

import fsspec
import numpy as np
import rasterio
from rasterio.io import MemoryFile
from rasterio.merge import merge
from rasterio.warp import Resampling, calculate_default_transform, reproject
from rio_cogeo.cogeo import cog_translate
from rio_cogeo.profiles import cog_profiles

from .geoanalytics_io_client import GeoanalyticsIOClient

if TYPE_CHECKING:
    import xarray

logger = logging.getLogger(__name__)


def reproject_raster_dataset(
    src: rasterio.DatasetReader, dst_crs
) -> rasterio.DatasetReader:
    """Reproject a raster dataset to a target CRS, returning an in-memory dataset."""
    src_crs = src.crs
    transform, width, height = calculate_default_transform(
        src_crs, dst_crs, src.width, src.height, *src.bounds
    )
    kwargs = src.meta.copy()
    kwargs.update(
        {"crs": dst_crs, "transform": transform, "width": width, "height": height}
    )
    memfile = MemoryFile()
    with memfile.open(**kwargs) as dst:
        for i in range(1, src.count + 1):
            reproject(
                source=rasterio.band(src, i),
                destination=rasterio.band(dst, i),
                src_transform=src.transform,
                src_crs=src.crs,
                dst_transform=transform,
                dst_crs=dst_crs,
                resampling=Resampling.nearest,
            )
    return memfile.open()


def mosaic_tifs(
    tif_files: List[str], dst_crs=None
) -> Tuple[np.ndarray, rasterio.Affine, Any]:
    """
    Mosaic multiple TIF files into a single array.

    Args:
        tif_files: List of paths to TIF files to mosaic.
        dst_crs: Target CRS for the output. If None, uses the CRS of the largest file.

    Returns:
        Tuple of (mosaic array, transform, CRS).
    """
    tif_files = sorted(tif_files, key=lambda x: os.path.getsize(x))
    src_files_to_mosaic = []
    dst_crs_ret = dst_crs

    for tif in reversed(tif_files):
        src = rasterio.open(tif, "r")
        crs = src.meta["crs"]
        if dst_crs_ret is None:
            dst_crs_ret = crs
            src_files_to_mosaic.append(src)
            continue
        if crs == dst_crs_ret:
            src_files_to_mosaic.append(src)
            continue
        reproj_src = reproject_raster_dataset(src, dst_crs=dst_crs_ret)
        src_files_to_mosaic.append(reproj_src)

    mosaic, out_trans = merge(src_files_to_mosaic)
    return mosaic, out_trans, dst_crs_ret


def stack_bands(
    tif_files: List[str],
    dst_crs=None,
    target_resolution: Optional[float] = None,
    clip_bbox: Optional[List[float]] = None,
    clip_bbox_crs: str = "EPSG:4326",
) -> Tuple[np.ndarray, rasterio.Affine, Any, Dict[str, Any]]:
    """
    Stack multiple single-band TIF files into a single multi-band array.

    Unlike mosaic_tifs which spatially merges tiles, this function stacks
    individual band files (e.g., B02.tif, B03.tif, B04.tif) into a single
    multi-band array.

    All bands are resampled/reprojected to match the grid of the first
    (reference) band.

    Args:
        tif_files: List of paths to single-band TIF files to stack.
        dst_crs: Target CRS. If None, uses CRS from the first file.
        target_resolution: Target resolution in CRS units. If None, uses
                          the resolution of the first file.
        clip_bbox: Optional bounding box [minx, miny, maxx, maxy] to clip output to.
        clip_bbox_crs: CRS of the clip_bbox (default: EPSG:4326 / WGS84).

    Returns:
        Tuple of (stacked array, transform, CRS, metadata dict).
        The stacked array has shape (num_bands, height, width).
    """
    from rasterio.warp import transform_bounds

    if not tif_files:
        raise ValueError("No TIF files provided for stacking")

    # Use the first file as the reference for grid alignment
    with rasterio.open(tif_files[0], "r") as ref_src:
        ref_crs = dst_crs or ref_src.crs
        ref_transform = ref_src.transform
        ref_width = ref_src.width
        ref_height = ref_src.height
        ref_bounds = ref_src.bounds
        ref_dtype = ref_src.dtypes[0]
        ref_nodata = ref_src.nodata

        # If clip_bbox is provided, transform to raster CRS and intersect with raster bounds
        if clip_bbox is not None:
            minx, miny, maxx, maxy = clip_bbox

            # Transform clip_bbox from its CRS to the raster's CRS
            try:
                transformed_bbox = transform_bounds(
                    clip_bbox_crs, ref_src.crs, minx, miny, maxx, maxy
                )
                t_minx, t_miny, t_maxx, t_maxy = transformed_bbox
                logger.info(
                    f"Transformed clip bbox from {clip_bbox_crs} to {ref_src.crs}: "
                    f"[{t_minx:.2f}, {t_miny:.2f}, {t_maxx:.2f}, {t_maxy:.2f}]"
                )
            except Exception as e:
                logger.warning(f"Failed to transform clip bbox: {e}, using as-is")
                t_minx, t_miny, t_maxx, t_maxy = minx, miny, maxx, maxy

            # Intersect transformed clip_bbox with raster bounds
            clipped_minx = max(t_minx, ref_bounds.left)
            clipped_miny = max(t_miny, ref_bounds.bottom)
            clipped_maxx = min(t_maxx, ref_bounds.right)
            clipped_maxy = min(t_maxy, ref_bounds.top)

            if clipped_minx >= clipped_maxx or clipped_miny >= clipped_maxy:
                logger.warning(
                    f"Clip bbox [{t_minx:.2f}, {t_miny:.2f}, {t_maxx:.2f}, {t_maxy:.2f}] "
                    f"does not intersect raster bounds [{ref_bounds.left:.2f}, {ref_bounds.bottom:.2f}, "
                    f"{ref_bounds.right:.2f}, {ref_bounds.top:.2f}]. Skipping clip."
                )
            else:
                ref_bounds = rasterio.coords.BoundingBox(
                    clipped_minx, clipped_miny, clipped_maxx, clipped_maxy
                )
                logger.info(
                    f"Clipping to AOI bounds: [{clipped_minx:.2f}, {clipped_miny:.2f}, "
                    f"{clipped_maxx:.2f}, {clipped_maxy:.2f}]"
                )

        # If target resolution is specified or clipping, recalculate dimensions
        if target_resolution is not None or clip_bbox is not None:
            res = target_resolution if target_resolution else abs(ref_transform[0])
            ref_transform, ref_width, ref_height = calculate_default_transform(
                ref_src.crs,
                ref_crs,
                ref_src.width,
                ref_src.height,
                *ref_bounds,
                resolution=res,
            )

    # Pre-allocate the output array
    num_bands = len(tif_files)
    stacked = np.zeros((num_bands, ref_height, ref_width), dtype=ref_dtype)

    # Read and resample each band
    for i, tif_path in enumerate(tif_files):
        with rasterio.open(tif_path, "r") as src:
            # Check if reprojection/resampling is needed
            needs_reproject = (
                src.crs != ref_crs
                or src.transform != ref_transform
                or src.width != ref_width
                or src.height != ref_height
            )

            if needs_reproject:
                # Reproject/resample to match reference grid
                reproject(
                    source=rasterio.band(src, 1),
                    destination=stacked[i],
                    src_transform=src.transform,
                    src_crs=src.crs,
                    dst_transform=ref_transform,
                    dst_crs=ref_crs,
                    resampling=Resampling.bilinear,
                )
            else:
                # Direct read
                stacked[i] = src.read(1)

    metadata = {
        "dtype": ref_dtype,
        "nodata": ref_nodata,
        "bounds": ref_bounds,
    }

    return stacked, ref_transform, ref_crs, metadata


def merge_tifs(
    tif_files: List[str],
    out_file: str,
    descriptions: str,
    descriptions_meta: str,
    bandnames: Optional[List[str]] = None,
    dst_crs=None,
    RGB: bool = False,
    min_max: Tuple[Optional[float], Optional[float]] = (None, None),
    **extra_info,
) -> Tuple[int, Any]:
    """
    Merge multiple TIF files into a single output file.

    Args:
        tif_files: List of paths to TIF files to merge.
        out_file: Output file path.
        descriptions: Description string to embed in the output.
        descriptions_meta: Metadata description to embed.
        bandnames: Optional list of band names for the output.
        dst_crs: Target CRS. If None, uses CRS from input files.
        RGB: If True, scales output to 0-255 uint8 for RGB visualization.
        min_max: Tuple of (min, max) values for RGB scaling.
        **extra_info: Additional metadata tags (e.g., cloud_percentage).

    Returns:
        Tuple of (success code, output CRS).
    """
    tif_files = sorted(tif_files, key=lambda x: os.path.getsize(x))
    if bandnames is not None:
        bandnames_c = bandnames.copy()
    else:
        bandnames_c = None

    src_files_to_mosaic = []
    dst_crs_ret = dst_crs

    for tif in reversed(tif_files):
        src = rasterio.open(tif, "r")
        crs = src.meta["crs"]
        if dst_crs_ret is None:
            dst_crs_ret = crs
            src_files_to_mosaic.append(src)
            continue
        if crs == dst_crs_ret:
            src_files_to_mosaic.append(src)
            continue
        reproj_src = reproject_raster_dataset(src, dst_crs=dst_crs_ret)
        src_files_to_mosaic.append(reproj_src)

    mosaic, out_trans = merge(src_files_to_mosaic)
    out_meta = src.meta.copy()

    if RGB:
        invalid_mask = mosaic == 0
        mosaic = (np.clip(mosaic, min_max[0], min_max[1]) / min_max[1]) * 255
        mosaic[invalid_mask] = 0
        mosaic = mosaic.astype("uint8")
        out_meta["dtype"] = rasterio.uint8

    out_meta.update(
        {
            "driver": "GTiff",
            "height": mosaic.shape[1],
            "width": mosaic.shape[2],
            "transform": out_trans,
            "count": mosaic.shape[0],
            "crs": dst_crs_ret,
        }
    )

    with rasterio.open(out_file, "w", **out_meta) as dst:
        dst.update_tags(info=descriptions)
        dst.update_tags(info_item=descriptions_meta)

        for key in extra_info:
            if key == "cloud_percentage":
                dst.update_tags(cloud_percentage=extra_info[key])

        dst.write(mosaic)
        if bandnames_c is not None:
            dst.descriptions = tuple(bandnames_c)

    return 1, dst_crs_ret


class DownloadDirIncompleteError(Exception):
    """Raised when a download directory does not contain valid TIF files."""

    def __init__(self, download_dir: str):
        self.download_dir = download_dir
        super().__init__(f"Download directory incomplete or empty: {download_dir}")


def merge_download_dir(
    download_dir: str,
    output_path: str,
    descriptions_meta: str,
    descriptions: List[str],
    dst_crs=None,
    bandnames: Optional[List[str]] = None,
    remove_temp: bool = True,
    RGB: bool = False,
    min_max: Tuple[Optional[float], Optional[float]] = (None, None),
    min_file_size_kb: float = 20.0,
    **extra_info,
) -> Any:
    """
    Merge all TIF files in a download directory into a single output file.

    This is designed for merging multiple downloaded tiles/chunks into a
    single cohesive raster file.

    Args:
        download_dir: Directory containing the downloaded TIF files.
        output_path: Path for the merged output file.
        descriptions_meta: Metadata description string.
        descriptions: List of description strings to join.
        dst_crs: Target CRS. If None, uses CRS from input files.
        bandnames: Optional list of band names.
        remove_temp: If True, removes the download directory after merge.
        RGB: If True, scales to uint8 RGB.
        min_max: Min/max values for RGB scaling.
        min_file_size_kb: Minimum file size in KB to consider valid.
        **extra_info: Additional metadata tags.

    Returns:
        The output CRS used.

    Raises:
        DownloadDirIncompleteError: If no valid TIF files found.
    """
    # Find TIF files matching the expected pattern and above minimum size
    tifs = [
        f
        for f in glob.glob(os.path.join(download_dir, "*.tif"))
        if (os.path.getsize(f) / 1024.0) > min_file_size_kb
    ]

    if len(tifs) < 1:
        raise DownloadDirIncompleteError(download_dir)

    ret, dst_crs, _coverage = merge_tifs(
        tifs,
        output_path,
        descriptions=":".join(descriptions)
        if isinstance(descriptions, list)
        else descriptions,
        descriptions_meta=descriptions_meta,
        bandnames=bandnames,
        dst_crs=dst_crs,
        RGB=RGB,
        min_max=min_max,
        **extra_info,
    )

    if ret == 1 and remove_temp:
        shutil.rmtree(download_dir)

    return dst_crs

def merge_downloaded_assets_to_cog(
    tif_files: List[str],
    output_path: str,
    io_client: Optional[GeoanalyticsIOClient],
    bandnames: List[str],
    descriptions: str,
    remove_temp: bool = False,
    cloud_percentage: Optional[float] = None,
    clip_bbox: Optional[Tuple[float, float, float, float]] = None,
    target_resolution: float = 10.0,  # Add this parameter with default 10m
) -> None:
    """
    Stack multiple downloaded single-band TIF files and write as a Cloud Optimized GeoTIFF (COG).
    """
    import tempfile
    import fsspec

    # Handle storage options based on whether io_client exists
    if io_client is not None:
        writer_opts = io_client._storage_options(output_path, write=True)
    else:
        # Local file system - no special storage options needed
        writer_opts = {}
        # Ensure output directory exists for local paths
        os.makedirs(os.path.dirname(output_path), exist_ok=True)

    if not tif_files:
        raise ValueError("No TIF files provided for stacking")

    # Filter out files that are too small (likely corrupt or empty)
    valid_tifs = [f for f in tif_files if os.path.getsize(f) > 20 * 1024]
    if not valid_tifs:
        raise DownloadDirIncompleteError("No valid TIF files found (all below 20KB)")

    try:
        # Stack bands into a single array with 10m resolution
        stacked, out_trans, dst_crs_ret, metadata = stack_bands(
            valid_tifs,
            dst_crs=None,
            clip_bbox=clip_bbox,
            target_resolution=target_resolution
        )

        # Build output metadata
        out_meta = {
            "driver": "GTiff",
            "height": stacked.shape[1],
            "width": stacked.shape[2],
            "transform": out_trans,
            "count": stacked.shape[0],
            "crs": dst_crs_ret,
            "dtype": stacked.dtype,
        }
        if metadata.get("nodata") is not None:
            out_meta["nodata"] = metadata["nodata"]

        # Write to a temporary file first, then convert to COG
        with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as tmp_src:
            tmp_src_path = tmp_src.name

        with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as tmp_cog:
            tmp_cog_path = tmp_cog.name

        try:
            # Write the stacked raster
            with rasterio.open(tmp_src_path, "w", **out_meta) as dst:
                if descriptions:
                    dst.update_tags(info=descriptions)
                if cloud_percentage is not None:
                    dst.update_tags(cloud_percentage=cloud_percentage)
                dst.write(stacked)
                if bandnames:
                    dst.descriptions = tuple(bandnames[: stacked.shape[0]])
                if clip_bbox is not None:
                    dst.update_tags(clipped_to_aoi="true")
                    dst.update_tags(clip_bbox=str(clip_bbox))

            # Convert to COG
            cog_profile = cog_profiles.get("deflate")
            cog_profile.update(
                {
                    "blockxsize": 256,
                    "blockysize": 256,
                }
            )

            config = {
                "GDAL_NUM_THREADS": "ALL_CPUS",
                "GDAL_TIFF_INTERNAL_MASK": True,
                "GDAL_TIFF_OVR_BLOCKSIZE": "128",
            }

            cog_translate(
                tmp_src_path,
                tmp_cog_path,
                cog_profile,
                config=config,
                in_memory=False,
            )

            # Write to output (handles both local and cloud storage)
            with open(tmp_cog_path, "rb") as src_file:
                with fsspec.open(
                    output_path, "wb", auto_mkdir=True, **writer_opts
                ) as dest_file:
                    shutil.copyfileobj(src_file, dest_file)

            logger.info(f"Successfully wrote stacked COG to {output_path}")

        finally:
            # Clean up temp files
            if os.path.exists(tmp_src_path):
                os.unlink(tmp_src_path)
            if os.path.exists(tmp_cog_path):
                os.unlink(tmp_cog_path)

        # Optionally remove source files
        if remove_temp:
            for tif in tif_files:
                if os.path.exists(tif):
                    os.unlink(tif)

        return output_path

    except Exception as e:
        logger.error(f"Failed to stack and write COG: {e}")
        raise


def collect_downloaded_tifs(
    download_dir: str, pattern: str = "*.tif", min_size_kb: float = 20.0
) -> List[str]:
    """
    Collect valid TIF files from a download directory.

    Args:
        download_dir: Directory to search.
        pattern: Glob pattern for matching files.
        min_size_kb: Minimum file size in KB.

    Returns:
        List of paths to valid TIF files, sorted by size (largest first).
    """
    all_tifs = glob.glob(os.path.join(download_dir, pattern))
    valid_tifs = [f for f in all_tifs if os.path.getsize(f) > min_size_kb * 1024]
    return sorted(valid_tifs, key=lambda x: os.path.getsize(x), reverse=True)


# =============================================================================
# Zarr Output Support (using xarray/rioxarray)
# =============================================================================


def stack_bands_to_xarray(
    tif_files: List[str],
    bandnames: Optional[List[str]] = None,
    chunks: Optional[Dict[str, int]] = None,
    clip_bbox: Optional[List[float]] = None,
    clip_bbox_crs: str = "EPSG:4326",
) -> "xarray.Dataset":
    """
    Stack multiple single-band TIF files into an xarray Dataset.

    Uses rioxarray to load files with full geospatial metadata preserved.
    Optionally uses Dask for lazy loading and parallel processing.

    Args:
        tif_files: List of paths to single-band TIF files to stack.
        bandnames: Optional list of band names. If None, uses filenames.
        chunks: Optional Dask chunk sizes as dict (e.g., {"x": 512, "y": 512}).
                If provided, returns a Dask-backed Dataset for parallel ops.
        clip_bbox: Optional bounding box [minx, miny, maxx, maxy] to clip output to.
        clip_bbox_crs: CRS of the clip_bbox (default: EPSG:4326 / WGS84).

    Returns:
        xarray.Dataset with stacked bands and geospatial metadata.

    Example:
        ```python
        ds = stack_bands_to_xarray(
            ["B02.tif", "B03.tif", "B04.tif"],
            bandnames=["blue", "green", "red"],
            chunks={"x": 512, "y": 512}
        )
        ```
    """
    import rioxarray  # noqa: F401
    import xarray as xr
    from rasterio.warp import transform_bounds

    if not tif_files:
        raise ValueError("No TIF files provided for stacking")

    # Generate band names if not provided
    if bandnames is None:
        bandnames = [os.path.splitext(os.path.basename(f))[0] for f in tif_files]

    # Ensure we have the right number of band names
    if len(bandnames) != len(tif_files):
        bandnames = [f"band_{i}" for i in range(len(tif_files))]

    data_arrays = []
    transformed_clip_bbox = None  # Cache the transformed bbox

    for i, tif_path in enumerate(tif_files):
        # Open with rioxarray (preserves CRS, transform, etc.)
        da = xr.open_dataarray(tif_path, engine="rasterio", chunks=chunks)

        # Clip to AOI bounding box if provided
        if clip_bbox is not None:
            minx, miny, maxx, maxy = clip_bbox

            # Transform clip_bbox to raster CRS if needed (cache for reuse)
            if transformed_clip_bbox is None and da.rio.crs is not None:
                try:
                    transformed_clip_bbox = transform_bounds(
                        clip_bbox_crs, da.rio.crs, minx, miny, maxx, maxy
                    )
                    logger.info(
                        f"Transformed clip bbox from {clip_bbox_crs} to {da.rio.crs}"
                    )
                except Exception as e:
                    logger.warning(f"Failed to transform clip bbox: {e}, using as-is")
                    transformed_clip_bbox = (minx, miny, maxx, maxy)

            if transformed_clip_bbox:
                t_minx, t_miny, t_maxx, t_maxy = transformed_clip_bbox
                try:
                    da = da.rio.clip_box(
                        minx=t_minx, miny=t_miny, maxx=t_maxx, maxy=t_maxy
                    )
                except Exception as e:
                    logger.warning(f"Failed to clip band {i}: {e}, using full extent")

        # Handle band dimension - we want each file to be a single band
        if "band" in da.dims:
            if da.sizes["band"] == 1:
                # Single band - squeeze and we'll re-add with our name
                da = da.isel(band=0, drop=True)
            else:
                # Multi-band file - just take the first band
                logger.warning(
                    f"File {tif_path} has {da.sizes['band']} bands, using first band only"
                )
                da = da.isel(band=0, drop=True)

        # Drop any existing 'band' coordinate (scalar or otherwise)
        if "band" in da.coords:
            da = da.drop_vars("band")

        # Now we should have a 2D array (y, x) - add band dimension with our name
        da = da.expand_dims(dim="band")
        da = da.assign_coords(band=[bandnames[i]])

        data_arrays.append(da)

    # Concatenate along band dimension
    stacked = xr.concat(data_arrays, dim="band")

    # Create a Dataset
    ds = stacked.to_dataset(name="data")

    # Copy CRS info from first file
    if hasattr(data_arrays[0], "rio") and data_arrays[0].rio.crs is not None:
        ds["data"].rio.write_crs(data_arrays[0].rio.crs, inplace=True)
        ds.attrs["crs"] = str(data_arrays[0].rio.crs)

    # Store transform as attribute
    if hasattr(data_arrays[0], "rio") and data_arrays[0].rio.transform() is not None:
        transform = data_arrays[0].rio.transform()
        ds.attrs["transform"] = list(transform)[:6]

    ds.attrs["bandnames"] = bandnames

    return ds


def write_xarray_to_zarr(
    ds: "xarray.Dataset",
    output_path: str,
    io_client,
    chunks: Optional[Dict[str, int]] = None,
    mode: str = "w",
    consolidated: bool = True,
    compute: bool = True,
    **extra_attrs,
) -> str:
    """
    Write an xarray Dataset to Zarr format with cloud storage support.

    Args:
        ds: xarray Dataset to write.
        output_path: Destination path (local, abfs://, s3://, etc.).
        io_client: GeoanalyticsIOClient for storage operations.
        chunks: Optional chunk sizes. If None, uses existing chunks or auto.
        mode: Write mode ('w' for overwrite, 'a' for append).
        consolidated: Whether to consolidate Zarr metadata.
        compute: If True (default), compute immediately. If False, return delayed.
        **extra_attrs: Additional attributes to add to the dataset.

    Returns:
        The output path.

    Example:
        ```python
        write_xarray_to_zarr(
            ds,
            "abfs://container/output.zarr",
            io_client,
            chunks={"band": 1, "y": 512, "x": 512},
            cloud_percentage=15.5,
        )
        ```
    """
    # Try to import compressor - zarr v3 uses numcodecs, v2 had it built-in
    try:
        from numcodecs import Blosc

        compressor = Blosc(cname="zstd", clevel=3, shuffle=Blosc.BITSHUFFLE)
    except ImportError:
        # Fallback for older zarr versions
        import zarr

        compressor = zarr.Blosc(cname="zstd", clevel=3, shuffle=zarr.Blosc.BITSHUFFLE)

    # Add extra attributes
    for key, value in extra_attrs.items():
        if value is not None:
            ds.attrs[key] = value

    # Rechunk if specified
    if chunks is not None:
        ds = ds.chunk(chunks)

    # Set encoding for compression
    encoding = {}
    for var in ds.data_vars:
        encoding[var] = {
            "compressor": compressor,
        }
        if chunks:
            encoding[var]["chunks"] = tuple(
                chunks.get(dim, ds.sizes[dim]) for dim in ds[var].dims
            )

    # Check if output is cloud storage
    is_cloud = output_path.startswith(("abfs://", "s3://", "gs://", "az://"))

    if is_cloud:
        # Get storage options for cloud write
        storage_opts = io_client._storage_options(output_path, write=True)

        # If mode is 'w' (overwrite), delete existing store first
        # This ensures a clean write and avoids conflicts with existing data
        if mode == "w":
            try:
                fs, path = fsspec.core.url_to_fs(output_path, **storage_opts)
                if fs.exists(path):
                    logger.info(f"Removing existing Zarr store: {output_path}")
                    fs.rm(path, recursive=True)
            except Exception as e:
                logger.warning(f"Could not check/remove existing store: {e}")

        # For zarr v3, pass storage_options directly to to_zarr()
        # This avoids the JSON serialization issue with credentials
        logger.info(f"Writing Zarr directly to cloud: {output_path}")

        write_job = ds.to_zarr(
            output_path,
            mode=mode,
            consolidated=consolidated,
            encoding=encoding,
            compute=compute,
            storage_options=storage_opts,
        )

        if not compute:
            return write_job

        logger.info(f"Successfully wrote xarray Dataset to {output_path}")
        return output_path
    else:
        # Local path - write directly
        # If mode is 'w', remove existing store first
        if mode == "w" and os.path.exists(output_path):
            import shutil

            logger.info(f"Removing existing local Zarr store: {output_path}")
            shutil.rmtree(output_path)

        write_job = ds.to_zarr(
            output_path,
            mode=mode,
            consolidated=consolidated,
            encoding=encoding,
            compute=compute,
        )

        if not compute:
            return write_job

        logger.info(f"Successfully wrote xarray Dataset to {output_path}")
        return output_path


# =============================================================================
# Hierarchical Zarr Store Functions
# =============================================================================
# These functions support a single Zarr store with groups for different dates/scenes,
# containing both individual bands and merged data.


def open_or_create_zarr_store(
    store_path: str,
    io_client,
    mode: str = "a",
) -> str:
    """
    Open an existing Zarr store or create a new one.

    Args:
        store_path: Path to the Zarr store (local or cloud).
        io_client: GeoanalyticsIOClient for storage operations.
        mode: 'a' to append/create, 'w' to overwrite.

    Returns:
        The store path.
    """
    import zarr

    is_cloud = store_path.startswith(("abfs://", "s3://", "gs://", "az://"))

    if is_cloud:
        storage_opts = io_client._storage_options(store_path, write=True)
        fs, path = fsspec.core.url_to_fs(store_path, **storage_opts)

        if mode == "w" and fs.exists(path):
            logger.info(f"Removing existing Zarr store: {store_path}")
            fs.rm(path, recursive=True)

        # Create root group if it doesn't exist
        if not fs.exists(path):
            logger.info(f"Creating new Zarr store: {store_path}")
            # Use zarr v3 approach with storage_options
            root = zarr.open_group(
                store_path,
                mode="w",
                storage_options=storage_opts,
            )
            root.attrs["created"] = str(np.datetime64("now"))
            root.attrs["format"] = "geoanalytics-zarr-v1"
    else:
        if mode == "w" and os.path.exists(store_path):
            shutil.rmtree(store_path)

        if not os.path.exists(store_path):
            logger.info(f"Creating new local Zarr store: {store_path}")
            root = zarr.open_group(store_path, mode="w")
            root.attrs["created"] = str(np.datetime64("now"))
            root.attrs["format"] = "geoanalytics-zarr-v1"

    return store_path


def write_band_to_zarr_group(
    tif_path: str,
    store_path: str,
    group_path: str,
    band_name: str,
    io_client,
    chunks: Tuple[int, int] = (512, 512),
    **extra_attrs,
) -> str:
    """
    Write a single band TIF to a Zarr store as a named array within a group.

    Args:
        tif_path: Path to the local TIF file.
        store_path: Path to the root Zarr store.
        group_path: Path within the store (e.g., "20250103/bands").
        band_name: Name for this band array (e.g., "blue", "B02").
        io_client: GeoanalyticsIOClient for storage operations.
        chunks: Chunk sizes as (y, x) tuple.
        **extra_attrs: Additional attributes for the array.

    Returns:
        Full path to the written array.
    """
    import rioxarray  # noqa: F401
    import xarray as xr

    # Open the TIF
    da = xr.open_dataarray(tif_path, engine="rasterio")

    # Squeeze band dimension if single band
    if "band" in da.dims and da.sizes["band"] == 1:
        da = da.isel(band=0, drop=True)

    # Add attributes
    if da.rio.crs is not None:
        extra_attrs["crs"] = str(da.rio.crs)
    if da.rio.transform() is not None:
        extra_attrs["transform"] = list(da.rio.transform())[:6]

    for key, value in extra_attrs.items():
        da.attrs[key] = value

    # Build the full output path
    full_path = f"{store_path}/{group_path}/{band_name}"
    is_cloud = store_path.startswith(("abfs://", "s3://", "gs://", "az://"))

    # Chunk the data
    da = da.chunk({"y": chunks[0], "x": chunks[1]})

    # Convert to dataset for to_zarr
    ds = da.to_dataset(name="data")

    if is_cloud:
        storage_opts = io_client._storage_options(store_path, write=True)
        ds.to_zarr(
            full_path,
            mode="w",
            consolidated=True,
            storage_options=storage_opts,
        )
    else:
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        ds.to_zarr(full_path, mode="w", consolidated=True)

    logger.info(f"Wrote band {band_name} to {full_path}")
    return full_path


def write_merged_to_zarr_group(
    tif_files: List[str],
    store_path: str,
    group_path: str,
    io_client,
    bandnames: Optional[List[str]] = None,
    chunks: Tuple[int, int, int] = (1, 512, 512),
    clip_bbox: Optional[List[float]] = None,
    **extra_attrs,
) -> str:
    """
    Stack multiple TIF files and write as a merged array to a Zarr group.

    Args:
        tif_files: List of local TIF file paths.
        store_path: Path to the root Zarr store.
        group_path: Path within the store (e.g., "20250103/merged").
        io_client: GeoanalyticsIOClient for storage operations.
        bandnames: Optional list of band names.
        chunks: Chunk sizes as (band, y, x) tuple.
        clip_bbox: Optional bounding box [minx, miny, maxx, maxy] to clip output to.
        **extra_attrs: Additional attributes for the dataset.

    Returns:
        Full path to the written merged array.
    """
    # Stack bands into xarray Dataset
    chunk_dict = {"x": chunks[2], "y": chunks[1]}
    ds = stack_bands_to_xarray(
        tif_files, bandnames=bandnames, chunks=chunk_dict, clip_bbox=clip_bbox
    )

    # Add clip_bbox info to attributes if provided
    if clip_bbox is not None:
        extra_attrs["clip_bbox"] = str(clip_bbox)
        extra_attrs["clipped_to_aoi"] = "true"

    # Add extra attributes
    for key, value in extra_attrs.items():
        if value is not None:
            ds.attrs[key] = value

    # Build the full output path
    full_path = f"{store_path}/{group_path}"
    is_cloud = store_path.startswith(("abfs://", "s3://", "gs://", "az://"))

    # Set up chunks for writing
    write_chunks = {"band": chunks[0], "y": chunks[1], "x": chunks[2]}
    ds = ds.chunk(write_chunks)

    # Set encoding
    try:
        from numcodecs import Blosc

        compressor = Blosc(cname="zstd", clevel=3, shuffle=Blosc.BITSHUFFLE)
    except ImportError:
        import zarr

        compressor = zarr.Blosc(cname="zstd", clevel=3, shuffle=zarr.Blosc.BITSHUFFLE)

    encoding = {}
    for var in ds.data_vars:
        encoding[var] = {"compressor": compressor}

    if is_cloud:
        storage_opts = io_client._storage_options(store_path, write=True)
        ds.to_zarr(
            full_path,
            mode="w",
            consolidated=True,
            encoding=encoding,
            storage_options=storage_opts,
        )
    else:
        os.makedirs(full_path, exist_ok=True)
        ds.to_zarr(full_path, mode="w", consolidated=True, encoding=encoding)

    logger.info(f"Wrote merged data to {full_path}")
    return full_path


def ingest_scene_to_zarr_store(
    tif_files: List[str],
    store_path: str,
    scene_id: str,
    io_client,
    bandnames: Optional[List[str]] = None,
    chunks: Tuple[int, int, int] = (1, 512, 512),
    write_individual_bands: bool = True,
    write_merged: bool = True,
    **scene_attrs,
) -> Dict[str, str]:
    """
    Ingest a complete scene (all bands) into a hierarchical Zarr store.

    Creates the following structure:
        <store>/<scene_id>/bands/<band_name>/   - Individual band arrays
        <store>/<scene_id>/merged/              - Stacked multi-band array

    Args:
        tif_files: List of local TIF file paths for each band.
        store_path: Path to the root Zarr store.
        scene_id: Identifier for this scene (e.g., "20250103" or "S2A_20250103_T10UEV").
        io_client: GeoanalyticsIOClient for storage operations.
        bandnames: Optional list of band names (derived from filenames if not provided).
        chunks: Chunk sizes as (band, y, x) tuple.
        write_individual_bands: If True, write each band as a separate array.
        write_merged: If True, write the stacked multi-band array.
        **scene_attrs: Additional attributes for the scene group (e.g., cloud_cover).

    Returns:
        Dict mapping group names to their paths.

    Example:
        ```python
        paths = ingest_scene_to_zarr_store(
            tif_files=["B02.tif", "B03.tif", "B04.tif"],
            store_path="abfs://container/sentinel2.zarr",
            scene_id="20250103",
            io_client=io_client,
            bandnames=["blue", "green", "red"],
            cloud_cover=15.5,
        )
        # Returns: {
        #     "bands/blue": "abfs://container/sentinel2.zarr/20250103/bands/blue",
        #     "bands/green": "...",
        #     "merged": "abfs://container/sentinel2.zarr/20250103/merged",
        # }
        ```
    """
    if not tif_files:
        raise ValueError("No TIF files provided")

    # Generate band names from filenames if not provided
    if bandnames is None:
        bandnames = [os.path.splitext(os.path.basename(f))[0] for f in tif_files]

    if len(bandnames) != len(tif_files):
        bandnames = [f"band_{i}" for i in range(len(tif_files))]

    # Ensure the store exists
    open_or_create_zarr_store(store_path, io_client, mode="a")

    result_paths = {}

    # Write individual bands
    if write_individual_bands:
        for tif_path, band_name in zip(tif_files, bandnames):
            try:
                path = write_band_to_zarr_group(
                    tif_path=tif_path,
                    store_path=store_path,
                    group_path=f"{scene_id}/bands",
                    band_name=band_name,
                    io_client=io_client,
                    chunks=(chunks[1], chunks[2]),
                    **scene_attrs,
                )
                result_paths[f"bands/{band_name}"] = path
            except Exception as e:
                logger.warning(f"Failed to write band {band_name}: {e}")

    # Write merged array
    if write_merged:
        try:
            path = write_merged_to_zarr_group(
                tif_files=tif_files,
                store_path=store_path,
                group_path=f"{scene_id}/merged",
                io_client=io_client,
                bandnames=bandnames,
                chunks=chunks,
                **scene_attrs,
            )
            result_paths["merged"] = path
        except Exception as e:
            logger.warning(f"Failed to write merged data: {e}")
            raise

    logger.info(f"Ingested scene {scene_id} with {len(result_paths)} groups")
    return result_paths


def merge_downloaded_assets_to_zarr(
    tif_files: List[str],
    output_path: str,
    io_client: Optional[GeoanalyticsIOClient],
    bandnames: List[str],
    descriptions: str,
    chunks: Dict[str, int],
    remove_temp: bool = False,
    cloud_percentage: Optional[float] = None,
    clip_bbox: Optional[Tuple[float, float, float, float]] = None,
) -> None:
    """
    Stack multiple downloaded single-band TIF files and write as a Zarr dataset.

    Uses xarray and rioxarray for efficient loading and writing. When parallel=True,
    uses Dask for parallel I/O operations.

    Args:
        tif_files: List of local paths to single-band TIF files to stack.
        output_path: Target path for Zarr (can be local, abfs://, s3://, etc.).
        io_client: GeoanalyticsIOClient instance for writing to cloud storage.
        bandnames: Optional list of band names for the output.
        descriptions: Optional description string to embed.
        dst_crs: Target CRS. If None, uses CRS from input files.
        chunks: Chunk sizes as (bands, height, width) tuple.
        remove_temp: If True, removes source TIF files after successful write.
        parallel: If True, uses Dask for parallel loading/writing.
        clip_bbox: Optional bounding box [minx, miny, maxx, maxy] to clip output to.
        **extra_attrs: Additional attributes to store in the Zarr dataset.

    Returns:
        The output path where the Zarr dataset was written.

    Example:
        ```python
        merge_downloaded_assets_to_zarr(
            tif_files=["B02.tif", "B03.tif", "B04.tif"],
            output_path="abfs://container/scene_2025-01-01.zarr",
            io_client=io_client,
            bandnames=["B02", "B03", "B04"],
            descriptions="Sentinel-2 L2A composite",
            parallel=True,
        )
        ```
    """
    if io_client is not None:
        writer_opts = io_client._storage_options(output_path, write=True)
    else:
        writer_opts = {}
        os.makedirs(os.path.dirname(output_path), exist_ok=True)

    if not tif_files:
        raise ValueError("No TIF files provided for stacking")

    # Filter out files that are too small (likely corrupt or empty)
    valid_tifs = [f for f in tif_files if os.path.getsize(f) > 20 * 1024]
    if not valid_tifs:
        raise DownloadDirIncompleteError("No valid TIF files found (all below 20KB)")

    try:
        # Convert chunks tuple to dict format for xarray
        chunk_dict = {"x": chunks[2], "y": chunks[1]} if parallel else None

        # Stack bands into xarray Dataset
        ds = stack_bands_to_xarray(
            valid_tifs,
            bandnames=bandnames,
            chunks=chunk_dict,
            clip_bbox=clip_bbox,
        )

        # Add clip_bbox info to attributes if provided
        if clip_bbox is not None:
            extra_attrs["clip_bbox"] = str(clip_bbox)
            extra_attrs["clipped_to_aoi"] = "true"

        # Add descriptions if provided
        if descriptions:
            extra_attrs["descriptions"] = descriptions

        # Reproject if target CRS specified and different from source
        if dst_crs is not None and "crs" in ds.attrs:
            source_crs = ds.attrs["crs"]
            if str(dst_crs) != str(source_crs):
                ds["data"] = ds["data"].rio.reproject(dst_crs)
                ds.attrs["crs"] = str(dst_crs)

        # Convert chunks to xarray format for writing
        write_chunks = {
            "band": chunks[0],
            "y": chunks[1],
            "x": chunks[2],
        }

        # Write to Zarr
        result_path = write_xarray_to_zarr(
            ds,
            output_path,
            io_client,
            chunks=write_chunks,
            **extra_attrs,
        )

        logger.info(f"Successfully wrote stacked Zarr dataset to {result_path}")

        # Optionally remove source files
        if remove_temp:
            for tif in tif_files:
                if os.path.exists(tif):
                    os.unlink(tif)

        return result_path

    except Exception as e:
        logger.error(f"Failed to stack and write Zarr: {e}")
        raise


# Legacy class for backwards compatibility - consider using xarray-based functions instead
class ZarrDatasetWriter:
    """
    A class for writing raster data directly to Zarr format.

    Note: For most use cases, prefer using `stack_bands_to_xarray` and
    `write_xarray_to_zarr` which provide better integration with the
    xarray ecosystem and support parallel writes via Dask.

    Example usage:
        ```python
        writer = ZarrDatasetWriter(
            output_path="abfs://container/path/to/output.zarr",
            io_client=io_client,
        )
        writer.open(
            shape=(num_bands, height, width),
            chunks=(1, 512, 512),
            dtype="float32",
            crs="EPSG:32610",
            transform=affine_transform,
            bandnames=["B02", "B03", "B04"],
        )
        for i, band_data in enumerate(downloaded_bands):
            writer.write_band(i, band_data)
        writer.close()
        ```
    """

    def __init__(
        self,
        output_path: str,
        io_client,
        consolidated: bool = True,
    ):
        self.output_path = output_path
        self.io_client = io_client
        self.consolidated = consolidated
        self.zarr_group = None
        self.store = None
        self._data_array = None
        self._metadata: Dict[str, Any] = {}

    def open(
        self,
        shape: Tuple[int, int, int],
        chunks: Tuple[int, int, int] = (1, 512, 512),
        dtype: str = "float32",
        crs: Optional[str] = None,
        transform: Optional[Any] = None,
        bandnames: Optional[List[str]] = None,
        nodata: Optional[float] = None,
        attrs: Optional[Dict[str, Any]] = None,
    ) -> "ZarrDatasetWriter":
        """Open the Zarr dataset for writing."""
        import zarr

        storage_opts = self.io_client._storage_options(self.output_path, write=True)
        fs, path = fsspec.core.url_to_fs(self.output_path, **storage_opts)
        self.store = fs.get_mapper(path, create=True)
        self.zarr_group = zarr.open_group(self.store, mode="w")

        self._data_array = self.zarr_group.create_dataset(
            "data",
            shape=shape,
            chunks=chunks,
            dtype=dtype,
            compressor=zarr.Blosc(
                cname="zstd", clevel=3, shuffle=zarr.Blosc.BITSHUFFLE
            ),
        )

        self._metadata = {
            "crs": str(crs) if crs else None,
            "transform": list(transform)[:6] if transform else None,
            "bandnames": bandnames,
            "nodata": nodata,
        }

        self.zarr_group.attrs["crs"] = self._metadata["crs"]
        self.zarr_group.attrs["transform"] = self._metadata["transform"]
        self.zarr_group.attrs["nodata"] = nodata
        if bandnames:
            self.zarr_group.attrs["bandnames"] = bandnames

        if attrs:
            for key, value in attrs.items():
                self.zarr_group.attrs[key] = value

        if transform and shape:
            num_bands, height, width = shape
            x_coords = np.array(
                [transform[2] + (i + 0.5) * transform[0] for i in range(width)]
            )
            y_coords = np.array(
                [transform[5] + (i + 0.5) * transform[4] for i in range(height)]
            )
            self.zarr_group.create_dataset("x", data=x_coords, dtype="float64")
            self.zarr_group.create_dataset("y", data=y_coords, dtype="float64")

        return self

    def write_band(self, band_index: int, data: np.ndarray) -> None:
        """Write a single band to the dataset."""
        if self._data_array is None:
            raise RuntimeError("Dataset not opened. Call open() first.")
        self._data_array[band_index, :, :] = data

    def write_all_bands(self, data: np.ndarray) -> None:
        """Write all bands at once."""
        if self._data_array is None:
            raise RuntimeError("Dataset not opened. Call open() first.")
        self._data_array[:, :, :] = data

    def close(self) -> str:
        """Finalize and close the Zarr dataset."""
        import zarr

        if self.consolidated and self.store is not None:
            zarr.consolidate_metadata(self.store)
        self.zarr_group = None
        self._data_array = None
        self.store = None
        return self.output_path

    def __enter__(self) -> "ZarrDatasetWriter":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if self.zarr_group is not None:
            self.close()


def write_raster_to_zarr(
    data: np.ndarray,
    output_path: str,
    io_client,
    crs: Optional[str] = None,
    transform: Optional[Any] = None,
    bandnames: Optional[List[str]] = None,
    nodata: Optional[float] = None,
    chunks: Tuple[int, int, int] = (1, 512, 512),
    attrs: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Write a raster array to Zarr format using xarray.

    This is a convenience function for writing a complete raster at once.

    Args:
        data: 3D numpy array of shape (bands, height, width).
        output_path: Destination path (local, abfs://, s3://, etc.).
        io_client: GeoanalyticsIOClient for storage operations.
        crs: Coordinate reference system string.
        transform: Affine transform for georeferencing.
        bandnames: List of band names.
        nodata: NoData value.
        chunks: Chunk sizes as (bands, height, width).
        attrs: Additional attributes to store.

    Returns:
        The output path.
    """
    import xarray as xr

    if data.ndim != 3:
        raise ValueError(f"Expected 3D array (bands, height, width), got {data.ndim}D")

    num_bands, height, width = data.shape

    # Generate coordinates
    if bandnames is None:
        bandnames = [f"band_{i}" for i in range(num_bands)]

    if transform is not None:
        x_coords = np.array(
            [transform[2] + (i + 0.5) * transform[0] for i in range(width)]
        )
        y_coords = np.array(
            [transform[5] + (i + 0.5) * transform[4] for i in range(height)]
        )
    else:
        x_coords = np.arange(width)
        y_coords = np.arange(height)

    # Create xarray DataArray
    da = xr.DataArray(
        data,
        dims=["band", "y", "x"],
        coords={
            "band": bandnames,
            "y": y_coords,
            "x": x_coords,
        },
    )

    # Create Dataset
    ds = da.to_dataset(name="data")

    # Add attributes
    if crs:
        ds.attrs["crs"] = crs
    if transform:
        ds.attrs["transform"] = list(transform)[:6]
    if nodata is not None:
        ds.attrs["nodata"] = nodata
    ds.attrs["bandnames"] = bandnames
    if attrs:
        ds.attrs.update(attrs)

    # Convert chunks to dict format
    chunk_dict = {
        "band": chunks[0],
        "y": chunks[1],
        "x": chunks[2],
    }

    return write_xarray_to_zarr(ds, output_path, io_client, chunks=chunk_dict)