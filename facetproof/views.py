"""Review displays derived from a plate's stored light measurement.

Conversion reads one content-addressed light channel, computes bytes, and
stores the result by its content hash. No geometry is planned or rasterized
and no SQLite write transaction is open while bytes are converted, so an
interactive display switch cannot stall a parallel exposure's heartbeat or
cancellation. A display is never a second source of truth: every record names
the light channel it was derived from.
"""
from .display import LIGHT_FORMAT, apply_light, validate_settings
from .errors import require
from .png import encode_rgb
from .values import identity, now

SCHEMA = """
CREATE TABLE IF NOT EXISTS facet_displays (
 id TEXT PRIMARY KEY,
 plate_id TEXT NOT NULL REFERENCES plates(id),
 light_sha256 TEXT NOT NULL,
 display_sha256 TEXT NOT NULL,
 display_bytes INTEGER NOT NULL,
 function TEXT NOT NULL,
 exposure REAL NOT NULL,
 black REAL NOT NULL,
 window TEXT NOT NULL,
 headroom REAL NOT NULL,
 created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS facet_displays_plate ON facet_displays(plate_id, created, id);
"""


class Displays:
    def __init__(self, db, plates, blobs):
        self.db, self.plates, self.blobs = db, plates, blobs
        with db.unit_of_work() as conn:
            for statement in SCHEMA.strip().split(";"):
                if statement.strip():
                    conn.execute(statement)
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(facet_displays)")}
            if "display_bytes" not in columns:
                # Rows written before the byte count was recorded cannot
                # supply a trustworthy size to a backup manifest; deriving it
                # from the plate extent keeps them readable and exact.
                conn.execute("ALTER TABLE facet_displays ADD COLUMN display_bytes INTEGER")
                for row in conn.execute("SELECT id,plate_id,display_sha256 FROM facet_displays").fetchall():
                    try:
                        payload = self.plates._get(conn, row["plate_id"], available=False)["payload"]
                        size = payload["width"] * payload["height"] * 3
                    except Exception:
                        size = 0
                    conn.execute("UPDATE facet_displays SET display_bytes=? WHERE id=?", (size, row["id"]))

    def _settings(self, plate, data):
        display = plate["payload"].get("display")
        require(isinstance(display, dict) and display.get("light_format") == LIGHT_FORMAT,
                "display_light_missing",
                "This plate stores only display bytes; its light measurement was never saved", 409)
        ceiling = display.get("windows", {}).get("ceiling")
        require(isinstance(ceiling, (int, float)) and ceiling > 0, "display_metadata",
                "Plate display metadata has no ceiling", 409)
        return validate_settings(data, float(ceiling), display.get("default"))

    def _row(self, row):
        record = {**dict(row)}
        record.pop("headroom", None)
        settings = {key: record.pop(key) for key in ("function", "exposure", "black", "window")}
        return {**record, "settings": settings, "derived_from_light": record["light_sha256"],
                "display_url": "/facet/v1/displays/" + record["id"] + "/png"}

    def _get(self, conn, display_id):
        row = conn.execute("SELECT * FROM facet_displays WHERE id=?", (display_id,)).fetchone()
        require(row is not None, "not_found", "Display not found", 404)
        # A derived view of a quarantined plate is not a deliverable image.
        self.plates._get(conn, row["plate_id"])
        return row

    def get(self, display_id):
        with self.db.unit_of_work() as conn:
            return self._row(self._get(conn, display_id))

    def list(self, plate_id):
        with self.db.unit_of_work() as conn:
            self.plates._get(conn, plate_id)
            rows = conn.execute("SELECT * FROM facet_displays WHERE plate_id=? ORDER BY created,id",
                                (plate_id,)).fetchall()
            return {"plate_id": plate_id, "items": [self._row(row) for row in rows],
                    "source": "all displays are derived from the plate light channel, not re-rendered"}

    def read(self, display_id):
        with self.db.unit_of_work() as conn:
            checksum = self._get(conn, display_id)["display_sha256"]
        return self.blobs.read(checksum)

    def read_png(self, display_id):
        raw = self.read(display_id)
        with self.db.unit_of_work() as conn:
            payload = self.plates._get(conn, self._get(conn, display_id)["plate_id"])["payload"]
        return encode_rgb(payload["width"], payload["height"], raw)

    def create(self, plate_id, data):
        # Read the plate and its light descriptor in one short transaction,
        # then release it. The conversion below holds no writer lock.
        with self.db.unit_of_work() as conn:
            plate = self.plates._get(conn, plate_id)
            payload = plate["payload"]
            descriptor = payload["passes"].get("light.hdr6")
            require(descriptor is not None, "display_light_missing",
                    "This plate stores only display bytes; its light measurement was never saved", 409)
            settings = self._settings(plate, data)
            light_checksum = descriptor["sha256"]
        light = self.blobs.read(light_checksum)
        display = apply_light(light, settings)
        descriptor = self.blobs.put(display)
        with self.db.unit_of_work() as conn:
            # The plate may have been quarantined while bytes were converted.
            # A view of an unavailable plate is not recorded as usable.
            current = self.plates._get(conn, plate_id)
            require(current["payload"]["passes"].get("light.hdr6", {}).get("sha256") == light_checksum,
                    "display_source", "Plate light changed while the display was derived", 409)
            display_id = identity("display")
            conn.execute("""INSERT INTO facet_displays VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                         (display_id, plate_id, light_checksum, descriptor["sha256"],
                          descriptor["size"], settings["function"], settings["exposure"],
                          settings["black"], settings["window"], settings["headroom"], now()))
            self.db.event(conn, display_id, "display_derived",
                          {"plate_id": plate_id, "light": light_checksum,
                           "display": descriptor["sha256"],
                           "settings": {k: settings[k] for k in ("function", "exposure", "black", "window")}})
            row = conn.execute("SELECT * FROM facet_displays WHERE id=?", (display_id,)).fetchone()
            return {**self._row(row), "bytes": descriptor["size"],
                    "recomputed_geometry": False,
                    "byte_source": "content-addressed display derived from light.hdr6"}
