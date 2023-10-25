from typing import Dict
from typing import Iterator
from typing import List
from typing import Optional
from typing import Set

import itertools


def flatten(value, sep='.', array_sep='[]'):
    value, lists = _flatten(value, sep, array_sep)

    if value is None:
        for k, vals in lists:
            for v in vals:
                if v is not None:
                    yield from flatten(v, sep)

    elif lists:
        keys, lists = zip(*lists)
        for vals in itertools.product(*lists):
            val = {
                sep.join(k): v
                for k, v in zip(keys, vals) if v is not None
            }
            val.update(value)
            yield from flatten(val, sep)

    else:
        yield value


def _flatten(value, sep, array_sep, key=()):
    if isinstance(value, dict):
        data = {}
        lists = []
        for k, v in value.items():
            if v is not None:
                v, more = _flatten(v, sep, array_sep, key + (k,))
                data.update(v or {})
                lists += more
        return data, lists

    elif isinstance(value, (list, Iterator)):
        if value:
            if len(key) > 0:
                key = list(key)
                key[-1] = f'{key[-1]}{array_sep}'
                key = tuple(key)
            return None, [(key, value)] if value is not None else []
        else:
            return None, []

    else:
        return {sep.join(key): value} if value is not None else {}, []


def build_select_tree(select: List[str]) -> Dict[str, Set[Optional[str]]]:
    tree: Dict[str, Set[Optional[str]]] = {}
    for name in select:
        split = name, None
        while len(split) == 2:
            name, node = split
            if name not in tree:
                tree[name] = set()
            if node:
                tree[name].add(node)
            split = name.rsplit('.', 1)
    return tree


def flat_dicts_to_nested(value):
    res = {}
    for k, v in dict(value).items():
        names = k.split('.')
        vref = res
        for name in names[:-1]:
            if name not in vref:
                vref[name] = {}
            vref = vref[name]
        vref[names[-1]] = v
    return res


def flatten_value(value, sep=".", key=()):
    value, _ = _flatten(value, sep, key)
    return value
