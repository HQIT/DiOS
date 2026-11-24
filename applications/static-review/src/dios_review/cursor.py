"""Cursor-specific launch composition; business review stays in this application."""
from .app import main as review_main
from dios_sdk.cursor import cursor_execute

def main(): return review_main(acp_execute=cursor_execute)
if __name__=='__main__':raise SystemExit(main())
