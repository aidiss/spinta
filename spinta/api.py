import os
import shutil
import uuid
import ruamel.yaml
from typing import Type

import pkg_resources as pres
import logging

from authlib.common.errors import AuthlibHTTPError
from authlib.oauth2.rfc6750.errors import InsufficientScopeError

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import Response, JSONResponse
from starlette.responses import RedirectResponse
from starlette.templating import Jinja2Templates
from starlette.staticfiles import StaticFiles
from starlette.routing import Route, Mount
from starlette.middleware import Middleware

from spinta import components, commands
from spinta.auth import AuthorizationServer, check_scope, query_client, get_clients_list, get_client_file_path, \
    client_exists, create_client_file
from spinta.auth import ResourceProtector
from spinta.auth import BearerTokenValidator
from spinta.auth import get_auth_request
from spinta.auth import get_auth_token
from spinta.commands import prepare, get_version
from spinta.components import Context
from spinta.exceptions import BaseError, MultipleErrors, error_response, InsufficientPermission, \
    UnknownPropertyInRequest, ClientAlreadyExists, InsufficientPermissionForUpdate
from spinta.middlewares import ContextMiddleware
from spinta.urlparams import Version
from spinta.urlparams import get_response_type
from spinta.utils import passwords
from spinta.utils.response import create_http_response
from spinta.accesslog import create_accesslog
from spinta.exceptions import NoAuthServer
from spinta.utils.types import is_str_uuid

log = logging.getLogger(__name__)

templates = Jinja2Templates(
    directory=pres.resource_filename('spinta', 'templates'),
)


async def favicon(request: Request):
    return Response('', media_type='text/plain', status_code=404)


async def robots(request: Request):
    content = '\n'.join([
        'User-Agent: *',
        'Allow: /',
        '',
    ])
    return Response(content, media_type='text/plain', status_code=200)


async def version(request: Request):
    version = get_version(request.state.context)
    return JSONResponse(version)


async def auth_token(request: Request):
    auth_server = request.state.context.get('auth.server')
    if auth_server.enabled():
        return auth_server.create_token_response({
            'method': request.method,
            'url': str(request.url.replace(query='')),
            'body': dict(await request.form()),
            'headers': request.headers,
        })
    else:
        raise NoAuthServer()


def _auth_client_context(request: Request) -> Context:
    context: Context = request.state.context
    context.set('auth.request', get_auth_request({
        'method': request.method,
        'url': str(request.url.replace(query='')),
        'body': None,
        'headers': request.headers,
    }))
    context.set('auth.token', get_auth_token(context))
    context.attach('accesslog', create_accesslog, context, loaders=(
        context.get('store'),
        context.get("auth.token"),
    ))
    return context


async def auth_clients_add(request: Request):
    try:
        context = _auth_client_context(request)
        check_scope(context, 'auth_clients')
        config = context.get('config')
        commands.load(context, config)

        path = config.config_path / 'clients'
        data = await request.json()
        for key in data.keys():
            if key not in ('client_id', 'secret', 'scopes'):
                raise UnknownPropertyInRequest(property=key, properties=('client_id', 'secret', 'scopes'))

        client_id = ("client_id" in data.keys() and data["client_id"]) or str(uuid.uuid4())
        while client_exists(path, client_id):
            if "client_id" in data.keys() and data["client_id"]:
                raise ClientAlreadyExists(client_id=client_id)
            client_id = str(uuid.uuid4())

        client_file, client_ = create_client_file(
            path,
            client_id,
            data["secret"] if "secret" in data.keys() else None,
            data["scopes"] if "scopes" in data.keys() else None,
        )

        return JSONResponse({
            "client_id": client_["client_id"],
            "scopes": client_["scopes"]
        })

    except InsufficientScopeError:
        raise InsufficientPermission()


async def auth_clients_get_all(request: Request):
    try:
        context = _auth_client_context(request)
        check_scope(context, 'auth_clients')
        config = context.get('config')
        commands.load(context, config)

        path = config.config_path / 'clients'
        ids = get_clients_list(path)
        return_values = []
        for client_path in ids:
            client = query_client(context, client_path)
            return_values.append({
                "client_id": client.id
            })
        return JSONResponse(return_values)

    except InsufficientScopeError:
        raise InsufficientPermission()


async def auth_clients_get_specific(request: Request):
    try:
        context = _auth_client_context(request)
        token = context.get('auth.token')
        client_id = request.path_params["client"]
        if client_id != token.get_client_id():
            check_scope(context, 'auth_clients')
        config = context.get('config')
        commands.load(context, config)

        client = query_client(context, client_id)
        return_value = {
            "client_id": client.id,
            "scopes": list(client.scopes)
        }
        return JSONResponse(return_value)

    except InsufficientScopeError:
        raise InsufficientPermission()


async def auth_clients_delete_specific(request: Request):
    try:
        context = _auth_client_context(request)
        client_id = request.path_params["client"]
        check_scope(context, 'auth_clients')
        config = context.get('config')
        commands.load(context, config)

        path = config.config_path / 'clients'
        query_client(context, client_id)
        if client_exists(path, client_id):
            remove_path = get_client_file_path(config.config_path / 'clients', client_id)
            os.remove(remove_path)
        return Response(status_code=204)

    except InsufficientScopeError:
        raise InsufficientPermission()


async def auth_clients_patch_specific(request: Request):
    try:
        context = _auth_client_context(request)
        token = context.get('auth.token')
        client_id = request.path_params["client"]

        has_permission = True
        try:
            check_scope(context, 'auth_clients')
        except InsufficientScopeError:
            has_permission = False
        if client_id != token.get_client_id() and not has_permission:
            raise InsufficientScopeError

        data = await request.json()
        for key in data.keys():
            if key not in ('client_id', 'secret', 'scopes'):
                properties = ('secret')
                if has_permission:
                    properties = ('client_id', 'secret', 'scopes')
                raise UnknownPropertyInRequest(property=key, properties=properties)
            elif key != 'secret' and not has_permission:
                raise InsufficientPermissionForUpdate(field=key)

        config = context.get('config')
        commands.load(context, config)
        path = config.config_path / 'clients'

        client = query_client(context, client_id)
        client_id = data["client_id"] if ("client_id" in data.keys() and data["client_id"]) else client.id

        old_path = get_client_file_path(path, client.id)
        new_path = get_client_file_path(path, client_id)
        if client_id != client.id:
            if client_exists(path, client_id):
                raise ClientAlreadyExists(client_id=client_id)
            if is_str_uuid(client_id):
                os.makedirs(path / 'id' / client_id[:2] / client_id[2:4], exist_ok=True)
            shutil.move(old_path, new_path)

        yml = ruamel.yaml.YAML()
        yml.indent(mapping=2, sequence=4, offset=2)
        yml.width = 80
        yml.explicit_start = False

        scopes = data["scopes"] if ("scopes" in data.keys()) else client.scopes
        if not scopes:
            scopes = []
        secret = passwords.crypt(data["secret"]) if ("secret" in data.keys() and data["secret"]) else client.secret_hash

        with open(new_path) as fp:
            data = yml.load(fp)
        data["client_id"] = client_id
        data["client_secret_hash"] = secret
        data["scopes"] = list(scopes)
        yml.dump(data, new_path)

        return_value = {
            "client_id": data["client_id"],
            "scopes": data["scopes"]
        }
        return JSONResponse(return_value)

    except InsufficientScopeError:
        raise InsufficientPermission()


async def homepage(request: Request):
    context: Context = request.state.context

    context.set('auth.request', get_auth_request({
        'method': request.method,
        'url': str(request.url.replace(query='')),
        'body': None,
        'headers': request.headers,
    }))
    context.set('auth.token', get_auth_token(context))

    config = context.get('config')
    UrlParams: Type[components.UrlParams]
    UrlParams = config.components['urlparams']['component']
    params: UrlParams = prepare(context, UrlParams(), Version(), request)

    context.attach('accesslog', create_accesslog, context, loaders=(
        context.get('store'),
        context.get("auth.token"),
        request,
        params,
    ))

    return await create_http_response(context, params, request)


async def error(request, exc):
    log.exception('Error: %s', exc)

    headers = {}

    if isinstance(exc, MultipleErrors):
        # TODO: probably there should be a more sophisticated way to get status
        #       code from error list.
        # XXX:  On the other hand, mixing status code is probably not a good
        #       idea, so instead, MultipleErrors must have one status code for
        #       all errors. And that should be enforced by MultipleErrors. If
        #       an exception comes in with a different status code, then this
        #       should be an error.
        status_code = exc.errors[0].status_code
        errors = [error_response(e) for e in exc.errors]

    elif isinstance(exc, BaseError):
        status_code = exc.status_code
        errors = [error_response(exc)]
        headers = exc.headers
    else:
        if isinstance(exc, HTTPException):
            status_code = exc.status_code
            message = exc.detail
        elif isinstance(exc, AuthlibHTTPError):
            status_code = exc.status_code
            if exc.description:
                # show overridden description
                message = str(f'{exc.error}: {exc.description}')
            elif exc.get_error_description():
                # show dynamic description
                message = str(f'{exc.error}: {exc.get_error_description()}')
            else:
                # if no description, show plain error string
                message = exc.error
            headers['www-authenticate'] = f'Bearer error="{exc.error}"'
        else:
            status_code = 500
            message = str(exc)

        errors = [
            {
                'code': type(exc).__name__,
                'message': message,
            }
        ]

    response = {'errors': errors}

    fmt = get_response_type(request.state.context, request)
    if fmt == 'json':
        return JSONResponse(
            response,
            status_code=status_code,
            headers=headers,
        )
    else:
        response = {
            **response,
            'request': request,
        }
        return templates.TemplateResponse(
            'error.html',
            response,
            status_code=status_code,
            headers=headers,
        )


async def srid_check(request: Request):
    from shapely.geometry import Point
    from geoalchemy2.shape import from_shape
    from spinta.backends.postgresql.types.geometry.helpers import get_osm_link

    srid = request.path_params['srid']
    x = request.path_params['x']
    y = request.path_params['y']

    point = Point(x, y)
    wkb = from_shape(point, srid)
    osm_link = get_osm_link(wkb, srid)
    return RedirectResponse(url=osm_link)


def init(context: Context):
    config = context.get('config')

    routes = [
        Route('/robots.txt', robots, methods=['GET']),
        Route('/favicon.ico', favicon, methods=['GET']),
        Route('/version', version, methods=['GET']),
        Route('/auth/token', auth_token, methods=['POST']),
        Route('/_srid/{srid:int}/{x:float}/{y:float}', srid_check, methods=['GET']),
        Route('/auth/clients', auth_clients_get_all, methods=['GET']),
        Route('/auth/clients', auth_clients_add, methods=['POST']),
        Route('/auth/clients/{client}', auth_clients_get_specific, methods=['GET']),
        Route('/auth/clients/{client}', auth_clients_delete_specific, methods=['DELETE']),
        Route('/auth/clients/{client}', auth_clients_patch_specific, methods=['PATCH'])
    ]

    if config.docs_path:
        routes += [
            Mount('/docs', StaticFiles(directory=config.docs_path, html=True)),
        ]

    # This route matches everything, so it must be added last.
    routes += [
        Route('/{path:path}', homepage, methods=[
            'HEAD',
            'GET',
            'POST',
            'PUT',
            'PATCH',
            'DELETE'
        ]),
    ]

    middleware = [
        Middleware(ContextMiddleware, context=context)
    ]

    exception_handlers = {
        Exception: error,
        BaseError: error,
        MultipleErrors: error,
        AuthlibHTTPError: error,
        HTTPException: error,
    }

    app = Starlette(
        debug=config.debug,
        routes=routes,
        middleware=middleware,
        exception_handlers=exception_handlers,
    )

    # Add context to state in order to pass it to request handlers
    app.state.context = context

    context.bind('auth.server', AuthorizationServer, context)
    context.bind(
        'auth.resource_protector',
        ResourceProtector,
        context,
        BearerTokenValidator,
    )

    return app
