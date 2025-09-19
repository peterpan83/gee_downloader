
# from .sentinel_2 import get_s2_cloudpercentage as get_s2cld
from .landsat import get_l8_cloudpercentage as get_lc08cld
from .landsat import get_l8_snowicepercentage as get_lc08snowice
from .landsat import get_descriptions_l8_l1toa as get_descriptions_lc08_l1toa
from .landsat import get_descriptions_l8_l2rgb as get_descriptions_lc08_l2rgb

from .landsat import get_l9_cloudpercentage as get_lc09cld
from .landsat import get_l9_snowicepercentage as get_lc09snowice
from .landsat import get_descriptions_l9_l1toa as get_descriptions_lc09_l1toa
from .landsat import get_descriptions_l9_l2rgb as get_descriptions_lc09_l2rgb

from .s2 import get_s2_cloudpercentage as get_s2cld
from .s2 import get_s2_snowicepercentage as get_s2snowice
from .s2 import get_s2_acquistion as get_s2acq
from .s2 import get_descriptions_l1toa as get_descriptions_s2_l1toa
from .s2 import get_descriptions_l2rgb as get_descriptions_s2_l2rgb
from .s2 import get_descriptions_l2surf as get_descriptions_s2_l2surf
from .s2 import get_obsgeo as get_obsgeo_s2_l1toa


from .s1 import get_descriptions_l1c as get_descriptions_s1_l1c