"""Independent reader for the stored-light measurement in a version-two plate.

The producer stores light as three little-endian uint16 words per pixel with
an 11-bit mantissa and a 5-bit biased exponent. This module reimplements the
decode from the documented layout only; it imports nothing from the service.
"""
import math
import struct

from .errors import require

LIGHT_BYTES_PER_PIXEL = 6
MANTISSA_BITS = 11
MANTISSA_MAX = (1 << MANTISSA_BITS) - 1
EXPONENT_BIAS = 15


def decode_word(word):
    require(0 <= word <= 0xFFFF, "light_value", "Light word is out of range")
    mantissa, biased = word & MANTISSA_MAX, word >> MANTISSA_BITS
    require(biased <= 30, "light_value", "Light word uses a reserved exponent")
    if biased == 0:
        return mantissa * 2.0 ** -24
    return mantissa * 2.0 ** (biased - 25)


def tone_samples(samples, function="clip", exposure=1.0, black=0.0, window="nominal",
                 ceiling=None):
    """Recompute a display from decoded light without the producer's renderer.

    This is the offline analogue of the service's display switch: the same
    documented arithmetic, applied to the same stored measurement. It never
    consults geometry, so it cannot be mistaken for a re-render.
    """
    require(function in {"clip", "reinhard"}, "display", "Unsupported display function")
    require(window in {"nominal", "ceiling"}, "display", "Unsupported display window")
    headroom = 1.0 if window == "nominal" else float(ceiling or 16.0)
    require(isinstance(exposure, (int, float)) and not isinstance(exposure, bool) and exposure > 0,
            "display", "Exposure must be positive")
    require(isinstance(black, (int, float)) and not isinstance(black, bool) and 0 <= black < headroom,
            "display", "Black level is invalid")
    output = bytearray(len(samples))
    for index, value in enumerate(samples):
        code = value * exposure - black
        if code <= 0:
            output[index] = 0
            continue
        unit = code / headroom if function == "clip" else code / (code + headroom)
        unit = min(1.0, max(0.0, unit))
        srgb = unit * 12.92 if unit <= 0.0031308 else 1.055 * unit ** (1/2.4) - 0.055
        output[index] = int(srgb * 255 + 0.5)
    return bytes(output)


def decode_light(raw, display, width, height):
    """Validate extent/declaration and return light samples plus summary facts."""
    require(display.get("bytes_per_pixel") == LIGHT_BYTES_PER_PIXEL,
            "light_format", "Light byte width disagrees with the declared encoding")
    require(len(raw) == width * height * LIGHT_BYTES_PER_PIXEL,
            "light_extent", "Light channel extent does not match the plate")
    layout = display.get("layout")
    require(isinstance(layout, str) and "uint16" in layout and "mantissa" in layout,
            "light_format", "Light encoding layout is not described")
    values = struct.iter_unpack("<H", raw)
    samples = []
    above_one = 0
    maximum = 0.0
    pixel = [0.0, 0.0, 0.0]
    channel = 0
    for (word,) in values:
        value = decode_word(word)
        require(math.isfinite(value) and value >= 0, "light_value", "Light sample is not finite nonnegative")
        samples.append(value)
        maximum = max(maximum, value)
        pixel[channel] = value
        channel += 1
        if channel == 3:
            above_one += max(pixel) > 1.0
            channel = 0
    require(channel == 0, "light_extent", "Light channel is not a whole number of pixels")
    return {"samples": samples, "maximum_linear": maximum,
            "above_one_pixels": above_one,
            "channel_count": len(samples),
            "note": "Light is the measurement; displayed color is derived from it."}
