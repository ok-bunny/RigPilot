"""Command-line entry point: ``python -m agent``."""

from __future__ import annotations

import argparse
import json
import sys

import requests

from server.pci_identity import LICENSE_NOTICE

from .client import ControllerRequestError, ENROLLMENT_NOT_CREATED_EXIT_CODE
from .config import ConfigError, load_config
from .logging_utils import configure_logging
from .linux_supervision import repair_supervision_policy
from .managed_miners import ManagedMinerManager
from .runtime import AgentRuntime
from .version import __version__


class ThirdPartyNoticesAction(argparse.Action):
    """Expose the embedded PCI license without config or runtime work."""

    def __call__(self, parser, namespace, values, option_string=None):
        sys.stdout.write(LICENSE_NOTICE)
        parser.exit()


class AgentArgumentParser(argparse.ArgumentParser):
    """Keep private maintenance flags portable across Python argparse versions."""

    def parse_args(
        self,
        args: list[str] | None = None,
        namespace: argparse.Namespace | None = None,
    ) -> argparse.Namespace:
        parsed = super().parse_args(args, namespace)
        private_actions = (
            parsed.stop_managed_mining_once,
            parsed.resume_managed_mining_once,
            parsed.repair_supervision_policy,
        )
        if sum(bool(value) for value in private_actions) > 1:
            self.error(
                "private maintenance actions cannot be used together"
            )
        return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = AgentArgumentParser(description="RigPilot cross-platform management agent")
    parser.add_argument(
        "--version", action="version", version=f"RigPilot Agent {__version__}"
    )
    parser.add_argument(
        "--third-party-notices",
        action=ThirdPartyNoticesAction,
        nargs=0,
        help="print the embedded PCI database copyright and license, then exit",
    )
    parser.add_argument("--config", required=True, help="path to the JSON configuration file")
    parser.add_argument(
        "--enrollment-token",
        help="one-time enrollment token (prefer RIGPILOT_ENROLLMENT_TOKEN to keep it out of process arguments)",
    )
    parser.add_argument(
        "--replacement-token",
        help=(
            "one-time device inheritance token (prefer "
            "RIGPILOT_REPLACEMENT_TOKEN to keep it out of process arguments)"
        ),
    )
    parser.add_argument("--once", action="store_true", help="send one heartbeat and exit")
    parser.add_argument(
        "--stop-managed-mining-once",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--resume-managed-mining-once",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--repair-supervision-policy",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="acknowledge authorized mutating commands without running them",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    memory_worker_flag = "--nvidia-memory-worker"
    if memory_worker_flag in arguments:
        if arguments != [memory_worker_flag]:
            print("rigpilot-agent: invalid fixed NVIDIA memory worker invocation", file=sys.stderr)
            return 2
        from .nvidia_memory import worker_main
        return worker_main()
    args = build_parser().parse_args(arguments)
    try:
        config = load_config(
            args.config, packaged_defaults=bool(getattr(sys, "frozen", False))
        )
        if args.dry_run:
            config = config.with_dry_run(True)
        configure_logging(config.log_level, config.log_file)
        if args.repair_supervision_policy:
            repair_supervision_policy(config, args.config)
            return 0
        if args.stop_managed_mining_once:
            print(json.dumps(ManagedMinerManager(config).stop(), sort_keys=True))
            return 0
        if args.resume_managed_mining_once:
            print(json.dumps(ManagedMinerManager(config).restart(), sort_keys=True))
            return 0
        AgentRuntime(config).run(
            args.enrollment_token,
            once=args.once,
            cli_replacement_token=args.replacement_token,
        )
        return 0
    except (
        ConfigError,
        RuntimeError,
        requests.RequestException,
    ) as exc:
        print(f"rigpilot-agent: {exc}", file=sys.stderr)
        if isinstance(exc, ControllerRequestError) and exc.enrollment_attempt_cleared:
            return ENROLLMENT_NOT_CREATED_EXIT_CODE
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
