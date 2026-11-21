"""Mid-rollout config changes: versioning, pending configs, activation."""
from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

import app.db as db
from app.main import app
from conftest import make_rollout, report_metrics, report_replica


def _promote_pending(client, rid: str) -> str:
    """Drive generation 0 to a pending promote decision; return decision id."""
    report_replica(client, rid, 0, "canary-1")
    report_replica(client, rid, 0, "canary-2")
    report_metrics(client, rid, 0, 30, 1000, 4)
    resp = client.post(f"/rollouts/{rid}/evaluate").json()
    assert resp["decision"]["action"] == "promote"
    assert resp["rollout"]["pending_decision_id"] == resp["decision"]["id"]
    return resp["decision"]["id"]


def _put_config(client, rid, base_version, **fields):
    return client.put(
        f"/rollouts/{rid}/config",
        json={"base_version": base_version, **fields},
    )


def test_detail_shows_effective_config_and_version(client: TestClient):
    rid = make_rollout(client)  # 30s window, 5% threshold
    view = client.get(f"/rollouts/{rid}").json()
    assert view["config_version"] == 0
    assert view["error_rate_threshold"] == 0.05
    assert view["min_stable_window_seconds"] == 30
    assert view["pending_config"] is None


def test_update_applies_immediately_without_pending_decision(client: TestClient):
    rid = make_rollout(client)

    resp = _put_config(client, rid, 0, error_rate_threshold=0.2,
                       min_stable_window_seconds=10)
    assert resp.status_code == 200
    body = resp.json()
    assert body["applied"] is True
    assert "v1" in body["message"]

    view = client.get(f"/rollouts/{rid}").json()
    assert view["config_version"] == 1
    assert view["error_rate_threshold"] == 0.2
    assert view["min_stable_window_seconds"] == 10
    assert view["pending_config"] is None

    # The new 10s window requirement drives the very next evaluation.
    report_replica(client, rid, 0, "canary-1")
    report_replica(client, rid, 0, "canary-2")
    report_metrics(client, rid, 0, 10, 100, 1)  # 10s window, 1% errors
    decision = client.post(f"/rollouts/{rid}/evaluate").json()
    assert decision["decision"]["action"] == "promote"
    assert decision["decision"]["detail"]["min_stable_window_seconds"] == 10
    assert decision["decision"]["detail"]["threshold"] == 0.2


def test_partial_update_inherits_unspecified_fields(client: TestClient):
    rid = make_rollout(client)
    resp = _put_config(client, rid, 0, error_rate_threshold=0.15)
    assert resp.status_code == 200
    view = resp.json()["rollout"]
    assert view["error_rate_threshold"] == 0.15
    assert view["min_stable_window_seconds"] == 30  # untouched


def test_update_requires_at_least_one_field(client: TestClient):
    rid = make_rollout(client)
    resp = client.put(f"/rollouts/{rid}/config", json={"base_version": 0})
    assert resp.status_code == 422


def test_stale_base_version_is_rejected(client: TestClient):
    rid = make_rollout(client)
    assert _put_config(client, rid, 0, error_rate_threshold=0.1).status_code == 200

    stale = _put_config(client, rid, 0, error_rate_threshold=0.2)
    assert stale.status_code == 409
    assert "latest accepted version is 1" in stale.json()["detail"]

    ok = _put_config(client, rid, 1, error_rate_threshold=0.2)
    assert ok.status_code == 200
    assert ok.json()["rollout"]["config_version"] == 2


def test_concurrent_same_version_submissions_accept_only_one(client: TestClient):
    rid = make_rollout(client)
    barrier = threading.Barrier(2)

    def submit(threshold: float):
        barrier.wait()
        return _put_config(client, rid, 0, error_rate_threshold=threshold)

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(submit, [0.11, 0.22]))

    assert sorted(r.status_code for r in responses) == [200, 409]
    view = client.get(f"/rollouts/{rid}").json()
    assert view["config_version"] == 1
    winner = next(r for r in responses if r.status_code == 200)
    assert view["error_rate_threshold"] == winner.json()["rollout"]["error_rate_threshold"]


def test_pending_config_while_promote_pending_then_activated_on_result(
    client: TestClient,
):
    rid = make_rollout(client)
    did = _promote_pending(client, rid)

    # Promote is waiting for its execution result: the change is queued.
    resp = _put_config(client, rid, 0, error_rate_threshold=0.25,
                       min_stable_window_seconds=45)
    assert resp.status_code == 200
    body = resp.json()
    assert body["applied"] is False
    view = body["rollout"]
    assert view["config_version"] == 0          # effective config untouched
    assert view["error_rate_threshold"] == 0.05
    assert view["min_stable_window_seconds"] == 30
    assert view["pending_config"]["version"] == 1
    assert view["pending_config"]["error_rate_threshold"] == 0.25
    assert view["pending_config"]["min_stable_window_seconds"] == 45

    # The pending decision still blocks evaluation and is processed as decided.
    blocked = client.post(f"/rollouts/{rid}/evaluate").json()
    assert blocked["evaluated"] is False
    assert blocked["decision"]["id"] == did

    done = client.post(
        f"/rollouts/{rid}/decisions/{did}/result",
        json={"status": "succeeded", "detail": "scaled to 2"},
    ).json()
    assert done["rollout"]["current_replicas"] == 2
    assert done["rollout"]["current_generation"] == 1
    assert "pending config v1 activated" in done["message"]
    # Queued config is now effective; nothing remains pending.
    assert done["rollout"]["config_version"] == 1
    assert done["rollout"]["error_rate_threshold"] == 0.25
    assert done["rollout"]["min_stable_window_seconds"] == 45
    assert done["rollout"]["pending_config"] is None

    # The next evaluation uses the new config: 20% errors are below the new
    # 25% threshold (old 5% would roll back), and a 30s window no longer
    # meets the new 45s minimum, so the rollout holds instead of promoting.
    report_replica(client, rid, 1, "canary-1")
    report_replica(client, rid, 1, "canary-2")
    report_metrics(client, rid, 1, 30, 100, 20)
    decision = client.post(f"/rollouts/{rid}/evaluate").json()
    assert decision["decision"]["action"] == "pause"
    assert "no window of 45s" in decision["message"]


def test_later_accepted_change_replaces_pending_config(client: TestClient):
    rid = make_rollout(client)
    did = _promote_pending(client, rid)

    assert _put_config(client, rid, 0, error_rate_threshold=0.25,
                       min_stable_window_seconds=45).status_code == 200
    # A change based on the superseded version is now stale.
    assert _put_config(client, rid, 0, error_rate_threshold=0.5).status_code == 409
    # Based on the queued v1, this one replaces it (window inherited).
    resp = _put_config(client, rid, 1, error_rate_threshold=0.3)
    assert resp.status_code == 200
    pending = resp.json()["rollout"]["pending_config"]
    assert pending["version"] == 2
    assert pending["error_rate_threshold"] == 0.3
    assert pending["min_stable_window_seconds"] == 45

    done = client.post(
        f"/rollouts/{rid}/decisions/{did}/result", json={"status": "succeeded"}
    ).json()
    assert done["rollout"]["config_version"] == 2
    assert done["rollout"]["error_rate_threshold"] == 0.3
    assert done["rollout"]["min_stable_window_seconds"] == 45
    assert done["rollout"]["pending_config"] is None


def test_pending_config_activated_on_failed_promote_result(client: TestClient):
    rid = make_rollout(client)
    did = _promote_pending(client, rid)
    assert _put_config(client, rid, 0, error_rate_threshold=0.4).status_code == 200

    failed = client.post(
        f"/rollouts/{rid}/decisions/{did}/result",
        json={"status": "failed", "detail": "controller timeout"},
    ).json()
    # The original decision is failed as usual; the queued config still
    # becomes effective for whatever the operator does next.
    assert failed["rollout"]["status"] == "paused"
    assert failed["rollout"]["current_replicas"] == 0
    assert failed["rollout"]["config_version"] == 1
    assert failed["rollout"]["error_rate_threshold"] == 0.4
    assert failed["rollout"]["pending_config"] is None
    assert "pending config v1 activated" in failed["message"]

    # Paused (not terminal, nothing pending): further changes apply at once.
    resp = _put_config(client, rid, 1, min_stable_window_seconds=5)
    assert resp.status_code == 200
    assert resp.json()["applied"] is True
    assert resp.json()["rollout"]["config_version"] == 2


def test_update_rejected_while_rollback_pending(client: TestClient):
    rid = make_rollout(client)
    did = _promote_pending(client, rid)
    client.post(f"/rollouts/{rid}/decisions/{did}/result",
                json={"status": "succeeded"})

    report_replica(client, rid, 1, "canary-1")
    report_metrics(client, rid, 1, 30, 100, 50)  # 50% > 5% -> rollback
    rb = client.post(f"/rollouts/{rid}/evaluate").json()
    assert rb["decision"]["action"] == "rollback"
    assert rb["rollout"]["pending_decision_id"] == rb["decision"]["id"]

    resp = _put_config(client, rid, 0, error_rate_threshold=0.5)
    assert resp.status_code == 409
    assert "rollback" in resp.json()["detail"]


def test_update_rejected_when_rollout_is_terminal(client: TestClient):
    rid = make_rollout(client)
    did = _promote_pending(client, rid)
    client.post(f"/rollouts/{rid}/decisions/{did}/result",
                json={"status": "succeeded"})
    report_metrics(client, rid, 1, 30, 100, 50)
    rb = client.post(f"/rollouts/{rid}/evaluate").json()
    done = client.post(
        f"/rollouts/{rid}/decisions/{rb['decision']['id']}/result",
        json={"status": "succeeded"},
    ).json()
    assert done["rollout"]["status"] == "rolled_back"

    resp = _put_config(client, rid, 0, error_rate_threshold=0.5)
    assert resp.status_code == 409
    assert "terminal" in resp.json()["detail"]


def test_pending_config_survives_restart(client: TestClient):
    rid = make_rollout(client)
    did = _promote_pending(client, rid)
    assert _put_config(client, rid, 0, error_rate_threshold=0.33).status_code == 200

    # Simulate a process restart: new app runtime, same SQLite database.
    with TestClient(app=client.app) as restarted:
        view = restarted.get(f"/rollouts/{rid}").json()
        assert view["pending_config"]["version"] == 1
        assert view["pending_config"]["error_rate_threshold"] == 0.33
        assert view["config_version"] == 0

        done = restarted.post(
            f"/rollouts/{rid}/decisions/{did}/result",
            json={"status": "succeeded"},
        ).json()
        assert done["rollout"]["config_version"] == 1
        assert done["rollout"]["error_rate_threshold"] == 0.33
        assert done["rollout"]["pending_config"] is None


# --- pre-upgrade databases -------------------------------------------------

_OLD_DDL = """
CREATE TABLE releases (
    id TEXT PRIMARY KEY,
    service TEXT NOT NULL,
    target_version TEXT NOT NULL,
    total_replicas INTEGER NOT NULL,
    max_replicas_per_batch INTEGER NOT NULL,
    min_stable_window_seconds INTEGER NOT NULL,
    error_rate_threshold REAL NOT NULL,
    status TEXT NOT NULL,
    current_generation INTEGER NOT NULL DEFAULT 0,
    current_replicas INTEGER NOT NULL DEFAULT 0,
    pending_decision_id TEXT,
    last_decision_id TEXT,
    failure_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE decisions (
    id TEXT PRIMARY KEY,
    rollout_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    action TEXT NOT NULL,
    from_replicas INTEGER NOT NULL,
    to_replicas INTEGER NOT NULL,
    reason TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    execution_status TEXT NOT NULL,
    execution_detail TEXT,
    created_at TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    executed_at TEXT
);
"""


def _build_legacy_db(path: str) -> tuple[str, str]:
    """Create a pre-upgrade database: no config_version, no pending_configs."""
    rid, did = "rl_legacy", "dec_legacy"
    now = "2026-11-21T00:00:00+00:00"
    conn = sqlite3.connect(path)
    conn.executescript(_OLD_DDL)
    conn.execute(
        """
        INSERT INTO releases (
            id, service, target_version, total_replicas,
            max_replicas_per_batch, min_stable_window_seconds,
            error_rate_threshold, status, current_generation,
            current_replicas, pending_decision_id, last_decision_id,
            failure_message, created_at, updated_at
        ) VALUES (?, 'legacy-svc', 'v1.0.0', 4, 2, 30, 0.05, 'pending',
                  0, 0, ?, ?, NULL, ?, ?)
        """,
        (rid, did, did, now, now),
    )
    conn.execute(
        """
        INSERT INTO decisions (
            id, rollout_id, generation, action, from_replicas, to_replicas,
            reason, detail_json, execution_status, execution_detail,
            created_at, decided_at, executed_at
        ) VALUES (?, ?, 0, 'promote', 0, 2, 'legacy promote', '{}',
                  'pending', NULL, ?, ?, NULL)
        """,
        (did, rid, now, now),
    )
    conn.commit()
    conn.close()
    return rid, did


def test_pre_upgrade_rollout_and_pending_decision_continue_to_work(
    monkeypatch, tmp_path
):
    rid, did = _build_legacy_db(str(tmp_path / "legacy.db"))
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "legacy.db"))

    # Startup runs the in-place migration on the legacy database.
    with TestClient(app) as c:
        view = c.get(f"/rollouts/{rid}").json()
        assert view["config_version"] == 0
        assert view["pending_config"] is None
        assert view["pending_decision_id"] == did

        # Config change while the legacy promote is pending -> queued.
        resp = c.put(f"/rollouts/{rid}/config",
                     json={"base_version": 0, "error_rate_threshold": 0.2})
        assert resp.status_code == 200
        assert resp.json()["applied"] is False

        # The pre-upgrade pending decision is still processed normally.
        done = c.post(
            f"/rollouts/{rid}/decisions/{did}/result",
            json={"status": "succeeded"},
        ).json()
        assert done["rollout"]["status"] == "promoting"
        assert done["rollout"]["current_replicas"] == 2
        assert done["rollout"]["current_generation"] == 1
        assert done["rollout"]["config_version"] == 1
        assert done["rollout"]["error_rate_threshold"] == 0.2
        assert done["rollout"]["pending_config"] is None
