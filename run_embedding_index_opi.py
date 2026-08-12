#!/usr/bin/env python3
"""
Run embedding indexer with Orange Pi 5 Ollama endpoint.
"""
import asyncio
import sys
import os

# Set the Ollama URL to Orange Pi 5
os.environ['OLLAMA_URL'] = 'http://10.10.10.2:11434'

sys.path.insert(0, '/home/sanel/personal-assistant-bot')

from scrapers.embedding_indexer import build_index

result = asyncio.run(build_index())
print(f'Result: {result}')
sys.exit(0 if result else 1)