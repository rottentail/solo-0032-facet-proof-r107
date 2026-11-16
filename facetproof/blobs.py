import hashlib
import os
from pathlib import Path
import re
import tempfile
from .errors import Fault, require


class Blobs:
    def __init__(self, root):
        self.root = Path(root).resolve() / "plates"
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, checksum):
        require(isinstance(checksum, str) and re.fullmatch(r"[a-f0-9]{64}", checksum),
                "blob_key", "Invalid plate content address")
        return self.root / checksum[:2] / checksum

    def put(self, data):
        checksum = hashlib.sha256(data).hexdigest()
        destination = self.path(checksum)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            self.read(checksum)
            return {"sha256": checksum, "size": len(data)}
        fd, scratch = tempfile.mkstemp(prefix=".writing-", dir=destination.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(scratch, destination)
            parent_fd = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        finally:
            if os.path.exists(scratch):
                os.unlink(scratch)
        return {"sha256": checksum, "size": len(data)}

    def read(self, checksum):
        path = self.path(checksum)
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise Fault("plate_missing", "Plate bytes unavailable", 409) from exc
        require(hashlib.sha256(data).hexdigest() == checksum,
                "plate_corrupt", "Plate checksum mismatch", 409)
        return data

    def size(self, checksum):
        """Report stored byte length without reading content into memory."""
        path = self.path(checksum)
        try:
            return path.stat().st_size
        except OSError as exc:
            raise Fault("plate_missing", "Plate bytes unavailable", 409) from exc

    def inventory(self):
        entries = []
        for directory in sorted(self.root.iterdir()):
            if not directory.is_dir():
                continue
            for path in sorted(directory.iterdir()):
                if re.fullmatch(r"[a-f0-9]{64}", path.name):
                    entries.append({"sha256": path.name, "size": path.stat().st_size})
        return entries
