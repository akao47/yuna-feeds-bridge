import sys
from pathlib import Path

# Repo root on sys.path so `import scrape_aisearch` works under any pytest invocation.
sys.path.insert(0, str(Path(__file__).parent))
