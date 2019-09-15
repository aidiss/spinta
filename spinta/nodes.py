from spinta.components import Context, Manifest, Node
from spinta.utils.schema import resolve_schema
from spinta import exceptions


def load_node(context: Context, node: Node, data: dict, manifest: Manifest, *, check_unknowns=True) -> Node:
    na = object()
    store = context.get('store')
    node.manifest = manifest
    node.type = data['type']
    node.path = data['path']
    node.name = data['name']
    node.parent = data['parent']

    node_schema = resolve_schema(node, Node)
    for name in set(node_schema) | set(data):
        if name not in node_schema:
            if check_unknowns:
                raise exceptions.UnknownParameter(node, param=name)
            else:
                continue
        schema = node_schema[name]
        value = data.get(name, na)
        if schema.get('inherit', False) and value is na:
            if node.parent and hasattr(node.parent, name):
                value = getattr(node.parent, name)
            else:
                value = None
        if schema.get('required', False) and value is na:
            raise exceptions.MissingRequiredProperty(node, prop=name)
        if schema.get('type') == 'backend' and isinstance(value, str):
            value = store.backends[value]
        if value is na:
            value = schema.get('default')
        setattr(node, name, value)
    return node
