from pathlib import Path

# Base directories
SCRIPT_DIR = Path(__file__).resolve().parent.parent  # src/proj261/util -> src/proj261
PROJECT_DIR = SCRIPT_DIR.parent.parent               # src/proj261 -> root

# Data and output directories
DATA_DIR = PROJECT_DIR / "data"
METADATA_PATH = DATA_DIR / "metadata.json"
BINARIES_DIR = DATA_DIR / "binaries"
DECOMPS_DIR = DATA_DIR / "decomps"
FILTERED_DECOMPS_DIR = DATA_DIR / "decomps_filtered"
CHUNKED_DECOMPS_DIR = DATA_DIR / "decomps_chunked"
CHUNKED_SOURCES_DIR = DATA_DIR / "source_chunked"
REPOS_DIR = DATA_DIR / "repos"
OUT_DIR = PROJECT_DIR / "out"
PRED_DIR = OUT_DIR / "pred"

def safe_name(full_name: str) -> str:
    """Standardized name sanitization for repo names (e.g. owner/repo -> owner__repo)."""
    return full_name.replace("/", "__")
