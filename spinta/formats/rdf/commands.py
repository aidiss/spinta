import datetime
from decimal import Decimal
from typing import Any
from typing import Dict
from typing import List
from typing import Optional

from lxml import etree
from lxml.etree import Element, QName
from starlette.requests import Request
from starlette.responses import StreamingResponse

from spinta import commands
from spinta.backends.components import SelectTree
from spinta.backends.helpers import get_model_reserved_props
from spinta.backends.helpers import select_model_props
from spinta.components import Action
from spinta.components import Context
from spinta.components import Model
from spinta.components import UrlParams
from spinta.dimensions.prefix.components import UriPrefix
from spinta.formats.components import Format
from spinta.formats.html.helpers import get_model_link
from spinta.formats.rdf.components import Rdf
from spinta.types.datatype import DataType
from spinta.types.datatype import File
from spinta.types.datatype import Ref
from spinta.types.datatype import Date
from spinta.types.datatype import Time
from spinta.types.datatype import DateTime
from spinta.types.datatype import Number
from spinta.utils.schema import NotAvailable

RDF = "rdf"
DESCRIPTION = "Description"


def _get_available_prefixes(model: Model) -> dict:
    prefixes = {}
    if model.manifest.datasets.get(model.ns.name):
        prefixes = model.manifest.datasets.get(model.ns.name).prefixes
        for key, val in prefixes.items():
            if isinstance(val, UriPrefix):
                prefixes[key] = val.uri
    return prefixes


def _get_attribute_name(
    name: str,
    prefix: str,
    available_prefixes: dict
) -> (str | QName):
    if prefix and available_prefixes.get(prefix):
        attribute_name = QName(available_prefixes.get(prefix), name)
    else:
        attribute_name = name
    return attribute_name


def _get_about_and_type_attributes(
    model: Model,
    data: dict,
    about_name: str | QName,
    type_name: str | QName
) -> (dict, dict):
    attributes = {}
    if '_id' in data:
        _id = data.pop('_id')
        if _id.text:
            attributes[about_name] = get_model_link(model, pk=_id.text)
    if '_type' in data:
        _type = data.pop('_type')
        if _type.text:
            attributes[type_name] = _type.text
    return data, attributes


def _get_prefix_and_name(uri: str) -> (str, str):
    prefix = name = None
    if uri and ':' in uri:
        prefix = uri.split(':')[0]
        name = uri.split(':')[1]
    return prefix, name


def _create_element(
    name: str | QName,
    text: str = None,
    base: str = None,
    nsmap: dict = None,
    attributes: dict = None,
    children: List[Element] = None,
) -> Element:
    if attributes is None:
        attributes = {}
    if children is None:
        children = []

    if nsmap is not None:
        elem = Element(name, nsmap=nsmap)
    else:
        elem = Element(name)

    if base is not None:
        elem.base = base
    if text is not None:
        elem.text = text
    for key, value in attributes.items():
        elem.attrib[key] = value
    for child in children:
        elem.append(child)
    return elem


def _create_model_element(
    model: Model,
    data: dict
):
    prefixes = _get_available_prefixes(model)
    about_name = _get_attribute_name('about', RDF, prefixes)
    type_name = _get_attribute_name('type', RDF, prefixes)
    model_prefix, model_name = _get_prefix_and_name(model.uri)

    data, attributes = _get_about_and_type_attributes(
        model,
        data,
        about_name,
        type_name
    )
    if model_name:
        name = _get_attribute_name(model_name, model_prefix, prefixes)
    else:
        name = _get_attribute_name(model.basename, model_prefix, prefixes)
    if not isinstance(name, QName):
        name = _get_attribute_name(DESCRIPTION, RDF, prefixes)
        attributes[type_name] = model.model_type()

    return _create_element(
        name=name,
        attributes=attributes,
        children=list(data.values())
    )


@commands.render.register(Context, Request, Model, Rdf)
def render(
    context: Context,
    request: Request,
    model: Model,
    fmt: Rdf,
    *,
    action: Action,
    params: UrlParams,
    data,
    status_code: int = 200,
    headers: Optional[dict] = None,
):
    headers = headers or {}
    headers['Content-Disposition'] = f'attachment; filename="{model.basename}.rdf"'

    prefixes = _get_available_prefixes(model)
    root_name = _get_attribute_name(RDF.capitalize(), RDF, prefixes)
    root = _create_element(
        name=root_name,
        base=str(request.base_url),
        nsmap=prefixes
    )
    if action == Action.GETONE:
        elem = _create_model_element(model, data)
        root.append(elem)
    else:
        for row in data:
            elem = _create_model_element(model, row)
            root.append(elem)

    return StreamingResponse(
        _stream(root, root_name),
        status_code=status_code,
        media_type=fmt.content_type,
        headers=headers,
    )


async def _stream(root, name):
    namespaces = ""
    for key, val in root.nsmap.items():
        namespaces += f'xmlns:{key}="{val}" '
    if root.base:
        namespaces += f'xml:base="{root.base}" '
    if isinstance(name, QName):
        name = f"{root.prefix}:{name.localname}"
    yield f"<{name} {namespaces}>\n"
    for elem in root:
        row = etree.tostring(elem, encoding="unicode", pretty_print=True)
        for key, val in elem.nsmap.items():
            row = row.replace(f'xmlns:{key}="{val}" ', "")
        yield row
    yield f"</{name}>\n"


@commands.prepare_data_for_response.register(Context, Model, Rdf, dict)
def prepare_data_for_response(
    context: Context,
    model: Model,
    fmt: Rdf,
    value: dict,
    *,
    action: Action,
    select: SelectTree,
    prop_names: List[str],
) -> dict:
    value = value.copy()
    reserved = get_model_reserved_props(action)

    available_prefixes = _get_available_prefixes(model)

    value['_available_prefixes'] = available_prefixes
    value['_about_name'] = _get_attribute_name('about', RDF, available_prefixes)
    value['_resource_name'] = _get_attribute_name('resource', RDF, available_prefixes)
    value['_type_name'] = _get_attribute_name('type', RDF, available_prefixes)

    data = {}
    for prop, val, sel in select_model_props(
        model,
        prop_names,
        value,
        select,
        reserved,
    ):
        prefix, name = _get_prefix_and_name(prop.uri)
        if name:
            value['_elem_name'] = _get_attribute_name(name, prefix, available_prefixes)
        else:
            value['_elem_name'] = _get_attribute_name(prop.name, prefix, available_prefixes)

        elem = commands.prepare_dtype_for_response(
            context,
            fmt,
            prop.dtype,
            val,
            data=value,
            action=action,
            select=sel
        )
        data[prop.name] = elem
    return data


@commands.prepare_dtype_for_response.register(Context, Rdf, DataType, type(None))
def prepare_dtype_for_response(
    context: Context,
    fmt: Rdf,
    dtype: DataType,
    value: None,
    *,
    data: Dict[str, Any],
    action: Action,
    select: dict = None
):
    return _create_element(
        name=data['_elem_name']
    )


@commands.prepare_dtype_for_response.register(Context, Rdf, DataType, object)
def prepare_dtype_for_response(
    context: Context,
    fmt: Rdf,
    dtype: DataType,
    value: Any,
    *,
    data: Dict[str, Any],
    action: Action,
    select: dict = None
):
    return _create_element(
        name=data['_elem_name'],
        text=str(value)
    )


@commands.prepare_dtype_for_response.register(Context, Rdf, Date, datetime.date)
def prepare_dtype_for_response(
    context: Context,
    fmt: Rdf,
    dtype: Date,
    value: datetime.date,
    *,
    data: Dict[str, Any],
    action: Action,
    select: dict = None
):
    return _create_element(
        name=data['_elem_name'],
        text=value.isoformat()
    )


@commands.prepare_dtype_for_response.register(Context, Rdf, Time, datetime.time)
def prepare_dtype_for_response(
    context: Context,
    fmt: Rdf,
    dtype: Time,
    value: datetime.time,
    *,
    data: Dict[str, Any],
    action: Action,
    select: dict = None
):
    return _create_element(
        name=data['_elem_name'],
        text=value.isoformat()
    )


@commands.prepare_dtype_for_response.register(Context, Rdf, DateTime, datetime.datetime)
def prepare_dtype_for_response(
    context: Context,
    fmt: Rdf,
    dtype: DateTime,
    value: datetime.datetime,
    *,
    data: Dict[str, Any],
    action: Action,
    select: dict = None
):
    return _create_element(
        name=data['_elem_name'],
        text=value.isoformat()
    )


@commands.prepare_dtype_for_response.register(Context, Rdf, Number, Decimal)
def prepare_dtype_for_response(
    context: Context,
    fmt: Rdf,
    dtype: Number,
    value: Decimal,
    *,
    data: Dict[str, Any],
    action: Action,
    select: dict = None
):
    return _create_element(
        name=data['_elem_name'],
        text=str(value)
    )


@commands.prepare_dtype_for_response.register(Context, Rdf, Ref, (dict, str, type(None)))
def prepare_dtype_for_response(
    context: Context,
    fmt: Rdf,
    dtype: File,
    value,
    *,
    data: Dict[str, Any],
    action: Action,
    select: dict = None
):
    super_ = commands.prepare_dtype_for_response[Context, Format, Ref, dict]
    data_dict = super_(context, fmt, dtype, value, data=data, action=action, select=select)
    prefixes = data['_available_prefixes']
    attributes = {}
    children = []

    if len(data_dict) == 1 and '_id' in data_dict:
        if data_dict['_id'].text:
            attributes[data['_resource_name']] = get_model_link(
                dtype.model,
                pk=data_dict['_id'].text
            )
    else:
        # TODO: write tests for denormalized form after task #216 is done

        if isinstance(data['_elem_name'], QName):
            prefix, name = _get_prefix_and_name(dtype.model.uri)
            if name:
                ref_model_name = _get_attribute_name(name, prefix, prefixes)
            else:
                ref_model_name = _get_attribute_name(dtype.model.basename, prefix, prefixes)
            data_dict, ref_model_attrs = _get_about_and_type_attributes(
                dtype.model,
                data_dict,
                data['_about_name'],
                data['_type_name']
            )
            ref_model_elem = _create_element(
                name=ref_model_name,
                attributes=ref_model_attrs,
                children=list(data_dict.values())
            )
            children.append(ref_model_elem)
        else:
            data_dict['_type'] = _create_element(
                name='_type',
                text=dtype.model.model_type()
            )
            data_dict, description_attrs = _get_about_and_type_attributes(
                dtype.model,
                data_dict,
                data['_about_name'],
                data['_type_name']
            )
            description_elem = _create_element(
                name=_get_attribute_name(DESCRIPTION, RDF, prefixes),
                attributes=description_attrs,
                children=list(data_dict.values())
            )
            children.append(description_elem)

    return _create_element(
        name=data['_elem_name'],
        attributes=attributes,
        children=children
    )


@commands.prepare_dtype_for_response.register(Context, Rdf, File, NotAvailable)
def prepare_dtype_for_response(
    context: Context,
    fmt: Rdf,
    dtype: File,
    value: NotAvailable,
    *,
    data: Dict[str, Any],
    action: Action,
    select: dict = None
):
    return _create_element(
        name=data['_elem_name']
    )


@commands.prepare_dtype_for_response.register(Context, Rdf, File, dict)
def prepare_dtype_for_response(
    context: Context,
    fmt: Rdf,
    dtype: File,
    value: Dict[str, Any],
    *,
    data: Dict[str, Any],
    action: Action,
    select: dict = None
):
    if value['_id'] is not None:
        attributes = {
            data['_about_name']: get_model_link(
                dtype.prop.model,
                pk=data['_id'],
                prop=dtype.prop.name
            )
        }
        elem = _create_element(
            name=data['_elem_name'],
            attributes=attributes
        )
    else:
        elem = _create_element(name=data['_elem_name'])
    return elem
