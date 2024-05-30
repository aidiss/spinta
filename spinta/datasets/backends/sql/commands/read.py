import logging
from typing import Any
from typing import Iterator

from sqlalchemy.engine.row import RowProxy

from spinta import commands
from spinta.components import Context
from spinta.components import Model
from spinta.core.ufuncs import Expr
from spinta.datasets.backends.helpers import handle_ref_key_assignment, generate_pk_for_row
from spinta.datasets.backends.sql.commands.query import Selected
from spinta.datasets.backends.sql.commands.query import SqlQueryBuilder
from spinta.datasets.backends.sql.components import Sql
from spinta.datasets.backends.sql.ufuncs.components import SqlResultBuilder
from spinta.datasets.helpers import get_enum_filters
from spinta.datasets.helpers import get_ref_filters
from spinta.datasets.keymaps.components import KeyMap
from spinta.datasets.utils import iterparams
from spinta.dimensions.enum.helpers import get_prop_enum
from spinta.exceptions import ValueNotInEnum
from spinta.types.datatype import PrimaryKey
from spinta.types.datatype import Ref
from spinta.typing import ObjectData
from spinta.ufuncs.basequerybuilder.components import QueryParams
from spinta.ufuncs.basequerybuilder.helpers import get_page_values
from spinta.ufuncs.helpers import merge_formulas
from spinta.utils.nestedstruct import flat_dicts_to_nested
from spinta.utils.schema import NA

log = logging.getLogger(__name__)


def _resolve_expr(context: Context, row: RowProxy, sel: Selected) -> Any:
    if sel.item is None:
        val = None
    else:
        val = row[sel.item]
    env = SqlResultBuilder(context).init(val, sel.prop, row)
    return env.resolve(sel.prep)


def _get_row_value(context: Context, row: RowProxy, sel: Any) -> Any:
    if isinstance(sel, Selected):
        if isinstance(sel.prep, Expr):
            val = _resolve_expr(context, row, sel)
        elif sel.prep is not NA:
            val = _get_row_value(context, row, sel.prep)
        else:
            val = row[sel.item]

        if enum := get_prop_enum(sel.prop):
            if val is None:
                pass
            elif str(val) in enum:
                item = enum[str(val)]
                if item.prepare is not NA:
                    val = item.prepare
            else:
                raise ValueNotInEnum(sel.prop, value=val)

        return val
    if isinstance(sel, tuple):
        return tuple(_get_row_value(context, row, v) for v in sel)
    if isinstance(sel, list):
        return [_get_row_value(context, row, v) for v in sel]
    if isinstance(sel, dict):
        return {k: _get_row_value(context, row, v) for k, v in sel.items()}
    return sel


@commands.getall.register(Context, Model, Sql)
def getall(
    context: Context,
    model: Model,
    backend: Sql,
    *,
    query: Expr = None,
    params: QueryParams = None,
    **kwargs
) -> Iterator[ObjectData]:
    conn = context.get(f'transaction.{backend.name}')
    builder = SqlQueryBuilder(context)
    builder.update(model=model)
    # Merge user passed query with query set in manifest.
    query = merge_formulas(model.external.prepare, query)
    query = merge_formulas(query, get_enum_filters(context, model))
    query = merge_formulas(query, get_ref_filters(context, model))
    keymap: KeyMap = context.get(f'keymap.{model.keymap.name}')
    for model_params in iterparams(context, model, model.manifest):
        table = model.external.name.format(**model_params)
        table = backend.get_table(model, table)
        env = builder.init(backend, table, params)
        env.update(params=model_params)
        expr = env.resolve(query)
        where = env.execute(expr)
        qry = env.build(where)
        for row in conn.execute(qry):
            res = {
                '_type': model.model_type()
            }
            if model.page.is_enabled:
                res['_page'] = get_page_values(env, row)

            for key, sel in env.selected.items():
                val = _get_row_value(context, row, sel)
                if sel.prop:
                    if isinstance(sel.prop.dtype, PrimaryKey):
                        val = generate_pk_for_row(sel.prop.model, row, keymap, val)
                    elif isinstance(sel.prop.dtype, Ref):
                        val = handle_ref_key_assignment(keymap, env, val, sel.prop.dtype)
                res[key] = val
            res = flat_dicts_to_nested(res)
            res = commands.cast_backend_to_python(context, model, backend, res)
            yield res


@commands.getone.register(Context, Model, Sql)
def getone(
    context: Context,
    model: Model,
    backend: Sql,
    *,
    id_: str,
) -> ObjectData:
    keymap: KeyMap = context.get(f'keymap.{model.keymap.name}')
    _id = keymap.decode(model.name, id_)

    # preparing query for retrieving item by pk (single column or multi column)
    query = {}
    if isinstance(_id, list):
        pkeys = model.external.pkeys
        for index, pk in enumerate(pkeys):
            query[pk.name] = _id[index]
    else:
        pk = model.external.pkeys[0].name
        query[pk] = _id

    # building sqlalchemy query
    context.attach(f'transaction.{backend.name}', backend.begin)
    conn = context.get(f'transaction.{backend.name}')
    table = model.external.name
    table = backend.get_table(model, table)

    qry = table.select()
    for column_name, column_value in query.items():
        id_column = table.c.get(column_name)
        qry = qry.where(id_column == column_value)

    # #executing query
    result = conn.execute(qry)
    row = result.fetchone()

    # preparing results
    data = {}

    for field in model.properties:
        if not field.startswith('_'):
            value = row[field]
            data[field] = value

    additional_data = {
        '_type': model.model_type(),
        '_id': id_
    }
    data.update(additional_data)
    data = flat_dicts_to_nested(data)
    return commands.cast_backend_to_python(context, model, backend, data)
