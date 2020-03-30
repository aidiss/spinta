from spinta import commands
from spinta.nodes import get_node, load_node
from spinta.components import Context, Manifest
from spinta.datasets.components import Dataset, Resource, Entity


@commands.load.register()
def load(context: Context, dataset: Dataset, data: dict, manifest: Manifest):
    config = context.get('config')

    load_node(context, dataset, data, parent=manifest)

    # Load resources
    dataset.resources = {}
    for name, params in (data.get('resources') or {}).items():
        resource = get_node(config, manifest, data, parent=dataset, group='datasets', ctype='resource')
        resource.type = params.get('type')
        resource.name = name
        resource.dataset = dataset
        dataset.resources[name] = load(context, resource, params, manifest)

    return dataset


@commands.load.register()
def load(context: Context, resource: Resource, data: dict, manifest: Manifest):
    load_node(context, resource, data, parent=resource.dataset)
    # Models will be added on `link` command.
    resource.models = {}
    return resource


@commands.load.register()
def load(context: Context, entity: Entity, data: dict, manifest: Manifest):
    return entity
