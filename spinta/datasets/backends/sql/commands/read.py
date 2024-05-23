import logging
from typing import Any
from typing import Iterator

from sqlalchemy.engine.row import RowProxy

from spinta import commands
from spinta.components import Context, Property
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


def _aggregate_values(data, target: Property):
    if target is None or target.list is None:
        return data

    key_path = target.place
    key_parts = key_path.split('.')

    # Drop first part, since if nested prop is part of the list will always be first value
    # ex: from DB we get {"notes": [{"note": 0}]}
    # but after fetching the value we only get [{"note": 0}]
    # so if our place is "notes.note", we need to drop "notes" part
    if len(key_parts) > 1:
        key_parts = key_parts[1:]

    def recursive_collect(sub_data, depth=0):
        if depth < len(key_parts):
            if isinstance(sub_data, list):
                collected = []
                for item in sub_data:
                    collected.extend(recursive_collect(item, depth))
                return collected
            elif isinstance(sub_data, dict) and key_parts[depth] in sub_data:
                return recursive_collect(sub_data[key_parts[depth]], depth + 1)
        else:
            return [sub_data]

        return []

    # Start the recursive collection process
    return recursive_collect(data, 0)


def _get_row_value(context: Context, row: RowProxy, sel: Any) -> Any:
    if isinstance(sel, Selected):
        if isinstance(sel.prep, Expr):
            val = _resolve_expr(context, row, sel)
        elif sel.prep is not NA:
            val = _get_row_value(context, row, sel.prep)
        else:
            if sel.item is not None:
                val = row[sel.item]
                val = _aggregate_values(val, sel.prop)
            else:
                val = None

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
            res = {}

            for key, sel in env.selected.items():
                val = _get_row_value(context, row, sel)
                if sel.prop:
                    if isinstance(sel.prop.dtype, PrimaryKey):
                        val = generate_pk_for_row(sel.prop.model, row, keymap, val)
                    elif isinstance(sel.prop.dtype, Ref):
                        val = handle_ref_key_assignment(keymap, env, val, sel.prop.dtype)
                res[key] = val

            if model.page.is_enabled:
                res['_page'] = get_page_values(env, row)

            res['_type'] = model.model_type()
            res = flat_dicts_to_nested(res)
            res = commands.cast_backend_to_python(context, model, backend, res)
            yield res

