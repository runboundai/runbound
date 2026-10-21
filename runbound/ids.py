"""The identities of one governed call (AAI-1 FR-1, RFC section 4).

Three ids, minted by Runbound and never taken from the caller or from a contextvar:

* ``action_id``: one per model-generated call, minted when the decorator accepts the call (``api._begin``);
* ``attempt_id``: one per acceptance by a boundary (today every call has exactly one attempt);
* ``evaluation_id``: one per admission, minted once when the engine starts the chain (``Engine._run_chain``).

A governed call made from inside another governed tool's body is a NEW action carrying ``parent_action_id``: :data:`CURRENT` holds the enclosing
(action, attempt) for the duration of that body and is read only to name the parent, never to share an id. The contextvar is copied into asyncio tasks
and ``asyncio.to_thread``; a bare ``threading.Thread`` does not copy it, so a call made there has no ``parent_action_id`` (which is also what a top-level call has:
the two cannot be told apart from here).

Never an argument, a result or a prompt; ids are random and carry nothing about the call.
"""

from __future__ import annotations

import contextvars
import hashlib
import uuid

#: The version of the identity contract carried on spans (``runbound.contract_version``).
CONTRACT_VERSION = 1
#: The boundary this SDK is: a call seen by the ``@runbound.tool`` decorator.
BOUNDARY_DECORATOR = "sdk-decorator"
#: The boundary a call is seen at when ``RunboundMiddleware`` / ``govern_tools`` itself governs it (a plain LangChain tool). A tool that is also a ``@runbound.tool`` function (the decorator
#: decides and the middleware passes the call through) and a callback handler keep ``sdk-decorator``. The span's ``runbound.boundary`` and the start marker's ``runbound.boundary`` /
#: ``runbound.declared_target`` all take this ONE value: a start is counted only where the marker's boundary is the call's declared target.
BOUNDARY_LANGCHAIN = "langchain-middleware"

#: ``(action_id, attempt_id)`` of the governed call whose body is running, so a governed call made inside it can name its parent.
CURRENT: "contextvars.ContextVar[tuple[str, str] | None]" = contextvars.ContextVar("runbound_current_attempt", default=None)


def mint() -> str:
    """A fresh random id."""
    return uuid.uuid4().hex


def new_call() -> dict:
    """The ids of a call being accepted now: a new action and a new attempt, with ``parent_action_id`` when it is made inside another governed body."""
    ids = {"action_id": mint(), "attempt_id": mint()}
    parent = CURRENT.get()
    if parent is not None:
        ids["parent_action_id"] = parent[0]
    return ids


def derive(tenant: str, thread_id: "str | None", checkpoint_ns: "str | None", tool_call_id: "str | None", retry_no: int = 0) -> "dict | None":
    """The ids of a call whose runtime names it stably (RFC section 4, LangChain): ``action_id = H(tenant, thread_id, checkpoint_ns, tool_call_id)`` and
    ``attempt_id = H(action_id, retry_no)``; ``None`` when there is no ``thread_id`` or no ``tool_call_id`` (the caller then mints random ids).

    A mapping, not a claim: deriving the ids does not make the call replay-safe, and nothing in this module looks an earlier attempt up.
    """
    if not thread_id or not tool_call_id or not tenant:
        return None
    action = hashlib.sha256("\x1f".join(("runbound.action.v1", tenant, str(thread_id), str(checkpoint_ns or ""), str(tool_call_id))).encode()).hexdigest()[:32]
    attempt = hashlib.sha256(f"runbound.attempt.v1\x1f{action}\x1f{int(retry_no)}".encode()).hexdigest()[:32]
    return {"action_id": action, "attempt_id": attempt}


def request_id_for(tenant: str, attempt_id: str) -> str:
    """The approval ``request_id`` of a replay-safe attempt: ``H(tenant, attempt_id)`` (RFC section 4), so a replayed node reaches the plane's idempotent create and gets the same approval."""
    return hashlib.sha256(f"runbound.request.v1\x1f{tenant}\x1f{attempt_id}".encode()).hexdigest()[:32]
