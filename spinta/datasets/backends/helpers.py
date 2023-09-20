from typing import Any

from spinta.components import Property, Model
from spinta.datasets.enums import Level
from spinta.datasets.keymaps.components import KeyMap
from spinta.exceptions import NotImplementedFeature


def handle_ref_key_assignment(keymap: KeyMap, value: Any, prop: Property) -> dict:
    if not prop.level or prop.level.value > 3:
        val = keymap.encode(prop.dtype.model.model_type(), value)
        val = {'_id': val}
    else:
        if len(prop.dtype.refprops) > 1 and not prop.model.external.unknown_primary_key:
            raise NotImplementedFeature(prop, feature="Ability to have multiple refprops")
        if len(prop.dtype.refprops) == 1:
            val = {
                prop.dtype.refprops[0].name: value
            }
        elif not prop.model.external.unknown_primary_key and prop.model.external.pkeys:
            val = {
                prop.model.external.pkeys[0].name: value
            }
        else:
            val = keymap.encode(prop.dtype.model.model_type(), value)
            val = {'_id': val}
    return val


def generate_pk_for_row(model: Model, row: dict, keymap, pk_val: Any):
    pk = None
    if model.base and ((model.base.level and model.base.level >= Level.identifiable) or not model.base.level):
        pk_val_base = _extract_values_from_row(row, model.base.pk or model.base.parent.external.pkeys)
        key = model.base.parent.model_type()
        if model.base.pk:
            joined = '_'.join(pk.name for pk in model.base.pk)
            key = f'{key}.{joined}'
        pk = keymap.encode(key, pk_val_base)

    pk = keymap.encode(model.model_type(), pk_val, pk)
    if pk and model.required_keymap_properties:
        for combination in model.required_keymap_properties:
            joined = '_'.join(combination)
            key = f'{model.model_type()}.{joined}'
            val = _extract_values_from_row(row, combination)
            keymap.encode(key, val, pk)
    return pk


def _extract_values_from_row(row: dict, keys: list):
    return_list = []
    for key in keys:
        if isinstance(key, Property):
            key = key.name
        return_list.append(row[key])
    if len(return_list) == 1:
        return_list = return_list[0]
    return return_list
