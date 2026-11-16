"""One frozen light measurement, many reversible review displays.

A plate records measured scene light independently of the display transform.
Light is stored as three 16-bit words per pixel (11-bit mantissa, 5-bit biased
exponent); this encoding has no saturation point for values the service can
produce, so distinct bright samples stay distinct. Every displayed byte is a
pure function of that one measurement, which is why changing exposure or curve
cannot be mistaken for changing geometry or illumination.

The light channel is lossy (6 bytes per pixel instead of 12 for f32 or 24 for
f64 per pixel). The bounded loss is measured at plate creation and declared in
the plate payload. A version-1 plate has only encoded display bytes and must
not claim to carry this measurement.
"""
import math
from array import array

from .errors import require
from .pigments import linear_to_srgb
from .values import choice, scalar

LIGHT_FORMAT = "facet-light-hdr6"
LIGHT_BYTES_PER_PIXEL = 6
BRIGHT_FORMAT = "facet-bright-point-srgb8"
PLATE_FORMAT = "facet-plate-2"
SAMPLE_MAX = 65504.0
EXPONENT_BIAS = 15
MANTISSA_BITS = 11
MANTISSA_MAX = (1 << MANTISSA_BITS) - 1
FUNCTIONS = ("clip", "reinhard")
WINDOWS = ("nominal", "ceiling")
DEFAULT_SETTINGS = {"function": "clip", "exposure": 1.0, "black": 0.0, "window": "nominal"}


def _round(value):
    # Deterministic nearest integer, independent of Python's banker's rounding.
    return int(math.floor(value + 0.5))


def encode_scalar(value):
    """Encode one nonnegative light sample as a 16-bit word."""
    require(isinstance(value, (int, float)) and not isinstance(value, bool),
            "light_value", "Light sample must be numeric")
    value = float(value)
    require(math.isfinite(value) and value >= 0, "light_value", "Light sample must be finite and nonnegative")
    value = min(value, SAMPLE_MAX)
    if value < 2.0 ** -14:
        # Subnormal range: exact steps of 2**-24.
        mantissa = _round(value * 2.0 ** 24)
        return min(mantissa, MANTISSA_MAX)
    exponent = math.floor(math.log2(value))
    mantissa = _round(value / 2.0 ** (exponent - 10))
    if mantissa == 1 << MANTISSA_BITS:
        mantissa, exponent = 1 << (MANTISSA_BITS - 1), exponent + 1
    biased = exponent + EXPONENT_BIAS
    if biased > 30:
        return MANTISSA_MAX | (30 << MANTISSA_BITS)
    return mantissa | (biased << MANTISSA_BITS)


def decode_word(word):
    """Decode a light word; total and deterministic for every valid pattern."""
    require(type(word) is int and 0 <= word <= 0xFFFF, "light_value", "Light word is invalid")
    mantissa, biased = word & MANTISSA_MAX, word >> MANTISSA_BITS
    require(biased <= 30, "light_value", "Reserved light exponent")
    if biased == 0:
        return mantissa * 2.0 ** -24
    return mantissa * 2.0 ** (biased - 25)


def word_stream(raw):
    require(isinstance(raw, (bytes, bytearray, memoryview)) and len(raw) % 2 == 0,
            "light_value", "Light channel length is invalid")
    for offset in range(0, len(raw), 2):
        yield raw[offset] | (raw[offset + 1] << 8)


def decode_light(raw):
    """Unpack a light channel into an array of doubles (for offline readers)."""
    result = array("d")
    for word in word_stream(raw):
        result.append(decode_word(word))
    return result


def tone(value, function, exposure, black, headroom):
    """Map one light sample into display units without clipping the light first."""
    value = value * exposure - black
    if value <= 0:
        return 0.0
    if function == "clip":
        return value / headroom
    return value / (value + headroom)


def _code(value):
    return round(linear_to_srgb(min(1.0, max(0.0, value))) * 255)


def validate_settings(data, window_ceiling, default=None):
    """Validate a display request and resolve its window into explicit headroom."""
    data = {} if data is None else data
    require(isinstance(data, dict), "display", "Display settings must be an object")
    base = DEFAULT_SETTINGS if default is None else default
    function = choice(data.get("function", base["function"]), "function", set(FUNCTIONS))
    window = choice(data.get("window", base["window"]), "window", set(WINDOWS))
    headroom = 1.0 if window == "nominal" else window_ceiling
    exposure = scalar(data.get("exposure", base["exposure"]), "exposure", 1e-4, 1e6)
    black = scalar(data.get("black", base["black"]), "black", 0.0, headroom)
    require(black < headroom, "black", "Black level must stay below the display window")
    return {"function": function, "exposure": exposure, "black": black,
            "window": window, "headroom": headroom}


def measure(rendered, width, height, samples, plan_width):
    """Average high-sample light per output pixel, and keep one point sample.

    Both parts come from the same assembled render used for color, so a later
    display switch never re-runs geometry. ``bright`` is the point sample at
    each output pixel's sample-grid origin, displayed at the fixed ceiling
    window: it keeps relative brightness instead of averaging highlights away.
    """
    colors = rendered["colors"]
    count = samples * samples
    means = array("d", [0.0]) * (width * height * 3)
    points = array("f", [0.0]) * (width * height * 3)
    light = bytearray(width * height * LIGHT_BYTES_PER_PIXEL)
    maximum = 0.0
    above_one = 0
    for y in range(height):
        for x in range(width):
            pixel = y * width + x
            total = [0.0, 0.0, 0.0]
            for sy in range(samples):
                row = (y * samples + sy) * plan_width + x * samples
                for sx in range(samples):
                    source = (row + sx) * 3
                    total[0] += colors[source]
                    total[1] += colors[source + 1]
                    total[2] += colors[source + 2]
            origin = ((y * samples) * plan_width + x * samples) * 3
            hot = False
            for channel in range(3):
                points[pixel * 3 + channel] = colors[origin + channel]
                value = total[channel] / count
                means[pixel * 3 + channel] = value
                maximum = max(maximum, value)
                hot = hot or value > 1.0
                word = encode_scalar(value)
                offset = pixel * 6 + channel * 2
                light[offset] = word & 0xFF
                light[offset + 1] = word >> 8
            above_one += hot
    ceiling = 16.0
    while ceiling < maximum and ceiling < SAMPLE_MAX:
        ceiling *= 2.0
    bright = bytearray(len(points))
    for index, value in enumerate(points):
        bright[index] = _code(tone(value, "clip", 1.0, 0.0, ceiling))
    return {"means": means, "light": bytes(light), "bright": bytes(bright),
            "maximum_linear": maximum, "above_one_pixels": above_one,
            "ceiling": min(ceiling, SAMPLE_MAX), "pixels": width * height}


def finalize(measurement, function="clip", exposure=1.0, black=0.0):
    """Derive the nominal display and declare what storing the light cost.

    The display is computed from the *stored* light words, so a reader can
    reproduce it exactly; the declared deltas measure that storage against the
    exact sample mean before encoding.
    """
    pixels = measurement["pixels"]
    means = measurement["means"]
    light = measurement["light"]
    ceiling = measurement["ceiling"]
    display = bytearray(pixels * 3)
    nominal_max_delta = nominal_changed = 0
    ceiling_max_delta = ceiling_changed = 0
    relative = 0.0
    table = [None] * 0x10000
    ceiling_table = [None] * 0x10000
    for index, word in enumerate(word_stream(light)):
        decoded = decode_word(word)
        exact = means[index]
        if exact > 0:
            relative = max(relative, abs(decoded - exact) / exact)
        code = table[word]
        if code is None:
            code = table[word] = _code(tone(decoded, function, exposure, black, 1.0))
        display[index] = code
        delta = abs(_code(tone(exact, function, exposure, black, 1.0)) - code)
        nominal_max_delta = max(nominal_max_delta, delta)
        nominal_changed += delta > 0
        wide = ceiling_table[word]
        if wide is None:
            wide = ceiling_table[word] = _code(tone(decoded, "clip", 1.0, 0.0, ceiling))
        delta = abs(_code(tone(exact, "clip", 1.0, 0.0, ceiling)) - wide)
        ceiling_max_delta = max(ceiling_max_delta, delta)
        ceiling_changed += delta > 0
    # Quantify exactly what an 8-bit display costs in the region a reviewer is
    # asked to judge: channels whose stored light exceeds the nominal window.
    light_values = [decode_word(word) for word in word_stream(light)]
    hot = [value for value in light_values if value > 1.0]
    hot_codes = {table[word] for word in word_stream(light) if decode_word(word) > 1.0}
    hot_wide = {ceiling_table[word] for word in word_stream(light) if decode_word(word) > 1.0}
    metadata = {
        "model": "facet-display-1",
        "light_format": LIGHT_FORMAT,
        "encoding": "light-hdr6",
        "bytes_per_pixel": LIGHT_BYTES_PER_PIXEL,
        "layout": "three little-endian uint16 words; low 11 bits are mantissa, "
                  "high 5 bits are biased exponent",
        "bright_format": BRIGHT_FORMAT,
        "bright_sampling": "point sample at each output pixel's sample-grid origin, "
                           "displayed at the ceiling window",
        "functions": list(FUNCTIONS),
        "windows": {"nominal": 1.0, "ceiling": ceiling},
        "default": {"function": function, "exposure": exposure, "black": black, "window": "nominal"},
        "lighting": {"maximum_linear": measurement["maximum_linear"],
                     "above_one_pixels": measurement["above_one_pixels"],
                     "suggested_ceiling": ceiling},
        "highlight_collapse": {
            "channels_above_nominal": len(hot),
            "distinct_light_levels": len({round(value, 6) for value in hot}),
            "nominal_distinct_codes": len(hot_codes),
            "ceiling_distinct_codes": len(hot_wide),
            "nominal_clipped_channels": sum(1 for value in hot
                                            if _code(tone(value, function, exposure, black, 1.0)) == 255),
            "note": "Channels whose stored light exceeds the nominal window. Counts show how many "
                    "distinct light levels each display can actually separate there.",
        },
        "quantization": {
            "max_relative_sample_error": relative,
            "nominal": {"window": 1.0, "max_srgb_code_delta": nominal_max_delta,
                        "channels_differing": nominal_changed},
            "ceiling": {"window": ceiling, "max_srgb_code_delta": ceiling_max_delta,
                        "channels_differing": ceiling_changed},
            "note": "Stored light is compared with the exact sample mean at exposure 1, black 0. "
                    "Display bytes are derived from light, so only storage loss is reported here.",
        },
        "limits": {"display_is_8bit_srgb": True, "display_is_lossy": True,
                   "light_is_lossy": True, "derived_from_one_measurement": True},
    }
    return bytes(display), metadata


def apply_light(light, settings):
    """Render a display from stored light bytes without touching geometry.

    Every word decodes and tones the same way under one display choice, so the
    mapping is memoized per distinct word present in this channel (typically a
    few hundred) rather than all 65536 patterns or per pixel.
    """
    headroom = settings["headroom"]
    table = [None] * 0x10000
    output = bytearray(len(light) // 2)
    offset = 0
    for word in word_stream(light):
        code = table[word]
        if code is None:
            code = table[word] = _code(tone(decode_word(word), settings["function"],
                                            settings["exposure"], settings["black"], headroom))
        output[offset] = code
        offset += 1
    return bytes(output)
