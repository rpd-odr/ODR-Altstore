#!/usr/bin/env python3
import json
import sys
from pathlib import Path

from providers.tweaks import resolve_builder_tweaks, write_github_output


def main() -> int:
    builder = sys.argv[1] if len(sys.argv) > 1 else "ytkace"
    config = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("tweaks.json")
    tweaks = resolve_builder_tweaks(builder, str(config))
    write_github_output(tweaks)
    print(json.dumps([t.__dict__ for t in tweaks], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
