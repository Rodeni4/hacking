"""Server-only plaintext secret files. Never mounted as static content."""
import os
import uuid
from pathlib import Path


class SecretStore:
    def __init__(self, directory):
        self.directory = Path(directory)

    def path(self, reference):
        return self.directory / (uuid.UUID(reference).hex + '.key')

    def read(self, reference):
        return self.path(reference).read_text(encoding='utf-8')

    def exists(self, reference):
        return self.path(reference).is_file()

    def create(self, key):
        self.directory.mkdir(parents=True, exist_ok=True)
        reference = uuid.uuid4().hex
        path = self.path(reference)
        try:
            with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w', encoding='utf-8') as stream:
                stream.write(key)
        except OSError:
            path.unlink(missing_ok=True)
            raise
        return reference

    def delete(self, reference):
        self.path(reference).unlink(missing_ok=True)
