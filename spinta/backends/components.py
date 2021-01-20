import enum
import contextlib
from typing import Any
from typing import Dict


class BackendFeatures(enum.Enum):
    # Files are stored in blocks and file metadata must include _bsize and
    # _blocks properties.
    FILE_BLOCKS = 'FILE_BLOCKS'


class Backend:
    metadata = {
        'name': 'backend',
    }

    type: str
    name: str
    features = set()

    # Original configuration values given in manifest, this is used to restore
    # manifest back to its original form.
    config: Dict[str, Any]

    def __repr__(self):
        return (
            f'<{self.__class__.__module__}.{self.__class__.__name__}'
            f'(name={self.name!r}) at 0x{id(self):02x}>'
        )

    @contextlib.contextmanager
    def transaction(self):
        raise NotImplementedError
