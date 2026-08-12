#!/usr/bin/env python3
import asyncio
import sys
sys.path.insert(0, '.')

from scrapers.embedding_indexer import build_index

result = asyncio.run(build_index())
print(f'Result: {result}')
sys.exit(0 if result else 1)