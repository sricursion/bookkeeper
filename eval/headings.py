"""Print the section headings of a Markdown file, for reading aloud on camera."""

import sys
from pathlib import Path

for path in sys.argv[1:] or ["WHAT_BROKE.md"]:
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            print(line[3:])
