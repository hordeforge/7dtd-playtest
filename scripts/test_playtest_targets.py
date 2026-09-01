#!/usr/bin/env python3
"""Offline gate: playtest_targets resolve / apply / report / env parse.

No game binaries. Pins the ownership contract:
  provision managed|attach x backend stock|zdtd, readonly is attach-only,
  attach never starts a server, a managed stock run is always a Safehouse
  instance named srv-<pair>/client-<pair>, and every sb call is a real
  subprocess whose failure surfaces as TargetError.
"""
from __future__ import annotations

import os
import stat
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import playtest_targets as pt  # noqa: E402


def test_axes_tuples() -> None:
    assert pt.PROVISIONS == ("managed", "attach")
    assert pt.BACKENDS == ("stock", "zdtd")


def test_normalize_rejects_unknown() -> None:
    for fn, bad in ((pt.normalize_provision, "prod"), (pt.normalize_backend, "bedrock")):
        try:
            fn(bad)
        except ValueError as ex:
            assert bad in str(ex)
        else:
            raise AssertionError(f"expected ValueError for {bad!r}")


def test_managed_stock_is_a_sandbox_instance() -> None:
    plan = pt.resolve_target(provision="managed", backend="stock", sandbox_name="lab")
    assert plan.provision == "managed"
    assert plan.backend == "stock"
    assert plan.is_sandbox is True
    assert plan.start_server is True
    assert plan.is_attach is False
    assert plan.sandbox_server == "srv-lab"
    assert plan.sandbox_client == "client-lab"


def test_managed_zdtd_is_not_a_sandbox_instance() -> None:
    plan = pt.resolve_target(provision="managed", backend="zdtd")
    assert plan.start_server is True
    assert plan.is_sandbox is False
    assert plan.sandbox_server is None


def test_attach_never_starts_a_server() -> None:
    for kwargs in ({"provision": "attach"}, {"no_server": True}):
        plan = pt.resolve_target(**kwargs)
        assert plan.is_attach is True
        assert plan.start_server is False
        assert plan.is_sandbox is False


def test_no_server_overrides_managed() -> None:
    """A caller that says do not start a server must never get one started."""
    plan = pt.resolve_target(provision="managed", no_server=True)
    assert plan.is_attach is True
    assert plan.start_server is False


def test_readonly_is_attach_only() -> None:
    plan = pt.resolve_target(provision="attach", readonly=True)
    assert plan.readonly is True
    assert plan.label == "attach/stock readonly"
    try:
        pt.resolve_target(provision="managed", readonly=True)
    except ValueError as ex:
        assert "attach-only" in str(ex)
    else:
        raise AssertionError("expected ValueError for readonly on a managed run")


def test_apply_plan_to_args() -> None:
    def fresh_args() -> SimpleNamespace:
        return SimpleNamespace(
            server="stock",
            no_server=False,
            port=None,
            admin_port=8081,
            readonly=False,
            provision="",
            _admin_port_explicit=False,
        )

    args = fresh_args()
    pt.apply_plan_to_args(args, pt.resolve_target(provision="attach", readonly=True))
    assert args.provision == "attach"
    assert args.no_server is True
    assert args.readonly is True

    args = fresh_args()
    pt.apply_plan_to_args(args, pt.resolve_target(provision="managed", backend="zdtd"))
    assert args.provision == "managed"
    assert args.no_server is False
    assert args.server == "zdtd"


def test_overlay_instance_env_wins_over_defaults() -> None:
    args = SimpleNamespace(port=26900, admin_port=8081, game_srv=None, userdata=None)
    pt.overlay_instance_env(
        args,
        {
            "SERVER_PORT": "27100",
            "SERVER_TELNET_PORT": "27101",
            "SERVER_GAME": "/lab/game",
            "SERVER_USERDATA": "/lab/userdata",
        },
    )
    assert args.port == 27100
    assert args.admin_port == 27101
    assert args.game_srv == Path("/lab/game")
    assert args.userdata == Path("/lab/userdata")


def test_target_report_fields() -> None:
    plan = pt.resolve_target(provision="managed", sandbox_name="pt")
    fields = pt.target_report_fields(plan)
    assert fields["provision"] == "managed"
    assert fields["backend"] == "stock"
    assert fields["readonly"] is False
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


def _plan_on(root: Path) -> pt.TargetPlan:
    return pt.TargetPlan(
        provision="managed",
        backend="stock",
        sandbox_server="srv-x",
        sandbox_client="client-x",
        sandbox_root=root,
    )


def test_missing_sb_names_the_path() -> None:
    with tempfile.TemporaryDirectory(prefix="playtest-targets-") as td:
        root = Path(td)
        checks = (
            pt.check_sandbox_available,
            lambda p: pt.ensure_sandbox_server(p, wipe=False),
        )
        for fn in checks:
            try:
                fn(_plan_on(root))
            except pt.TargetError as ex:
                assert "Safehouse CLI missing" in str(ex)
            else:
                raise AssertionError("expected TargetError when sb is missing")


def test_resolving_a_target_never_creates_an_instance() -> None:
    """Resolution is read-only: an offline gate that calls main() must not
    leave a multi-gigabyte game copy behind."""
    with tempfile.TemporaryDirectory(prefix="playtest-targets-") as td:
        root = Path(td)
        _fake_sb(root, 'echo "$@" >> "$PWD/calls.txt"\n')
        pt.check_sandbox_available(_plan_on(root))
        assert not (root / "calls.txt").exists(), "resolution shelled out to sb"
        assert not (root / "instances").exists(), "resolution created an instance"


def _fake_sb(root: Path, body: str) -> None:
    sb = root / "scripts" / "sb"
    sb.parent.mkdir(parents=True, exist_ok=True)
    sb.write_text("#!/usr/bin/env bash\nset -eu\n" + body, encoding="utf-8")
    sb.chmod(sb.stat().st_mode | stat.S_IEXEC)


def test_sb_failure_surfaces_its_message() -> None:
    with tempfile.TemporaryDirectory(prefix="playtest-targets-") as td:
        root = Path(td)
        _fake_sb(root, 'echo "sb: server base missing" >&2\nexit 1\n')
        try:
            pt.ensure_sandbox_server(_plan_on(root), wipe=False)
        except pt.TargetError as ex:
            assert "server base missing" in str(ex), ex
        else:
            raise AssertionError("expected TargetError when sb exits non-zero")


def test_ensure_sandbox_server_creates_a_missing_instance() -> None:
    with tempfile.TemporaryDirectory(prefix="playtest-targets-") as td:
        root = Path(td)
        _fake_sb(
            root,
            'if [[ "$1" == "create-server" ]]; then\n'
            '  mkdir -p "instances/$2"\n'
            '  printf "SERVER_PORT=27105\\nSERVER_TELNET_PORT=27106\\n'
            'SERVER_GAME=%s/instances/$2/game\\nSERVER_USERDATA=%s/instances/$2/userdata\\n" '
            '"$PWD" "$PWD" > "instances/$2/instance.env"\n'
            "fi\n",
        )
        env_map = pt.ensure_sandbox_server(_plan_on(root))
        assert env_map["SERVER_PORT"] == "27105"
        assert env_map["SERVER_TELNET_PORT"] == "27106"
        assert (root / "instances" / "srv-x" / "instance.env").is_file()


def test_ensure_sandbox_server_calls_sb_in_order() -> None:
    """wipe, stage and render-config all precede the blocking `sb up`."""
    with tempfile.TemporaryDirectory(prefix="playtest-targets-") as td:
        root = Path(td)
        inst = root / "instances" / "srv-x"
        inst.mkdir(parents=True)
        (inst / "instance.env").write_text("SERVER_PORT=27105\n", encoding="utf-8")
        _fake_sb(root, 'echo "$@" >> "$PWD/calls.txt"\n')
        env_map = pt.ensure_sandbox_server(
            _plan_on(root),
            wipe=True,
            mods=[Path("/mods/DemoMod")],
            config={"GameWorld": "Navezgane"},
        )
        assert env_map["SERVER_PORT"] == "27105"
        calls = (root / "calls.txt").read_text(encoding="utf-8").splitlines()
        assert calls == [
            "wipe srv-x",
            "stage srv-x /mods/DemoMod",
            "render-config srv-x GameWorld=Navezgane",
            f"up srv-x --timeout {pt.SANDBOX_UP_TIMEOUT_SEC}",
        ], calls


def test_stop_is_best_effort_and_skips_non_sandbox() -> None:
    with tempfile.TemporaryDirectory(prefix="playtest-targets-") as td:
        root = Path(td)
        _fake_sb(root, 'echo "$@" >> "$PWD/calls.txt"\nexit 3\n')
        # A failing stop must not raise out of a teardown path.
        pt.stop_sandbox_server(_plan_on(root))
        assert (root / "calls.txt").read_text(encoding="utf-8").strip() == "stop srv-x"
        # Attach and zdtd plans have no instance to stop.
        pt.stop_sandbox_server(pt.resolve_target(provision="attach"))
        pt.stop_sandbox_server(pt.resolve_target(provision="managed", backend="zdtd"))


def main() -> int:
    fails = 0
    cases = [
        ("axes_tuples", test_axes_tuples),
        ("normalize_rejects_unknown", test_normalize_rejects_unknown),
        ("managed_stock_is_a_sandbox_instance", test_managed_stock_is_a_sandbox_instance),
        ("managed_zdtd_is_not_a_sandbox_instance", test_managed_zdtd_is_not_a_sandbox_instance),
        ("attach_never_starts_a_server", test_attach_never_starts_a_server),
        ("no_server_overrides_managed", test_no_server_overrides_managed),
        ("readonly_is_attach_only", test_readonly_is_attach_only),
        ("apply_plan_to_args", test_apply_plan_to_args),
        ("overlay_instance_env_wins_over_defaults", test_overlay_instance_env_wins_over_defaults),
        ("target_report_fields", test_target_report_fields),
        ("parse_sb_env_output", test_parse_sb_env_output),
        ("missing_sb_names_the_path", test_missing_sb_names_the_path),
        ("sb_failure_surfaces_its_message", test_sb_failure_surfaces_its_message),
        (
            "resolving_a_target_never_creates_an_instance",
            test_resolving_a_target_never_creates_an_instance,
        ),
        (
            "ensure_sandbox_server_creates_a_missing_instance",
            test_ensure_sandbox_server_creates_a_missing_instance,
        ),
        ("ensure_sandbox_server_calls_sb_in_order", test_ensure_sandbox_server_calls_sb_in_order),
        (
            "stop_is_best_effort_and_skips_non_sandbox",
            test_stop_is_best_effort_and_skips_non_sandbox,
        ),
    ]
    saved = {k: os.environ.pop(k, None) for k in ("PLAYTEST_PROVISION", "PLAYTEST_BACKEND")}
    try:
        for name, fn in cases:
            try:
                fn()
                print(f"PASS {name}")
            except Exception as ex:
                fails += 1
                print(f"FAIL {name}: {ex}")
    finally:
        for key, value in saved.items():
            if value is not None:
                os.environ[key] = value
    if fails:
        print(f"FAILED {fails}/{len(cases)}")
        return 1
    print(f"OK {len(cases)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
