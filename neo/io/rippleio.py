from neo.io.basefromrawio import BaseFromRaw
from neo.rawio.ripplerawio import RippleRawIO


class RippleIO(RippleRawIO, BaseFromRaw):
    """
    Supplementary class for reading Ripple data using only a single nfx file.
    """
    name = 'Ripple IO for single nfx'
    description = (
        "This IO reads an nfx file from a Ripple Grapevine recording system."
    )
    _prefered_signal_group_mode = 'group-by-same-units'

    def __init__(self, filename, **kargs):
        RippleRawIO.__init__(self, filename=filename, **kargs)
        BaseFromRaw.__init__(self, filename)
