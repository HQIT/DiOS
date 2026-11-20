"""Registered Cursor application entrypoint; common runtime remains vendor-neutral."""
from .inline_review import main
from app.runtime.cursor_acp import cursor_execute

if __name__=='__main__':raise SystemExit(main(acp_execute=cursor_execute))
