"""Allow `python -m mivta` execution."""

from mivta.bot import main

if __name__ == "__main__":
    raise SystemExit(main())
