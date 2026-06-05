"""Configuration module for the Document Analysis System."""

import os
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Project paths
PROJECT_ROOT = Path(__file__).parent
SRC_DIR = PROJECT_ROOT / "src"
SAMPLE_DOCS_DIR = PROJECT_ROOT / "sample_documents"

# Azure OpenAI settings
AZURE_OPENAI_API_KEY = os.getenv("AZURE_OPENAI_API_KEY")
AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT")
AZURE_OPENAI_DEPLOYMENT_NAME = os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME")
AZURE_OPENAI_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2024-02-15-preview")

# Document processing settings
MAX_DOCUMENT_SIZE_MB = int(os.getenv("MAX_DOCUMENT_SIZE_MB", "50"))
SUPPORTED_FORMATS = set(
    fmt.strip() for fmt in os.getenv(
        "SUPPORTED_FORMATS",
        ".py,.cobol,.cbl,.cic,.cpy,.mps,.src,.ct1,.jcv,.prv,.docx,.html,.xlsx,.xlsm,.xlsb,.txt"
    ).split(",")
)

# Analysis settings
CHUNK_SIZE = int(os.getenv("CHUNK_SIZE", "2000"))
CHUNK_OVERLAP = int(os.getenv("CHUNK_OVERLAP", "200"))

# Bulk mode: path(s) to documents directory (comma-separated for multiple)
DOCUMENTS_PATH = os.getenv("DOCUMENTS_PATH", "")
DOCUMENTS_PATHS = [
    Path(p.strip()) for p in DOCUMENTS_PATH.split(",") if p.strip()
] if DOCUMENTS_PATH else []

# Output directory — written inside the first DOCUMENTS_PATH folder so output
# is co-located with inputs.  The leading underscore and specific name make it
# visually distinct from source files and prevent it being re-scanned on the
# next run (the pipeline excludes this folder from the scan automatically).
OUTPUT_DIR_NAME = "_cobol_transformer_output"
OUTPUT_PATH: Path = (
    DOCUMENTS_PATHS[0] / OUTPUT_DIR_NAME
    if DOCUMENTS_PATHS
    else Path(OUTPUT_DIR_NAME)
)

# Model settings
MODEL_REASONING_EFFORT = os.getenv("MODEL_REASONING_EFFORT", "medium")  # low | medium | high
MODEL_MAX_TOKENS = int(os.getenv("MODEL_MAX_TOKENS", "8000"))

def validate_config():
    """Validate that all required configuration is set."""
    if not AZURE_OPENAI_API_KEY:
        raise ValueError("AZURE_OPENAI_API_KEY not set in .env")
    if not AZURE_OPENAI_ENDPOINT:
        raise ValueError("AZURE_OPENAI_ENDPOINT not set in .env")
    if not AZURE_OPENAI_DEPLOYMENT_NAME:
        raise ValueError("AZURE_OPENAI_DEPLOYMENT_NAME not set in .env")
    return True
