"""
DRACOON Commander
A CLI DRACOON client

"""

__version__ = "0.1.0"

# std imports
import sys
import platform
import asyncio
from datetime import datetime

# external imports
from dracoon import DRACOON, OAuth2ConnectionType
from dracoon.nodes.models import NodeType
from dracoon.errors import (
    HTTPConflictError,
    HTTPForbiddenError,
    HTTPStatusError,
    InvalidPathError,
    InvalidFileError,
    FileConflictError,
    HTTPNotFoundError,
)
import typer


# internal imports
from dccmd.main.util import (
    parse_base_url,
    parse_file_name,
    parse_path,
    parse_new_path,
    format_error_message,
    format_success_message,
    format_and_print_node,
    graceful_exit,
    to_readable_size,
)
from dccmd.main.auth import auth_app
from dccmd.main.auth.client import client_app, register_client, remove_client
from dccmd.main.auth.util import init_dracoon, is_dracoon_url
from dccmd.main.auth.credentials import (
    get_credentials,
    get_crypto_credentials,
    store_crypto_credentials,
    store_client_credentials,
    get_client_credentials,
    delete_client_credentials,
    delete_credentials,
    delete_crypto_credentials,
)

from dccmd.main.crypto import crypto_app
from dccmd.main.crypto.keys import distribute_missing_keys
from dccmd.main.crypto.util import get_keypair, init_keypair
from dccmd.main.upload import create_folder_struct, bulk_upload, is_directory, is_file
from dccmd.main.models.errors import (DCPathParseError, DCClientParseError, ConnectError, ConnectTimeout)

# initialize CLI app
app = typer.Typer()
app.add_typer(typer_instance=client_app, name="client")
app.add_typer(typer_instance=auth_app, name="auth")
app.add_typer(typer_instance=crypto_app, name="crypto")


@app.command()
def upload(
    source_dir_path: str = typer.Argument(
        ..., help="Source directory path to a file or folder to upload."
    ),
    target_path: str = typer.Argument(
        ..., help="Target path in a DRACOON instance pointing to a folder or room."
    ),
    cli_mode: bool = typer.Option(
        False, help="When active, accepts username and password"
    ),
    debug: bool = typer.Option(
        False, help="When active, sets log level to DEBUG and streams log"
    ),
    overwrite: bool = typer.Option(
        False,
        help="When active, will overwrite uploads of files with same name.",
    ),
    auto_rename: bool = typer.Option(
        False, help="When active, will auto-rename uploads of files with same name."
    ),
    recursive: bool = typer.Option(
        False, "--recursive", "-r", help="Upload a folder content recursively"
    ),
    velocity: int = typer.Option(
        2,
        "--velocity",
        "-v",
        help="Concurrent requests factor (1: slow, 2: normal, 3: fast)",
    ),
    username: str = typer.Argument(
        None, help="Username to log in to DRACOON - only works with active cli mode"
    ),
    password: str = typer.Argument(
        None, help="Password to log in to DRACOON - only works with active cli mode"
    ),
):
    """Upload a file into DRACOON by providing a source path and a target room or folder"""

    async def _upload():

        # get authenticated DRACOON instance
        dracoon, base_url = await init_dracoon(
            url_str=target_path,
            username=username,
            password=password,
            cli_mode=cli_mode,
            debug=debug,
        )

        # remove base url from path
        parsed_path = parse_path(target_path)

        node_info = await dracoon.nodes.get_node_from_path(path=parsed_path)

        if node_info is None:
            typer.echo(format_error_message(msg=f"Invalid target path: {target_path}"))
            sys.exit(1)

        if node_info.isEncrypted is True:

            crypto_secret = get_crypto_credentials(base_url)
            await init_keypair(
                dracoon=dracoon, base_url=base_url, crypto_secret=crypto_secret
            )

        is_folder = is_directory(folder_path=source_dir_path)
        is_file_path = is_file(folder_path=source_dir_path)

        resolution_strategy = "fail"

        if overwrite:
            resolution_strategy = "overwrite"

        if overwrite and auto_rename:
            typer.echo(
                format_error_message(
                    msg="Conflict: cannot use both resolution strategies (auto-rename / overwrite)."
                )
            )
            sys.exit(1)

        if auto_rename:
            resolution_strategy = "autorename"

        # uploading a folder must be used with -r flag
        if is_folder and not recursive:
            typer.echo(
                format_error_message(
                    msg="Folder can only be uploaded via recursive (-r) flag."
                )
            )
        # upload a folder and all related content
        elif is_folder and recursive:
            await create_folder_struct(
                source=source_dir_path, target=parsed_path, dracoon=dracoon
            )
            await bulk_upload(
                source=source_dir_path,
                target=parsed_path,
                dracoon=dracoon,
                resolution_strategy=resolution_strategy,
                velocity=velocity,
            )
            folder_name = parse_file_name(full_path=source_dir_path)
            typer.echo(f'{format_success_message(f"Folder {folder_name} uploaded.")}')
        # upload a single file
        elif is_file_path:
            try:
                await dracoon.upload(
                    file_path=source_dir_path,
                    target_path=parsed_path,
                    resolution_strategy=resolution_strategy,
                    display_progress=True,
                    raise_on_err=True,
                )
            except HTTPForbiddenError:
                await dracoon.logout()
                typer.echo(
                    format_error_message(
                        msg="Insufficient permissions (create required)."
                    )
                )
                sys.exit(2)
            except HTTPConflictError:
                await dracoon.logout()
                typer.echo(format_error_message(msg="File already exists."))
                sys.exit(2)
            except InvalidPathError:
                await dracoon.logout()
                typer.echo(
                    format_error_message(msg=f"Target path not found. ({target_path})")
                )
                sys.exit(2)
            except HTTPStatusError:
                await dracoon.logout()
                typer.echo(
                    format_error_message(msg="An error ocurred uploading the file.")
                )
                sys.exit(2)
     
            try:
                file_name = parse_file_name(full_path=source_dir_path)
            except DCPathParseError:
                file_name = source_dir_path

            typer.echo(f'{format_success_message(f"File {file_name} uploaded.")}')
        # handle invalid path
        else:
            typer.echo(
                format_error_message(msg=f"Provided path must be a folder or file. ({source_dir_path})")
            )

        # node id must be from parent room if folder
        if node_info.type == NodeType.folder:
            distrib_node_id = node_info.authParentId
        if node_info.type == NodeType.room:
            distrib_node_id = node_info.id

        if node_info.isEncrypted is True:
            await distribute_missing_keys(dracoon=dracoon, room_id=distrib_node_id)

        await dracoon.logout()

    asyncio.run(_upload())


@app.command()
def mkdir(
    dir_path: str = typer.Argument(
        ...,
        help="Full path to create a folder in DRACOON (e.g. dracoon.team/mynewfolder).",
    ),
    cli_mode: bool = typer.Option(
        False, help="When active, accepts username and password"
    ),
    debug: bool = typer.Option(
        False, help="When active, sets log level to DEBUG and streams log"
    ),
    username: str = typer.Argument(
        None, help="Username to log in to DRACOON - only works with active cli mode"
    ),
    password: str = typer.Argument(
        None, help="Password to log in to DRACOON - only works with active cli mode"
    ),
):
    """Create a folder in a DRACOON parent path"""

    async def _create_folder():

        # get authenticated DRACOON instance
        dracoon, _ = await init_dracoon(
            url_str=dir_path,
            username=username,
            password=password,
            cli_mode=cli_mode,
            debug=debug,
        )

        # remove base url from path
        parsed_path = parse_new_path(full_path=dir_path)

        folder_name = parse_file_name(full_path=dir_path)

        parent_node = await dracoon.nodes.get_node_from_path(path=parsed_path)

        if parent_node is None:
            await dracoon.logout()
            typer.echo(format_error_message(msg=f"Node not found: {parsed_path}"))
            sys.exit(1)

        payload = dracoon.nodes.make_folder(name=folder_name, parent_id=parent_node.id)

        try:
            await dracoon.nodes.create_folder(folder=payload, raise_on_err=True)
        except HTTPConflictError:
            await dracoon.logout()
            typer.echo(format_error_message(msg=f"Name already exists: {dir_path}"))
            sys.exit(1)
        except HTTPForbiddenError:
            await dracoon.logout()
            typer.echo(
                format_error_message(
                    msg="Insufficient permissions (create permission required)."
                )
            )
            sys.exit(1)
        except HTTPStatusError:
            await dracoon.logout()
            typer.echo(
                format_error_message(
                    msg="An error ocurred - folder could not be created."
                )
            )
            sys.exit(1)

        typer.echo(format_success_message(msg=f"Folder {folder_name} created."))
        await dracoon.logout()

    asyncio.run(_create_folder())


@app.command()
def mkroom(
    dir_path: str = typer.Argument(
        ...,
        help="Full path to create a room (inherit permissions) in DRACOON (e.g. dracoon.team/room)",
    ),
    cli_mode: bool = typer.Option(
        False, help="When active, accepts username and password"
    ),
    debug: bool = typer.Option(
        False, help="When active, sets log level to DEBUG and streams log"
    ),
    username: str = typer.Argument(
        None, help="Username to log in to DRACOON - only works with active cli mode"
    ),
    password: str = typer.Argument(
        None, help="Password to log in to DRACOON - only works with active cli mode"
    ),
):
    """Create a room (inherit permissions) in a DRACOON parent path"""

    async def _create_room():

        # get authenticated DRACOON instance
        dracoon, _ = await init_dracoon(
            url_str=dir_path,
            username=username,
            password=password,
            cli_mode=cli_mode,
            debug=debug,
        )

        # remove base url from path
        parsed_path = parse_new_path(full_path=dir_path)

        room_name = parse_file_name(full_path=dir_path)

        parent_node = await dracoon.nodes.get_node_from_path(path=parsed_path)

        if parent_node is None:
            await dracoon.logout()
            typer.echo(format_error_message(msg=f"Node not found: {parsed_path}"))
            sys.exit(1)
        if parent_node.type != NodeType.room:
            await dracoon.logout()
            typer.echo(
                format_error_message(msg=f"Parent path must be a room: {parsed_path}")
            )
            sys.exit(1)

        payload = dracoon.nodes.make_room(
            name=room_name, parent_id=parent_node.id, inherit_perms=True
        )

        try:
            await dracoon.nodes.create_room(room=payload, raise_on_err=True)
        except HTTPConflictError:
            typer.echo(format_error_message(msg=f"Name already exists: {dir_path}"))
            await dracoon.logout()
            sys.exit(1)
        except HTTPForbiddenError:
            await dracoon.logout()
            typer.echo(
                format_error_message(
                    msg="Insufficient permissions (room admin required)."
                )
            )
            sys.exit(1)
        except HTTPStatusError:
            await dracoon.logout()
            typer.echo(
                format_error_message(
                    msg="An error ocurred - room could not be created."
                )
            )
            sys.exit(1)
        except ConnectTimeout:
            typer.echo(
                format_error_message(
                    msg="Connection timeout - room could not be created."
                )
            )
            sys.exit(1)
        except ConnectError:
            typer.echo(
                format_error_message(
                    msg="Connection error - room could not be created."
                )
            )
            sys.exit(1)



        typer.echo(format_success_message(msg=f"Room {room_name} created."))
        await dracoon.logout()

    asyncio.run(_create_room())


@app.command()
#pylint: disable=C0103
def rm(
    source_path: str = typer.Argument(
        ...,
        help="Full path to delete a file / folder or room in DRACOON (e.g. dracoon.team/file.txt).",
    ),
    recursive: bool = typer.Option(
        False, "--recursive", "-r", help="Delete room / folder recursively."
    ),
    cli_mode: bool = typer.Option(
        False, help="When active, accepts username and password"
    ),
    debug: bool = typer.Option(
        False, help="When active, sets log level to DEBUG and streams log"
    ),
    username: str = typer.Argument(
        None, help="Username to log in to DRACOON - only works with active cli mode"
    ),
    password: str = typer.Argument(
        None, help="Password to log in to DRACOON - only works with active cli mode"
    ),
):
    """Delete a file / folder / room in DRACOON"""

    async def _delete_node():

        # get authenticated DRACOON instance
        dracoon, _ = await init_dracoon(
            url_str=source_path,
            username=username,
            password=password,
            cli_mode=cli_mode,
            debug=debug,
        )

        # remove base url from path
        parsed_path = parse_path(full_path=source_path)

        node_name = parse_file_name(full_path=source_path)

        node = await dracoon.nodes.get_node_from_path(path=parsed_path)

        if node is None:
            await dracoon.logout()
            typer.echo(format_error_message(msg=f"Node not found: {parsed_path}"))
            sys.exit(1)
        if node.type == NodeType.room and not recursive:
            await dracoon.logout()
            typer.echo(
                format_error_message(
                    msg="Room can only be deleted with recursive flag (-r)."
                )
            )
            sys.exit(1)
        if node.type == NodeType.folder and not recursive:
            await dracoon.logout()
            typer.echo(
                format_error_message(
                    msg="Folder can only be deleted with recursive flag (-r)."
                )
            )
            sys.exit(1)
        try:
            await dracoon.nodes.delete_node(node_id=node.id, raise_on_err=True)
        except HTTPForbiddenError:
            await dracoon.logout()
            typer.echo(
                format_error_message(msg="Insufficient permissions (delete required).")
            )
            sys.exit(1)
        except HTTPStatusError:
            await dracoon.logout()
            typer.echo(
                format_error_message(
                    msg="An error ocurred - node could not be deleted."
                )
            )
            sys.exit(1)
        except ConnectTimeout:
            typer.echo(
                format_error_message(
                    msg="Connection timeout - room could not be created."
                )
            )
            sys.exit(1)
        except ConnectError:
            typer.echo(
                format_error_message(
                    msg="Connection error - room could not be created."
                )
            )
            sys.exit(1)


        typer.echo(format_success_message(msg=f"Node {node_name} deleted."))
        await dracoon.logout()

    asyncio.run(_delete_node())


@app.command()
#pylint: disable=C0103
def ls(
    source_path: str = typer.Argument(
        ...,
        help="Full path to delete a file / folder or room in DRACOON (e.g. dracoon.team/file.txt).",
    ),
    inode: bool = typer.Option(False, "--inode", "-i", help="Display node id"),
    long_list: bool = typer.Option(
        False, "--long", "-l", help="Use a long listing format"
    ),
    human_readable: bool = typer.Option(
        False, "--human-readable", "-h", help="Use human readable sizes"
    ),
    cli_mode: bool = typer.Option(
        False, help="When active, accepts username and password"
    ),
    debug: bool = typer.Option(
        False, help="When active, sets log level to DEBUG and streams log"
    ),
    username: str = typer.Argument(
        None, help="Username to log in to DRACOON - only works with active cli mode"
    ),
    password: str = typer.Argument(
        None, help="Password to log in to DRACOON - only works with active cli mode"
    ),
):
    """List all nodes in a DRACOON path"""

    async def _list_nodes():

        # get authenticated DRACOON instance
        dracoon, _ = await init_dracoon(
            url_str=source_path,
            username=username,
            password=password,
            cli_mode=cli_mode,
            debug=debug,
        )

        # remove base url from path
        parsed_path = parse_path(full_path=source_path)

        if parsed_path != "/":
            parent_node = await dracoon.nodes.get_node_from_path(path=parsed_path)
        elif parsed_path == "/":
            parent_node = None
            parent_id = 0

        if parent_node:
            parent_id = parent_node.id

        if parent_node is None and parsed_path != "/":
            await dracoon.logout()
            typer.echo(format_error_message(msg=f"Node not found: {parsed_path}"))
            sys.exit(1)
        if parent_node and parent_node.type == NodeType.file:
            await dracoon.logout()
            typer.echo(
                format_error_message(
                    msg=f"Path must be a room or a folder ({source_path})"
                )
            )
            sys.exit(1)
        try:
            nodes = await dracoon.nodes.get_nodes(parent_id=parent_id, raise_on_err=True)
        except HTTPForbiddenError:
            await dracoon.logout()
            typer.echo(
                format_error_message(msg="Insufficient permissions (delete required).")
            )
            sys.exit(1)
        except HTTPStatusError:
            await dracoon.logout()
            typer.echo(format_error_message(msg="Error listing nodes."))
            sys.exit(1)
        except ConnectTimeout:
            typer.echo(
                format_error_message(
                    msg="Connection timeout - could not list nodes."
                )
            )
            sys.exit(1)
        except ConnectError:
            typer.echo(
                format_error_message(
                    msg="Connection error - could not list nodes."
                )
            )
            sys.exit(1)


        # handle more than 500 items
        if nodes.range.total > 500:
            show_all = typer.confirm(
                f"More than 500 nodes in {parsed_path} - display all?"
            )

            if not show_all:
                typer.echo(f"{nodes.range.total} nodes – only 500 displayed.")
                raise typer.Abort()

            for offset in range(500, nodes.range.total, 500):
                try:
                    nodes_res = await dracoon.nodes.get_nodes(
                        parent_id=parent_id, offset=offset, raise_on_err=True
                    )
                    nodes.items.extend(nodes_res.items)
                except HTTPForbiddenError:
                    await dracoon.logout()
                    typer.echo(
                        format_error_message(
                            msg="Insufficient permissions (delete required)."
                        )
                    )
                    sys.exit(1)
                except HTTPStatusError:
                    await dracoon.logout()
                    typer.echo(format_error_message(msg="Error listing nodes."))
                    sys.exit(1)
                except ConnectTimeout:
                    typer.echo(
                        format_error_message(
                            msg="Connection timeout - could not list nodes."
                        )
                    )
                    sys.exit(1)
                except ConnectError:
                    typer.echo(
                        format_error_message(
                            msg="Connection error - could not list nodes."
                        )
                    )
                    sys.exit(1)

        if long_list and parent_id is not 0 and human_readable:
            typer.echo(f"total {to_readable_size(parent_node.size)}")
        elif long_list and parent_id is not 0:
            typer.echo(f"total {parent_node.size}")

        for node in nodes.items:
            format_and_print_node(
                node=node,
                inode=inode,
                long_list=long_list,
                readable_size=human_readable,
            )

        await dracoon.logout()

    asyncio.run(_list_nodes())


@app.command()
def download(
    source_path: str = typer.Argument(
        ..., help="Source path to a file in DRACOON to download."
    ),
    target_dir_path: str = typer.Argument(
        ..., help="Target directory path to a folder."
    ),
    cli_mode: bool = typer.Option(
        False, help="When active, accepts username and password"
    ),
    debug: bool = typer.Option(
        False, help="When active, sets log level to DEBUG and streams log"
    ),
    username: str = typer.Argument(
        None, help="Username to log in to DRACOON - only works with active cli mode"
    ),
    password: str = typer.Argument(
        None, help="Password to log in to DRACOON - only works with active cli mode"
    ),
):
    """
    Download a file from DRACOON by providing a source path
    and a target directory / path for the file
    """

    async def _download():

        # get authenticated DRACOON instance
        dracoon, base_url = await init_dracoon(
            url_str=source_path,
            username=username,
            password=password,
            cli_mode=cli_mode,
            debug=debug,
        )

        # remove base url from path
        parsed_path = parse_path(full_path=source_path)

        file_name = parse_file_name(full_path=source_path)

        node_info = await dracoon.nodes.get_node_from_path(path=parsed_path)

        if not node_info:
            typer.echo(format_error_message(msg=f"Node not found ({parsed_path})."))
            sys.exit(1)
        if node_info.type != NodeType.file:
            typer.echo(format_error_message(msg=f"Node not a file ({parsed_path})"))
            sys.exit(1)
    
        if node_info and node_info.isEncrypted is True:

            crypto_secret = get_crypto_credentials(base_url)
            await init_keypair(
                dracoon=dracoon, base_url=base_url, crypto_secret=crypto_secret
            )

        try:
            await dracoon.download(
                file_path=parsed_path,
                target_path=target_dir_path,
                display_progress=True,
                raise_on_err=True,
            )
        # to do: replace with handling via PermissionError
        except UnboundLocalError:
            typer.echo(
                format_error_message(msg=f"Insufficient permissions on target path ({target_dir_path})")
            )
            sys.exit(1)
        except InvalidPathError:
            typer.echo(
                format_error_message(msg=f"Path must be a folder ({target_dir_path})")
            )
            sys.exit(1)
        except InvalidFileError:
            await dracoon.logout()
            typer.echo(format_error_message(msg=f"File does not exist ({parsed_path})"))
            sys.exit(1)
        except FileConflictError:
            typer.echo(
                format_error_message(
                    msg=f"File already exists on target path ({target_dir_path})"
                )
            )
            sys.exit(1)
        except HTTPStatusError:
            await dracoon.logout()
            typer.echo(format_error_message(msg="Error downloading file."))
            sys.exit(1)
        except PermissionError:
            await dracoon.logout()
            typer.echo(
                format_error_message(
                    msg=f"Cannot write on target path ({target_dir_path})"
                )
            )
            sys.exit(1)
        except ConnectTimeout:
            typer.echo(
                format_error_message(
                    msg="Connection timeout - could not download file."
                )
            )
            sys.exit(1)
        except ConnectError:
            typer.echo(
                format_error_message(
                    msg="Connection error - could not download file."
                )
            )
            sys.exit(1)

        typer.echo(
            f'{format_success_message(f"File {file_name} downloaded to {target_dir_path}.")}'
        )

    asyncio.run(_download())


if __name__ == "__main__":
    app()