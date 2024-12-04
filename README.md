Download images to local storage from Google Earth Engine in format of geotiff for a given AOI (geojson) and date.
Warns: it can be very slow if AOI is too large because the maximum number of pixels and dimensions of extent that can be downloaded from
GEE are limited.

Python requirements:  
`rasterio, earthengine-api, google-cloud-bigquery, db_dtypes`  

Install the `gcloud CLI` on your machine (https://cloud.google.com/sdk/docs/install)  

In the browser, create a new project on google cloud, and enable big query + Google Earth Engine in your gcloud project 
(also register your project to use G.E.E.: https://code.earthengine.google.com/register)

run these commands to authenticate gcloud in the cli:  
(inside the gee-downloader directory)  
(you might need to set the project id as created in the browser)  
`gcloud auth login`  
`gcloud auth application-default login`


usage:  
`python main.py -c download.ini`

This repository was migrated from arctus_2023 on Dec. 2024
