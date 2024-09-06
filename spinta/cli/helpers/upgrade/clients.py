import os
import pathlib
import uuid
from typing import Any
from typer import echo
import ruamel.yaml

from spinta.cli.helpers.upgrade.components import UPGRADE_CLIENTS_SCRIPT
from spinta.cli.helpers.upgrade.helpers import script_destructive_warning
from spinta.components import Context, Config
from spinta.core.config import DEFAULT_CONFIG_PATH
from spinta.utils.config import get_clients_path, get_keymap_path, get_id_path, get_helpers_path
from spinta.utils.types import is_str_uuid

from authlib.oauth2.rfc6749.errors import InvalidClientError

yaml = ruamel.yaml.YAML(typ="safe")

yml = ruamel.yaml.YAML()
yml.indent(mapping=2, sequence=4, offset=2)
yml.width = 80
yml.explicit_start = False


def migrate_clients(context: Context, destructive: bool, **kwargs: Any):
    config: Config = context.get('config')
    config_path = config.config_path or DEFAULT_CONFIG_PATH
    config_path = pathlib.Path(config_path)

    clients_path = get_clients_path(config_path)

    helpers_path = get_helpers_path(clients_path)
    keymap_path = get_keymap_path(clients_path)
    id_path = get_id_path(clients_path)

    # Ensure clients path exists
    os.makedirs(clients_path, exist_ok=True)

    # Ensure helpers path exists
    os.makedirs(helpers_path, exist_ok=True)

    # Ensure keymap.yml exists
    keymap_path.touch(exist_ok=True)

    # Ensure id path exists
    os.makedirs(id_path, exist_ok=True)

    keymap = yaml.load(keymap_path)
    if keymap is None:
        keymap = {}

    if destructive:
        script_destructive_warning(UPGRADE_CLIENTS_SCRIPT, "override already migrated files with old ones")

    items = os.listdir(clients_path)
    for item in items:
        if item.endswith('.yml'):
            status = _migrate_client_file(
                client_file_path=clients_path / item,
                id_path=id_path,
                destructive=destructive,
                keymap=keymap
            )
            echo(f"\tMigrating {item.ljust(40)}\tStatus: {status}")

    _recreate_keymap(
        id_path=id_path,
        keymap_path=keymap_path
    )


def _recreate_keymap(
    id_path: pathlib.Path,
    keymap_path: pathlib.Path
):
    id_items = os.listdir(id_path)
    keymap = {}
    for id0 in id_items:
        if len(id0) != 2:
            continue

        id0_items = os.listdir(id_path / id0)
        for id1 in id0_items:
            if len(id1) != 2:
                continue

            id1_items = os.listdir(id_path / id0 / id1)
            for uuid_item in id1_items:
                if uuid_item.endswith('.yml') and len(uuid_item) == 36:
                    try:
                        data = yaml.load(id_path / id0 / id1 / uuid_item)
                        keymap[data["client_name"]] = data["client_id"]
                    except FileNotFoundError:
                        raise (InvalidClientError(description='Invalid client id or secret'))

    echo(f"Created keymap with {len(keymap)} users")
    yml.dump(keymap, keymap_path)


def _generate_new_file_path(
    id_path: pathlib.Path,
    client_id: str,
    with_yml: bool = True
) -> pathlib.Path:
    folder_path = id_path / client_id[:2] / client_id[2:4]
    if with_yml:
        return folder_path / f'{client_id[4:]}.yml'
    return folder_path


def _validate_file_structure(
    data: dict,
    old: bool = True
) -> bool:
    # Data must be dict
    if not isinstance(data, dict):
        return False

    # All versions must contain 'client_id' field
    if 'client_id' not in data:
        return False
    else:
        # If it's not old, then 'client_id' must be uuid
        if not old and is_str_uuid(data["client_id"]):
            return False

    # All versions must contain 'client_secret_hash'
    if 'client_secret_hash' not in data:
        return False

    # All versions must contain 'scopes
    if 'scopes' not in data:
        return False

    # Only new version must contain 'client_name'
    if not old and 'client_name' not in data:
        return False

    return True


def _migrate_client_file(
    client_file_path: pathlib.Path,
    id_path: pathlib.Path,
    destructive: bool,
    keymap: dict
) -> str:
    try:
        old_data = yaml.load(client_file_path)
    except FileNotFoundError:
        raise (InvalidClientError(description='Could not open client file'))

    if not _validate_file_structure(old_data):
        return "FAILED (INVALID STRUCTURE)"

    # Check if new valid client file exists, if so, skip it
    if old_data['client_id'] in keymap:
        keymap_value = keymap[old_data['client_id']]
        new_file_path = _generate_new_file_path(
            id_path,
            keymap_value
        )
        if new_file_path.exists() and not destructive:
            return "SKIPPED (ALREADY MIGRATED)"

    old_data["client_name"] = old_data["client_id"]
    if not is_str_uuid(old_data["client_id"]):
        old_data["client_id"] = str(uuid.uuid4())
    new_client_file = old_data["client_id"]

    new_path = _generate_new_file_path(id_path, new_client_file)
    os.makedirs(_generate_new_file_path(id_path, new_client_file, False), exist_ok=True)

    data = {
        "client_id": old_data["client_id"],
        "client_name": old_data["client_name"],
        "client_secret_hash": old_data["client_secret_hash"],
        "scopes": old_data["scopes"]
    }
    yml.dump(data, new_path)
    return "SUCCESS"


def cli_requires_clients_migration(context: Context, **kwargs: Any) -> bool:
    config: Config = context.get('config')
    config_path = config.config_path or DEFAULT_CONFIG_PATH
    config_path = pathlib.Path(config_path)

    clients_path = get_clients_path(config_path)
    return requires_client_migration(clients_path)


def requires_client_migration(clients_path: pathlib.Path) -> bool:
    # Cannot apply any migrations, if there are no files
    if not clients_path.exists():
        return False

    items = os.listdir(clients_path)
    contains_yml = any([item.endswith('.yml') for item in items])
    if contains_yml:
        keymap_path = get_keymap_path(clients_path)

        # If there are yml files, there should /helpers/keymap.yml
        if not keymap_path.exists():
            return True

        # If there are yml files, there should be /id folder
        id_path = get_id_path(clients_path)
        if not id_path.exists():
            return True

        keymap = yaml.load(keymap_path)
        # Keymap should not be empty or None
        if not keymap:
            return True

        ids = os.listdir(id_path)
        # id folder should contain subfolders that are 2 characters long (meaning there are migrated files)
        if not any(len(item) == 2 for item in ids):
            return True

    return False
