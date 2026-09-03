"""Lifespan tests for multi-worker API deployments without a real database."""
from __future__ import annotations

import asyncio
from contextlib import ExitStack, asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from app import main
from app import worker
from app.database import BackgroundWorkerLease, BackgroundWorkerLeaseLostError


class _Lease:
    def __init__(self, *, acquired: bool) -> None:
        self.acquired = acquired
        self.assert_held = Mock()
        self.release = Mock()


@asynccontextmanager
async def _empty_mcp_session():
    yield


async def _enter_lifespan() -> None:
    async with main.lifespan(main.app):
        await asyncio.sleep(0)


def _common_lifespan_patches():
    return (
        patch.object(main, "init_db"),
        patch.object(
            main.object_storage_service,
            "configuration",
            return_value=SimpleNamespace(configured=False),
        ),
        patch.object(
            main.agent_mcp_server.mcp_server.session_manager,
            "run",
            return_value=_empty_mcp_session(),
        ),
    )


def test_non_leader_api_worker_skips_durable_startup_recovery_and_consumers() -> None:
    lease = _Lease(acquired=False)
    prepare = AsyncMock()
    contexts = (
        *_common_lifespan_patches(),
        patch.object(main, "settings", SimpleNamespace(run_background_workers=True)),
        patch.object(main, "acquire_background_worker_lease", return_value=lease),
        patch.object(main, "prepare_durable_worker_state", prepare),
        patch.object(main, "_operations_worker", new=AsyncMock()),
        patch.object(main, "_assistant_compilation_worker", new=AsyncMock()),
    )
    with ExitStack() as stack:
        for context in contexts:
            stack.enter_context(context)
        asyncio.run(_enter_lifespan())

    prepare.assert_not_awaited()
    lease.release.assert_called_once()


def test_leader_api_worker_prepares_durable_state_once_before_starting_loops() -> None:
    lease = _Lease(acquired=True)
    prepare = AsyncMock()
    operations_loop = AsyncMock()
    compilation_loop = AsyncMock()
    contexts = (
        *_common_lifespan_patches(),
        patch.object(main, "settings", SimpleNamespace(run_background_workers=True)),
        patch.object(main, "acquire_background_worker_lease", return_value=lease),
        patch.object(main, "prepare_durable_worker_state", prepare),
        patch.object(main, "_operations_worker", operations_loop),
        patch.object(main, "_assistant_compilation_worker", compilation_loop),
    )
    with ExitStack() as stack:
        for context in contexts:
            stack.enter_context(context)
        asyncio.run(_enter_lifespan())

    prepare.assert_awaited_once()
    operations_loop.assert_called_once()
    compilation_loop.assert_called_once()
    operations_loop.assert_called_once_with(lease)
    compilation_loop.assert_called_once_with(lease)
    lease.release.assert_called_once()


def test_only_the_local_api_leader_initializes_minio() -> None:
    leader = _Lease(acquired=True)
    follower = _Lease(acquired=False)
    configured_storage = SimpleNamespace(configured=True, bucket_name="ontology")

    def enter(lease: _Lease):
        contexts = (
            patch.object(main, "init_db"),
            patch.object(
                main.object_storage_service,
                "configuration",
                return_value=configured_storage,
            ),
            patch.object(main.object_storage_service, "ensure_bucket"),
            patch.object(
                main.agent_mcp_server.mcp_server.session_manager,
                "run",
                return_value=_empty_mcp_session(),
            ),
            patch.object(main, "settings", SimpleNamespace(run_background_workers=True)),
            patch.object(main, "acquire_background_worker_lease", return_value=lease),
            patch.object(main, "prepare_durable_worker_state", new=AsyncMock()),
            patch.object(main, "_operations_worker", new=AsyncMock()),
            patch.object(main, "_assistant_compilation_worker", new=AsyncMock()),
        )
        with ExitStack() as stack:
            mocks = [stack.enter_context(context) for context in contexts]
            asyncio.run(_enter_lifespan())
        return mocks[2]

    leader_ensure_bucket = enter(leader)
    follower_ensure_bucket = enter(follower)

    leader_ensure_bucket.assert_called_once_with("ontology")
    follower_ensure_bucket.assert_not_called()


def test_production_api_leaves_minio_initialization_to_dedicated_worker() -> None:
    contexts = (
        patch.object(main, "init_db"),
        patch.object(
            main.object_storage_service,
            "configuration",
            return_value=SimpleNamespace(configured=True, bucket_name="ontology"),
        ),
        patch.object(main.object_storage_service, "ensure_bucket"),
        patch.object(
            main.agent_mcp_server.mcp_server.session_manager,
            "run",
            return_value=_empty_mcp_session(),
        ),
        patch.object(main, "settings", SimpleNamespace(run_background_workers=False)),
    )
    with ExitStack() as stack:
        mocks = [stack.enter_context(context) for context in contexts]
        asyncio.run(_enter_lifespan())

    mocks[2].assert_not_called()


def test_dedicated_worker_does_not_initialize_state_without_the_lease() -> None:
    lease = _Lease(acquired=False)
    prepare = AsyncMock()
    with (
        patch.object(worker, "init_db"),
        patch.object(worker, "acquire_background_worker_lease", return_value=lease),
        patch.object(worker, "prepare_durable_worker_state", prepare),
    ):
        try:
            asyncio.run(worker.run())
        except RuntimeError as exc:
            assert "租约" in str(exc)
        else:  # pragma: no cover - duplicate workers must not continue.
            raise AssertionError("expected advisory-lock failure")

    prepare.assert_not_awaited()


def test_lease_liveness_probe_rejects_a_reconnected_postgresql_session() -> None:
    connection = SimpleNamespace(scalar=Mock(return_value=9876), close=Mock())
    lease = BackgroundWorkerLease(connection=connection, backend_pid=1234)

    try:
        lease.assert_held()
    except BackgroundWorkerLeaseLostError as exc:
        assert "丢失" in str(exc)
    else:  # pragma: no cover - a reconnected session cannot own the old lock.
        raise AssertionError("expected advisory lease loss")

    assert not lease.acquired
    connection.close.assert_called_once()


def test_api_worker_stops_durable_loop_when_the_lease_connection_is_lost() -> None:
    lease = _Lease(acquired=True)
    lease.assert_held.side_effect = BackgroundWorkerLeaseLostError("lost")
    tick = Mock()
    with (
        patch.object(main, "settings", SimpleNamespace(background_worker_poll_seconds=1)),
        patch.object(main.operations_service, "worker_tick", tick),
    ):
        asyncio.run(main._operations_worker(lease))

    tick.assert_not_called()
