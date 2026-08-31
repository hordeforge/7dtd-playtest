#!/usr/bin/env python3
"""Offline gate: playtest_targets resolve / apply / report / env parse.

No game binaries. Pins the ownership contract:
  stock|sandbox|attach|zdtd|live, live/attach never start a server,
  sandbox names srv-<pair>/client-<pair>, legacy --server/--no-server still map.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import playtest_targets as pt  # noqa: E402


def test_targets_tuple() -> None:
    assert pt.TARGETS == ("stock", "sandbox", "attach", "zdtd", "live")


def test_normalize_rejects_unknown() -> None:
    try:
        pt.normalize_target("prod")
    except ValueError as ex:
        assert "prod" in str(ex)
    else:
        raise AssertionError("expected ValueError for unknown target")


def test_resolve_explicit_targets() -> None:
    stock = pt.resolve_target(target="stock")
    assert stock.target == "stock"
    assert stock.server_backend == "stock"
    assert stock.start_server is True
    assert stock.is_attach is False

    sandbox = pt.resolve_target(target="sandbox", sandbox_name="lab")
    assert sandbox.target == "sandbox"
    assert sandbox.server_backend == "stock"
    assert sandbox.start_server is True
    assert sandbox.sandbox_server == "srv-lab"
    assert sandbox.sandbox_client == "client-lab"

    attach = pt.resolve_target(target="attach")
    assert attach.is_attach is True
    assert attach.start_server is False

    zdtd = pt.resolve_target(target="zdtd")
    assert zdtd.target == "zdtd"
    assert zdtd.server_backend == "zdtd"
    assert zdtd.start_server is True

    live = pt.resolve_target(target="live", no_server=True)
    assert live.target == "live"
    assert live.is_attach is True
    assert live.start_server is False


def test_resolve_legacy_server_and_no_server() -> None:
    from_server = pt.resolve_target(server="zdtd")
    assert from_server.target == "zdtd"
    assert from_server.server_backend == "zdtd"

    from_attach = pt.resolve_target(no_server=True)
    assert from_attach.target == "attach"
    assert from_attach.is_attach is True


def test_apply_plan_to_args_attach() -> None:
    args = SimpleNamespace(
        server="stock",
        no_server=False,
        port=None,
        admin_port=8081,
        _admin_port_explicit=False,
    )
    plan = pt.resolve_target(target="attach")
    pt.apply_plan_to_args(args, plan)
    assert args.target == "attach"
    assert args.no_server is True
    assert args.server == "stock"


def test_apply_plan_to_args_stock() -> None:
    args = SimpleNamespace(
        server="stock",
        no_server=False,
        port=None,
        admin_port=8081,
        _admin_port_explicit=False,
    )
    plan = pt.resolve_target(target="stock")
    pt.apply_plan_to_args(args, plan)
    assert args.target == "stock"
    assert args.no_server is False
    assert args.server == "stock"


def test_target_report_fields() -> None:
    plan = pt.resolve_target(target="sandbox", sandbox_name="pt")
    fields = pt.target_report_fields(plan)
    assert fields["target"] == "sandbox"
    assert fields["server_backend"] == "stock"
    assert fields["start_server"] is True
    assert fields["sandbox_server"] == "srv-pt"
    assert fields["sandbox_client"] == "client-pt"
    assert isinstance(fields["notes"], list)


def test_parse_sb_env_output() -> None:
    text = """
# comment
export SERVER_PORT=26900
SERVER_TELNET_PORT="8081"
SERVER_GAME=/tmp/game
SERVER_USERDATA='/tmp/ud'
EMPTY=
"""
    env = pt.parse_sb_env_output(text)
    assert env["SERVER_PORT"] == "26900"
    assert env["SERVER_TELNET_PORT"] == "8081"
    assert env["SERVER_GAME"] == "/tmp/game"
    assert env["SERVER_USERDATA"] == "/tmp/ud"
    assert env["EMPTY"] == ""


def test_ensure_sandbox_missing_cli_raises() -> None:
    with tempfile.TemporaryDirectory(prefix="playtest-targets-") as td:
        root = Path(td)
        plan = pt.resolve_target(
            target="sandbox",
            sandbox_name="x",
            sandbox_root=root,
            workspace=root.parent,
        )
        # Force sandbox_root onto the empty temp tree.
        plan = pt.TargetPlan(
            target="sandbox",
            server_backend="stock",
            start_server=True,
            sandbox_server="srv-x",
            sandbox_client="client-x",
            sandbox_root=root,
        )
        try:
            pt.ensure_sandbox_server(plan, wipe=False)
        except RuntimeError as ex:
            assert "sandbox CLI missing" in str(ex)
        else:
            raise AssertionError("expected RuntimeError when sb is missing")


def main() -> int:
    fails = 0
    cases = [
        ("targets_tuple", test_targets_tuple),
        ("normalize_rejects_unknown", test_normalize_rejects_unknown),
        ("resolve_explicit_targets", test_resolve_explicit_targets),
        ("resolve_legacy_server_and_no_server", test_resolve_legacy_server_and_no_server),
        ("apply_plan_to_args_attach", test_apply_plan_to_args_attach),
        ("apply_plan_to_args_stock", test_apply_plan_to_args_stock),
        ("target_report_fields", test_target_report_fields),
        ("parse_sb_env_output", test_parse_sb_env_output),
        ("ensure_sandbox_missing_cli_raises", test_ensure_sandbox_missing_cli_raises),
    ]
    for name, fn in cases:
        try:
            fn()
            print(f"PASS {name}")
        except Exception as ex:
            fails += 1
            print(f"FAIL {name}: {ex}")
    if fails:
        print(f"FAILED {fails}/{len(cases)}")
        return 1
    print(f"OK {len(cases)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
