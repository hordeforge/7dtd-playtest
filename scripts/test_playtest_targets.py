#!/usr/bin/env python3
"""Offline gate: playtest_targets resolve / apply / report / env parse.

No game binaries. Pins the ownership contract:
  provision managed|attach x backend stock|zdtd, readonly is attach-only,
  attach never starts a server, a managed stock run is always a Safehouse
  instance named srv-<pair>/client-<pair>, and every sb call is a real
  subprocess whose failure surfaces as TargetError.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import os
import random
import re
import stat
import sys
import tempfile
from pathlib import Path
from unittest import mock

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import playtest_targets as pt  # noqa: E402

# Rounds per fuzz case. The generator is cheap (string building, no disk in
# the first loop), so this stays well inside the gate's budget.
FUZZ_ROUNDS = 400

_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


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
    def fresh_args() -> argparse.Namespace:
        return argparse.Namespace(
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
    args = argparse.Namespace(port=26900, admin_port=8081, game_srv=None, userdata=None)
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


_NAME_TOKENS = (
    "lab",
    "playtest",
    "pt-1",
    "..",
    ".",
    "",
    " ",
    "../../etc",
    "a/b",
    "a\\b",
    "-flag",
    ".hidden",
    "with space",
    "srv-lab",
    "láb",
    "n" * 80,
    "l;n",
    "l$(id)",
    "l`id`",
    "l\tx",
)

_PORT_VALUES = (
    "27100",
    " 27100 ",
    "27100 # trailing",
    "0",
    "-1",
    "65536",
    "999999999999999999999",
    "0x10",
    "٢٧٠٠٠",
    "'27100'",
    '"27100"',
    "",
    "   ",
    "not-a-port",
    "27100\nSERVER_TELNET_PORT=27101",
    "∞",
    "1_000",
)

_ENV_LINE_HEADS = (
    "SERVER_PORT=",
    "export SERVER_PORT=",
    "SERVER_PORT =",
    "SERVER_PORT",
    "SERVER_GAME=",
    "SERVER_USERDATA=",
    "SERVER_TELNET_PORT=",
    "# SERVER_PORT=",
    "1SERVER_PORT=",
    "SERVER_PORT:",
)


def _fuzz_name(rng: random.Random) -> str:
    parts = [rng.choice(_NAME_TOKENS) for _ in range(rng.randint(1, 3))]
    return rng.choice(("", " ", "\n")).join(parts)


def _fuzz_env_text(rng: random.Random) -> str:
    lines: list[str] = []
    for _ in range(rng.randint(0, 6)):
        head = rng.choice(_ENV_LINE_HEADS)
        value = rng.choice(_PORT_VALUES + _NAME_TOKENS)
        if rng.random() < 0.3:
            value = value.replace("=", "", 1)
        lines.append(head + value)
    text = "\n".join(lines)
    if rng.random() < 0.2:
        text += "\n"
    return text


def _declares_port(text: str) -> bool:
    """Does this contract text carry a SERVER_PORT assignment at all?

    The parser may skip a malformed line, but a well-formed
    `SERVER_PORT=` assignment must never disappear: a dropped port leaves the
    run on the pre-`sb up` placeholder and points it at the wrong server.
    """
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if re.match(r"^(?:export\s+)?SERVER_PORT=", stripped):
            return True
    return False


def _assert_single_component(kind: str, name: str | None, root: Path) -> None:
    """An instance name must name one directory under `<root>/instances`.

    The name is an argv element of every `sb` call and a path component of
    every instance path, so a name with a separator or a `..` walks the run
    outside the sandbox it was pointed at, onto an instance the operator
    never named.
    """
    assert name is not None, f"{kind} was not derived from the pair name"
    path = Path(name)  # mypy: name is not None from here on
    assert path.name == name, f"{kind}={name!r} is more than one path component"
    assert name != "." and name != "..", f"{kind}={name!r} is a relative directory"
    assert not name.startswith(("-", ".")), f"{kind}={name!r} is a flag or hidden name"
    assert len(name) <= 255, f"{kind}={name!r} is not a usable directory component"
    assert (root / "instances" / name).parent == root / "instances"


def test_fuzz_sandbox_name_and_instance_env_hold_the_instance_boundary() -> None:
    """Seeded grammar fuzzer over the two inputs a managed stock run trusts.

    The pair name comes from `--sandbox-name` or `PLAYTEST_SANDBOX_NAME`, and
    the contract text from an `instance.env` a Safehouse write half finished
    or a hand edit corrupted. Neither may crash the resolver, and neither may
    resolve to an instance outside the sandbox root or to a port that is not
    a number.
    """
    rng = random.Random(20260928)
    accepted_names = 0
    resolved = 0
    refused = 0
    for _ in range(FUZZ_ROUNDS):
        name = _fuzz_name(rng)
        try:
            plan = pt.resolve_target(
                provision="managed",
                backend="stock",
                sandbox_name=name,
                sandbox_root=Path("/nonexistent-sandbox-root"),
            )
        except ValueError:
            refused += 1
            continue
        accepted_names += 1
        _assert_single_component("sandbox_server", plan.sandbox_server, Path("/sb"))
        _assert_single_component("sandbox_client", plan.sandbox_client, Path("/sb"))

        text = _fuzz_env_text(rng)
        env = pt.parse_sb_env_output(text)
        for key, value in env.items():
            assert _IDENT_RE.fullmatch(key), f"contract key {key!r} is not an identifier"
            assert isinstance(value, str), f"contract value for {key!r} is not a string"
        assert ("SERVER_PORT" in env) == _declares_port(text), (
            f"a declared SERVER_PORT was dropped: {text!r} -> {env!r}"
        )
        resolved += 1

    with tempfile.TemporaryDirectory(prefix="playtest-targets-fuzz-") as td:
        root = Path(td)
        inst = root / "instances" / "srv-lab"
        inst.mkdir(parents=True)
        for _ in range(FUZZ_ROUNDS // 2):
            text = _fuzz_env_text(rng)
            (inst / "instance.env").write_text(text, encoding="utf-8")
            try:
                plan = pt.resolve_target(
                    provision="managed",
                    backend="stock",
                    sandbox_name="lab",
                    sandbox_root=root,
                )
            except pt.TargetError as ex:
                # A corrupt port is named, never dropped: the run must not
                # fall through to a placeholder port it did not get.
                assert "in the instance contract" in str(ex), ex
                assert any(
                    k in str(ex)
                    for k in ("SERVER_PORT", "SERVER_TELNET_PORT", "SERVER_GAME", "SERVER_USERDATA")
                ), ex
                continue
            if plan.port is not None:
                assert isinstance(plan.port, int) and not isinstance(plan.port, bool)
                assert 0 <= plan.port <= 65535, f"resolved port {plan.port} out of range"
            for attr in ("game_srv", "userdata"):
                got = getattr(plan, attr)
                if got is not None:
                    assert not str(got).startswith(".."), f"{attr}={got} walks out"

    assert accepted_names >= 5, f"fuzzer only resolved {accepted_names} names: corpus is too weak"
    assert resolved >= FUZZ_ROUNDS - refused
    assert refused >= 5, "fuzzer never reached the refusal path"
    print(
        f"PASS target_fuzz {FUZZ_ROUNDS} names and contracts, "
        f"{accepted_names} resolved, {refused} refused"
    )


def test_contract_port_and_path_outside_their_range_are_named() -> None:
    """A contract line the run cannot use is named, not carried.

    An out-of-range port or a relative instance path reads as a healthy
    value to every later step, so the failure only surfaces as a connect
    timeout against an address no listener can own.
    """
    cases = (
        ("SERVER_PORT", "0", "outside the port range"),
        ("SERVER_PORT", "65536", "outside the port range"),
        ("SERVER_PORT", "99999999999999999999", "outside the port range"),
        ("SERVER_GAME", "game/rel", "not an absolute path"),
        ("SERVER_USERDATA", "../../elsewhere", "not an absolute path"),
    )
    for key, value, expected in cases:
        try:
            pt.overlay_instance_env(argparse.Namespace(), {key: value})
        except pt.TargetError as ex:
            assert expected in str(ex), ex
            assert key in str(ex) and value in str(ex), ex
        else:
            raise AssertionError(f"expected TargetError for {key}={value!r}")


def test_sandbox_name_outside_one_component_is_refused() -> None:
    for name in ("../../etc", "lab/../x", "-flag", ".hidden", "l b", "n" * 80):
        try:
            pt.resolve_target(
                provision="managed",
                backend="stock",
                sandbox_name=name,
                sandbox_root=Path("/nonexistent-sandbox-root"),
            )
        except ValueError as ex:
            assert "sandbox name" in str(ex), ex
        else:
            raise AssertionError(f"expected ValueError for sandbox name {name!r}")
    assert pt.normalize_sandbox_name(" lab-1 ") == "lab-1"


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
        # A failing stop must not raise out of a teardown path, but it must be
        # reported: an abandoned dedicated holds the instance's port block.
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            pt.stop_sandbox_server(_plan_on(root))
        assert (root / "calls.txt").read_text(encoding="utf-8").strip() == "stop srv-x"
        assert "srv-x" in stderr.getvalue(), stderr.getvalue()
        # Attach and zdtd plans have no instance to stop.
        pt.stop_sandbox_server(pt.resolve_target(provision="attach"))
        pt.stop_sandbox_server(pt.resolve_target(provision="managed", backend="zdtd"))


def test_sb_that_never_returns_fails_instead_of_hanging() -> None:
    """An sb wedged past its bound must surface, not block the poll loop.

    The run's own wall-clock deadline only fires between polls, so a bring-up
    that never returns strands the live client and the exclusive-lock claim
    with nothing to time out against.
    """
    with tempfile.TemporaryDirectory(prefix="playtest-targets-") as td:
        root = Path(td)
        _fake_sb(root, "echo 'stage: downloading depot'\nsleep 30\n")
        try:
            pt._run_sb(_plan_on(root), ["stage", "srv-x"], timeout=1.0)
        except pt.TargetError as ex:
            assert "did not finish" in str(ex), ex
        else:
            raise AssertionError("expected TargetError for an sb that overran its bound")


def test_sb_output_with_non_ascii_bytes_is_decoded_not_raised() -> None:
    """sb echoes sandbox paths, which carry the operator's home directory name.

    Those bytes are UTF-8 whatever the process locale says, and a path that
    is not even UTF-8 must not turn a healthy run into a traceback.
    """
    with tempfile.TemporaryDirectory(prefix="playtest-targets-") as td:
        root = Path(td)
        _fake_sb(
            root,
            "printf 'ready for /home/caf\\xc3\\xa9/instances\\n'\n"
            "printf 'raw byte: \\xe9\\n'\n",
        )
        proc = pt._run_sb(_plan_on(root), ["up", "srv-x"], check=False)
        assert "café" in proc.stdout, proc.stdout
        assert "raw byte:" in proc.stdout, proc.stdout


def test_non_utf8_instance_env_is_named_not_ignored() -> None:
    """A corrupt instance.env must not read as "no instance": that hands the
    run default ports instead of this instance's."""
    with tempfile.TemporaryDirectory(prefix="playtest-targets-") as td:
        root = Path(td)
        inst = root / "instances" / "srv-x"
        inst.mkdir(parents=True)
        (inst / "instance.env").write_bytes(b"SERVER_PORT=27105\nSERVER_GAME=/caf\xe9/game\n")
        try:
            pt.load_sandbox_env(root, "srv-x")
        except pt.TargetError as ex:
            assert "not valid UTF-8" in str(ex), ex
        else:
            raise AssertionError("expected TargetError for a non-UTF-8 instance.env")


def test_unreadable_instance_env_is_named_not_ignored() -> None:
    """The file is there but will not read: same wrong-port outcome, so it
    must be named rather than read as an absent contract."""
    with tempfile.TemporaryDirectory(prefix="playtest-targets-") as td:
        root = Path(td)
        inst = root / "instances" / "srv-x"
        inst.mkdir(parents=True)
        inst_env = inst / "instance.env"
        inst_env.write_text("SERVER_PORT=27105\n", encoding="utf-8")
        real_read_text = Path.read_text

        def refuse(self: Path, *a: object, **kw: object) -> str:
            if self == inst_env:
                raise PermissionError(13, "Permission denied")
            return real_read_text(self, *a, **kw)  # type: ignore[arg-type]

        with mock.patch.object(Path, "read_text", refuse):
            try:
                pt.load_sandbox_env(root, "srv-x")
            except pt.TargetError as ex:
                assert str(inst_env) in str(ex), ex
            else:
                raise AssertionError("expected TargetError for an unreadable instance.env")


def test_malformed_port_in_instance_env_is_named_not_ignored() -> None:
    """A port that does not parse must not read as "not allocated": the run
    would keep the pre-`sb up` placeholder and report an unusable number."""
    args = argparse.Namespace(port=0, admin_port=8081, game_srv=None, userdata=None)
    try:
        pt.overlay_instance_env(args, {"SERVER_PORT": "27OOO"})
    except pt.TargetError as ex:
        assert "SERVER_PORT" in str(ex) and "27OOO" in str(ex), ex
    else:
        raise AssertionError("expected TargetError for a non-integer SERVER_PORT")
    assert args.port == 0, "a refused overlay must leave args untouched"


def test_malformed_port_stops_target_resolution() -> None:
    with tempfile.TemporaryDirectory(prefix="playtest-targets-") as td:
        root = Path(td)
        inst = root / "instances" / "srv-x"
        inst.mkdir(parents=True)
        (inst / "instance.env").write_text(
            "SERVER_PORT=not-a-port\nSERVER_TELNET_PORT=27101\n", encoding="utf-8"
        )
        try:
            pt.resolve_target(
                provision="managed", sandbox_name="x", sandbox_root=root, workspace=root
            )
        except pt.TargetError as ex:
            assert "SERVER_PORT" in str(ex), ex
        else:
            raise AssertionError("expected TargetError for a non-integer SERVER_PORT")


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
        (
            "malformed_port_in_instance_env_is_named_not_ignored",
            test_malformed_port_in_instance_env_is_named_not_ignored,
        ),
        (
            "malformed_port_stops_target_resolution",
            test_malformed_port_stops_target_resolution,
        ),
        ("target_report_fields", test_target_report_fields),
        ("parse_sb_env_output", test_parse_sb_env_output),
        (
            "sandbox_name_outside_one_component_is_refused",
            test_sandbox_name_outside_one_component_is_refused,
        ),
        (
            "contract_port_and_path_outside_their_range_are_named",
            test_contract_port_and_path_outside_their_range_are_named,
        ),
        (
            "fuzz_sandbox_name_and_instance_env_hold_the_instance_boundary",
            test_fuzz_sandbox_name_and_instance_env_hold_the_instance_boundary,
        ),
        ("missing_sb_names_the_path", test_missing_sb_names_the_path),
        ("sb_failure_surfaces_its_message", test_sb_failure_surfaces_its_message),
        (
            "sb_output_with_non_ascii_bytes_is_decoded_not_raised",
            test_sb_output_with_non_ascii_bytes_is_decoded_not_raised,
        ),
        (
            "non_utf8_instance_env_is_named_not_ignored",
            test_non_utf8_instance_env_is_named_not_ignored,
        ),
        (
            "unreadable_instance_env_is_named_not_ignored",
            test_unreadable_instance_env_is_named_not_ignored,
        ),
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
        (
            "sb_that_never_returns_fails_instead_of_hanging",
            test_sb_that_never_returns_fails_instead_of_hanging,
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
