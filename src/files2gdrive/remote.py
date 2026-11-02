"""Idempotent, durable simulator; never contacts Google Drive."""
import base64
import json
import os
from pathlib import Path


class SimulatedRemote:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _path(self, file_id):
        from uuid import UUID
        return self.directory / (str(UUID(file_id)) + '.json')

    def read(self, file_id):
        path = self._path(file_id)
        return json.loads(path.read_text()) if path.exists() else None

    def _write(self, file_id, data):
        path = self._path(file_id)
        temporary = path.with_suffix('.tmp')
        with temporary.open('w') as stream:
            os.chmod(temporary, 0o600)
            json.dump(data, stream)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)

    def upload(self, file_id, source, metadata):
        # Simulator only: intentionally not suitable for multi-GB production transfers.
        content = source.read()
        self._write(file_id, dict(metadata, content=base64.b64encode(content).decode(), state='active'))

    def retain(self, file_id, deleted_at):
        data = self.read(file_id)
        if data and data['state'] != 'trashed':
            data.update(state='excluded', deleted_at=deleted_at)
            self._write(file_id, data)

    def trash(self, file_id):
        data = self.read(file_id)
        if data:
            data['state'] = 'trashed'
            self._write(file_id, data)
