import datetime
import hashlib
import itertools
import json
import logging
import pathlib
import time
from typing import Any
from typing import Dict
from typing import Iterable
from typing import Iterator
from typing import List
from typing import Optional
from typing import Tuple

import msgpack
import requests
import sqlalchemy as sa
import tqdm
from typer import Argument
from typer import Context as TyperContext
from typer import Option
from typer import Exit
from typer import echo

from spinta import exceptions
from spinta import spyna
from spinta.cli.helpers.auth import require_auth
from spinta.cli.helpers.data import ModelRow
from spinta.cli.helpers.data import count_rows
from spinta.cli.helpers.data import iter_model_rows
from spinta.cli.helpers.store import prepare_manifest
from spinta.client import get_access_token
from spinta.components import Action
from spinta.components import Context
from spinta.components import Mode
from spinta.components import Model
from spinta.core.context import configure_context
from spinta.types.namespace import sort_models_by_refs
from spinta.utils.data import take
from spinta.utils.json import fix_data_for_json
from spinta.utils.nestedstruct import flatten
from spinta.utils.units import tobytes
from spinta.utils.units import toseconds

log = logging.getLogger(__name__)


def push(
    ctx: TyperContext,
    manifests: Optional[List[str]] = Argument(None, help=(
        "Source manifest files to copy from"
    )),
    output: Optional[str] = Option(None, '-o', '--output', help=(
        "Output data to a given location, by default outputs to stdout"
    )),
    credentials: str = Option(None, '--credentials', help=(
        "Credentials file, defaults to {config_path}/credentials.cfg"
    )),
    dataset: str = Option(None, '-d', '--dataset', help=(
        "Push only specified dataset"
    )),
    auth: str = Option(None, '-a', '--auth', help=(
        "Authorize as a client, defaults to {default_auth_client}"
    )),
    limit: int = Option(None, help=(
        "Limit number of rows read from each model"
    )),
    chunk_size: str = Option('1m', help=(
        "Push data in chunks (1b, 1k, 2m, ...), default: 1m"
    )),
    stop_time: str = Option(None, help=(
        "Stop pushing after given time (1s, 1m, 2h, ...), by default does not "
        "stops until all data is pushed"
    )),
    stop_row: int = Option(None, help=(
        "Stop after pushing n rows, by default does not stop until all data "
        "is pushed"
    )),
    state: pathlib.Path = Option(None, help=(
        "Save push state into a file, by default state is saved in "
        "{data_path}/pushstate.db"
    )),
    mode: Mode = Option('external', help=(
        "Mode of backend operation, default: external"
    )),
):
    """Push data to external data store"""
    if chunk_size:
        chunk_size = tobytes(chunk_size)

    if stop_time:
        stop_time = toseconds(stop_time)

    context = configure_context(ctx.obj, manifests, mode=mode)
    store = prepare_manifest(context)
    config = context.get('config')

    if credentials:
        credentials = pathlib.Path(credentials)
        if not credentials.exists():
            echo(f"Credentials file {credentials} does not exit.")
            raise Exit(code=1)
    else:
        credentials = config.credentials_file


    manifest = store.manifest
    if dataset and dataset not in manifest.datasets:
        echo(str(exceptions.NodeNotFound(manifest, type='dataset', name=dataset)))
        raise Exit(code=1)

    ns = manifest.namespaces['']

    with context:
        require_auth(context, auth)
        context.attach('transaction', manifest.backend.transaction)

        backends = set()
        for backend in store.backends.values():
            backends.add(backend.name)
            context.attach(f'transaction.{backend.name}', backend.begin)
        for backend in manifest.backends.values():
            backends.add(backend.name)
            context.attach(f'transaction.{backend.name}', backend.begin)
        for dataset_ in manifest.datasets.values():
            for resource in dataset_.resources.values():
                if resource.backend and resource.backend.name not in backends:
                    backends.add(resource.backend.name)
                    context.attach(f'transaction.{resource.backend.name}', resource.backend.begin)
        for keymap in store.keymaps.values():
            context.attach(f'keymap.{keymap.name}', lambda: keymap)

        from spinta.types.namespace import traverse_ns_models

        models = traverse_ns_models(context, ns, Action.SEARCH, dataset)
        models = sort_models_by_refs(models)
        models = list(reversed(list(models)))
        counts = count_rows(context, models, limit)

        if state:
            engine, metadata = _init_push_state(state, models)
            context.attach('push.state.conn', engine.begin)

        rows = iter_model_rows(context, models, counts, limit)
        rows = _prepare_rows_for_push(rows)

        rows = tqdm.tqdm(rows, 'PUSH', ascii=True, total=sum(counts.values()))

        if stop_time:
            rows = _add_stop_time(rows, stop_time)

        if state:
            rows = _check_push_state(context, rows, metadata)

        if stop_row:
            rows = itertools.islice(rows, stop_row)

        rows = _push_to_remote_spinta(rows, output, credentials, chunk_size)

        if state:
            rows = _save_push_state(context, rows, metadata)

        while True:
            try:
                next(rows)
            except StopIteration:
                break
            except Exception:
                log.exception("Error while reading data.")


class _PushRow:
    model: Model
    data: dict
    rev: Optional[str]
    saved: bool = False

    def __init__(self, model: Model, data: Dict[str, Any]):
        self.model = model
        self.data = data
        self.rev = None
        self.saved = False


def _prepare_rows_for_push(rows: Iterable[ModelRow]) -> Iterator[_PushRow]:
    for model, row in rows:
        _id = row['_id']
        _type = row['_type']
        where = {
            'name': 'eq',
            'args': [
                {'name': 'bind', 'args': ['_id']},
                _id,
            ]
        }
        payload = {
            '_op': 'upsert',
            '_type': _type,
            '_id': _id,
            '_where': spyna.unparse(where),
            **{k: v for k, v in row.items() if not k.startswith('_')}
        }
        yield _PushRow(model, payload)


def _push_to_remote_spinta(
    rows: Iterable[_PushRow],
    target: str,
    credentials: pathlib.Path,
    chunk_size: int,
) -> Iterator[_PushRow]:
    echo(f"Get access token from {target}")
    token = get_access_token(target, credentials)

    session = requests.Session()
    session.headers['Content-Type'] = 'application/json'
    session.headers['Authorization'] = f'Bearer {token}'

    prefix = '{"_data":['
    suffix = ']}'
    slen = len(suffix)
    chunk = prefix
    ready = []

    for row in rows:
        data = fix_data_for_json(row.data)
        data = json.dumps(data, ensure_ascii=False)
        if ready and len(chunk) + len(data) + slen > chunk_size:
            yield from _send_and_receive(session, target, ready, chunk + suffix)
            chunk = prefix
            ready = []
        chunk += (',' if ready else '') + data
        ready.append(row)

    if ready:
        yield from _send_and_receive(session, target, ready, chunk + suffix)


def _send_and_receive(
    session: requests.Session,
    target: str,
    rows: List[_PushRow],
    data: str,
):
    data = data.encode('utf-8')

    try:
        resp = session.post(target, data=data)
        resp.raise_for_status()
        data = resp.json()['_data']
    except Exception:
        log.exception("Error when sending and receiving data.")
        return

    assert len(rows) == len(data), (
        f"len(sent) = {len(rows)}, len(received) = {len(data)}"
    )
    for sent, recv in zip(rows, data):
        assert sent.data['_id'] == recv['_id'], (
            f"sent._id = {sent.data['_id']}, received._id = {recv['_id']}"
        )
        yield sent


def _add_stop_time(rows, stop):
    start = time.time()
    for row in rows:
        yield row
        if time.time() - start > stop:
            break


def _init_push_state(
    file: pathlib.Path,
    models: List[Model],
) -> Tuple[sa.engine.Engine, sa.MetaData]:
    engine = sa.create_engine(f'sqlite:///{file}')
    metadata = sa.MetaData(engine)
    for model in models:
        table = sa.Table(
            model.name, metadata,
            sa.Column('id', sa.Unicode, primary_key=True),
            sa.Column('rev', sa.Unicode),
            sa.Column('pushed', sa.DateTime),
        )
        table.create(checkfirst=True)
    return engine, metadata


def _get_model_type(row: _PushRow) -> str:
    return row.data['_type']


def _check_push_state(
    context: Context,
    rows: Iterable[_PushRow],
    metadata: sa.MetaData,
):
    conn = context.get('push.state.conn')

    for model_type, group in itertools.groupby(rows, key=_get_model_type):
        table = metadata.tables[model_type]

        query = sa.select([table.c.id, table.c.rev])
        saved = {
            state[table.c.id]: state[table.c.rev]
            for state in conn.execute(query)
        }

        for row in group:
            _id = row.data['_id']

            rev = fix_data_for_json(take(row.data))
            rev = flatten([rev])
            rev = [[k, v] for x in rev for k, v in sorted(x.items())]
            rev = msgpack.dumps(rev, strict_types=True)
            rev = hashlib.sha1(rev).hexdigest()

            row.rev = rev
            row.saved = _id in saved

            if saved.get(_id) == row.rev:
                continue  # Nothing has changed.

            yield row


def _save_push_state(
    context: Context,
    rows: Iterable[_PushRow],
    metadata: sa.MetaData,
):
    conn = context.get('push.state.conn')
    for row in rows:
        table = metadata.tables[row.data['_type']]
        if row.saved:
            conn.execute(
                table.update().
                where(table.c.id == row.data['_id']).
                values(
                    id=row.data['_id'],
                    rev=row.rev,
                    pushed=datetime.datetime.now(),
                )
            )
        else:
            conn.execute(
                table.insert().
                values(
                    id=row.data['_id'],
                    rev=row.rev,
                    pushed=datetime.datetime.now(),
                )
            )
        yield row
