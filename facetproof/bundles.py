import io
import zipfile
from .values import canonical


class Bundles:
    def __init__(self, db, plates, blobs):
        self.db, self.plates, self.blobs = db, plates, blobs

    def export(self, plate_id):
        with self.db.unit_of_work() as conn:
            plate = self.plates._get(conn, plate_id)
            self.plates.verify_payload(plate["payload"])
            seal = self.db.get(conn, plate["payload"]["seal_id"], "seal")
            payload = plate["payload"]
            # Legacy plates are exported as they are: the package must not be
            # padded with light that was never recorded.
            light = payload["passes"].get("light.hdr6")
            members = {"plate.json": canonical(plate).encode(),
                       "seal.json": canonical(seal).encode()}
            for name, descriptor in payload["passes"].items():
                members["passes/" + name] = self.blobs.read(descriptor["sha256"])
            if light is not None:
                metadata = {"format": "facet-light-1",
                            "light": light,
                            "display": payload["display"],
                            "color": payload["passes"]["color.rgb"],
                            "bright": payload["passes"]["bright.rgb"],
                            "note": "Display bytes are derived from light; light is the measurement."}
                members["light.json"] = canonical(metadata).encode()
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for name, raw in sorted(members.items()):
                    entry = zipfile.ZipInfo(name, date_time=(2000, 1, 1, 0, 0, 0))
                    entry.compress_type = zipfile.ZIP_DEFLATED
                    entry.external_attr = 0o644 << 16
                    archive.writestr(entry, raw)
            self.db.event(conn, plate_id, "bundle_exported", {"members": sorted(members)})
            return buffer.getvalue()
