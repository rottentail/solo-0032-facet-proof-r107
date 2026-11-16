"""Resumable local backups of a fixed SQL snapshot and its content closure.

The snapshot is published once. Later content copies never consult live plate
or tile rows. Inventory describes one SQL observation and a subsequent file
observation; it deliberately grants no authority to delete unreferenced bytes.
"""
from contextlib import contextmanager
import hashlib
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile

from .errors import Fault, require
from .values import canonical, identity, now, parse_json


SCHEMA = """
CREATE TABLE IF NOT EXISTS facet_maintenance (
 id TEXT PRIMARY KEY,
 owner_root TEXT NOT NULL,
 owner_instance TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('running','failed','succeeded')),
 snapshot_sha256 TEXT, snapshot_size INTEGER, closure_sha256 TEXT,
 manifest_sha256 TEXT,
 total_blobs INTEGER NOT NULL DEFAULT 0,
 copied_blobs INTEGER NOT NULL DEFAULT 0,
 error_code TEXT, error_detail TEXT,
 created REAL NOT NULL, updated REAL NOT NULL
);
"""
HEX = re.compile(r"[a-f0-9]{64}\Z")
OPERATION = re.compile(r"backup_[a-f0-9]{32}\Z")
CHUNK = 1024 * 1024


class Maintenance:
    def __init__(self, db, blobs, plates):
        self.db, self.blobs, self.plates = db, blobs, plates
        self.root = Path(db.root).resolve()
        require(Path(blobs.root).absolute() == self.root / "plates",
                "maintenance_path", "Content store must belong to this data root")
        try:
            self.owner_instance = self._root_identity()
        except OSError as exc:
            raise Fault("maintenance_io", "Maintenance data-root identity is unavailable", 503) from exc
        with db.unit_of_work() as conn:
            conn.execute(SCHEMA)
            columns = {r["name"] for r in conn.execute("PRAGMA table_info(facet_maintenance)")}
            if "owner_root" not in columns:
                # Pre-release records with unknown origins are readable, but
                # must not silently gain authority to operate on this root.
                conn.execute("ALTER TABLE facet_maintenance ADD COLUMN owner_root TEXT")
            if "owner_instance" not in columns:
                conn.execute("ALTER TABLE facet_maintenance ADD COLUMN owner_instance TEXT")

    def _root_identity(self):
        # This local marker is deliberately outside the backed-up SQL/CAS
        # closure. A restored directory, even at the same path, must not
        # inherit authority over historical backup control records.
        marker = self.root / ".facet-maintenance-owner"
        self._directory(self.root)
        if not marker.exists() and not marker.is_symlink():
            fd, temporary = tempfile.mkstemp(prefix=".maintenance-owner-", dir=self.root)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(os.urandom(16).hex().encode())
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    # Link a fully written inode without overwriting another
                    # process's marker or exposing an empty marker to it.
                    os.link(temporary, marker)
                except FileExistsError:
                    pass
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
                self._sync_directory(self.root)
        with self._reader(marker) as stream:
            value = stream.read(33)
        require(re.fullmatch(rb"[a-f0-9]{32}", value), "maintenance_origin",
                "Data-root maintenance identity is invalid", 409)
        return value.decode()

    def _operation(self, operation_id):
        require(isinstance(operation_id, str) and OPERATION.fullmatch(operation_id),
                "backup_id", "Invalid backup operation identifier")
        with self.db.unit_of_work() as conn:
            row = conn.execute("SELECT * FROM facet_maintenance WHERE id=?", (operation_id,)).fetchone()
            require(row is not None, "not_found", "Backup operation not found", 404)
            return dict(row)

    def _update(self, operation_id, action=None, **fields):
        allowed = {"state", "snapshot_sha256", "snapshot_size", "closure_sha256", "manifest_sha256",
                   "total_blobs", "copied_blobs", "error_code", "error_detail"}
        require(fields and set(fields) <= allowed, "maintenance_fields", "Invalid backup update")
        fields["updated"] = now()
        with self.db.unit_of_work() as conn:
            changed = conn.execute("UPDATE facet_maintenance SET " + ",".join(k + "=?" for k in fields) + " WHERE id=?",
                                   [*fields.values(), operation_id]).rowcount
            require(changed == 1, "not_found", "Backup operation not found", 404)
            if action:
                self.db.event(conn, operation_id, action, fields)

    def get(self, operation_id):
        row = self._operation(operation_id)
        operation = self.root / "backups" / operation_id
        # Polling never creates directories, including after a crash before
        # begin_backup managed to create its operation directory.
        for path in (self.root / "backups", operation, operation / "data"):
            if not path.exists() and not path.is_symlink():
                break
            self._directory(path)
        historical = row["owner_root"] != str(self.root) or row["owner_instance"] != self.owner_instance
        return {**row, "operation_id": operation_id, "operation_directory": str(operation),
                "data_directory": str(operation / "data"), "historical_control_record": historical,
                "resumable_here": not historical,
                "control_records": "A restored SQL snapshot retains historical operation states. Its records do not resume operations in the original data root."}

    def _require_origin(self, row):
        require(row["owner_root"] == str(self.root) and row["owner_instance"] == self.owner_instance, "backup_origin",
                "Historical backup control records cannot resume operations in this data root; begin a new backup", 409)

    def _failure(self, operation_id, exc):
        if isinstance(exc, Fault):
            fault = Fault(exc.code, exc.detail, exc.status)
        elif isinstance(exc, sqlite3.Error):
            fault = Fault("backup_sqlite", "Backup SQL snapshot could not be read", 409)
        else:
            fault = Fault("maintenance_io", "Backup storage is unavailable", 503)
        # A failed metadata write must not turn the operation into a reported
        # success. The original failure remains visible to the caller.
        try:
            self._update(operation_id, "backup_failed", state="failed",
                         error_code=fault.code, error_detail=fault.detail[:500])
        except Fault:
            pass
        return fault

    def _directory(self, path, create=False):
        path = Path(path).absolute()
        require(path == self.root or self.root in path.parents,
                "maintenance_path", "Backup path escapes the data root")
        relative = path.relative_to(self.root)
        current = self.root
        require(not current.is_symlink() and current.is_dir(),
                "maintenance_path", "Data root must be a real directory")
        for component in relative.parts:
            current = current / component
            if create:
                try:
                    current.mkdir(mode=0o700)
                    self._sync_directory(current.parent)
                except FileExistsError:
                    pass
            require(not current.is_symlink() and current.is_dir(),
                    "maintenance_path", "Backup and content directories cannot be symlinks", 409)
        require(path.resolve() == path, "maintenance_path", "Ambiguous backup path", 409)
        return path

    def _paths(self, operation_id, create=False):
        require(isinstance(operation_id, str) and OPERATION.fullmatch(operation_id),
                "backup_id", "Invalid backup operation identifier")
        operation = self._directory(self.root / "backups" / operation_id, create)
        data = self._directory(operation / "data", create)
        return operation, data

    @staticmethod
    def _sync_directory(path):
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    @contextmanager
    def _lock(self, operation):
        # POSIX flock is released by the kernel after a crash/SIGKILL. A
        # persisted "running" flag alone cannot safely distinguish that case
        # from another live process resuming the same backup.
        try:
            import fcntl
        except ImportError as exc:
            raise Fault("maintenance_platform", "Local backup locking requires POSIX file locks", 503) from exc
        fd = os.open(operation / ".lock", os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            require(stat.S_ISREG(os.fstat(fd).st_mode), "maintenance_path", "Invalid operation lock", 409)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise Fault("backup_busy", "Another process is operating on this backup", 409) from exc
            yield
        finally:
            os.close(fd)

    @contextmanager
    def _reader(self, path):
        require(path.parent.exists() or path.parent.is_symlink(), "backup_missing",
                "Required content directory is missing", 409)
        self._directory(path.parent)
        require(not path.is_symlink(), "maintenance_path", "Content files cannot be symlinks", 409)
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        except FileNotFoundError as exc:
            raise Fault("backup_missing", "Required backup or content file is missing: " + path.name, 409) from exc
        try:
            require(stat.S_ISREG(os.fstat(fd).st_mode), "maintenance_path", "Content must be a regular file", 409)
            with os.fdopen(fd, "rb", closefd=False) as stream:
                yield stream
        finally:
            os.close(fd)

    def _file(self, path, checksum=None, size=None):
        digest, length = hashlib.sha256(), 0
        with self._reader(path) as stream:
            before = os.fstat(stream.fileno())
            while True:
                data = stream.read(CHUNK)
                if not data:
                    break
                digest.update(data)
                length += len(data)
            after = os.fstat(stream.fileno())
        require((before.st_size, before.st_mtime_ns, before.st_ctime_ns) ==
                (after.st_size, after.st_mtime_ns, after.st_ctime_ns),
                "backup_changed", "Content changed while it was being checked", 409)
        actual = digest.hexdigest()
        require(checksum is None or actual == checksum, "backup_corrupt", "Backup content checksum mismatch: " + path.name, 409)
        require(size is None or length == size, "backup_size", "Backup content length mismatch: " + path.name, 409)
        return {"sha256": actual, "size": length}

    def _write_json(self, path, value):
        self._directory(path.parent)
        require(not path.is_symlink(), "maintenance_path", "Manifest cannot be a symlink", 409)
        raw = canonical(value).encode()
        fd, temporary = tempfile.mkstemp(prefix=".manifest-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            self._sync_directory(path.parent)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return hashlib.sha256(raw).hexdigest()

    @staticmethod
    def _merge_reference(refs, descriptor, source):
        require(isinstance(descriptor, dict), "backup_reference", "Invalid content reference", 409)
        checksum, size = descriptor.get("sha256"), descriptor.get("size")
        require(isinstance(checksum, str) and HEX.fullmatch(checksum), "backup_reference", "Invalid content address", 409)
        require(isinstance(size, int) and not isinstance(size, bool) and 0 <= size <= 2**63-1,
                "backup_reference", "Invalid content length", 409)
        if checksum in refs:
            require(refs[checksum]["size"] == size, "backup_reference", "Conflicting lengths for shared content", 409)
        else:
            refs[checksum] = {"sha256": checksum, "size": size, "sources": []}
        if source not in refs[checksum]["sources"]:
            refs[checksum]["sources"].append(source)

    def _closure(self, conn):
        refs, states = {}, {}
        rows = conn.execute("SELECT id,state,payload FROM plates ORDER BY id").fetchall()
        for row in rows:
            try:
                payload = parse_json(row["payload"])
            except Fault as exc:
                raise Fault("backup_reference", "Plate content metadata is invalid", 409) from exc
            passes = payload.get("passes") if isinstance(payload, dict) else None
            require(isinstance(passes, dict) and passes, "backup_reference", "Plate has no valid content closure", 409)
            states[row["state"]] = states.get(row["state"], 0) + 1
            for name, descriptor in sorted(passes.items()):
                require(isinstance(name, str), "backup_reference", "Invalid channel name", 409)
                self._merge_reference(refs, descriptor, {"kind": "plate", "plate_id": row["id"],
                                                        "state": row["state"], "channel": name})
        tile_count = 0
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='facet_tiles'").fetchone():
            try:
                from .tile_store import TileStore
            except ImportError as exc:
                raise Fault("backup_schema", "Tile checkpoint reader is unavailable", 409) from exc
            for descriptor in TileStore.snapshot_refs(conn):
                source = {**{k: v for k, v in descriptor.items() if k not in {"sha256", "size"}}, "kind": "tile_checkpoint"}
                self._merge_reference(refs, descriptor, source)
                tile_count += 1
        display_count = 0
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='facet_displays'").fetchone():
            # Review displays are derived from plate light, but their bytes are
            # already published. A restore must reproduce the same records, so
            # reference them rather than leave rows pointing at absent content.
            for row in conn.execute("SELECT id,plate_id,light_sha256,display_sha256 FROM facet_displays ORDER BY id"):
                for name, checksum_ref in (("display", row["display_sha256"]),):
                    descriptor = {"sha256": checksum_ref, "size": self.blobs.size(checksum_ref)}
                    self._merge_reference(refs, descriptor,
                                          {"kind": "review_display", "display_id": row["id"],
                                           "plate_id": row["plate_id"], "channel": name})
                    display_count += 1
        for item in refs.values():
            item["sources"].sort(key=canonical)
        return {"references": [refs[key] for key in sorted(refs)], "plate_count": len(rows),
                "plate_states": states, "tile_channel_references": tile_count,
                "display_references": display_count,
                "event_sequence": conn.execute("SELECT COALESCE(MAX(seq),0) FROM events").fetchone()[0]}

    @contextmanager
    def _snapshot_connection(self, path):
        # First inspect/open the file without following links. immutable=1
        # prevents verification itself creating WAL/SHM files or using live WAL.
        with self._reader(path):
            pass
        conn = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            require(conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok",
                    "backup_sqlite", "Backup database integrity failed", 409)
            require(not conn.execute("PRAGMA foreign_key_check").fetchall(),
                    "backup_sqlite", "Backup database references are inconsistent", 409)
            yield conn
        finally:
            conn.close()

    def _prepare_snapshot(self, operation_id, operation, data, row, allow_creation=True, persist=True):
        snapshot = data / "facetproof.sqlite3"
        if not snapshot.exists() and not snapshot.is_symlink():
            require(allow_creation and row["snapshot_sha256"] is None,
                    "backup_missing", "Previously published SQL snapshot is missing", 409)
            scratch = data / (".snapshot-" + identity("writing") + ".sqlite3")
            try:
                self.db.backup(str(scratch))
                self._file(scratch)
                with self._snapshot_connection(scratch):
                    pass
                with self._reader(scratch) as stream:
                    os.fsync(stream.fileno())
                os.replace(scratch, snapshot)
                self._sync_directory(data)
            finally:
                if scratch.exists():
                    scratch.unlink()
        info = self._file(snapshot, row["snapshot_sha256"], row["snapshot_size"])
        with self._snapshot_connection(snapshot) as conn:
            closure = self._closure(conn)
        closure_hash = hashlib.sha256(canonical(closure).encode()).hexdigest()
        require(row["closure_sha256"] in {None, closure_hash}, "backup_reference", "Backup reference closure changed", 409)
        path = operation / "manifest.json"
        if path.exists() or path.is_symlink():
            with self._reader(path) as stream:
                raw = stream.read()
            try:
                manifest = parse_json(raw)
            except Fault as exc:
                raise Fault("backup_manifest", "Backup manifest is not valid JSON", 409) from exc
            allowed = {"format", "operation_id", "source_root", "source_instance", "control_records",
                       "snapshot", "closure", "created", "complete", "completed"}
            require(isinstance(manifest, dict) and manifest.get("format") == "facet-backup-1"
                    and set(manifest) <= allowed
                    and manifest.get("operation_id") == operation_id
                    and manifest.get("source_root") == row["owner_root"]
                    and manifest.get("source_instance") == row["owner_instance"]
                    and manifest.get("control_records") == "historical_at_snapshot"
                    and manifest.get("snapshot") == info and manifest.get("closure") == closure
                    and manifest.get("created") == row["created"]
                    and isinstance(manifest.get("complete"), bool),
                    "backup_manifest", "Backup manifest does not describe its SQL snapshot", 409)
            completed = manifest.get("completed")
            require(not manifest["complete"] or (isinstance(completed, (int, float))
                    and not isinstance(completed, bool) and math.isfinite(completed)),
                    "backup_manifest", "Backup completion time is invalid", 409)
            if row["manifest_sha256"]:
                require(hashlib.sha256(raw).hexdigest() == row["manifest_sha256"],
                        "backup_manifest", "Completed backup manifest changed", 409)
        else:
            require(allow_creation, "backup_missing", "Backup manifest is missing", 409)
            manifest = {"format": "facet-backup-1", "operation_id": operation_id,
                        "source_root": row["owner_root"], "source_instance": row["owner_instance"],
                        "control_records": "historical_at_snapshot",
                        "snapshot": info, "closure": closure, "created": row["created"], "complete": False}
            self._write_json(path, manifest)
        if persist:
            self._update(operation_id, snapshot_sha256=info["sha256"], snapshot_size=info["size"],
                         closure_sha256=closure_hash, total_blobs=len(closure["references"]))
        return manifest

    def _copy_content(self, data, reference):
        checksum, size = reference["sha256"], reference["size"]
        destination_dir = self._directory(data / "plates" / checksum[:2], True)
        destination = destination_dir / checksum
        if destination.exists() or destination.is_symlink():
            self._file(destination, checksum, size)
            return
        source = self.blobs.path(checksum)
        fd, temporary = tempfile.mkstemp(prefix=".copy-", dir=destination_dir)
        try:
            digest, length = hashlib.sha256(), 0
            with os.fdopen(fd, "wb") as output, self._reader(source) as stream:
                before = os.fstat(stream.fileno())
                while True:
                    block = stream.read(CHUNK)
                    if not block:
                        break
                    output.write(block)
                    digest.update(block)
                    length += len(block)
                after = os.fstat(stream.fileno())
                require((before.st_size, before.st_mtime_ns, before.st_ctime_ns) ==
                        (after.st_size, after.st_mtime_ns, after.st_ctime_ns),
                        "backup_changed", "Source content changed during backup", 409)
                require(digest.hexdigest() == checksum, "backup_corrupt", "Source content checksum mismatch: " + checksum, 409)
                require(length == size, "backup_size", "Source content length mismatch: " + checksum, 409)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, destination)
            self._sync_directory(destination_dir)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _verify_content(self, data, manifest):
        self._file(data / "facetproof.sqlite3", manifest["snapshot"]["sha256"], manifest["snapshot"]["size"])
        with self._snapshot_connection(data / "facetproof.sqlite3") as conn:
            require(self._closure(conn) == manifest["closure"], "backup_reference", "Snapshot references do not match manifest", 409)
        for reference in manifest["closure"]["references"]:
            self._file(data / "plates" / reference["sha256"][:2] / reference["sha256"], reference["sha256"], reference["size"])

    def _result(self, operation_id, data, manifest):
        return {**self.get(operation_id), "data_directory": str(data),
                "manifest": manifest, "restore_requires_copy_before_writing": True}

    def begin_backup(self):
        operation_id, stamp = identity("backup"), now()
        with self.db.unit_of_work() as conn:
            conn.execute("INSERT INTO facet_maintenance(id,owner_root,owner_instance,state,created,updated) VALUES (?,?,?,'running',?,?)",
                         (operation_id, str(self.root), self.owner_instance, stamp, stamp))
            self.db.event(conn, operation_id, "backup_started", {})
        try:
            operation, data = self._paths(operation_id, True)
            with self._lock(operation):
                manifest = self._prepare_snapshot(operation_id, operation, data, self._operation(operation_id))
                return self._result(operation_id, data, manifest)
        except (Fault, OSError, sqlite3.Error) as exc:
            raise self._failure(operation_id, exc) from exc

    def resume_backup(self, operation_id):
        row = self._operation(operation_id)
        self._require_origin(row)
        try:
            operation, data = self._paths(operation_id, True)
            with self._lock(operation):
                self._update(operation_id, "backup_resumed", state="running", error_code=None,
                             error_detail=None, copied_blobs=0)
                manifest = self._prepare_snapshot(operation_id, operation, data, row)
                for count, reference in enumerate(manifest["closure"]["references"], 1):
                    self._copy_content(data, reference)
                    self._update(operation_id, copied_blobs=count)
                self._verify_content(data, manifest)
                if not manifest["complete"]:
                    manifest = {**manifest, "complete": True, "completed": now()}
                checksum = self._write_json(operation / "manifest.json", manifest)
                self._update(operation_id, "backup_succeeded", state="succeeded", manifest_sha256=checksum,
                             error_code=None, error_detail=None)
                return self._result(operation_id, data, manifest)
        except (Fault, OSError, sqlite3.Error) as exc:
            if not isinstance(exc, Fault) or exc.code != "backup_busy":
                raise self._failure(operation_id, exc) from exc
            raise

    def verify_backup(self, operation_id):
        row = self._operation(operation_id)
        self._require_origin(row)
        require(row["state"] == "succeeded", "backup_incomplete", "Backup has not completed", 409)
        try:
            operation, data = self._paths(operation_id)
            with self._lock(operation):
                row = self._operation(operation_id)
                require(row["state"] == "succeeded", "backup_incomplete", "Backup has not completed", 409)
                require(row["snapshot_sha256"] and row["closure_sha256"] and row["manifest_sha256"],
                        "backup_incomplete", "Backup completion evidence is missing", 409)
                manifest = self._prepare_snapshot(operation_id, operation, data, row,
                                                  allow_creation=False, persist=False)
                require(manifest["complete"], "backup_incomplete", "Backup manifest is incomplete", 409)
                self._verify_content(data, manifest)
                result = self._result(operation_id, data, manifest)
                result["valid"] = True
                return result
        except (Fault, OSError, sqlite3.Error) as exc:
            if not isinstance(exc, Fault) or exc.code != "backup_busy":
                raise self._failure(operation_id, exc) from exc
            raise

    def inspect(self):
        try:
            # Capture references and event sequence together, then release SQL
            # before reading bytes. Later files may belong to in-flight work.
            with self.db.unit_of_work() as conn:
                closure = self._closure(conn)
                operations = [{**dict(row), "historical_control_record": row["owner_root"] != str(self.root) or row["owner_instance"] != self.owner_instance}
                              for row in conn.execute("SELECT * FROM facet_maintenance ORDER BY created,id")]
            refs = {r["sha256"]: r for r in closure["references"]}
            checked = []
            for reference in closure["references"]:
                path = self.blobs.path(reference["sha256"])
                try:
                    self._file(path, reference["sha256"], reference["size"])
                    checked.append({**reference, "status": "ok"})
                except (Fault, OSError) as exc:
                    code = exc.code if isinstance(exc, Fault) else "maintenance_io"
                    checked.append({**reference, "status": "missing" if code == "backup_missing" else "corrupt",
                                    "error": code})
            inventory, unexpected = [], []
            content_root = Path(self.blobs.root)
            if content_root.exists() or content_root.is_symlink():
                content_root = self._directory(content_root)
                directories = sorted(content_root.iterdir())
            else:
                directories = []
                unexpected.append("content_root_missing")
            for directory in directories:
                if directory.is_symlink() or not directory.is_dir() or not re.fullmatch(r"[a-f0-9]{2}", directory.name):
                    unexpected.append(directory.name)
                    continue
                for path in sorted(directory.iterdir()):
                    if path.is_symlink() or not path.is_file() or not HEX.fullmatch(path.name) or path.name[:2] != directory.name:
                        unexpected.append(str(path.relative_to(content_root)))
                        continue
                    inventory.append({"sha256": path.name, "size": path.stat().st_size,
                                      "referenced_in_sql_snapshot": path.name in refs, "deletion_authorized": False})
            return {"sql_observation": {k: v for k, v in closure.items() if k != "references"},
                    "references": checked, "shared_contents": sum(len(r["sources"]) > 1 for r in checked),
                    "inventory": inventory, "unexpected_entries": unexpected, "operations": operations,
                    "deletion_authorized": False,
                    "boundary": "References describe one committed SQL observation; files are checked afterwards. Unreferenced files may belong to in-flight work and are not authorized for deletion."}
        except sqlite3.Error as exc:
            raise Fault("maintenance_sqlite", "Content inspection could not read its SQL observation", 503) from exc
        except OSError as exc:
            raise Fault("maintenance_io", "Content inventory is unavailable", 503) from exc
