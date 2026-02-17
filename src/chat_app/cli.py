"""Click CLI for the FastAPI Chat App."""
import os
from pathlib import Path

import click
import uvicorn

from nono_py import CapabilitySet, AccessMode, apply as nono_apply

# Project paths
APP_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = APP_DIR.parent.parent


def _apply_sandbox():
    """Apply an OS-enforced sandbox before the server starts.

    nono uses Landlock (Linux) or Seatbelt (macOS) to restrict this process
    at the kernel level. Once applied, the sandbox is irreversible -- no code
    running inside this process (including the AI agent) can expand permissions.

    The sandbox is applied here in the CLI entry point, BEFORE uvicorn.run().
    This means the uvicorn master process and all forked worker processes
    inherit the same restrictions. The agent, FastAPI handlers, and any
    libraries they call are all constrained transparently.

    What is blocked after apply():
    - Reading/writing any filesystem path not listed below
    - Accessing ~/.ssh, ~/.aws, ~/.gnupg, or any credentials
    - Writing to the Python installation or system directories
    - Any filesystem operation outside the explicit allow-list

    What remains allowed:
    - Outbound network (required for OpenAI API calls)
    - Reading/writing the paths listed below
    """
    caps = CapabilitySet()

    # -------------------------------------------------------------------------
    # Application directory -- READ_WRITE
    # -------------------------------------------------------------------------
    # Contains: chat_app.py, chat_app.html, chat_app.ts (source files)
    #           .chat_app_messages.sqlite (SQLite database)
    #
    # Why READ_WRITE and not READ + allow_file for the .sqlite?
    # SQLite creates journal files alongside the database:
    #   .chat_app_messages.sqlite-journal  (rollback journal)
    #   .chat_app_messages.sqlite-wal      (write-ahead log)
    #   .chat_app_messages.sqlite-shm      (shared memory for WAL)
    # These are created/deleted dynamically, so the entire directory needs
    # write access. In production, move the DB to a dedicated directory to
    # keep source files read-only.
    caps.allow_path(str(APP_DIR), AccessMode.READ_WRITE)

    # -------------------------------------------------------------------------
    # Temporary directory -- READ_WRITE
    # -------------------------------------------------------------------------
    # Used by: uvicorn (event loop internals), asyncio (pipe fds),
    #          httpx/httpcore (connection pooling), SSL certificate validation.
    # On macOS, /tmp is a symlink to /private/tmp -- nono handles both
    # automatically by emitting Seatbelt rules for the original and resolved paths.
    caps.allow_path("/tmp", AccessMode.READ_WRITE)

    # -------------------------------------------------------------------------
    # Python runtime -- READ only
    # -------------------------------------------------------------------------
    # Three separate paths cover the full Python environment:
    #
    # 1. Standard library (os, asyncio, json, sqlite3, ssl, etc.)
    #    Resolved from os.__file__ which points into the stdlib directory.
    python_prefix = os.path.dirname(os.path.dirname(os.__file__))
    caps.allow_path(python_prefix, AccessMode.READ)

    # 2. Virtualenv site-packages (fastapi, uvicorn, pydantic-ai, openai, etc.)
    #    The venv's site-packages is separate from the stdlib location.
    #    site.getsitepackages() returns all site-packages directories.
    import site
    for sp in site.getsitepackages():
        caps.allow_path(sp, AccessMode.READ)

    # 3. uv-managed Python interpreter
    #    uv installs Python to ~/.local/share/uv/python/... which is outside
    #    both the stdlib prefix and the venv. The interpreter binary and its
    #    support files (encodings, codecs) live here.
    import sys
    caps.allow_path(os.path.dirname(os.path.dirname(sys.executable)), AccessMode.READ)

    # -------------------------------------------------------------------------
    # Current working directory -- READ only
    # -------------------------------------------------------------------------
    # Why: pydantic-ai pulls in logfire as a plugin, and logfire calls
    # Path('.').resolve() at import time to detect user code stack frames.
    # Without CWD read access, this raises PermissionError during module import.
    caps.allow_path(os.getcwd(), AccessMode.READ)

    # -------------------------------------------------------------------------
    # System paths for DNS resolution (macOS-specific)
    # -------------------------------------------------------------------------
    # On macOS, DNS resolution goes through the mDNSResponder daemon via a
    # Unix domain socket at /var/run/mDNSResponder (symlink: /private/var/run/).
    # Connecting to a Unix socket requires file-level access, not just network
    # permissions -- Seatbelt's (allow network-outbound) does not cover it.
    #
    # /etc (-> /private/etc) contains resolv.conf and hosts, read by the
    # resolver as fallback configuration.
    #
    # Without these, any outbound HTTP request (including to api.openai.com)
    # fails with: [Errno 8] nodename nor servname provided, or not known
    caps.allow_path("/etc", AccessMode.READ)
    caps.allow_path("/var/run", AccessMode.READ_WRITE)

    # -------------------------------------------------------------------------
    # Network -- left OPEN (not blocked)
    # -------------------------------------------------------------------------
    # Required for: OpenAI API calls (api.openai.com over HTTPS).
    # If you wanted to restrict network, you could call caps.block_network()
    # and route API traffic through a local proxy that holds the API key.
    # See the nono credential isolation docs for the proxy architecture.

    # -------------------------------------------------------------------------
    # Apply -- irreversible from this point on
    # -------------------------------------------------------------------------
    # After this call, the kernel enforces the above rules on every syscall.
    # All child processes (uvicorn workers) inherit the same restrictions.
    # There is no API to expand permissions after apply().
    nono_apply(caps)


@click.group()
@click.version_option(version="0.1.0", prog_name="chat-app")
def cli():
    """FastAPI Chat App with Pydantic AI."""
    pass


@cli.command()
@click.option("--host", default="127.0.0.1", help="Host to bind to.")
@click.option("--port", default=8000, type=int, help="Port to bind to.")
@click.option("--reload", is_flag=True, help="Enable auto-reload for development.")
@click.option("--workers", default=1, type=int, help="Number of worker processes.")
@click.option("--no-sandbox", is_flag=True, help="Disable nono sandbox.")
def serve(host: str, port: int, reload: bool, workers: int, no_sandbox: bool):
    """Start the chat application server."""
    # Sandbox is applied BEFORE uvicorn starts. This is intentional:
    # - uvicorn.run() forks worker processes that inherit the sandbox
    # - The FastAPI app, Pydantic AI agent, and OpenAI client all run
    #   inside the sandbox without knowing it exists
    # - If a prompt injection causes the agent to attempt file access
    #   outside the allowed paths, the kernel returns EPERM
    # - The --no-sandbox flag is provided for development/debugging only
    if not no_sandbox:
        _apply_sandbox()

    click.echo(f"Starting server at http://{host}:{port}")
    uvicorn.run(
        "chat_app.chat_app:app",
        host=host,
        port=port,
        reload=reload,
        workers=workers if not reload else 1,
    )


@cli.command()
@click.option("--db-path", default=".chat_app_messages.sqlite", help="Path to SQLite database.")
@click.confirmation_option(prompt="Are you sure you want to clear the chat history?")
def clear_history(db_path: str):
    """Clear all chat history from the database."""
    import sqlite3
    from pathlib import Path

    db_file = Path(db_path)
    if not db_file.exists():
        click.echo("No chat history found.")
        return

    con = sqlite3.connect(str(db_file))
    cur = con.cursor()
    cur.execute("DELETE FROM messages;")
    con.commit()
    con.close()
    click.echo("Chat history cleared.")


@cli.command()
@click.option("--model", default="openai:gpt-4o", help="Model to use for the agent.")
def info(model: str):
    """Display information about the chat app configuration."""
    click.echo("Chat App Configuration")
    click.echo("-" * 30)
    click.echo(f"Default Model: {model}")
    click.echo("Database: SQLite (.chat_app_messages.sqlite)")
    click.echo("Framework: FastAPI + Pydantic AI")


if __name__ == "__main__":
    cli()
