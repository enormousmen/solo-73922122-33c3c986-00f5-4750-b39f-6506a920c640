#!/usr/bin/env python3
"""Callable Python example for the canary rollout API (stdlib only).

Run the API first (docker compose up -d, or: uvicorn app.main:app), then:

    python examples/client_demo.py

The script creates a rollout, drives one healthy batch to a pending promote,
changes the rollout config mid-flight (queued until the execution result
lands), then feeds a window that breaches the new error-rate threshold and
follows the resulting rollback decision to its terminal state.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

BASE = os.environ.get("BASE", "http://127.0.0.1:8000")


def call_raw(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def call(method: str, path: str, payload: dict | None = None) -> dict:
    status, body = call_raw(method, path, payload)
    if status >= 400:
        raise SystemExit(f"HTTP {status} on {method} {path}: {body}")
    return body


def iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main() -> None:
    print("== create rollout ==")
    rollout = call("POST", "/rollouts", {
        "service": "checkout-service",
        "target_version": "v2.3.1",
        "total_replicas": 4,
        "max_replicas_per_batch": 1,
        "min_stable_window_seconds": 30,
        "error_rate_threshold": 0.05,
    })
    rid = rollout["id"]
    print("created", rid, "status:", rollout["status"],
          "config_version:", rollout["config_version"])

    print("\n== healthy canary batch, generation 0 ==")
    call("POST", f"/rollouts/{rid}/replicas", {
        "generation": 0, "replica_id": "canary-1",
        "state": "ready", "observed_version": "v2.3.1",
    })
    call("POST", f"/rollouts/{rid}/metrics", {
        "generation": 0,
        "window_started_at": iso_now(),
        "window_seconds": 30,
        "request_count": 1000,
        "error_count": 4,  # 0.4% < 5%
    })
    decision = call("POST", f"/rollouts/{rid}/evaluate", {})
    print("decision:", decision["decision"]["action"], "-", decision["message"])
    assert decision["decision"]["action"] == "promote"

    print("\n== operator loosens the threshold to 10% while the promote is pending ==")
    update = call("PUT", f"/rollouts/{rid}/config", {
        "base_version": 0, "error_rate_threshold": 0.1,
    })
    print("applied:", update["applied"], "-", update["message"])
    assert update["applied"] is False  # queued behind the pending promote
    view = call("GET", f"/rollouts/{rid}")
    print("effective threshold:", view["error_rate_threshold"],
          "| pending config:", view["pending_config"])
    assert view["pending_config"]["version"] == 1

    print("\n== a stale submission (base_version 0 again) is rejected ==")
    status, body = call_raw("PUT", f"/rollouts/{rid}/config", {
        "base_version": 0, "error_rate_threshold": 0.9,
    })
    print("HTTP", status, "-", body["detail"])
    assert status == 409

    print("\n== platform executes the promote successfully ==")
    result = call(
        "POST",
        f"/rollouts/{rid}/decisions/{decision['decision']['id']}/result",
        {"status": "succeeded", "detail": "scaled to 1"},
    )
    print("status:", result["rollout"]["status"],
          "replicas:", result["rollout"]["current_replicas"],
          "generation:", result["rollout"]["current_generation"])
    print("config activated:", result["message"])
    assert result["rollout"]["config_version"] == 1
    assert result["rollout"]["error_rate_threshold"] == 0.1
    assert result["rollout"]["pending_config"] is None

    print("\n== late generation-0 report is ignored ==")
    ack = call("POST", f"/rollouts/{rid}/replicas", {
        "generation": 0, "replica_id": "late-canary", "state": "ready",
    })
    print("ignored:", ack["ignored"], "-", ack["reason"])
    assert ack["ignored"] is True

    print("\n== generation 1 window breaches the new 10% threshold -> rollback ==")
    call("POST", f"/rollouts/{rid}/replicas", {
        "generation": 1, "replica_id": "canary-1",
        "state": "ready", "observed_version": "v2.3.1",
    })
    call("POST", f"/rollouts/{rid}/metrics", {
        "generation": 1,
        "window_started_at": iso_now(),
        "window_seconds": 30,
        "request_count": 100,
        "error_count": 20,  # 20% > 10% (new threshold)
    })
    decision = call("POST", f"/rollouts/{rid}/evaluate", {})
    print("decision:", decision["decision"]["action"], "-", decision["message"])
    assert decision["decision"]["action"] == "rollback"

    result = call(
        "POST",
        f"/rollouts/{rid}/decisions/{decision['decision']['id']}/result",
        {"status": "succeeded", "detail": "traffic shifted to v2.3.0"},
    )
    print("final status:", result["rollout"]["status"])
    assert result["rollout"]["status"] == "rolled_back"

    print("\nall decisions:")
    for d in call("GET", f"/rollouts/{rid}/decisions")["decisions"]:
        print(f"  gen {d['generation']:>2} {d['action']:<9} "
              f"{d['from_replicas']}->{d['to_replicas']} {d['execution_status']}")
    print("\nOK")


if __name__ == "__main__":
    sys.exit(main())
