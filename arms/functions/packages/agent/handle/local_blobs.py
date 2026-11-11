"""Directory-backed stand-in for Spaces, used only by local tests."""

import os
import shutil
from pathlib import Path


class LocalBlobs:
    def __init__(self, root):
        self.root = Path(root)

    def put_file(self, key, path):
        dst = self.root / key
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, dst)
        return os.path.getsize(path)

    def get_to_file(self, key, path):
        shutil.copyfile(self.root / key, path)
        return os.path.getsize(path)

    def delete(self, key):
        (self.root / key).unlink(missing_ok=True)
        return 204

    def keys(self):
        return sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*")
                      if p.is_file())
