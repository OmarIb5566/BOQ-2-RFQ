from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Settings:
    # --- LLM (Ollama) ---
    ollama_url: str = os.getenv("OLLAMA_URL", "http://localhost:11434")
    model: str = os.getenv("BOQ_LLM_MODEL", "gemma4:e4b")
    temperature: float = 0.0
    num_ctx: int = 16384
    request_timeout: int = 900
    batch_size: int = 10              # items per classification call
    review_threshold: float = 0.70    # items below this confidence get a second (reflective) look
    use_llm: bool = True              # False -> keyword-only fallback (no Ollama needed)

    # --- paths ---
    workspace: Path = field(default_factory=lambda: Path(os.getenv("BOQ_WORKSPACE", PROJECT_ROOT / "workspace")))
    rules_path: Path = field(default_factory=lambda: PROJECT_ROOT / "boq_agent" / "knowledge" / "flowchart_rules.json")

    # --- OCR for scanned PDFs ---
    tesseract_dir: str = os.getenv("TESSERACT_DIR", r"C:\Program Files\Tesseract-OCR")
    ocr_languages: str = "ara+eng"

    # --- rendering ---
    # "auto": Excel (COM) when available, otherwise the built-in HTML renderer
    excel_renderer: str = os.getenv("BOQ_EXCEL_RENDERER", "auto")   # auto | excel | html
    font_dir: str = r"C:\Windows\Fonts"
    font_file: str = "arial.ttf"

    def ensure_dirs(self) -> None:
        self.workspace.mkdir(parents=True, exist_ok=True)
