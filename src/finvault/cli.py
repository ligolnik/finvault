"""Command-line entry point. JSON on stdout, diagnostics on stderr."""

from __future__ import annotations

import argparse
import getpass
import json
import sys
import time
from datetime import datetime

from finvault import __version__, vault
from finvault.config import DEFAULT_CONFIG, load_config
from finvault.errors import EXIT_OK, FinvaultError
from finvault.platform import MacPlatform


def _warn(message: str) -> None:
    print(f"finvault: warning: {message}", file=sys.stderr)


def ask_passphrase() -> str:
    """Read the vault passphrase from the terminal with no echo."""
    pw = getpass.getpass("Vault passphrase: ")
    if not pw:
        raise FinvaultError("no passphrase entered", "run the command again and type the passphrase")
    return pw


def ask_new_passphrase() -> str:
    """Read a new passphrase twice with no echo."""
    pw = getpass.getpass("New vault passphrase: ")
    if len(pw) < vault.MIN_PASSPHRASE_LEN:
        raise FinvaultError(
            f"the passphrase is shorter than {vault.MIN_PASSPHRASE_LEN} characters",
            "pick a longer one and keep it in your password manager",
        )
    if getpass.getpass("Type it again: ") != pw:
        raise FinvaultError("the two passphrases differ", "run the command again")
    return pw


def default_deps() -> vault.Deps:
    return vault.Deps(
        platform=MacPlatform(),
        ask_passphrase=ask_passphrase,
        ask_new_passphrase=ask_new_passphrase,
        now=datetime.now,
        sleep=time.sleep,
        warn=_warn,
    )


def build_parser() -> argparse.ArgumentParser:
    config_help = f"vault config file (default {DEFAULT_CONFIG})"
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=argparse.SUPPRESS, help=config_help)
    parser = argparse.ArgumentParser(
        prog="finvault",
        description="Keep folders in encrypted APFS sparse bundles, unlocked at their own paths.",
    )
    parser.add_argument("--config", default=None, help=config_help)
    parser.add_argument("--version", action="version", version=f"finvault {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="<command>")
    p = sub.add_parser("init", parents=[common], help="create the encrypted image for a folder")
    p.add_argument("name")
    p = sub.add_parser("migrate", parents=[common], help="copy a folder into its image (keeps <path>.plain)")
    p.add_argument("name")
    sub.add_parser("archive", parents=[common], help="build a read-only encrypted archive of the .plain folders")
    sub.add_parser("unlock", parents=[common], help="unlock every image at its folder's path")
    p = sub.add_parser("lock", parents=[common], help="sweep, close every image, set mountpoints to 000")
    p.add_argument("--force", action="store_true", help="end hc terminals working in the vault")
    p.add_argument("--skip-sweep", action="store_true", help="close the images without sweeping")
    sub.add_parser("status", parents=[common], help="show what is unlocked, busy and waiting to be swept")
    sub.add_parser("verify", parents=[common], help="check unlocked images against their checksum lists")
    p = sub.add_parser("sweep", parents=[common], help="move vault traces out of Claude Code's shared files")
    p.add_argument("--dry-run", action="store_true", help="only count what would move")
    sub.add_parser("restore-history", parents=[common], help="merge swept history lines back")
    return parser


def dispatch(args: argparse.Namespace, deps: vault.Deps) -> vault.Outcome:
    cfg = load_config(args.config)
    match args.command:
        case "init":
            return vault.init(cfg, deps, args.name)
        case "migrate":
            return vault.migrate(cfg, deps, args.name)
        case "archive":
            return vault.archive(cfg, deps)
        case "unlock":
            return vault.unlock(cfg, deps)
        case "lock":
            return vault.lock(cfg, deps, force=args.force, skip_sweep=args.skip_sweep)
        case "status":
            return vault.status(cfg, deps)
        case "verify":
            return vault.verify(cfg, deps)
        case "sweep":
            return vault.sweep(cfg, deps, dry_run=args.dry_run)
        case "restore-history":
            return vault.restore_history(cfg, deps)
        case _:
            raise FinvaultError(f"unknown command {args.command}", "run finvault --help")


def main(argv: list[str] | None = None, deps: vault.Deps | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        outcome = dispatch(args, deps or default_deps())
    except FinvaultError as exc:
        print(f"finvault: error: {exc.message}", file=sys.stderr)
        if exc.hint:
            print(f"finvault: what to do: {exc.hint}", file=sys.stderr)
        payload = {"ok": False, "command": args.command, "error": exc.message, "hint": exc.hint, **exc.details}
        print(json.dumps(payload, indent=2, sort_keys=True))
        return exc.exit_code
    payload = {"ok": outcome.exit_code == EXIT_OK, "command": args.command, **outcome.data}
    print(json.dumps(payload, indent=2, sort_keys=True))
    return outcome.exit_code


if __name__ == "__main__":
    sys.exit(main())
