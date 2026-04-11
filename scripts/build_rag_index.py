#!/usr/bin/env python3
"""Remplacé par scripts/embed.py — ce script est conservé pour compatibilité."""

import subprocess
import sys
from pathlib import Path

print(
    "Attention: build_rag_index.py est obsolète.\n"
    "Utilise à la place: python scripts/embed.py --provider <openai|sentence-transformers|cohere>\n"
)

embed = Path(__file__).parent / "embed.py"
raise SystemExit(subprocess.call([sys.executable, str(embed)] + sys.argv[1:]))
