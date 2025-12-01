class NoEEImageFoundError(Exception):
    def __init__(self, ee_source, date):
        self.msg = f"{date}, No {ee_source} image found"
        super().__init__(self.msg)

class EEImageOverlayError(Exception):
    def __init__(self, ee_source, date):
        self.msg = f"{date},likely overlay exception {ee_source}, total pixels == 0"
        super().__init__(self.msg)


class DownloadDirIncompleteError(Exception):
    def __init__(self, download_dir):
        self.msg = f"No tif files found, not a complete download dir: {download_dir} "
        super().__init__(self.msg)
class NoGoodTifsError(Exception):
    def __init__(self, download_dir):
        self.msg = f"No good tif files found, check download dir: {download_dir} "
        super().__init__(self.msg)


class NoEEIntersectionBandsError(Exception):
    def __init__(self):
        self.msg = f"no intersection bands"
        super().__init__(self.msg)


class BigQueryError(Exception):
    def __init__(self, query_str):
        self.msg = f"BigQueryError: {query_str}"
        super().__init__(self.msg)


class OldFormat(Exception):
    def __init__(self, message):
        self.msg = message
        super().__init__(self.msg)

class OldSentinelFormat(OldFormat):
    def __init__(self, message):
        self.msg = message
        self.senor = 'sentinel'
        super().__init__(f'old {self.senor}:{self.msg}')

class GsutilError(Exception):
    def __init__(self, message):
        self.msg = message
        super().__init__(self.msg)








