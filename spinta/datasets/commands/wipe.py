from spinta import commands
from spinta.components import Context
from spinta.datasets.components import ExternalBackend
from spinta.datasets.components import Entity


@commands.wipe.register()
def wipe(context: Context, entity: Entity, backend: ExternalBackend):
    raise NotImplementedError
