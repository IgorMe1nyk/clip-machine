import argparse
from collections.abc import Sequence


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="clipmachine",
        description="Turn a long video into short vertical clips.",
    )
    parser.add_subparsers(dest="command", metavar="{run,report}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    build_parser().parse_args(argv)
    return 0
