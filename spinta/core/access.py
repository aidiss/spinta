from typing import Iterable
from typing import Union

from spinta.components import Model
from spinta.components import Namespace
from spinta.components import Property
from spinta.core.enums import Access
from spinta.datasets.components import Dataset
from spinta.datasets.components import Resource
from spinta.manifests.components import Manifest
from spinta.utils.enums import enum_by_name


def load_access_param(
    component: Union[Dataset, Resource, Namespace, Model, Property],
    given_access: str,
    parents: Iterable[Union[Manifest, Dataset, Namespace, Model]] = (),
) -> None:
    access = enum_by_name(component, 'access', Access, given_access)

    # If child has higher access than parent, increase parent access.
    if access is not None:
        for parent in parents:
            if parent.access is None or access > parent.access:
                parent.access = access

    component.access = access
    component.given.access = given_access


def link_access_param(
    component: Union[Dataset, Resource, Namespace, Model, Property],
    parents: Iterable[Union[Manifest, Dataset, Namespace, Model]] = (),
) -> None:
    if component.access is None:
        for parent in parents:
            if parent.access and parent.given.access:
                component.access = parent.access
                break
        else:
            component.access = Access.protected
