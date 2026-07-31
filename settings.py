"""User-adjustable MoviePortal configuration."""


# Physical panel orientation. Supported values are 0 and 180 degrees.
DISPLAY_ROTATION = 180

# HUB75 PWM bitplanes. The RGB565 framebuffer carries six green bits, but a
# five-bit matrix discards the green least-significant bit.
DISPLAY_BIT_DEPTH = 5
