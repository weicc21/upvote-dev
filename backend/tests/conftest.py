"""Fakes and app fixtures for the route tests.

No test here touches a real Supabase, Redis, or network endpoint. Redis is
`fakeredis`; Supabase is a hand-rolled double that records calls and replays
canned rows, because the supabase-py client is a fluent query builder and a
`MagicMock` would happily accept a wrong chain of calls.
"""

from __future__ import annotations

from typing import Any

import fakeredis.aioredis
import pytest
from fastapi.testclient import TestClient

from backend import deps


class FakeQuery:
    """Records the builder chain and returns a canned result."""

    def __init__(self, table: str, store: FakeSupabase) -> None:
        self._table = table
        self._store = store
        self._filters: dict[str, Any] = {}
        self._payload: Any = None
        self._op: str | None = None

    # -- builder methods (each returns self so the chain composes) ----------
    def select(self, *_a: Any, **_k: Any) -> FakeQuery:
        self._op = "select"
        return self

    def insert(self, payload: Any, **_k: Any) -> FakeQuery:
        self._op = "insert"
        self._payload = payload
        return self

    def update(self, payload: Any, **_k: Any) -> FakeQuery:
        self._op = "update"
        self._payload = payload
        return self

    def eq(self, col: str, val: Any) -> FakeQuery:
        self._filters[col] = ("eq", val)
        return self

    def in_(self, col: str, vals: Any) -> FakeQuery:
        self._filters[col] = ("in", list(vals))
        return self

    def is_(self, col: str, val: Any) -> FakeQuery:
        # PostgREST spells SQL NULL as the string "null"
        self._filters[col] = ("is", None if str(val).lower() == "null" else val)
        return self

    def neq(self, col: str, val: Any) -> FakeQuery:
        self._filters[col] = ("neq", val)
        return self

    def order(self, *_a: Any, **_k: Any) -> FakeQuery:
        return self

    def limit(self, *_a: Any, **_k: Any) -> FakeQuery:
        return self

    def lt(self, *_a: Any, **_k: Any) -> FakeQuery:
        return self

    def gt(self, *_a: Any, **_k: Any) -> FakeQuery:
        return self

    def or_(self, *_a: Any, **_k: Any) -> FakeQuery:
        return self

    def maybe_single(self) -> FakeQuery:
        self._single = True
        return self

    def single(self) -> FakeQuery:
        self._single = True
        return self

    async def execute(self) -> Any:
        self._store.calls.append(
            {"table": self._table, "op": self._op, "filters": dict(self._filters), "payload": self._payload}
        )
        if self._op == "insert":
            exc = self._store.insert_raises.get(self._table)
            if exc is not None:
                raise exc
            return type("Resp", (), {"data": [self._payload]})()
        rows = self._apply_filters(self._store.rows.get(self._table, []))
        if getattr(self, "_single", False):
            return type("Resp", (), {"data": rows[0] if rows else None})()
        return type("Resp", (), {"data": list(rows)})()


    def _apply_filters(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Apply the recorded eq/in_/is_/neq filters.

        Without this a root-rows-only query returns split children too, and the
        test passes while the product is broken.
        """
        out = list(rows)
        for col, spec in self._filters.items():
            if not isinstance(spec, tuple):
                spec = ("eq", spec)
            op, val = spec
            if op == "eq":
                out = [r for r in out if r.get(col) == val]
            elif op == "in":
                out = [r for r in out if r.get(col) in val]
            elif op == "is":
                out = [r for r in out if r.get(col) is val]
            elif op == "neq":
                out = [r for r in out if r.get(col) != val]
        return out


class FakeSupabase:
    """Minimal stand-in for the supabase-py async client."""

    def __init__(self) -> None:
        self.rows: dict[str, list[dict[str, Any]]] = {}
        self.calls: list[dict[str, Any]] = []
        self.insert_raises: dict[str, Exception] = {}
        self.rpc_result: Any = None
        self.rpc_raises: Exception | None = None
        self.rpc_calls: list[tuple[str, dict[str, Any]]] = []

    def table(self, name: str) -> FakeQuery:
        return FakeQuery(name, self)

    def rpc(self, fn: str, params: dict[str, Any] | None = None) -> Any:
        self.rpc_calls.append((fn, params or {}))
        store = self

        class _RpcQuery:
            async def execute(self_inner) -> Any:
                if store.rpc_raises is not None:
                    raise store.rpc_raises
                return type("Resp", (), {"data": store.rpc_result})()

        return _RpcQuery()


@pytest.fixture
def fake_supabase() -> FakeSupabase:
    return FakeSupabase()


@pytest.fixture
async def fake_redis() -> Any:
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


@pytest.fixture
def make_client(fake_supabase: FakeSupabase, fake_redis: Any):
    """Build a TestClient with the deps seam bound to fakes and an identity stamped."""

    def _make(user_id: str | None = "11111111-1111-4111-8111-111111111111") -> TestClient:
        from backend.main import app

        async def _sb() -> Any:
            return fake_supabase

        async def _rd() -> Any:
            return fake_redis

        app.dependency_overrides[deps.get_supabase] = _sb
        app.dependency_overrides[deps.get_redis] = _rd

        client = TestClient(app, raise_server_exceptions=False)
        # main.py's middleware normally stamps this; in dev mode it reads X-Dev-User.
        if user_id:
            client.headers.update({"X-Dev-User": user_id})
        return client

    yield _make

    from backend.main import app

    app.dependency_overrides.clear()
