import pandas as pd
import re
import subprocess

S2_GCS_BUCKET = 'gs://gcp-public-data-sentinel-2'
## the tile token is last in an 'essential' product id, so do not require a trailing _
_S2_TILE_RE = re.compile(r'_T(\d{2})([A-Z])([A-Z]{2})(?:_|$)')


def derive_url_l1toa(product_id: str):
    """
    Locate the .SAFE directory straight from the product id: the tile token T17UPT maps to
    tiles/17/U/PT. The trailing wildcard covers the generation timestamp, which is absent
    from an essential product id (see get_l1toa_prodid_essential).

    Returns None when the id carries no tile token, or nothing matches on GCS.
    """
    m = _S2_TILE_RE.search(product_id)
    if m is None:
        return None
    zone, band, square = m.groups()
    pattern = f'{S2_GCS_BUCKET}/tiles/{zone}/{band}/{square}/{product_id}*.SAFE'
    try:
        out = subprocess.run(['gsutil', 'ls', '-d', pattern],
                             capture_output=True, text=True, check=True).stdout
    except Exception as e:
        print(f'gsutil ls {pattern}: {e}')
        return None
    matches = [_.rstrip('/') for _ in out.split('\n') if _.strip()]
    if not matches:
        return None
    ## several generations of the same acquisition sort by their trailing timestamp
    return sorted(matches)[-1]


def search_url_l1toa(product_id: str):
    # product_id = "S2A_MSIL1C_20230131T154711_N0509_R054_T29SNU_20230131T180745"

    url = derive_url_l1toa(product_id)
    if url is not None:
        return url

    ## Fallback only. This query is metered against the 1TiB monthly free allowance and
    ## `bq query --dry_run` puts it at ~14GB a call - about 80 calls before "Quota
    ## exceeded: free query bytes scanned" - because CONTAINS_SUBSTR cannot prune
    ## anything and reads the whole table. STARTS_WITH matches the same essential-id
    ## prefix and brings it down to ~3.5GB, so use that instead.
    print(f'{product_id}: not at the derived path, asking the BigQuery index')
    from google.cloud import bigquery

    client = bigquery.Client()  # requires BigQuery API enabled + billing project

    sql = f"""
        SELECT base_url, source_url, generation_time
        FROM `bigquery-public-data.cloud_storage_geo_index.sentinel_2_index`
        WHERE STARTS_WITH(product_id, "{product_id}")
        ORDER BY generation_time DESC
        LIMIT 10
    """
    df = client.query(sql).to_dataframe()
    if df.empty:
        print(f"No S2 L1TOA URL results found for product_id: {product_id}")
        return None
    df = df.iloc[0]
    url = df['source_url'] if pd.notna(df['source_url']) else df['base_url']
    if url == "":
        print(f"Empty URL found for product_id: {product_id}")
        return None
    return url


def get_l1toa_prodid_essential(product_id: str):
    '''
    Convert Sentinel-2 L2A product ID to L1C essential product ID.

    for example,  "S2A_MSIL2A_20241005T162141_N0511_R040_T17UPT_20241005T195259" to "S2A_MSIL1C_20241005T162141_N0511_R040_T17UPT"
    '''
    if product_id.find("MSIL2A") >= 0:
        product_id = product_id.replace("L2A","L1C")
        if len(product_id.split('_')) == 7:
            product_id = '_'.join(product_id.split('_')[:6])

    return product_id




