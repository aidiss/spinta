import contextlib
import pathlib
import os
import tempfile
from typing import Dict
from typing import List

import pytest

import sqlalchemy as sa

from spinta.cli import push
from spinta.scripts import copycsvmodels
from spinta.core.config import RawConfig
from spinta.testing.data import listdata
from spinta.testing.client import create_test_client
from spinta.testing.context import create_test_context
from spinta.testing.tabular import striptable
from spinta.testing.tabular import create_tabular_manifest
from spinta.testing.utils import error
from spinta.testing.client import create_remote_server


Schema = Dict[str, List[sa.Column]]


class Sqlite:

    def __init__(self, dsn: str):
        self.dsn = dsn
        self.engine = sa.create_engine(dsn)
        self.schema = sa.MetaData(self.engine)
        self.tables = {}

    def init(self, tables: Schema):
        self.tables = {
            k: sa.Table(k, self.schema, *v)
            for k, v in tables.items()
        }
        self.schema.create_all()

    def write(self, table, data):
        table = self.tables[table]
        with self.engine.begin() as conn:
            conn.execute(table.insert(), data)


@contextlib.contextmanager
def create_sqlite_db(tables: Schema):
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Sqlite('sqlite:///' + os.path.join(tmpdir, 'db.sqlite'))
        db.init(tables)
        yield db


@pytest.fixture()
def sqlite():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Sqlite('sqlite:///' + os.path.join(tmpdir, 'db.sqlite'))


@pytest.fixture(scope='module')
def geodb():
    with create_sqlite_db({
        'salis': [
            sa.Column('id', sa.Integer, primary_key=True),
            sa.Column('kodas', sa.Text),
            sa.Column('pavadinimas', sa.Text),
        ],
        'miestas': [
            sa.Column('id', sa.Integer, primary_key=True),
            sa.Column('pavadinimas', sa.Text),
            sa.Column('salis', sa.Text),
        ],
    }) as db:
        db.write('salis', [
            {'kodas': 'lt', 'pavadinimas': 'Lietuva'},
            {'kodas': 'lv', 'pavadinimas': 'Latvija'},
            {'kodas': 'ee', 'pavadinimas': 'Estija'},
        ])
        db.write('miestas', [
            {'salis': 'lt', 'pavadinimas': 'Vilnius'},
            {'salis': 'lv', 'pavadinimas': 'Ryga'},
            {'salis': 'ee', 'pavadinimas': 'Talinas'},
        ])
        yield db


def create_rc(rc: RawConfig, tmpdir: pathlib.Path, geodb: Sqlite):
    return rc.fork({
        'manifests': {
            'default': {
                'type': 'tabular',
                'path': str(tmpdir / 'manifest.csv'),
                'backend': 'sql',
                'keymap': 'default',
            },
        },
        'backends': {
            'sql': {
                'type': 'sql',
                'dsn': geodb.dsn,
            },
        },
        # tests/config/clients/3388ea36-4a4f-4821-900a-b574c8829d52.yml
        'default_auth_client': '3388ea36-4a4f-4821-900a-b574c8829d52',
    })


def configure_remote_server(cli, rc: RawConfig, tmpdir: pathlib.Path, responses):
    cli.invoke(rc, copycsvmodels.main, [
        '--no-external', '--access', 'open',
        str(tmpdir / 'manifest.csv'),
        str(tmpdir / 'remote.csv'),
    ])

    # Create remote server with PostgreSQL backend
    tmpdir = pathlib.Path(tmpdir)
    remoterc = rc.fork({
        'manifests': {
            'default': {
                'type': 'tabular',
                'path': str(tmpdir / 'remote.csv'),
                'backend': 'default',
            },
        },
        'backends': ['default'],
    })
    return create_remote_server(
        remoterc,
        tmpdir,
        responses,
        scopes=['spinta_set_meta_fields', 'spinta_upsert'],
        credsfile=True,
    )


def create_client(rc: RawConfig, tmpdir: pathlib.Path, geodb: Sqlite):
    rc = create_rc(rc, tmpdir, geodb)
    context = create_test_context(rc)
    return create_test_client(context)


def test_filter(rc, tmpdir, geodb):
    create_tabular_manifest(tmpdir / 'manifest.csv', striptable('''
    id | d | r | b | m | property | source      | prepare   | type   | ref     | level | access | uri | title   | description
       | datasets/gov/example     |             |           |        |         |       |        |     | Example |
       |   | data                 |             |           | sql    |         |       |        |     | Data    |
       |   |   |                  |             |           |        |         |       |        |     |         |
       |   |   |   | country      | salis       | code='lt' |        | code    |       |        |     | Country |
       |   |   |   |   | code     | kodas       |           | string |         | 3     | open   |     | Code    |
       |   |   |   |   | name     | pavadinimas |           | string |         | 3     | open   |     | Name    |
    '''))

    app = create_client(rc, tmpdir, geodb)

    resp = app.get('/datasets/gov/example/country')
    assert listdata(resp, 'code', 'name') == [
        ('lt', 'Lietuva'),
    ]


def test_filter_join(rc, tmpdir, geodb):
    create_tabular_manifest(tmpdir / 'manifest.csv', striptable('''
    id | d | r | b | m | property | source      | prepare           | type   | ref     | level | access | uri | title   | description
       | datasets/gov/example     |             |                   |        |         |       |        |     | Example |
       |   | data                 |             |                   | sql    |         |       |        |     | Data    |
       |   |   |                  |             |                   |        |         |       |        |     |         |
       |   |   |   | country      | salis       |                   |        | code    |       |        |     | Country |
       |   |   |   |   | code     | kodas       |                   | string |         | 3     | open   |     | Code    |
       |   |   |   |   | name     | pavadinimas |                   | string |         | 3     | open   |     | Name    |
       |   |   |                  |             |                   |        |         |       |        |     |         |
       |   |   |   | city         | miestas     | country.code='lt' |        | name    |       |        |     | City    |
       |   |   |   |   | name     | pavadinimas |                   | string |         | 3     | open   |     | Name    |
       |   |   |   |   | country  | salis       |                   | ref    | country | 4     | open   |     | Country |
    '''))

    app = create_client(rc, tmpdir, geodb)

    resp = app.get('/datasets/gov/example/country')
    codes = dict(listdata(resp, '_id', 'code'))

    resp = app.get('/datasets/gov/example/city?sort(name)')
    data = listdata(resp, 'country._id', 'name')
    data = [(codes.get(country), city) for country, city in data]
    assert data == [
        ('lt', 'Vilnius'),
    ]


def test_filter_join_array_value(rc, tmpdir, geodb):
    create_tabular_manifest(tmpdir / 'manifest.csv', striptable('''
    id | d | r | b | m | property | source      | prepare                  | type   | ref     | level | access | uri | title   | description
       | datasets/gov/example     |             |                          |        |         |       |        |     | Example |
       |   | data                 |             |                          | sql    |         |       |        |     | Data    |
       |   |   |                  |             |                          |        |         |       |        |     |         |
       |   |   |   | country      | salis       |                          |        | code    |       |        |     | Country |
       |   |   |   |   | code     | kodas       |                          | string |         | 3     | open   |     | Code    |
       |   |   |   |   | name     | pavadinimas |                          | string |         | 3     | open   |     | Name    |
       |   |   |                  |             |                          |        |         |       |        |     |         |
       |   |   |   | city         | miestas     | country.code=['lt','lv'] |        | name    |       |        |     | City    |
       |   |   |   |   | name     | pavadinimas |                          | string |         | 3     | open   |     | Name    |
       |   |   |   |   | country  | salis       |                          | ref    | country | 4     | open   |     | Country |
    '''))

    app = create_client(rc, tmpdir, geodb)

    resp = app.get('/datasets/gov/example/country')
    codes = dict(listdata(resp, '_id', 'code'))

    resp = app.get('/datasets/gov/example/city?sort(name)')
    data = listdata(resp, 'country._id', 'name', sort='name')
    data = [(codes.get(country), city) for country, city in data]
    assert data == [
        ('lv', 'Ryga'),
        ('lt', 'Vilnius'),
    ]


def test_filter_join_ne_array_value(rc, tmpdir, geodb):
    create_tabular_manifest(tmpdir / 'manifest.csv', striptable('''
    id | d | r | b | m | property | source      | prepare                   | type   | ref     | level | access | uri | title   | description
       | datasets/gov/example     |             |                           |        |         |       |        |     | Example |
       |   | data                 |             |                           | sql    |         |       |        |     | Data    |
       |   |   |                  |             |                           |        |         |       |        |     |         |
       |   |   |   | country      | salis       |                           |        | code    |       |        |     | Country |
       |   |   |   |   | code     | kodas       |                           | string |         | 3     | open   |     | Code    |
       |   |   |   |   | name     | pavadinimas |                           | string |         | 3     | open   |     | Name    |
       |   |   |                  |             |                           |        |         |       |        |     |         |
       |   |   |   | city         | miestas     | country.code!=['lt','lv'] |        | name    |       |        |     | City    |
       |   |   |   |   | name     | pavadinimas |                           | string |         | 3     | open   |     | Name    |
       |   |   |   |   | country  | salis       |                           | ref    | country | 4     | open   |     | Country |
    '''))

    app = create_client(rc, tmpdir, geodb)

    resp = app.get('/datasets/gov/example/country')
    codes = dict(listdata(resp, '_id', 'code'))

    resp = app.get('/datasets/gov/example/city?sort(name)')
    data = listdata(resp, 'country._id', 'name', sort='name')
    data = [(codes.get(country), city) for country, city in data]
    assert data == [
        ('ee', 'Talinas'),
    ]


@pytest.mark.skip('todo')
def test_filter_multi_column_pk(rc, tmpdir, geodb):
    create_tabular_manifest(tmpdir / 'manifest.csv', striptable('''
    id | d | r | b | m | property | source      | prepare            | type   | ref           | level | access | uri | title   | description
       | datasets/gov/example     |             |                    |        |               |       |        |     | Example |
       |   | data                 |             |                    | sql    |               |       |        |     | Data    |
       |   |   |                  |             |                    |        |               |       |        |     |         |
       |   |   |   | country      | salis       |                    |        | id,code       |       |        |     | Country |
       |   |   |   |   | id       | id          |                    | string |               | 3     | open   |     | Code    |
       |   |   |   |   | code     | kodas       |                    | string |               | 3     | open   |     | Code    |
       |   |   |   |   | name     | pavadinimas |                    | string |               | 3     | open   |     | Name    |
       |   |   |                  |             |                    |        |               |       |        |     |         |
       |   |   |   | city         | miestas     | country.code!='ee' |        | name          |       |        |     | City    |
       |   |   |   |   | name     | pavadinimas |                    | string |               | 3     | open   |     | Name    |
       |   |   |   |   | country  | salis       |                    | ref    | country[code] | 4     | open   |     | Country |
    '''))

    app = create_client(rc, tmpdir, geodb)

    resp = app.get('/datasets/gov/example/country')
    codes = dict(listdata(resp, '_id', 'code'))

    resp = app.get('/datasets/gov/example/city?sort(name)')
    data = listdata(resp, 'country._id', 'name', sort='name')
    data = [(codes.get(country), city) for country, city in data]
    assert data == [
        ('lv', 'Ryga'),
        ('lt', 'Vilnius'),
    ]


def test_getall(rc, tmpdir, geodb):
    create_tabular_manifest(tmpdir / 'manifest.csv', striptable('''
    id | d | r | b | m | property | source      | prepare | type   | ref     | level | access | uri | title   | description
       | datasets/gov/example     |             |         |        |         |       |        |     | Example |
       |   | data                 |             |         | sql    |         |       |        |     | Data    |
       |   |   |                  |             |         |        |         |       |        |     |         |
       |   |   |   | country      | salis       |         |        | code    |       |        |     | Country |
       |   |   |   |   | code     | kodas       |         | string |         | 3     | open   |     | Code    |
       |   |   |   |   | name     | pavadinimas |         | string |         | 3     | open   |     | Name    |
       |   |   |                  |             |         |        |         |       |        |     |         |
       |   |   |   | city         | miestas     |         |        | name    |       |        |     | City    |
       |   |   |   |   | name     | pavadinimas |         | string |         | 3     | open   |     | Name    |
       |   |   |   |   | country  | salis       |         | ref    | country | 4     | open   |     | Country |
    '''))

    app = create_client(rc, tmpdir, geodb)

    resp = app.get('/datasets/gov/example/country?sort(code)')
    codes = dict(listdata(resp, '_id', 'code'))
    assert listdata(resp, 'code', 'name', '_type') == [
        ('ee', 'Estija', 'datasets/gov/example/country'),
        ('lt', 'Lietuva', 'datasets/gov/example/country'),
        ('lv', 'Latvija', 'datasets/gov/example/country'),
    ]

    resp = app.get('/datasets/gov/example/city?sort(name)')
    data = listdata(resp, 'country._id', 'name', '_type', sort='name')
    data = [(codes.get(country), city, _type) for country, city, _type in data]
    assert data == [
        ('lv', 'Ryga', 'datasets/gov/example/city'),
        ('ee', 'Talinas', 'datasets/gov/example/city'),
        ('lt', 'Vilnius', 'datasets/gov/example/city'),
    ]


def test_select(rc, tmpdir, geodb):
    create_tabular_manifest(tmpdir / 'manifest.csv', striptable('''
    id | d | r | b | m | property | source      | prepare | type   | ref     | level | access | uri | title   | description
       | datasets/gov/example     |             |         |        |         |       |        |     | Example |
       |   | data                 |             |         | sql    |         |       |        |     | Data    |
       |   |   |                  |             |         |        |         |       |        |     |         |
       |   |   |   | country      | salis       |         |        | code    |       |        |     | Country |
       |   |   |   |   | code     | kodas       |         | string |         | 3     | open   |     | Code    |
       |   |   |   |   | name     | pavadinimas |         | string |         | 3     | open   |     | Name    |
    '''))

    app = create_client(rc, tmpdir, geodb)

    resp = app.get('/datasets/gov/example/country?select(code,name)')
    assert listdata(resp, 'code', 'name') == [
        ('ee', 'Estija'),
        ('lt', 'Lietuva'),
        ('lv', 'Latvija'),
    ]


@pytest.mark.skip('TODO')
def test_select_len(rc, tmpdir, geodb):
    create_tabular_manifest(tmpdir / 'manifest.csv', striptable('''
    id | d | r | b | m | property | source      | prepare | type   | ref     | level | access | uri | title   | description
       | datasets/gov/example     |             |         |        |         |       |        |     | Example |
       |   | data                 |             |         | sql    |         |       |        |     | Data    |
       |   |   |                  |             |         |        |         |       |        |     |         |
       |   |   |   | country      | salis       |         |        | code    |       |        |     | Country |
       |   |   |   |   | code     | kodas       |         | string |         | 3     | open   |     | Code    |
       |   |   |   |   | name     | pavadinimas |         | string |         | 3     | open   |     | Name    |
    '''))

    app = create_client(rc, tmpdir, geodb)

    resp = app.get('/datasets/gov/example/country?select(code,len(name))')
    assert listdata(resp, 'code', 'len(name)') == [
        ('ee', 6),
        ('lt', 7),
        ('lv', 7),
    ]


def test_filter_len(rc, tmpdir, geodb):
    create_tabular_manifest(tmpdir / 'manifest.csv', striptable('''
    id | d | r | b | m | property | source      | prepare | type   | ref     | level | access | uri | title   | description
       | datasets/gov/example     |             |         |        |         |       |        |     | Example |
       |   | data                 |             |         | sql    |         |       |        |     | Data    |
       |   |   |                  |             |         |        |         |       |        |     |         |
       |   |   |   | country      | salis       |         |        | code    |       |        |     | Country |
       |   |   |   |   | code     | kodas       |         | string |         | 3     | open   |     | Code    |
       |   |   |   |   | name     | pavadinimas |         | string |         | 3     | open   |     | Name    |
    '''))

    app = create_client(rc, tmpdir, geodb)

    resp = app.get('/datasets/gov/example/country?select(code,name)&len(name)=7&sort(code)')
    assert listdata(resp, 'code', 'name') == [
        ('lt', 'Lietuva'),
        ('lv', 'Latvija'),
    ]


def test_private_property(rc, tmpdir, geodb):
    create_tabular_manifest(tmpdir / 'manifest.csv', striptable('''
    id | d | r | b | m | property | source      | prepare    | type   | ref     | level | access  | uri | title   | description
       | datasets/gov/example     |             |            |        |         |       |         |     | Example |
       |   | data                 |             |            | sql    |         |       |         |     | Data    |
       |   |   |                  |             |            |        |         |       |         |     |         |
       |   |   |   | country      | salis       | code!='ee' |        | code    |       |         |     | Country |
       |   |   |   |   | code     | kodas       |            | string |         | 3     | private |     | Code    |
       |   |   |   |   | name     | pavadinimas |            | string |         | 3     | open    |     | Name    |
    '''))

    app = create_client(rc, tmpdir, geodb)

    resp = app.get('/datasets/gov/example/country')
    assert listdata(resp) == [
        'Latvija',
        'Lietuva',
    ]


def test_all_private_properties(rc, tmpdir, geodb):
    create_tabular_manifest(tmpdir / 'manifest.csv', striptable('''
    id | d | r | b | m | property | source      | prepare    | type   | ref     | level | access  | uri | title   | description
       | datasets/gov/example     |             |            |        |         |       |         |     | Example |
       |   | data                 |             |            | sql    |         |       |         |     | Data    |
       |   |   |                  |             |            |        |         |       |         |     |         |
       |   |   |   | country      | salis       | code!='ee' |        | code    |       |         |     | Country |
       |   |   |   |   | code     | kodas       |            | string |         | 3     | private |     | Code    |
       |   |   |   |   | name     | pavadinimas |            | string |         | 3     | private |     | Name    |
    '''))

    app = create_client(rc, tmpdir, geodb)

    resp = app.get('/datasets/gov/example/country')
    assert error(resp, status=401) == 'AuthorizedClientsOnly'


def test_default_access(rc, tmpdir, geodb):
    create_tabular_manifest(tmpdir / 'manifest.csv', striptable('''
    id | d | r | b | m | property | source      | prepare    | type   | ref     | level | access  | uri | title   | description
       | datasets/gov/example     |             |            |        |         |       |         |     | Example |
       |   | data                 |             |            | sql    |         |       |         |     | Data    |
       |   |   |                  |             |            |        |         |       |         |     |         |
       |   |   |   | country      | salis       | code!='ee' |        | code    |       |         |     | Country |
       |   |   |   |   | code     | kodas       |            | string |         | 3     |         |     | Code    |
       |   |   |   |   | name     | pavadinimas |            | string |         | 3     |         |     | Name    |
    '''))

    app = create_client(rc, tmpdir, geodb)

    resp = app.get('/datasets/gov/example/country')
    assert error(resp, status=401) == 'AuthorizedClientsOnly'


def test_model_open_access(rc, tmpdir, geodb):
    create_tabular_manifest(tmpdir / 'manifest.csv', striptable('''
    id | d | r | b | m | property | source      | prepare    | type   | ref     | level | access  | uri | title   | description
       | datasets/gov/example     |             |            |        |         |       |         |     | Example |
       |   | data                 |             |            | sql    |         |       |         |     | Data    |
       |   |   |                  |             |            |        |         |       |         |     |         |
       |   |   |   | country      | salis       | code!='ee' |        | code    |       | open    |     | Country |
       |   |   |   |   | code     | kodas       |            | string |         | 3     |         |     | Code    |
       |   |   |   |   | name     | pavadinimas |            | string |         | 3     |         |     | Name    |
    '''))

    app = create_client(rc, tmpdir, geodb)

    resp = app.get('/datasets/gov/example/country')
    assert listdata(resp) == [
        ('lt', 'Lietuva'),
        ('lv', 'Latvija'),
    ]


def test_property_public_access(rc, tmpdir, geodb):
    create_tabular_manifest(tmpdir / 'manifest.csv', striptable('''
    id | d | r | b | m | property | source      | prepare    | type   | ref     | level | access  | uri | title   | description
       | datasets/gov/example     |             |            |        |         |       |         |     | Example |
       |   | data                 |             |            | sql    |         |       |         |     | Data    |
       |   |   |                  |             |            |        |         |       |         |     |         |
       |   |   |   | country      | salis       | code!='ee' |        | code    |       |         |     | Country |
       |   |   |   |   | code     | kodas       |            | string |         | 3     | public  |     | Code    |
       |   |   |   |   | name     | pavadinimas |            | string |         | 3     | open    |     | Name    |
    '''))

    app = create_client(rc, tmpdir, geodb)

    resp = app.get('/datasets/gov/example/country')
    assert listdata(resp) == [
        'Latvija',
        'Lietuva',
    ]

    resp = app.get('/datasets/gov/example/country', headers={'Accept': 'text/html'})
    assert listdata(resp) == [
        'Latvija',
        'Lietuva',
    ]


def test_select_protected_property(rc, tmpdir, geodb):
    create_tabular_manifest(tmpdir / 'manifest.csv', striptable('''
    id | d | r | b | m | property | source      | prepare    | type   | ref     | level | access  | uri | title   | description
       | datasets/gov/example     |             |            |        |         |       |         |     | Example |
       |   | data                 |             |            | sql    |         |       |         |     | Data    |
       |   |   |                  |             |            |        |         |       |         |     |         |
       |   |   |   | country      | salis       | code!='ee' |        | code    |       |         |     | Country |
       |   |   |   |   | code     | kodas       |            | string |         | 3     | public  |     | Code    |
       |   |   |   |   | name     | pavadinimas |            | string |         | 3     | open    |     | Name    |
    '''))

    app = create_client(rc, tmpdir, geodb)

    resp = app.get('/datasets/gov/example/country?select(code,name)')
    assert error(resp) == 'PropertyNotFound'

    resp = app.get('/datasets/gov/example/country?select(code,name)', headers={'Accept': 'text/html'})
    assert error(resp) == 'PropertyNotFound'


def test_ns_getall(rc, tmpdir, geodb):
    create_tabular_manifest(tmpdir / 'manifest.csv', striptable('''
    id | d | r | b | m | property | source      | prepare    | type   | ref     | level | access  | uri | title   | description
       | datasets/gov/example     |             |            |        |         |       |         |     | Example |
       |   | data                 |             |            | sql    |         |       |         |     | Data    |
       |   |   |                  |             |            |        |         |       |         |     |         |
       |   |   |   | country      | salis       | code!='ee' |        | code    |       |         |     | Country |
       |   |   |   |   | code     | kodas       |            | string |         | 3     | public  |     | Code    |
       |   |   |   |   | name     | pavadinimas |            | string |         | 3     | open    |     | Name    |
    '''))

    app = create_client(rc, tmpdir, geodb)

    resp = app.get('/datasets/gov/example')
    assert listdata(resp, '_id', 'title') == [
        ('datasets/gov/example/country', 'Country'),
    ]

    resp = app.get('/datasets/gov/example', headers={'Accept': 'text/html'})
    assert listdata(resp, '_id', 'title') == [
        ('datasets/gov/example/country', 'Country'),
    ]


def test_push(postgresql, rc, cli, responses, tmpdir, geodb, request):
    create_tabular_manifest(tmpdir / 'manifest.csv', striptable('''
    d | r | b | m | property | source      | type   | ref     | access
    datasets/gov/example     |             |        |         |
      | data                 |             | sql    |         |
      |   |                  |             |        |         |
      |   |   | country      | salis       |        | code    |
      |   |   |   | code     | kodas       | string |         | open
      |   |   |   | name     | pavadinimas | string |         | open
      |   |                  |             |        |         |
      |   |   | city         | miestas     |        | name    |
      |   |   |   | name     | pavadinimas | string |         | open
      |   |   |   | country  | salis       | ref    | country | open
    '''))

    # Configure remote server
    remote = configure_remote_server(cli, rc, tmpdir, responses)
    request.addfinalizer(remote.app.context.wipe_all)

    # Configure local server with SQL backend
    localrc = create_rc(rc, tmpdir, geodb)

    # Push data from local to remote.
    cli.invoke(localrc, push, [
        remote.url,
        '-r', str(remote.credsfile),
        '-c', remote.client,
        '-d', 'datasets/gov/example',
    ])

    remote.app.authmodel('datasets/gov/example/country', ['getall'])
    resp = remote.app.get('/datasets/gov/example/country')
    assert listdata(resp, 'code', 'name') == [
        ('ee', 'Estija'),
        ('lt', 'Lietuva'),
        ('lv', 'Latvija')
    ]

    codes = dict(listdata(resp, '_id', 'code'))
    remote.app.authmodel('datasets/gov/example/city', ['getall'])
    resp = remote.app.get('/datasets/gov/example/city')
    data = listdata(resp, 'country._id', 'name')
    data = [(codes.get(country), city) for country, city in data]
    assert sorted(data) == [
        ('ee', 'Talinas'),
        ('lt', 'Vilnius'),
        ('lv', 'Ryga'),
    ]

    # Add new data to local server
    geodb.write('miestas', [
        {'salis': 'lt', 'pavadinimas': 'Kaunas'},
    ])

    # Push data from local to remote.
    cli.invoke(localrc, push, [
        remote.url,
        '-r', str(remote.credsfile),
        '-c', remote.client,
        '-d', 'datasets/gov/example',
    ])

    resp = remote.app.get('/datasets/gov/example/country')
    assert listdata(resp, 'code', 'name') == [
        ('ee', 'Estija'),
        ('lt', 'Lietuva'),
        ('lv', 'Latvija')
    ]

    resp = remote.app.get('/datasets/gov/example/city')
    data = listdata(resp, 'country._id', 'name')
    data = [(codes.get(country), city) for country, city in data]
    assert sorted(data) == [
        ('ee', 'Talinas'),
        ('lt', 'Kaunas'),
        ('lt', 'Vilnius'),
        ('lv', 'Ryga'),
    ]


def test_no_primary_key(rc, tmpdir, geodb):
    create_tabular_manifest(tmpdir / 'manifest.csv', striptable('''
    d | r | b | m | property | source      | type   | ref | access
    datasets/gov/example     |             |        |     |
      | data                 |             | sql    |     |
      |   |                  |             |        |     |
      |   |   | country      | salis       |        |     |
      |   |   |   | code     | kodas       | string |     | open
      |   |   |   | name     | pavadinimas | string |     | open
    '''))

    app = create_client(rc, tmpdir, geodb)

    resp = app.get('/datasets/gov/example/country')
    codes = dict(listdata(resp, '_id', 'code'))
    data = listdata(resp, '_id', 'code', 'name', sort='code')
    data = [(codes.get(_id), code, name) for _id, code, name in data]
    assert data == [
        ('ee', 'ee', 'Estija'),
        ('lt', 'lt', 'Lietuva'),
        ('lv', 'lv', 'Latvija'),
    ]


def test_count(rc, tmpdir, geodb):
    create_tabular_manifest(tmpdir / 'manifest.csv', striptable('''
    d | r | b | m | property | source      | type   | ref | access
    datasets/gov/example     |             |        |     |
      | data                 |             | sql    |     |
      |   |                  |             |        |     |
      |   |   | country      | salis       |        |     |
      |   |   |   | code     | kodas       | string |     | open
      |   |   |   | name     | pavadinimas | string |     | open
    '''))

    app = create_client(rc, tmpdir, geodb)

    resp = app.get('/datasets/gov/example/country?select(count())')
    assert listdata(resp) == [3]


def test_push_chunks(postgresql, rc, cli, responses, tmpdir, geodb, request):
    create_tabular_manifest(tmpdir / 'manifest.csv', striptable('''
    d | r | b | m | property | source      | type   | ref     | access
    datasets/gov/example     |             |        |         |
      | data                 |             | sql    |         |
      |   |                  |             |        |         |
      |   |   | country      | salis       |        | code    |
      |   |   |   | code     | kodas       | string |         | open
      |   |   |   | name     | pavadinimas | string |         | open
    '''))

    # Configure remote server
    remote = configure_remote_server(cli, rc, tmpdir, responses)
    request.addfinalizer(remote.app.context.wipe_all)

    # Configure local server with SQL backend
    localrc = create_rc(rc, tmpdir, geodb)

    # Push data from local to remote.
    cli.invoke(localrc, push, [
        remote.url,
        '-r', str(remote.credsfile),
        '-c', remote.client,
        '-d', 'datasets/gov/example',
        '--chunk-size=1',
    ])

    remote.app.authmodel('datasets/gov/example/country', ['getall'])
    resp = remote.app.get('/datasets/gov/example/country')
    assert listdata(resp, 'code', 'name') == [
        ('ee', 'Estija'),
        ('lt', 'Lietuva'),
        ('lv', 'Latvija')
    ]


def test_push_state(postgresql, rc, cli, responses, tmpdir, geodb, request):
    create_tabular_manifest(tmpdir / 'manifest.csv', striptable('''
    d | r | b | m | property | source      | type   | ref     | access
    datasets/gov/example     |             |        |         |
      | data                 |             | sql    |         |
      |   |                  |             |        |         |
      |   |   | country      | salis       |        | code    |
      |   |   |   | code     | kodas       | string |         | open
      |   |   |   | name     | pavadinimas | string |         | open
    '''))

    # Configure remote server
    remote = configure_remote_server(cli, rc, tmpdir, responses)
    request.addfinalizer(remote.app.context.wipe_all)

    # Configure local server with SQL backend
    localrc = create_rc(rc, tmpdir, geodb)

    # Push one row, save state and stop.
    cli.invoke(localrc, push, [
        remote.url,
        '-r', str(remote.credsfile),
        '-c', remote.client,
        '-d', 'datasets/gov/example',
        '--chunk-size', '1k',
        '--stop-time', '1h',
        '--stop-row', '1',
        '--state', str(tmpdir / 'state.db'),
    ])

    remote.app.authmodel('datasets/gov/example/country', ['getall'])
    resp = remote.app.get('/datasets/gov/example/country')
    assert len(listdata(resp)) == 1

    cli.invoke(localrc, push, [
        remote.url,
        '-r', str(remote.credsfile),
        '-c', remote.client,
        '-d', 'datasets/gov/example',
        '--stop-row', '1',
        '--state', str(tmpdir / 'state.db'),
    ])

    resp = remote.app.get('/datasets/gov/example/country')
    assert len(listdata(resp)) == 2


def test_prepared_property(rc, tmpdir, geodb):
    create_tabular_manifest(tmpdir / 'manifest.csv', striptable('''
    d | r | b | m | property  | source      | prepare   | type   | ref  | access
    datasets/gov/example      |             |           |        |      |
      | data                  |             |           | sql    |      |
      |   |   | country       | salis       |           |        | code | open
      |   |   |   | code      | kodas       |           | string |      |
      |   |   |   | name      | pavadinimas |           | string |      |
      |   |   |   | continent |             | 'EU'      | string |      |
    '''))

    app = create_client(rc, tmpdir, geodb)

    resp = app.get('/datasets/gov/example/country')
    assert listdata(resp, 'continent', 'code', 'name') == [
        ('EU', 'ee', 'Estija'),
        ('EU', 'lt', 'Lietuva'),
        ('EU', 'lv', 'Latvija'),
    ]


def test_composite_keys(rc, tmpdir, sqlite):
    create_tabular_manifest(tmpdir / 'manifest.csv', striptable('''
    d | r | m | property | source | prepare | type   | ref     | access
    datasets/ds          |        |         |        |         |
      | rs               |        |         | sql    |         |
      |   | t1           | t1     |         |        | a1      | open
      |   |   | p1       | p1     |         | string |         |
      |   |   | p2       | p2     |         | string |         |
      |   |   | a1       |        | p1, p2  | ref    | t2      |
      |   | t2           | t2     |         |        | p3, p4  | open
      |   |   | p3       | p3     |         | string |         |
      |   |   | p4       | p4     |         | string |         |
    '''))

    app = create_client(rc, tmpdir, sqlite)

    sqlite.init({
        't1': [
            sa.Column('p1', sa.Text),
            sa.Column('p2', sa.Text),
        ],
        't2': [
            sa.Column('p3', sa.Text),
            sa.Column('p4', sa.Text),
        ],
    })

    sqlite.write('t1', [{'p1': 'a', 'p2': 'b'}])
    sqlite.write('t2', [{'p3': 'a', 'p4': 'b'}])

    resp = app.get('/datasets/ds/t2')
    data = listdata(resp, '_id', 'p3', 'p4', sort='p3')
    t2pk = {_id: (p3, p4) for _id, p3, p4 in data}
    data = [(t2pk.get(_id), p3, p4) for _id, p3, p4 in data]
    assert data == [(('a', 'b'), 'a', 'b')]

    resp = app.get('/datasets/ds/t1')
    data = listdata(resp, 'p1', 'p2', 'a1', sort='p1')
    data = [
        (
            p1,
            p2,
            {
                **a1,
                '_id': t2pk.get(a1.get('_id')),
            },
        )
        for p1, p2, a1 in data
    ]
    assert data == [
        ('a', 'b', {'_id': ('a', 'b')}),
    ]
