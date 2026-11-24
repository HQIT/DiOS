"""DSH launch composition for the independent static review application."""

from dios_sdk.dsh import dsh_execute

from .app import main as review_main


def main():
    return review_main(acp_execute=dsh_execute)


if __name__ == "__main__":
    raise SystemExit(main())
