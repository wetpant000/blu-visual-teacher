import os, yaml
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
CFG = yaml.safe_load(open(ROOT / "config.yaml", encoding="utf-8"))
if os.getenv("BLU_OLLAMA_MODEL"):
    CFG["ollama"]["model"] = os.environ["BLU_OLLAMA_MODEL"]
if os.getenv("BLU_FRONTEND_URL"):
    CFG["export"]["frontend_url"] = os.environ["BLU_FRONTEND_URL"]
LANGS = CFG["languages"]
