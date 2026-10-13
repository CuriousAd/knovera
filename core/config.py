import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(override=True)

LM_STUDIO_BASE = os.getenv("LM_STUDIO_BASE")
LM_MODEL = os.getenv("LM_MODEL", "local-model")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY")
HF_TOKEN = os.getenv("HF_TOKEN") or os.getenv("HUGGINGFACEHUB_API_TOKEN")

MODEL_PROVIDERS = {
    "Groq - Llama 3.3 70B (Free)": {
        "base_url": "https://api.groq.com/openai/v1",
        "api_key": GROQ_API_KEY or "",
        "model": "llama-3.3-70b-versatile",
    },
    "Mistral - Small (Free)": {
        "base_url": "https://api.mistral.ai/v1",
        "api_key": MISTRAL_API_KEY or "",
        "model": "mistral-small-latest",
    },
    "OpenAI - GPT-4o Mini": {
        "base_url": os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        "api_key": OPENAI_API_KEY or "",
        "model": os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
    },
    "Hugging Face - Qwen 2.5 72B": {
        "base_url": "https://router.huggingface.co/hf-inference/v1",
        "api_key": HF_TOKEN or "",
        "model": os.getenv("HF_INFERENCE_MODEL", "Qwen/Qwen2.5-72B-Instruct"),
    },
    "Local (LM Studio)": {
        "base_url": LM_STUDIO_BASE or "http://127.0.0.1:1234/v1",
        "api_key": os.getenv("LM_STUDIO_API_KEY", "lm-studio"),
        "model": LM_MODEL,
    },
}

_IS_HF_SPACE = os.getenv("SPACE_ID") is not None

AVAILABLE_PROVIDERS = [
    name for name, cfg in MODEL_PROVIDERS.items()
    if cfg.get("api_key") and not (_IS_HF_SPACE and name.startswith("Local"))
]

NO_PROVIDER_PLACEHOLDER = "No API Key Configured (Set GROQ_API_KEY or MISTRAL_API_KEY)"

if not AVAILABLE_PROVIDERS:
    # Fallback placeholder so app startup never fails with IndexError on Hugging Face Spaces
    AVAILABLE_PROVIDERS = [NO_PROVIDER_PLACEHOLDER]

ENABLE_BUCKET_SYNC = os.getenv("ENABLE_BUCKET_SYNC", "false").lower() in ("true", "1", "yes")
HF_BUCKET_URI = os.getenv("HF_BUCKET_URI", "")
NORMALIZE_EMBEDDINGS = True

_ROOT = Path(__file__).parent.parent
DB_NAME = os.getenv("DB_NAME", str(_ROOT / "data" / "vector_db"))
KNOWLEDGE_BASE = Path(os.getenv("KNOWLEDGE_BASE", str(_ROOT / "data" / "raw")))

COLLECTION_NAME = os.getenv("COLLECTION_NAME", "langchain")
EMBEDDING_MODEL_NAME = os.getenv("EMBEDDING_MODEL_NAME", "all-MiniLM-L6-v2")

RETRIEVAL_K = int(os.getenv("RETRIEVAL_K", "10"))
FINAL_K = int(os.getenv("FINAL_K", "6"))
MAX_CONTEXT_CHARS = int(os.getenv("MAX_CONTEXT_CHARS", "12000"))
CHUNK_SIZE = int(os.getenv("CHUNK_SIZE", "400"))
CHUNK_OVERLAP = int(os.getenv("CHUNK_OVERLAP", "100"))
