"""Codex ACP composition for the independent static review application."""

from dios_sdk.codex import codex_execute

from .app import main as review_main


def main():
    return review_main(acp_execute=codex_execute)


if __name__ == "__main__":
    raise SystemExit(main())
