"""FastAPI Chat App with Pydantic AI and streaming responses."""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from collections.abc import AsyncIterator
from concurrent.futures.thread import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Annotated, Literal

import fastapi
from fastapi import Depends, Form
from fastapi.responses import FileResponse, Response, StreamingResponse
from typing_extensions import TypedDict

from pydantic_ai import Agent
from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)
from pydantic_ai.exceptions import UnexpectedModelBehavior

log = logging.getLogger("chat_app.sandbox")

THIS_DIR = Path(__file__).parent


# ---------------------------------------------------------------------------
# Agent tool: read_file
# ---------------------------------------------------------------------------
# This tool lets the agent read files from the filesystem. It demonstrates
# how nono protects against prompt injection: the agent can read files inside
# the sandbox (e.g., app source), but any attempt to read outside (e.g.,
# ~/.ssh/id_rsa, /etc/passwd) is blocked by the kernel with EPERM.
#
# Try asking the agent:
#   "Read the file ~/.ssh/id_rsa"
#   "What's in /etc/passwd?"
#   "Show me the contents of chat_app.py"  (this one works -- it's inside the sandbox)

# Accumulates tool events during a single request so they can be streamed
# to the UI after the model response completes.
_tool_events: list[dict] = []


def read_file(path: str) -> str:
    """Read a file from the filesystem and return its contents.

    Args:
        path: The path to read. Can be absolute or relative.
    """
    try:
        target = Path(path).expanduser().resolve()
        content = target.read_text(errors="replace")[:4096]
        log.info("ALLOWED  read_file(%s) -> %d bytes", path, len(content))
        _tool_events.append({
            "role": "tool",
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            "content": f"read_file({path}) -- ALLOWED ({len(content)} bytes read)",
        })
        return content
    except PermissionError:
        log.warning("BLOCKED  read_file(%s) -> PermissionError (sandbox deny)", path)
        _tool_events.append({
            "role": "tool",
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            "content": f"read_file({path}) -- BLOCKED by nono sandbox (permission denied)",
        })
        return f"BLOCKED by nono sandbox: permission denied reading {path}"
    except FileNotFoundError:
        log.info("NOTFOUND read_file(%s) -> FileNotFoundError", path)
        _tool_events.append({
            "role": "tool",
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            "content": f"read_file({path}) -- file not found",
        })
        return f"File not found: {path}"
    except IsADirectoryError:
        return f"Path is a directory, not a file: {path}"
    except OSError as e:
        return f"OS error reading {path}: {e}"


agent = Agent(
    "openai:gpt-4o",
    system_prompt=(
        "You are a helpful chat assistant. You have a read_file tool that "
        "can read files from the local filesystem. Use it when the user asks "
        "to see file contents. If a read is blocked by the sandbox, explain "
        "that the file is outside the allowed sandbox permissions."
    ),
    tools=[read_file],
)


@asynccontextmanager
async def lifespan(app: fastapi.FastAPI):
    """Application lifespan context manager."""
    async with Database.connect() as db:
        app.state.db = db
        yield


app = fastapi.FastAPI(lifespan=lifespan)


def get_db(request: fastapi.Request) -> "Database":
    """Dependency to get database from app state."""
    return request.app.state.db


@app.get("/")
async def index() -> FileResponse:
    """Serve the main HTML page."""
    return FileResponse(THIS_DIR / "chat_app.html", media_type="text/html")


@app.get("/chat_app.ts")
async def main_ts() -> FileResponse:
    """Serve the TypeScript file."""
    return FileResponse(THIS_DIR / "chat_app.ts", media_type="text/plain")


class ChatMessage(TypedDict):
    """Chat message format for API responses."""

    role: Literal["user", "model", "tool"]
    timestamp: str
    content: str


def to_chat_message(m: ModelMessage) -> ChatMessage:
    """Convert a ModelMessage to a ChatMessage for the frontend."""
    first_part = m.parts[0]
    if isinstance(m, ModelRequest):
        if isinstance(first_part, UserPromptPart):
            content = first_part.content
            if isinstance(content, str):
                return {
                    "role": "user",
                    "timestamp": first_part.timestamp.isoformat(),
                    "content": content,
                }
    elif isinstance(m, ModelResponse):
        if isinstance(first_part, TextPart):
            return {
                "role": "model",
                "timestamp": m.timestamp.isoformat(),
                "content": first_part.content,
            }
    raise UnexpectedModelBehavior(f"Unexpected message type for chat app: {m}")


@app.get("/chat/")
async def get_chat(database: "Database" = Depends(get_db)) -> Response:
    """Get chat history."""
    msgs = await database.get_messages()
    return Response(
        b"\n".join(json.dumps(to_chat_message(m)).encode("utf-8") for m in msgs),
        media_type="text/plain",
    )


@app.post("/chat/")
async def post_chat(
    prompt: Annotated[str, Form()],
    database: "Database" = Depends(get_db),
) -> StreamingResponse:
    """Handle chat message with streaming response."""

    async def stream_messages():
        yield (
            json.dumps(
                {
                    "role": "user",
                    "timestamp": datetime.now(tz=timezone.utc).isoformat(),
                    "content": prompt,
                }
            ).encode("utf-8")
            + b"\n"
        )

        messages = await database.get_messages()

        _tool_events.clear()

        async with agent.run_stream(prompt, message_history=messages) as result:
            async for text in result.stream_text(debounce_by=0.01):
                # Flush any tool events that accumulated during this chunk
                for evt in _tool_events:
                    yield json.dumps(evt).encode("utf-8") + b"\n"
                _tool_events.clear()

                m = ModelResponse(parts=[TextPart(content=text)], timestamp=result.timestamp())
                yield json.dumps(to_chat_message(m)).encode("utf-8") + b"\n"

        # Flush any remaining tool events after stream completes
        for evt in _tool_events:
            yield json.dumps(evt).encode("utf-8") + b"\n"
        _tool_events.clear()

        await database.add_messages(result.new_messages_json())

    return StreamingResponse(stream_messages(), media_type="text/plain")


@dataclass
class Database:
    """Async SQLite database wrapper using thread pool."""

    con: sqlite3.Connection
    _loop: asyncio.AbstractEventLoop
    _executor: ThreadPoolExecutor

    @classmethod
    @asynccontextmanager
    async def connect(
        cls, file: Path = THIS_DIR / ".chat_app_messages.sqlite"
    ) -> AsyncIterator["Database"]:
        """Connect to the database."""
        loop = asyncio.get_event_loop()
        executor = ThreadPoolExecutor(max_workers=1)
        con = await loop.run_in_executor(executor, cls._connect, file)
        slf = cls(con, loop, executor)
        try:
            yield slf
        finally:
            await slf._asyncify(con.close)

    @staticmethod
    def _connect(file: Path) -> sqlite3.Connection:
        """Create database connection and initialize schema."""
        con = sqlite3.connect(str(file))
        cur = con.cursor()
        cur.execute(
            "CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, message_list TEXT);"
        )
        con.commit()
        return con

    def _execute(self, sql: str, *args, commit: bool = False) -> sqlite3.Cursor:
        """Execute SQL statement."""
        cur = self.con.cursor()
        cur.execute(sql, args)
        if commit:
            self.con.commit()
        return cur

    async def add_messages(self, messages: bytes) -> None:
        """Add messages to the database."""
        await self._asyncify(
            self._execute,
            "INSERT INTO messages (message_list) VALUES (?);",
            messages,
            commit=True,
        )

    async def get_messages(self) -> list[ModelMessage]:
        """Get all messages from the database."""
        c = await self._asyncify(
            self._execute, "SELECT message_list FROM messages ORDER BY id"
        )
        rows = await self._asyncify(c.fetchall)
        messages: list[ModelMessage] = []
        for row in rows:
            messages.extend(ModelMessagesTypeAdapter.validate_json(row[0]))
        return messages

    async def _asyncify(self, func, *args, **kwargs):
        """Run a sync function in the thread pool."""
        return await self._loop.run_in_executor(
            self._executor, partial(func, **kwargs), *args
        )
