import logging
from pathlib import Path
from typing import List
from typing import Optional

from typer import Argument
from typer import Context as TyperContext
from typer import Exit
from typer import Option
from typer import echo

from spinta import commands
from spinta.cli.helpers.store import load_store
from spinta.cli.helpers.store import prepare_manifest
from spinta.components import Mode
from spinta.core.context import configure_context

log = logging.getLogger(__name__)


def run(
    ctx: TyperContext,
    manifests: Optional[List[str]] = Argument(None, help=(
        "Manifest files to load"
    )),
    mode: Mode = Option('internal', help="Mode of backend operation"),
    host: str = Option('127.0.0.1', help="Run server on given host"),
    port: int = Option(8000, help="Run server on given port"),
):
    """Run development server"""
    import uvicorn
    import spinta.api

    context = configure_context(ctx.obj, manifests, mode=mode)
    prepare_manifest(context)
    app = spinta.api.init(context)

    echo("Spinta has started!")
    uvicorn.run(app, host=host, port=port)


def wait(
    ctx: TyperContext,
    seconds: Optional[int] = Argument(None),
):
    """Wait while all backends are up"""
    context = ctx.obj
    store = load_store(context)
    if not commands.wait(context, store, seconds=seconds, verbose=True):
        raise Exit(code=1)
