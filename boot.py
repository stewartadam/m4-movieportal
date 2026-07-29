"""Enable a dedicated binary USB serial port for movie frames."""

import usb_cdc


usb_cdc.enable(console=True, data=True)
