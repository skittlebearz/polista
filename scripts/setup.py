#!/usr/bin/env python3
"""First-run setup for Polista: ask a handful of questions, then wire everything up.

Creates the virtualenv, installs the runtime deps, writes the port map and an
initial mappings file, and generates `polista.env` with every environment
variable the app reads. Standard library only — this runs *before* the venv
exists.

    python3 scripts/setup.py            # interactive
    python3 scripts/setup.py --yes      # accept every default, no questions
    python3 scripts/setup.py --no-venv  # skip venv/pip, just write config
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
from getpass import getpass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / "polista.env"
VENV_DIR = ROOT / ".venv"
VENDOR_DIR = ROOT / "vendor"

DEFAULT_PORT_COUNT = 8
DEFAULT_BIND = "127.0.0.1:8000"
DEFAULT_USERNAME = "admin"
DEFAULT_MAPPINGS_FILE = "data/mappings.json"
DEFAULT_PORT_MAP_FILE = "data/port_map.json"
DEFAULT_AUTH_FILE = "data/auth.json"
DEFAULT_GRPC_TARGET = "127.0.0.1:50052"
DEFAULT_DEVICE_ID = "0"
DEFAULT_PROGRAM_NAME = "polista"

EMPTY_STATE = {"mappings": [], "labels": {}, "last_sync_status": "ok"}

# bfrt_grpc is vendored (see vendor/README.md), so the bfrt backend only needs
# these gRPC wheels -- no SDE on the box, no matching its interpreter.
BFRT_REQUIREMENTS = ("grpcio", "grpcio-status", "six")


class Abort(Exception):
    """User bailed out, or we cannot continue."""


# --------------------------------------------------------------------------- ask


class Prompter:
    """Question asker. With assume_yes every answer is the default."""

    def __init__(self, assume_yes: bool):
        self.assume_yes = assume_yes
        if not assume_yes and not sys.stdin.isatty():
            raise Abort("stdin is not a terminal — re-run with --yes to accept defaults")

    def _input(self, prompt: str) -> str:
        try:
            return input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            raise Abort("cancelled")

    def text(self, question: str, default: str, validate=None) -> str:
        while True:
            if self.assume_yes:
                answer = default
            else:
                answer = self._input(f"{question} [{default}]: ") or default
            if validate is None:
                return answer
            try:
                return validate(answer)
            except ValueError as exc:
                if self.assume_yes:
                    raise Abort(f"invalid default for {question!r}: {exc}")
                print(f"  ! {exc}")

    def yes_no(self, question: str, default: bool) -> bool:
        if self.assume_yes:
            return default
        hint = "Y/n" if default else "y/N"
        while True:
            answer = self._input(f"{question} [{hint}]: ").lower()
            if not answer:
                return default
            if answer in ("y", "yes"):
                return True
            if answer in ("n", "no"):
                return False
            print("  ! answer y or n")

    def choice(self, question: str, options, default_index: int = 0) -> str:
        """options: list of (value, description)."""
        if self.assume_yes:
            return options[default_index][0]
        print(f"\n{question}")
        for index, (value, description) in enumerate(options, start=1):
            marker = " (default)" if index - 1 == default_index else ""
            print(f"  {index}) {value:<10} {description}{marker}")
        while True:
            answer = self._input(f"choice [1-{len(options)}]: ")
            if not answer:
                return options[default_index][0]
            if answer.isdigit() and 1 <= int(answer) <= len(options):
                return options[int(answer) - 1][0]
            for value, _ in options:
                if answer == value:
                    return value
            print("  ! pick a number from the list")

    def password(self, question: str) -> str:
        if self.assume_yes:
            return generated_password()
        while True:
            try:
                first = getpass(f"{question} (blank = generate one for you): ")
            except (EOFError, KeyboardInterrupt):
                raise Abort("cancelled")
            if not first:
                return generated_password()
            if len(first) < 8:
                print("  ! use at least 8 characters")
                continue
            try:
                second = getpass("  confirm: ")
            except (EOFError, KeyboardInterrupt):
                raise Abort("cancelled")
            if first != second:
                print("  ! passwords did not match")
                continue
            return first


def generated_password() -> str:
    return secrets.token_urlsafe(12)


# ---------------------------------------------------------------------- validate


def positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise ValueError("must be a whole number")
    if number <= 0:
        raise ValueError("must be greater than zero")
    return number


def bind_addr(value: str) -> str:
    host, _, port = value.rpartition(":")
    if not host or not port.isdigit() or not 1 <= int(port) <= 65535:
        raise ValueError("must look like HOST:PORT, e.g. 127.0.0.1:8000")
    return value


def custom_port_map(value: str, port_count: int) -> dict:
    parts = [part.strip() for part in value.replace(",", " ").split()]
    if len(parts) != port_count:
        raise ValueError(f"expected {port_count} device ports, got {len(parts)}")
    try:
        device_ports = [int(part) for part in parts]
    except ValueError:
        raise ValueError("device ports must be whole numbers")
    if len(set(device_ports)) != len(device_ports):
        raise ValueError("device ports must be unique (the map is a bijection)")
    return {str(ui): dev for ui, dev in enumerate(device_ports, start=1)}


# ------------------------------------------------------------------------- bfrt


def probe_bfrt(interpreter: str | None = None) -> tuple[bool, str]:
    """Try to import the vendored bfrt_grpc the way the app will.

    Returns (ok, detail). A failure here is almost always the gRPC deps not
    being installed yet, not anything to do with the SDE.
    """
    interpreter = interpreter or sys.executable
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    vendor = str(VENDOR_DIR)
    env["PYTHONPATH"] = f"{vendor}{os.pathsep}{existing}" if existing else vendor
    try:
        result = subprocess.run(
            [interpreter, "-c", "import bfrt_grpc.client; print('ok')"],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    if result.returncode == 0:
        return True, ""
    detail = (result.stderr or result.stdout).strip().splitlines()
    return False, detail[-1] if detail else f"exit {result.returncode}"


# ------------------------------------------------------------------------ files


def read_json(path: Path):
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def backup(path: Path) -> Path:
    target = path.with_suffix(path.suffix + ".bak")
    counter = 1
    while target.exists():
        target = path.with_suffix(f"{path.suffix}.bak{counter}")
        counter += 1
    path.rename(target)
    return target


def shell_quote(value: str) -> str:
    """Single-quote for both `source polista.env` and systemd EnvironmentFile."""
    return "'" + value.replace("'", "'\\''") + "'"


def resolve(path_value: str) -> Path:
    path = Path(path_value)
    return path if path.is_absolute() else ROOT / path


# ------------------------------------------------------------------------- venv


def venv_python(venv_dir: Path) -> Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def run(command, description: str) -> None:
    print(f"\n>>> {description}")
    result = subprocess.run(command, cwd=str(ROOT))
    if result.returncode != 0:
        raise Abort(f"{description} failed (exit {result.returncode})")


def build_venv(prompter: Prompter) -> Path:
    python = venv_python(VENV_DIR)
    if VENV_DIR.exists():
        if not python.exists():
            raise Abort(f"{VENV_DIR} exists but has no interpreter — remove it and re-run")
        print(f"\nReusing existing virtualenv at {VENV_DIR}")
    else:
        run([sys.executable, "-m", "venv", str(VENV_DIR)], f"creating virtualenv at {VENV_DIR}")

    if prompter.yes_no("Install/update the runtime dependencies with pip?", default=True):
        run(
            [str(python), "-m", "pip", "install", "--upgrade", "pip"],
            "upgrading pip",
        )
        run(
            [str(python), "-m", "pip", "install", "-r", str(ROOT / "requirements.txt")],
            "installing requirements.txt",
        )
    return python


# ------------------------------------------------------------------------ answers


def collect_answers(prompter: Prompter) -> dict:
    print("Polista setup — press Enter to accept the value in brackets.\n")

    answers = {}
    answers["backend"] = prompter.choice(
        "Which device backend should Polista talk to?",
        [
            ("fake", "in-memory emulation — no hardware, best for a first look"),
            ("bfrt", "real Tofino / tofino-model over BF Runtime gRPC"),
        ],
    )
    answers["port_count"] = prompter.text(
        "How many UI ports per column?", str(DEFAULT_PORT_COUNT), positive_int
    )
    answers["bind"] = prompter.text("Bind the web UI to which HOST:PORT?", DEFAULT_BIND, bind_addr)

    scheme_default = 1 if answers["backend"] == "bfrt" else 0
    scheme = prompter.choice(
        "How do UI port numbers map to device ports?",
        [
            ("identity", "1->1, 2->2, ... (fake backend, or a switch numbered from 1)"),
            ("zero", "1->0, 2->1, ... (typical tofino-model veth layout)"),
            ("custom", "type the device port for each UI port yourself"),
        ],
        default_index=scheme_default,
    )
    port_count = answers["port_count"]
    if scheme == "identity":
        answers["port_map"] = {str(ui): ui for ui in range(1, port_count + 1)}
    elif scheme == "zero":
        answers["port_map"] = {str(ui): ui - 1 for ui in range(1, port_count + 1)}
    else:
        identity = " ".join(str(ui) for ui in range(1, port_count + 1))
        answers["port_map"] = prompter.text(
            f"Device ports for UI ports 1..{port_count}, in order",
            identity,
            lambda value: custom_port_map(value, port_count),
        )

    auth_path = resolve(DEFAULT_AUTH_FILE)
    answers["auth_action"] = plan_auth(prompter, auth_path)
    if answers["auth_action"] == "keep":
        existing = read_json(auth_path) or {}
        answers["username"] = str(existing.get("username", DEFAULT_USERNAME))
        answers["password"] = generated_password()
        answers["password_is_live"] = False
    else:
        answers["username"] = prompter.text("Login username", DEFAULT_USERNAME)
        answers["password"] = prompter.password("Login password")
        answers["password_is_live"] = True

    if answers["backend"] == "bfrt":
        answers["grpc_target"] = prompter.text(
            "bf_switchd BF Runtime gRPC target", DEFAULT_GRPC_TARGET, bind_addr
        )
        answers["device_id"] = str(
            prompter.text("Tofino device id", DEFAULT_DEVICE_ID, lambda v: int(v))
        )
        answers["program_name"] = prompter.text("P4 program name", DEFAULT_PROGRAM_NAME)

    return answers


def check_bfrt_deps(venv_created: bool) -> None:
    """Report whether the vendored bfrt_grpc can actually be imported.

    Nothing to configure here: the client is in vendor/, so the only question is
    whether its gRPC dependencies are installed. Advisory only -- a venv built
    later in this run will supply them.
    """
    ok, detail = probe_bfrt()
    if ok:
        print("\nThe vendored bfrt_grpc imports cleanly — no SDE needed to run Polista.")
        return

    print(f"\n! bfrt_grpc cannot be imported yet: {detail}")
    if venv_created:
        print("  The virtualenv this run creates installs the gRPC deps, which should fix it.")
    else:
        print(f"  Install the bfrt dependencies: pip install {' '.join(BFRT_REQUIREMENTS)}")
    print("  Polista will report the reason on /health until then.")


def plan_auth(prompter: Prompter, auth_path: Path) -> str:
    """Return "create", "replace", or "keep"."""
    if not auth_path.exists():
        return "create"

    data = read_json(auth_path) or {}
    password_hash = str(data.get("password_hash", ""))
    if not password_hash.startswith("scrypt$"):
        print(
            f"\n! {auth_path} uses an old password-hash format that Polista refuses to start with.\n"
            "  It will be backed up so new credentials can be bootstrapped."
        )
        return "replace"

    user = data.get("username", "?")
    print(f"\nAn auth file already exists at {auth_path} (user {user!r}).")
    if prompter.yes_no("Set a new username/password? (the old file is backed up)", default=False):
        return "replace"
    return "keep"


# ------------------------------------------------------------------------- apply


def write_env_file(answers: dict) -> None:
    entries = [
        ("PORT_COUNT", str(answers["port_count"])),
        ("HTTP_BIND_ADDR", answers["bind"]),
        ("MAPPINGS_FILE", DEFAULT_MAPPINGS_FILE),
        ("PORT_MAP_FILE", DEFAULT_PORT_MAP_FILE),
        ("AUTH_FILE", DEFAULT_AUTH_FILE),
        ("SESSION_SECRET", secrets.token_urlsafe(32)),
        ("BOOTSTRAP_USERNAME", answers["username"]),
        ("BOOTSTRAP_PASSWORD", answers["password"]),
        ("TOFINO_BACKEND", answers["backend"]),
    ]
    if answers["backend"] == "bfrt":
        entries += [
            ("TOFINO_GRPC_TARGET", answers["grpc_target"]),
            ("TOFINO_DEVICE_ID", answers["device_id"]),
            ("TOFINO_PROGRAM_NAME", answers["program_name"]),
        ]

    header = [
        "# Polista environment — generated by scripts/setup.py.",
        "# Read by scripts/run.sh and by polista.service (EnvironmentFile=).",
        "# Contains a password: keep it out of git and mode 0600.",
    ]
    if not answers["password_is_live"]:
        header.append(
            "# BOOTSTRAP_* below is a placeholder: AUTH_FILE already exists, so these"
        )
        header.append("# values only take effect if that file is deleted.")

    lines = header + [f"{key}={shell_quote(value)}" for key, value in entries]
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    ENV_FILE.chmod(0o600)
    print(f"wrote {ENV_FILE} (mode 0600)")


def apply(answers: dict, prompter: Prompter) -> None:
    port_map_path = resolve(DEFAULT_PORT_MAP_FILE)
    if port_map_path.exists() and read_json(port_map_path) != answers["port_map"]:
        if prompter.yes_no(f"Overwrite the existing {port_map_path}?", default=True):
            write_json(port_map_path, answers["port_map"])
            print(f"wrote {port_map_path}")
        else:
            print(f"kept {port_map_path} as-is")
    else:
        write_json(port_map_path, answers["port_map"])
        print(f"wrote {port_map_path}")

    mappings_path = resolve(DEFAULT_MAPPINGS_FILE)
    if mappings_path.exists():
        print(f"kept {mappings_path} (existing cross-connects preserved)")
    else:
        write_json(mappings_path, EMPTY_STATE)
        print(f"wrote {mappings_path}")

    if answers["auth_action"] == "replace":
        moved = backup(resolve(DEFAULT_AUTH_FILE))
        print(f"backed up old auth file to {moved}")

    write_env_file(answers)


def report(answers: dict, used_venv: bool) -> None:
    host, _, port = answers["bind"].rpartition(":")
    display_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host

    print("\n" + "=" * 68)
    print("Polista is configured.")
    print("=" * 68)
    print(f"  backend    {answers['backend']}")
    print(f"  ports      {answers['port_count']} per column")
    print(f"  URL        http://{display_host}:{port}/ui")
    print(f"  username   {answers['username']}")
    if answers["password_is_live"]:
        print(f"  password   {answers['password']}")
    else:
        print("  password   (unchanged — existing auth file kept)")
    if answers["backend"] == "bfrt":
        print(f"  switchd    {answers['grpc_target']} (BF Runtime gRPC)")
    print(f"\nSettings live in {ENV_FILE}. Start it with:\n")
    print("  bash scripts/run.sh")
    if not used_venv:
        print("\n(no virtualenv was created; run.sh will fall back to your system python)")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Interactive first-run setup for Polista.")
    parser.add_argument(
        "-y", "--yes", action="store_true", help="accept every default, ask nothing"
    )
    parser.add_argument(
        "--no-venv", action="store_true", help="skip virtualenv creation and pip install"
    )
    args = parser.parse_args(argv)

    if sys.version_info < (3, 9):
        print("Polista needs Python 3.9 or newer.", file=sys.stderr)
        return 1

    try:
        prompter = Prompter(args.yes)
        answers = collect_answers(prompter)
        apply(answers, prompter)
        if not args.no_venv:
            build_venv(prompter)
        if answers["backend"] == "bfrt":
            check_bfrt_deps(venv_created=not args.no_venv)
        report(answers, used_venv=not args.no_venv)

        if not args.yes and prompter.yes_no("\nStart Polista now?", default=True):
            os.execvp("bash", ["bash", str(ROOT / "scripts" / "run.sh")])
    except Abort as exc:
        print(f"\nSetup stopped: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
