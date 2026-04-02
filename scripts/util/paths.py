from pathlib import Path

# Base directories
SCRIPT_DIR = Path(__file__).resolve().parent.parent
PROJECT_DIR = SCRIPT_DIR.parent

# Data and output directories
DATA_DIR = PROJECT_DIR / "data"
METADATA_PATH = DATA_DIR / "metadata.json"
BINARIES_DIR = DATA_DIR / "binaries"
DECOMPS_DIR = DATA_DIR / "decomps"
REPOS_DIR = DATA_DIR / "repos"
OUT_DIR = PROJECT_DIR / "out"

def safe_name(full_name: str) -> str:
    """Standardized name sanitization for repo names (e.g. owner/repo -> owner__repo)."""
    return full_name.replace("/", "__")
