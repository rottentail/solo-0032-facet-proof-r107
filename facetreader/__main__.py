"""CLI output uses fixed filenames, never ZIP member paths."""
import argparse
import json
from pathlib import Path
import sys
from .archive import load_file
from .errors import InvalidBundle
from .format import evaluate
from .light import decode_light, tone_samples
from .png import encode_rgb


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise InvalidBundle("arguments", message)


def _retone(report, members, window, exposure, function, black):
    """Derive a display from stored light using reader code only."""
    display = report["light"]
    windows = display.get("windows") or {}
    ceiling = windows.get("ceiling") if isinstance(windows, dict) else None
    decoded = decode_light(members["passes/light.hdr6"], display,
                           report["summary"]["width"], report["summary"]["height"])
    return tone_samples(decoded["samples"], function=function, exposure=exposure,
                        black=black, window=window, ceiling=ceiling)


def main(argv=None):
    try:
        parser = Parser(description="Independently inspect an offline Facet Proof plate bundle")
        parser.add_argument("bundle", help="Path to plate-bundle.zip")
        parser.add_argument("--output", type=Path, help="Write summary, provenance, picks and color.png")
        parser.add_argument("--display", choices=("nominal", "ceiling"), default=None,
                            help="Also write a display derived from stored light, offline")
        parser.add_argument("--exposure", type=float, default=1.0,
                            help="Exposure for --display, in linear units")
        parser.add_argument("--function", choices=("clip", "reinhard"), default="clip",
                            help="Tone function for --display")
        parser.add_argument("--black", type=float, default=0.0,
                            help="Black level for --display")
        arguments = parser.parse_args(argv)
        report, members = evaluate(load_file(arguments.bundle))
        if arguments.display and not report["light"].get("format"):
            raise InvalidBundle("display_light_missing",
                                "This plate stores no light measurement; a new display cannot be derived")
        if arguments.output is not None:
            try:
                arguments.output.mkdir(parents=True, exist_ok=True)
                for name, value in (("summary.json", {"summary": report["summary"], "trust": report["trust"]}),
                                    ("provenance.json", report["provenance"]), ("picks.json", report["picks"])):
                    (arguments.output / name).write_text(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                                                  indent=2, allow_nan=False) + "\n", encoding="utf-8")
                (arguments.output / "color.png").write_bytes(members["passes/color.png"])
                if arguments.display:
                    pixels = _retone(report, members, arguments.display, arguments.exposure,
                                     arguments.function, arguments.black)
                    image = encode_rgb(report["summary"]["width"], report["summary"]["height"], pixels)
                    (arguments.output / ("color." + arguments.display + ".png")).write_bytes(image)
                    (arguments.output / ("display." + arguments.display + ".json")).write_text(
                        json.dumps({"derived_from": "passes/light.hdr6",
                                    "window": arguments.display, "exposure": arguments.exposure,
                                    "function": arguments.function, "black": arguments.black,
                                    "note": "Derived offline from stored light; not the committed color pass."},
                                   ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n",
                        encoding="utf-8")
            except OSError as exc:
                raise InvalidBundle("output_write", "Cannot write the output directory") from exc
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, allow_nan=False))
        return 0
    except InvalidBundle as exc:
        print(json.dumps({"valid": False, "error": exc.code, "message": exc.message}, sort_keys=True))
        return 2


if __name__ == "__main__":
    sys.exit(main())
