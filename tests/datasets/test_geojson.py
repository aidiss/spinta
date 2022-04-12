from spinta import commands
from spinta.components import Mode
from spinta.core.config import RawConfig
from spinta.testing.manifest import load_manifest_and_context


def test_geojson_resource(rc: RawConfig):
    table = '''
    d | r | b | m | property | type    | ref  | source                        | prepare | access
    example                  |         |      |                               |         |
      | data                 | geojson |      | https://example.com/data.json |         |
                             |         |      |                               |         |
      |   |   | City         |         | name | CITY                          |         |
      |   |   |   | name     | string  |      | NAME                          |         | open
    '''
    context, manifest = load_manifest_and_context(rc, table, mode=Mode.external)
    backend = manifest.models['example/City'].backend
    assert backend.type == 'geojson'
    assert manifest == table

    commands.wait(context, backend)
