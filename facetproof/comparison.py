"""Compare geometry and exposure without confusing either with a display choice.

Three separate questions are reported, never merged:

* Did the scene change?  Object identity, silhouette and depth are compared in
  the light-independent channels. Two plates of one frozen scene match here
  even when their displays differ.
* Did the illumination change?  Stored light is compared in linear units with a
  declared window, so two exposures of one scene are placed on one scale.
* Do these two displayed images differ?  Encoded bytes are compared only after
  the caller states the display settings both sides are shown with. A display
  difference is reported as a display difference, never as scene change.
"""
import math
import struct

from .display import decode_word, tone, word_stream
from .errors import require
from .pigments import linear_to_srgb
from .values import identity, scalar


class Comparisons:
    def __init__(self, db, plates, blobs):
        self.db, self.plates, self.blobs = db, plates, blobs

    @staticmethod
    def _light(payload):
        descriptor = payload["passes"].get("light.hdr6")
        return descriptor["sha256"] if descriptor is not None else None

    def create(self, data):
        tolerance = scalar(data.get("tolerance", 2), "tolerance", 0, 255)
        with self.db.unit_of_work() as conn:
            left = self.plates._get(conn, data.get("left"))
            right = self.plates._get(conn, data.get("right"))
            a, b = left["payload"], right["payload"]
            require((a["width"], a["height"], a["lens_hash"]) == (b["width"], b["height"], b["lens_hash"]),
                    "alignment", "Plate extent and lens must match", 409)
            read = lambda p, n: self.blobs.read(p["passes"][n]["sha256"])
            da, db = read(a, "depth.f32"), read(b, "depth.f32")
            ia, ib = read(a, "object.u32"), read(b, "object.u32")
            la, lb = self._light(a), self._light(b)
            lights = (self.blobs.read(la), self.blobs.read(lb)) if la and lb else None
            scene = self._scene(da, db, ia, ib)
            scene["light_available"] = bool(lights)
            if lights:
                ceiling = self._ceiling(a, b)
                scene["exposure"] = self._exposure(lights[0], lights[1], ceiling)
                scene["double_exposure"] = self._double_exposure(lights[0], lights[1])
            else:
                scene["exposure"] = None
                scene["double_exposure"] = None
                scene["light_note"] = "One or both plates predate stored light; only display bytes exist."
            # Scene facts that do not require light must hold before an image
            # difference can be attributed to anything, but a genuine scene
            # change is reported rather than hidden by an error.
            display = self._display(a, b, data.get("display"), la, lb, tolerance)
            result = {"left": left["id"], "right": right["id"], "tolerance": tolerance,
                      "pixels": a["width"]*a["height"],
                      "scene": scene, "display": display,
                      "pass_hashes": {"left": a["visual_hash"], "right": b["visual_hash"]},
                      "verdicts": {
                          "scene_changed": scene["object_changed"] > 0 or scene["silhouette_changed"] > 0
                                           or scene["depth_changed"] > 0,
                          "illumination_changed": bool(scene["exposure"]) and
                                                  scene["exposure"]["pixels_changed"] > 0,
                          "display_bytes_differ": display["pixels_changed"] > 0,
                          "comparison_scale": "scene facts are light-independent; exposure facts share "
                                              "one declared window; display facts share declared settings",
                      }}
            return self.db.insert(conn, identity("comparison"), "comparison", "computed", result)

    @staticmethod
    def _ceiling(a, b):
        values = []
        for payload in (a, b):
            display = payload.get("display") or {}
            ceiling = (display.get("windows") or {}).get("ceiling")
            if isinstance(ceiling, (int, float)) and ceiling > 0:
                values.append(float(ceiling))
        require(bool(values), "display_metadata", "Plates have no declared display ceiling", 409)
        return max(values)

    @staticmethod
    def _scene(da, db, ia, ib):
        silhouette = depth_changed = objects = 0
        maximum_depth = 0.
        for offset in range(0, len(da), 4):
            va, vb = struct.unpack_from("<f", da, offset)[0], struct.unpack_from("<f", db, offset)[0]
            silhouette += math.isfinite(va) != math.isfinite(vb)
            if math.isfinite(va) and math.isfinite(vb):
                delta = abs(va-vb)
                maximum_depth = max(maximum_depth, delta)
                depth_changed += delta > 1e-5
        for offset in range(0, len(ia), 4):
            objects += struct.unpack_from("<I", ia, offset)[0] != struct.unpack_from("<I", ib, offset)[0]
        return {"object_changed": objects, "silhouette_changed": silhouette,
                "depth_changed": depth_changed, "maximum_depth_delta": maximum_depth,
                "source": "object.u32, depth.f32",
                "note": "These channels do not depend on the display transform."}

    @staticmethod
    def _exposure(la, lb, ceiling):
        """Compare stored light in linear units under one declared window."""
        changed = channels = pixels = 0
        maximum = total = 0
        pixel_changed = False
        for index, (wa, wb) in enumerate(zip(word_stream(la), word_stream(lb)), 1):
            a, b = decode_word(wa), decode_word(wb)
            delta = abs(a-b)
            maximum = max(maximum, delta)
            total += delta
            hot = delta / ceiling > 1e-4
            channels += hot
            pixel_changed = pixel_changed or hot
            if index % 3 == 0:
                pixels += pixel_changed
                pixel_changed = False
        return {"window": ceiling, "pixels_changed": pixels, "channels_changed": channels,
                "mean_channel_delta_linear": total/index, "maximum_delta_linear": maximum,
                "scale": "linear light; one window for both plates",
                "note": "A change here is measured illumination, not a display setting. "
                        "A display difference cannot be compared across unstated exposures."}

    @staticmethod
    def _double_exposure(la, lb):
        """Fit one gain between the two light records and report the residual.

        A genuine exposure change multiplies every sample by one factor; then
        the residual is near zero and the gain is the relative exposure. A
        residual that survives the fit means light changed in shape (for
        example an emitter was added), which is not a pure exposure change.
        Working on linear samples means clipped display bytes cannot hide it.
        """
        a_values = [decode_word(w) for w in word_stream(la)]
        b_values = [decode_word(w) for w in word_stream(lb)]
        total_a, total_b = sum(a_values), sum(b_values)
        if not a_values or total_a <= 0:
            return {"status": "no_light", "note": "Left plate has no measurable light."}
        gain = total_b / total_a
        absolute = relative = 0.
        ratios = []
        for a, b in zip(a_values, b_values):
            error = abs(b - a*gain)
            absolute = max(absolute, error)
            if a > 0:
                relative = max(relative, error / (a*gain))
                ratios.append(b / a)
        ratios.sort()
        median = ratios[len(ratios)//2] if ratios else None
        return {"status": "gain_fit", "gain": gain,
                "same_as_left_times_gain": relative <= 1e-3,
                "median_channel_ratio": median,
                "ratio_range": [ratios[0], ratios[-1]] if ratios else None,
                "maximum_absolute_residual": absolute,
                "maximum_relative_residual": relative,
                "note": "gain is the fitted relative exposure in linear units. A small residual "
                        "means the images differ only by that exposure; a large one means the "
                        "illumination changed in shape, not just in exposure."}

    def _display(self, a, b, settings, la, lb, tolerance):
        """Compare displayed bytes only under one explicitly shared display."""
        if settings is None:
            ca, cb = (self.blobs.read(p["passes"]["color.rgb"]["sha256"]) for p in (a, b))
            deltas = [abs(x-y) for x, y in zip(ca, cb)]
            changed = sum(max(deltas[i:i+3]) > tolerance for i in range(0, len(deltas), 3))
            return {"settings": "each plate defaults", "exposure_matched": False,
                    "pixels_changed": changed,
                    "mean_channel_delta": sum(deltas)/len(deltas),
                    "scale": "unmatched displays; a difference here is not scene change",
                    "warning": "Display defaults differ between exposures. State one shared display "
                               "to compare images, and read scene facts above."}
        require(la and lb, "display_light_missing",
                "Sharing a display needs stored light on both plates", 409)
        common = {"function": settings.get("function", "clip"),
                  "exposure": settings.get("exposure", 1.0),
                  "black": settings.get("black", 0.0),
                  "window": settings.get("window", "nominal")}
        require(common["function"] in ("clip", "reinhard"), "display", "Unknown display function")
        require(common["window"] in ("nominal", "ceiling"), "display", "Unknown display window")
        ceiling = self._ceiling(a, b)
        headroom = ceiling if common["window"] == "ceiling" else 1.0
        la_raw, lb_raw = self.blobs.read(la), self.blobs.read(lb)
        channels = pixels = 0
        total = maximum = 0
        pixel_changed = False
        for index, (wa, wb) in enumerate(zip(word_stream(la_raw), word_stream(lb_raw)), 1):
            x = _display_code(decode_word(wa), common, headroom)
            y = _display_code(decode_word(wb), common, headroom)
            delta = abs(x-y)
            total += delta
            maximum = max(maximum, delta)
            hot = delta > tolerance
            channels += hot
            pixel_changed = pixel_changed or hot
            if index % 3 == 0:
                pixels += pixel_changed
                pixel_changed = False
        return {"settings": common, "exposure_matched": True, "window": headroom,
                "pixels_changed": pixels, "channels_changed": channels,
                "mean_channel_delta": total/index, "maximum_channel_delta": maximum,
                "scale": "both images shown under the same declared display",
                "note": "Differences here are what a reviewer would see at this setting; they are "
                        "not scene measurements."}


def _display_code(value, settings, headroom):
    """The same sRGB code a bound display would store for one light sample."""
    unit = tone(value, settings["function"], settings["exposure"], settings["black"], headroom)
    return round(linear_to_srgb(min(1.0, max(0.0, unit))) * 255)
