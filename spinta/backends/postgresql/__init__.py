from typing import AsyncIterator, Optional, List

import contextlib
import copy
import datetime
import hashlib
import itertools
import re
import typing
import types

import pytz
import unidecode
import sqlalchemy as sa
import sqlalchemy.exc
from sqlalchemy.dialects.postgresql import JSONB, BIGINT, UUID
from sqlalchemy.engine.result import RowProxy
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.expression import FunctionElement
from starlette.requests import Request

from spinta import commands
from spinta.backends import Backend
from spinta.commands import wait, load, prepare, migrate, getone, getall, wipe, authorize
from spinta.common import NA
from spinta.components import Context, Manifest, Model, Property, Action, UrlParams, DataItem
from spinta.config import RawConfig
from spinta.renderer import render
from spinta.types.datatype import Array, DataType, Date, DateTime, File, Object, PrimaryKey, Ref, String, Node, Integer
from spinta import exceptions
from spinta.utils.nestedstruct import flatten, parent_key_for_item

from spinta.exceptions import (
    MultipleRowsFound,
    NotFoundError,
    ItemDoesNotExist,
    UniqueConstraint,
    UnavailableSubresource,
)

# Maximum length for PostgreSQL identifiers (e.g. table names, column names,
# function names).
# https://github.com/postgres/postgres/blob/master/src/include/pg_config_manual.h
NAMEDATALEN = 63


PG_CLEAN_NAME_RE = re.compile(r'[^a-z0-9]+', re.IGNORECASE)

MAIN_TABLE = 'M'
LISTS_TABLE = 'L'
CHANGES_TABLE = 'C'
CACHE_TABLE = 'T'

UNSUPPORTED_TYPES = [
    'backref',
    'generic',
    'rql',
]


class PostgreSQL(Backend):
    metadata = {
        'name': 'postgresql',
        'properties': {
            'dsn': {'type': 'string', 'required': True},
        },
    }

    engine = None
    schema = None
    tables = None

    # List of properties who are in lists.
    props_in_lists: List[str] = None

    @contextlib.contextmanager
    def transaction(self, write=False):
        with self.engine.begin() as connection:
            if write:
                table = self.tables['internal']['transaction']
                result = connection.execute(
                    table.main.insert().values(
                        datetime=utcnow(),
                        client_type='',
                        client_id='',
                        errors=0,
                    )
                )
                transaction_id = result.inserted_primary_key[0]
                yield WriteTransaction(connection, transaction_id)
            else:
                yield ReadTransaction(connection)

    def get(self, connection, columns, condition, default=NA):
        scalar = isinstance(columns, sa.Column)
        columns = columns if isinstance(columns, list) else [columns]

        result = connection.execute(
            sa.select(columns).where(condition)
        )
        result = list(itertools.islice(result, 2))

        if len(result) == 1:
            if scalar:
                return result[0][columns[0]]
            else:
                return result[0]

        elif len(result) == 0:
            if default is NA:
                raise NotFoundError()
            else:
                return default
        else:
            raise MultipleRowsFound()


class ReadTransaction:

    def __init__(self, connection):
        self.connection = connection


class WriteTransaction(ReadTransaction):

    def __init__(self, connection, id):
        super().__init__(connection)
        self.id = id
        self.errors = 0


@wait.register()
def wait(context: Context, backend: PostgreSQL, config: RawConfig, *, fail: bool = False):
    dsn = config.get('backends', backend.name, 'dsn', required=True)
    engine = sa.create_engine(dsn, connect_args={'connect_timeout': 0})
    try:
        conn = engine.connect()
    except sqlalchemy.exc.OperationalError:
        if fail:
            raise
        else:
            return False
    else:
        conn.close()
        engine.dispose()
        return True


@load.register()
def load(context: Context, backend: PostgreSQL, config: RawConfig):
    backend.dsn = config.get('backends', backend.name, 'dsn', required=True)
    backend.engine = sa.create_engine(backend.dsn, echo=False)
    backend.schema = sa.MetaData(backend.engine)
    backend.tables = {}


@prepare.register()
def prepare(context: Context, backend: PostgreSQL, manifest: Manifest):
    if manifest.name not in backend.tables:
        backend.tables[manifest.name] = {}

    # Prepare backend for models.
    for model in manifest.objects['model'].values():
        if model.backend.name == backend.name:
            prepare(context, backend, model)

    # Prepare backend for datasets.
    for dataset in manifest.objects.get('dataset', {}).values():
        for resource in dataset.resources.values():
            for model in resource.models():
                if model.backend.name == backend.name:
                    prepare(context, backend, model)


@prepare.register()
def prepare(context: Context, backend: PostgreSQL, model: Model):
    columns = []
    for prop in model.properties.values():
        column = prepare(context, backend, prop)
        if isinstance(column, list):
            columns.extend(column)
        elif column is not None:
            columns.append(column)

    # Create main table.
    main_table_name = get_table_name(backend, model.manifest.name, model.name, MAIN_TABLE)
    main_table = sa.Table(
        main_table_name, backend.schema,
        sa.Column('_transaction', sa.Integer, sa.ForeignKey('transaction._id')),
        sa.Column('_created', sa.DateTime),
        sa.Column('_updated', sa.DateTime),
        *columns,
    )

    if _has_lists(model):
        # Create table for nested lists.
        lists_table_name = get_table_name(backend, model.manifest.name, model.name, LISTS_TABLE)
        lists_table = sa.Table(
            lists_table_name, backend.schema,
            sa.Column('transaction', sa.Integer, sa.ForeignKey('transaction._id')),
            sa.Column('id', main_table.c._id.type, sa.ForeignKey(f'{main_table_name}._id')),  # reference to main table
            sa.Column('key', sa.String),  # parent key of the data inside `data` column
            sa.Column('data', JSONB),
        )
    else:
        lists_table = None

    # Create changes table.
    changes_table_name = get_table_name(backend, model.manifest.name, model.name, CHANGES_TABLE)
    # XXX: not sure if I should pass main_table.c.id.type.__class__() or a
    #      shorter form.
    changes_table = get_changes_table(backend, changes_table_name, main_table.c._id.type)

    backend.tables[model.manifest.name][model.name] = ModelTables(main_table, lists_table, changes_table)

    if backend.props_in_lists is None:
        backend.props_in_lists = list(_find_props_in_lists(model))
    else:
        backend.props_in_lists.extend(list(_find_props_in_lists(model)))


def _find_props_in_lists(node: Node, inlist: bool = False):
    if isinstance(node, Model):
        for prop in node.properties.values():
            yield from _find_props_in_lists(prop, inlist)
    elif isinstance(node.dtype, Object):
        for prop in node.dtype.properties.values():
            yield from _find_props_in_lists(prop, inlist)
    elif isinstance(node.dtype, Array):
        yield from _find_props_in_lists(node.dtype.items, inlist=True)
    elif inlist:
        yield node.place


@prepare.register()
def prepare(context: Context, backend: PostgreSQL, dtype: DataType):
    if dtype.prop.backend.name != backend.name:
        # If property backend differs from model backend, then no columns should
        # be added to the table. If some property type require adding columns
        # even for a property with different backend, then this type must
        # implement prepare command and do custom logic there.
        return

    if dtype.name == 'type':
        return
    elif dtype.name == 'string':
        return sa.Column(dtype.prop.name, sa.Text)
    elif dtype.name == 'date':
        return sa.Column(dtype.prop.name, sa.Date)
    elif dtype.name == 'datetime':
        return sa.Column(dtype.prop.name, sa.DateTime)
    elif dtype.name == 'integer':
        return sa.Column(dtype.prop.name, sa.Integer)
    elif dtype.name == 'number':
        return sa.Column(dtype.prop.name, sa.Float)
    elif dtype.name == 'boolean':
        return sa.Column(dtype.prop.name, sa.Boolean)
    elif dtype.name in ('spatial', 'image'):
        # TODO: these property types currently are not implemented
        return sa.Column(dtype.prop.name, sa.Text)
    elif dtype.name in UNSUPPORTED_TYPES:
        return
    else:
        raise Exception(f"Unknown property type {dtype.name!r}.")


@prepare.register()
def prepare(context: Context, backend: PostgreSQL, dtype: PrimaryKey):
    if dtype.prop.manifest.name == 'internal':
        return sa.Column('_id', BIGINT, primary_key=True)
    else:
        return sa.Column('_id', UUID(), primary_key=True)


@prepare.register()
def prepare(context: Context, backend: PostgreSQL, dtype: Ref):
    # TODO: rename dtype.object to dtype.model
    ref_model = dtype.prop.model.manifest.objects['model'][dtype.object]
    table_name = get_table_name(backend, ref_model.manifest.name, ref_model.name)
    if ref_model.manifest.name == 'internal':
        column_type = sa.Integer()
    else:
        column_type = UUID()
    return [
        sa.Column(dtype.prop.name, column_type),
        sa.ForeignKeyConstraint(
            [dtype.prop.name], [f'{table_name}._id'],
            name=_get_pg_name(f'fk_{dtype.prop.model.name}_{dtype.prop.name}'),
        )
    ]


@prepare.register()
def prepare(context: Context, backend: PostgreSQL, dtype: File):
    if dtype.prop.backend.name == backend.name:
        return sa.Column(dtype.prop.name, sa.LargeBinary)
    else:
        # If file property has a different backend, then here we just need to
        # save file name of file stored externally.
        return sa.Column(dtype.prop.name, JSONB)


@prepare.register()
def prepare(context: Context, backend: PostgreSQL, dtype: Array):
    return sa.Column(dtype.prop.name, JSONB)


@prepare.register()
def prepare(context: Context, backend: PostgreSQL, dtype: Object):
    return sa.Column(dtype.prop.name, JSONB)


def _get_pg_name(name):
    if len(name) > NAMEDATALEN:
        name_hash = hashlib.sha256(name.encode()).hexdigest()
        return name[:NAMEDATALEN - 7] + '_' + name_hash[-6:]
    else:
        return name


class ModelTables(typing.NamedTuple):
    main: sa.Table = None
    lists: sa.Table = None
    changes: sa.Table = None
    cache: sa.Table = None


@migrate.register()
def migrate(context: Context, backend: PostgreSQL):
    # XXX: I found, that this some times leaks connection, you can check that by
    #      comparing `backend.engine.pool.checkedin()` before and after this
    #      line.
    backend.schema.create_all(checkfirst=True)


@commands.check_unique_constraint.register()
def check_unique_constraint(
    context: Context,
    data: DataItem,
    dtype: DataType,
    prop: Property,
    backend: PostgreSQL,
    value: object,
):
    table = backend.tables[prop.manifest.name][prop.model.name].main

    if data.action in (Action.UPDATE, Action.PATCH):
        condition = sa.and_(
            table.c[prop.name] == value,
            table.c._id != data.saved['_id'],
        )
    # PATCH requests are allowed to send protected fields in requests JSON
    # PATCH handling will use those fields for validating data, though
    # won't change them.
    elif data.action == Action.PATCH and dtype.prop.name in {'_id', '_type', '_revision'}:
        return
    else:
        condition = table.c[prop.name] == value
    not_found = object()
    connection = context.get('transaction').connection
    result = backend.get(connection, table.c[prop.name], condition, default=not_found)
    if result is not not_found:
        raise UniqueConstraint(prop)


def _update_lists_table(context: Context, model: Model, table: sa.Table, action: Action, data: dict) -> None:
    if table is None:
        return

    pk = data['_id']
    transaction = context.get('transaction')
    connection = transaction.connection
    if action != Action.INSERT:
        connection.execute(table.delete().where(table.c.id == pk))
    data = _get_lists_only(data)
    if data:
        connection.execute(table.insert(), [
            {
                'transaction': transaction.id,
                'id': pk,
                'key': parent_key_for_item(item),
                'data': item,
            }
            for item in flatten([data])
        ])


def _has_lists(node: Node):
    if isinstance(node, Model):
        for prop in node.properties.values():
            if _has_lists(prop):
                return True
    elif isinstance(node.dtype, Object):
        for prop in node.dtype.properties.values():
            if _has_lists(prop):
                return True
    elif isinstance(node.dtype, Array):
        return True


def _get_lists_only(value):
    if isinstance(value, dict):
        result = {}
        for k, v in value.items():
            v = _get_lists_only(v)
            if v is not None:
                result[k] = v
        if result:
            return result
    elif isinstance(value, list):
        return value


@commands.insert.register()
async def insert(
    context: Context,
    model: Model,
    backend: PostgreSQL,
    *,
    dstream: AsyncIterator[DataItem],
    stop_on_error: bool = True,
):
    transaction = context.get('transaction')
    connection = transaction.connection
    table = backend.tables[model.manifest.name][model.name]
    async for data in dstream:
        # TODO: Refactor this to insert batches with single query.
        qry = table.main.insert().values(
            _transaction=transaction.id,
            _created=utcnow(),
        )
        connection.execute(qry, [{
            '_id': data.patch['_id'],
            '_revision': data.patch['_revision'],
            **{k: v for k, v in data.patch.items() if not k.startswith('_')},
        }])

        # Update lists table
        _update_lists_table(context, model, table.lists, Action.INSERT, data.patch)

        yield data


@commands.update.register()
async def update(
    context: Context,
    model: Model,
    backend: PostgreSQL,
    *,
    dstream: dict,
    stop_on_error: bool = True,
):
    transaction = context.get('transaction')
    connection = transaction.connection
    table = backend.tables[model.manifest.name][model.name]

    async for data in dstream:
        if not data.patch:
            yield data
            continue

        id_ = data.saved['_id']

        # Support patching nested properties.
        #
        # Create a copy of data.patch and fill it with the data
        # that we are missing in patch to not override object attributes
        # missing from data.patch
        full_patch = copy.deepcopy(data.patch)
        if data.prop and data.saved and data.action == Action.PATCH:
            patched_keys = data.patch[data.prop.name].keys()
            for k, v in data.saved.items():
                if not k.startswith('_') and k not in patched_keys:
                    full_patch[data.prop.name][k] = v

        values = {k: v for k, v in full_patch.items()}
        values['_revision'] = data.patch['_revision']
        if '_id' in data.patch:
            values['_id'] = data.patch['_id']

        result = connection.execute(
            table.main.update().
            where(table.main.c._id == id_).
            where(table.main.c._revision == data.saved['_revision']).
            values(values)
        )

        if result.rowcount == 0:
            raise Exception("Update failed, {model} with {id_} not found.")
        elif result.rowcount > 1:
            raise Exception("Update failed, {model} with {id_} has found and update {result.rowcount} rows.")

        # Update lists table
        _update_lists_table(context, model, table.lists, data.action, {
            **data.saved,
            **data.patch,
        })

        yield data


@commands.delete.register()
async def delete(
    context: Context,
    model: Model,
    backend: PostgreSQL,
    *,
    dstream: AsyncIterator[DataItem],
    stop_on_error: bool = True,
):
    transaction = context.get('transaction')
    connection = transaction.connection
    table = backend.tables[model.manifest.name][model.name]
    async for data in dstream:
        connection.execute(
            table.main.delete().
            where(table.main.c._id == data.saved['_id'])
        )

        # Update lists table
        _update_lists_table(context, model, table.lists, data.action, {
            '_id': data.saved['_id'],
            '_revision': data.saved['_revision'],
        })

        yield data


@getone.register()
async def getone(
    context: Context,
    request: Request,
    model: Model,
    backend: PostgreSQL,
    *,
    action: Action,
    params: UrlParams,
):
    authorize(context, action, model)
    data = getone(context, model, backend, id_=params.pk)
    # TODO: All meta properties eventually should have `_` prefix.
    data['id'] = data['_id']
    data = prepare(context, Action.GETONE, model, backend, data, select=params.select)
    return render(context, request, model, params, data, action=action)


@getone.register()
def getone(
    context: Context,
    model: Model,
    backend: PostgreSQL,
    *,
    id_: str,
):
    connection = context.get('transaction').connection
    table = backend.tables[model.manifest.name][model.name].main
    try:
        result = backend.get(connection, table, table.c._id == id_)
    except NotFoundError:
        raise ItemDoesNotExist(model, id=id_)
    return dict(result)


@getone.register()
async def getone(
    context: Context,
    request: Request,
    prop: Property,
    dtype: DataType,
    backend: PostgreSQL,
    *,
    action: Action,
    params: UrlParams,
):
    raise UnavailableSubresource(prop=prop.name, prop_type=prop.dtype.name)


@getone.register()
async def getone(
    context: Context,
    request: Request,
    prop: Property,
    dtype: (Object, File),
    backend: PostgreSQL,
    *,
    action: Action,
    params: UrlParams,
):
    authorize(context, action, prop)
    data = getone(context, prop, dtype, backend, id_=params.pk)
    data = prepare(context, Action.GETONE, prop.dtype, backend, data)
    return render(context, request, prop, params, data, action=action)


@getone.register()
def getone(
    context: Context,
    prop: Property,
    dtype: Object,
    backend: PostgreSQL,
    *,
    id_: str,
):
    table = backend.tables[prop.manifest.name][prop.model.name].main
    connection = context.get('transaction').connection
    selectlist = [
        table.c._id,
        table.c._revision,
        table.c[prop.name],
    ]
    try:
        data = backend.get(connection, selectlist, table.c._id == id_)
    except NotFoundError:
        raise ItemDoesNotExist(prop.model, id=id_)

    result = {
        '_id': data[table.c._id],
        '_revision': data[table.c._revision],
        '_type': prop.model.model_type(),
        **(data[table.c[prop.name]] or {}),
    }
    return result


@getone.register()
def getone(
    context: Context,
    prop: Property,
    dtype: File,
    backend: PostgreSQL,
    *,
    id_: str,
):
    table = backend.tables[prop.manifest.name][prop.model.name].main
    connection = context.get('transaction').connection
    selectlist = [
        table.c._id,
        table.c._revision,
        table.c[prop.name],
    ]
    try:
        data = backend.get(connection, selectlist, table.c._id == id_)
    except NotFoundError:
        raise ItemDoesNotExist(prop.model, id=id_)

    result = {
        '_id': data[table.c._id],
        '_revision': data[table.c._revision],
        '_type': prop.model.model_type(),
        **(data[prop.name] if data[prop.name]
           else {'content_type': None, 'filename': None}),
    }
    return result


@getall.register()
async def getall(
    context: Context,
    request: Request,
    model: Model,
    backend: PostgreSQL,
    *,
    action: Action,
    params: UrlParams,
):
    authorize(context, action, model)
    result = (
        prepare(context, action, model, backend, row, select=params.select)
        for row in getall(
            context, model, backend,
            select=params.select,
            sort=params.sort,
            offset=params.offset,
            limit=params.limit,
            query=params.query,
            # TODO: Add count support.
        )
    )
    return render(context, request, model, params, result, action=action)


@getall.register()
def getall(
    context: Context,
    model: Model,
    backend: PostgreSQL,
    *,
    select: typing.List[str] = None,
    sort: typing.Dict[str, dict] = None,
    offset: int = None,
    limit: int = None,
    query: typing.List[typing.Dict[str, str]] = None,
):
    connection = context.get('transaction').connection
    table = backend.tables[model.manifest.name][model.name]

    joins = []

    # TODO: Select list must be taken from params.select.
    qry = sa.select([table.main])
    qry = _getall_query(context, model, backend, table, joins, qry, query)
    qry = _getall_order_by(model, backend, qry, table, joins, sort)
    qry = _getall_offset(qry, offset)
    qry = _getall_limit(qry, limit)

    if joins:
        join = table.main
        for alias, cond in joins:
            join = join.join(alias, cond)
        qry = qry.select_from(join)

    for row in connection.execute(qry):
        yield dict(row)


def _getall_query(
    context: Context,
    model: Model,
    backend: PostgreSQL,
    table: ModelTables,
    joins: List[str],
    qry: sa.sql.Select,
    query: Optional[List[dict]],
) -> sa.sql.Select:
    where = []
    for qp in query or []:
        key = qp['args'][0]
        # TODO: Fix RQL parser to support `foo.bar=baz` notation.
        #       https://github.com/pjwerneck/pyrql/pull/2
        key = '.'.join(key) if isinstance(key, tuple) else key
        if key not in model.flatprops:
            raise exceptions.FieldNotInResource(model, property=key)
        prop = model.flatprops[key]
        name = qp['name']
        value = commands.load_search_params(context, prop.dtype, backend, qp)

        if prop.place in backend.props_in_lists:
            jsonb = table.lists.c.data[prop.place]
            if _is_dtype(prop, String):
                field = jsonb.astext
            elif _is_dtype(prop, Integer):
                field = jsonb
                value = sa.cast(value, JSONB)
            elif _is_dtype(prop, DateTime):
                field = jsonb.astext
                value = datetime.datetime.fromisoformat(value)
                if value.tzinfo is not None:
                    # XXX: Think below probably must be hander in a more proper way.
                    # FIXME: Same conversion must be done when storing data.
                    # Convert dates to utc and drop timezone information, we can't
                    # compare dates in JSONB.
                    value = value.astimezone(pytz.utc).replace(tzinfo=None)
                value = value.isoformat()
            elif _is_dtype(prop, Date):
                field = jsonb.astext
                value = datetime.date.fromisoformat(value)
                value = value.isoformat()
            else:
                value = sa.cast(value, JSONB)
                field = sa.cast(value, JSONB)
        else:
            jsonb = None
            field = table.main.c[prop.name]

        if isinstance(prop.dtype, String):
            field = sa.func.lower(field)
            value = value.lower()

        if name == 'eq':
            cond = field == value
        elif name == 'ge':
            cond = field >= value
        elif name == 'gt':
            cond = field > value
        elif name == 'le':
            cond = field <= value
        elif name == 'lt':
            cond = field < value
        elif name == 'ne':
            raise NotImplementedError

            # XXX: this implementation is not finished
            # problem is with SQLAlchemy query generation
            key = parent_key_for_item({prop.place: None})

            # Problem with not-equal query is that we want to filter out all
            # resource ids from the lists table, that either do not have
            # the item we are looking for or have the item, but it's value is
            # not equal to the value we are searching for.
            #
            # select lists with a key for an item we are searching for
            select_keys = sa.select([table.lists]).where(table.lists.c.key == key)

            # select key ids, to be used later (XXX: can id's be reused from query above?)
            select_key_ids = sa.select([table.lists.c.id]).where(table.lists.c.key == key)

            # get keys which do not have the item we are searching form
            select_non_keys = sa.select([table.lists], table.lists.c.id.notin_(select_key_ids))

            # make a union to get a table from select_keys and select_non_keys,
            # to combine results and get a table with resource ids, where
            # item values are not equal to the value or either search items do
            # not exist at all
            temp_table = select_keys.union(select_non_keys).alias('temp_table')

            field._orig[0].table = temp_table

            cond = sa.or_(
                field == None,  # noqa
                field != value,
            )
        elif name == 'contains':
            cond = field.contains(value)
        elif name == 'startswith':
            cond = field.startswith(value)
        else:
            raise exceptions.UnknownOperator(prop, operator=name)

        if jsonb is not None:
            if name == 'ne':
                # use temporary table we created for in the `ne` condition
                join = (
                    sa.select([temp_table.c.id, field], distinct=temp_table.c.id).
                    where(cond).
                    alias()
                )
            else:
                join = (
                    sa.select([table.lists.c.id, field], distinct=table.lists.c.id).
                    where(cond).
                    alias()
                )
            joins.append((join, table.main.c._id == join.c.id))
        else:
            where.append(cond)

    if where:
        qry = qry.where(sa.and_(*where))

    return qry


def _is_dtype(prop, DataType):
    return (
        isinstance(prop.dtype, DataType) or (
            isinstance(prop.dtype, Array) and
            isinstance(prop.dtype.items.dtype, DataType)
        )
    )


def _getall_order_by(
    model: Model,
    backend: PostgreSQL,
    qry: sa.sql.Select,
    table: ModelTables,
    joins: List[str],
    sort: typing.List[typing.Tuple[str, str]],
) -> sa.sql.Select:
    if sort:
        direction = {
            '+': lambda c: c.asc(),
            '-': lambda c: c.desc(),
        }
        db_sort_keys = []
        for key in sort:
            # Optional sort direction: sort(+key) or sort(key)
            # XXX: Probably move this to spinta/urlparams.py.
            if len(key) == 1:
                d, key = ('+',) + key
            else:
                d, key = key

            if key not in model.flatprops:
                raise exceptions.FieldNotInResource(model, property=key)

            prop = model.flatprops[key]

            if prop.place in backend.props_in_lists:
                field = sa.cast(table.lists.c.data[prop.place], JSONB)
                subqry = (
                    sa.select([
                        table.lists.c.id,
                        field.label('value'),
                        sa.func.row_number().over(
                            partition_by=table.lists.c.id,
                            order_by=direction[d](field),
                        ).label('rn'),
                    ]).alias()
                )
                alias = sa.select([subqry]).where(subqry.c.rn == 1).alias()
                joins.append((alias, table.main.c._id == alias.c.id))
                field = alias.c.value
            else:
                field = table.main.c[prop.name]

            field = direction[d](field)

            db_sort_keys.append(field)

        return qry.order_by(*db_sort_keys)
    else:
        return qry


def _getall_offset(qry: sa.sql.Select, offset: Optional[int]) -> sa.sql.Select:
    if offset:
        return qry.offset(offset)
    else:
        return qry


def _getall_limit(qry: sa.sql.Select, limit: Optional[int]) -> sa.sql.Select:
    if limit:
        return qry.limit(limit)
    else:
        return qry


@commands.changes.register()
async def changes(
    context: Context,
    request: Request,
    model: Model,
    backend: PostgreSQL,
    *,
    action: Action,
    params: UrlParams,
):
    authorize(context, action, model)
    data = changes(context, model, backend, id_=params.pk, limit=params.limit, offset=params.offset)
    data = (
        {
            **row,
            '_created': row['_created'].isoformat(),
        }
        for row in data
    )
    return render(context, request, model, params, data, action=action)


@changes.register()
def changes(
    context: Context,
    model: Model,
    backend: PostgreSQL,
    *,
    id_: str = None,
    limit: int = 100,
    offset: int = -10,
):
    connection = context.get('transaction').connection
    table = backend.tables[model.manifest.name][model.name].changes

    qry = sa.select([table]).order_by(table.c.change)
    qry = _changes_id(table, qry, id_)
    qry = _changes_offset(table, qry, offset)
    qry = _changes_limit(qry, limit)

    result = connection.execute(qry)
    for row in result:
        yield {
            '_change': row[table.c.change],
            '_revision': row[table.c.revision],
            '_transaction': row[table.c.transaction],
            '_id': row[table.c.id],
            '_created': row[table.c.datetime],
            '_op': row[table.c.action],
            **dict(row[table.c.data]),
        }


def _changes_id(table, qry, id_):
    if id_:
        return qry.where(table.c.id == id_)
    else:
        return qry


def _changes_offset(table, qry, offset):
    if offset:
        if offset > 0:
            offset = offset
        else:
            offset = (
                qry.with_only_columns([
                    sa.func.max(table.c.change) - abs(offset),
                ]).
                order_by(None).alias()
            )
        return qry.where(table.c.change > offset)
    else:
        return qry


def _changes_limit(qry, limit):
    if limit:
        return qry.limit(limit)
    else:
        return qry


@wipe.register()
def wipe(context: Context, model: Model, backend: PostgreSQL):
    authorize(context, Action.WIPE, model)

    table = backend.tables[model.manifest.name][model.name]
    connection = context.get('transaction').connection

    if table.lists is not None:
        connection.execute(table.lists.delete())
    connection.execute(table.changes.delete())
    connection.execute(table.main.delete())


class utcnow(FunctionElement):
    type = sa.DateTime()


@compiles(utcnow, 'postgresql')
def pg_utcnow(element, compiler, **kw):
    return "TIMEZONE('utc', CURRENT_TIMESTAMP)"


def get_table_name(backend: PostgreSQL, manifest: str, name: str, table_type=MAIN_TABLE):
    assert isinstance(table_type, str)
    assert len(table_type) == 1
    assert table_type.isupper()

    # Table name construction depends on internal tables, so we must construct
    # internal table names differently.
    if manifest == 'internal':
        if table_type == MAIN_TABLE:
            return name
        else:
            return f'{name}_{table_type}'

    table = backend.tables['internal']['table'].main
    table_id = backend.get(backend.engine, table.c._id, table.c.name == name, default=None)
    if table_id is None:
        result = backend.engine.execute(
            table.insert(),
            name=name,
        )
        table_id = result.inserted_primary_key[0]
    name = unidecode.unidecode(name)
    name = PG_CLEAN_NAME_RE.sub('_', name)
    name = name.upper()
    name = name[:NAMEDATALEN - 6]
    name = name.rstrip('_')
    return f"{name}_{table_id:04d}{table_type}"


def get_changes_table(backend, table_name, id_type):
    table = sa.Table(
        table_name, backend.schema,
        # XXX: This will not work with multi master setup. Consider changing it
        #      to UUID or something like that.
        #
        #      `change` should be monotonically incrementing, in order to
        #      have that, we could always create new `change_id`, by querying,
        #      previous `change_id` and increment it by one. This will create
        #      duplicates, but we simply know, that these changes happened at at
        #      the same time. So that's probably OK.
        sa.Column('change', BIGINT, primary_key=True),
        sa.Column('revision', sa.String(40)),
        sa.Column('transaction', sa.Integer, sa.ForeignKey('transaction._id')),
        sa.Column('id', id_type),  # reference to main table
        sa.Column('datetime', sa.DateTime),
        sa.Column('action', sa.String(8)),  # insert, update, delete
        sa.Column('data', JSONB),
    )
    return table


@prepare.register()
def prepare(context: Context, action: Action, model: Model, backend: PostgreSQL, value: RowProxy, *, select: typing.List[str] = None) -> dict:
    return prepare(context, action, model, backend, dict(value), select=select)


@prepare.register()
def prepare(context: Context, dtype: DateTime, backend: PostgreSQL, value: datetime.datetime) -> object:
    # convert datetime object to isoformat string if it belongs
    # to a nested property
    if dtype.prop.parent is dtype.prop.model:
        return value
    else:
        return value.isoformat()


@prepare.register()
def prepare(context: Context, dtype: Date, backend: PostgreSQL, value: datetime.date) -> object:
    # convert date object to isoformat string if it belongs
    # to a nested property
    if dtype.prop.parent is dtype.prop.model:
        return value
    else:
        return value.isoformat()


@commands.unload_backend.register()
def unload_backend(context: Context, backend: PostgreSQL):
    # Make sure all connections are released, since next test will create
    # another connection pool and connection pool is not reused between
    # tests. Maybe it would be a good idea to reuse same connection between
    # all tests?
    backend.engine.dispose()


def _fix_data_for_json(data):
    # XXX: a temporary workaround
    #
    #      Changelog data are stored as JSON and data must be JSON serializable.
    #      Probably there should be a command, that would make data JSON
    #      serializable.
    _data = {}
    for k, v in data.items():
        if isinstance(v, (datetime.datetime, datetime.date)):
            v = v.isoformat()
        _data[k] = v
    return _data


@commands.create_changelog_entry.register()
async def create_changelog_entry(
    context: Context,
    model: (Model, Property),
    backend: PostgreSQL,
    *,
    dstream: types.AsyncGeneratorType,
) -> None:
    transaction = context.get('transaction')
    connection = transaction.connection
    if isinstance(model, Model):
        table = backend.tables[model.manifest.name][model.name]
    else:
        table = backend.tables[model.model.manifest.name][model.model.name]
    async for data in dstream:
        qry = table.changes.insert().values(
            transaction=transaction.id,
            datetime=utcnow(),
            action=Action.INSERT.value,
        )
        connection.execute(qry, [{
            'id': data.saved['_id'] if data.saved else data.patch['_id'],
            '_revision': data.patch['_revision'] if data.patch else data.saved['_revision'],
            'transaction': transaction.id,
            'datetime': utcnow(),
            'action': data.action.value,
            'data': _fix_data_for_json({
                k: v for k, v in data.patch.items() if not k.startswith('_')
            }),
        }])
        yield data
