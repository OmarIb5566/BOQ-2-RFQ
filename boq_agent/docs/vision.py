"""The vision model (qwen2.5vl via Ollama) — used ONLY for pages without a text layer.

Rules it follows:
* transcribe, never interpret: it copies what is printed; matching to packages happens afterwards in code;
* find first, read second: one whole-sheet pass (legend + where the title block is), then a sharper crop
  of only the title block for the drawing number / title;
* "" / [] is always an allowed answer (not visible / not legible);
* output is capped (num_predict) and repeated lines are dropped in code — qwen2.5vl can loop on dense sheets;
* every answer is cached by image hash + model + prompt version.
"""
from __future__ import annotations

import base64
import hashlib
from typing import Callable, Optional

from ..llm.client import LLMError, OllamaClient
from .common import Cache, key_of

PROMPT_VERSION = "v1"

_SHEET_SCHEMA = {
    "type": "object",
    "properties": {
        "is_drawing": {"type": "boolean"},
        "sheet_title": {"type": "string"},
        "discipline": {"type": "string"},
        "title_block": {"type": "string", "enum": ["bottom-right", "right-strip", "bottom-strip", "bottom-left",
                                                    "top-right", "left-strip", "none"]},
        "legend_items": {"type": "array", "items": {"type": "string"}, "maxItems": 30},
    },
    "required": ["is_drawing", "sheet_title", "discipline", "title_block", "legend_items"],
}
_SHEET_PROMPT = (
    "This image is one page of a construction document set. Transcribe exactly what is printed; do not interpret, "
    "translate or invent anything.\n"
    "- is_drawing: true if the page is a drawing sheet (plan, elevation, section, detail, schedule); false for a "
    "letter, table, vendor list, photo or text page.\n"
    "- sheet_title: the sheet title as printed in the title block (\"\" if not legible).\n"
    "- discipline: architectural / structural / mechanical / electrical / plumbing / fire fighting / landscape / "
    "civil / interior, only if printed or obvious from the title block; else \"\".\n"
    "- title_block: where the title block is on the page.\n"
    "- legend_items: each material / finish / legend / schedule entry, verbatim, each entry once. [] if none.")

_TB_SCHEMA = {
    "type": "object",
    "properties": {"drawing_number": {"type": "string"}, "sheet_title": {"type": "string"},
                   "revision": {"type": "string"}},
    "required": ["drawing_number", "sheet_title", "revision"],
}
_TB_PROMPT = ("This image is the title block of a construction drawing. Copy exactly as printed: the drawing number "
              "(the field labelled Drawing No / Dwg No / Sheet No or similar), the sheet title, and the revision. "
              "Use \"\" for anything not visible or not legible.")

# fraction of the page (x0, y0, x1, y1) for each title-block position
_TB_BOX = {"bottom-right": (0.55, 0.62, 1, 1), "right-strip": (0.78, 0, 1, 1), "bottom-strip": (0, 0.8, 1, 1),
           "bottom-left": (0, 0.62, 0.45, 1), "top-right": (0.55, 0, 1, 0.38), "left-strip": (0, 0, 0.22, 1)}


class Vision:
    def __init__(self, settings, cache: Cache, log: Callable[[str], None] = print):
        self.s = settings
        self.cache = cache
        self.log = log
        self.client = OllamaClient(settings.ollama_url, settings.vision_model, 0.0, 8192, settings.request_timeout)
        self._ok: Optional[bool] = None
        self.used = False

    @property
    def ok(self) -> bool:
        if self._ok is None:
            self._ok, msg = self.client.status()
            if not self._ok:
                self.log(f"Vision model unavailable ({msg}) — scanned sheets will need manual review.")
        return self._ok

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def render(page, long_px: int, frac=(0, 0, 1, 1)) -> bytes:
        import pymupdf as fitz

        r = page.rect
        clip = fitz.Rect(r.x0 + frac[0] * r.width, r.y0 + frac[1] * r.height,
                         r.x0 + frac[2] * r.width, r.y0 + frac[3] * r.height)
        z = long_px / max(clip.width, clip.height)
        return page.get_pixmap(matrix=fitz.Matrix(z, z), clip=clip, colorspace=fitz.csGRAY).tobytes("png")

    def _ask(self, op: str, prompt: str, schema: dict, png: bytes) -> Optional[dict]:
        k = key_of(op, hashlib.sha256(png).hexdigest(), self.s.vision_model, PROMPT_VERSION, self.s.vision_max_tokens)
        hit = self.cache.get("vision", k)
        if hit is not None:
            return hit
        if not self.ok:
            return None
        self.used = True
        try:
            ans = self.client.chat_json("", prompt, schema, images=[base64.b64encode(png).decode()], retries=0,
                                        op=op, max_tokens=self.s.vision_max_tokens)
        except LLMError:
            # output cut off by the cap (a repetition loop) or a bad reply: one retry with a smaller budget
            try:
                ans = self.client.chat_json("", prompt + "\nKeep the answer short.", schema, retries=0, op=op + "-retry",
                                            images=[base64.b64encode(png).decode()],
                                            max_tokens=self.s.vision_max_tokens // 2)
            except LLMError as e:
                self.log(f"Vision model could not read a page: {e}")
                return None
        self.cache.put("vision", k, ans)
        return ans

    # ---------------------------------------------------------------- reads
    def read_sheet(self, page) -> Optional[dict]:
        """Whole sheet at moderate resolution, then the title-block crop at higher resolution."""
        a = self._ask("sheet", _SHEET_PROMPT, _SHEET_SCHEMA, self.render(page, self.s.vision_px))
        if a is None:
            return None
        seen, legend = set(), []
        for x in a.get("legend_items", []):
            x = " ".join(str(x).strip(" []").split())
            if x and x.lower() not in seen and len(x) <= 200:
                seen.add(x.lower())
                legend.append(x)
        out = {"is_drawing": bool(a.get("is_drawing")), "title": a.get("sheet_title", "").strip(),
               "discipline": a.get("discipline", "").strip(), "legend": legend, "number": ""}
        box = _TB_BOX.get(a.get("title_block", "none"))
        if out["is_drawing"] and box:
            tb = self._ask("title-block", _TB_PROMPT, _TB_SCHEMA, self.render(page, self.s.vision_crop_px, box))
            if tb:
                out["number"] = tb.get("drawing_number", "").strip()
                out["title"] = tb.get("sheet_title", "").strip() or out["title"]
        return out

    def read_header(self, page) -> str:
        """Running header of a scanned spec page (top 14 %) -> its text, for the SECTION number."""
        schema = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}
        a = self._ask("spec-header", "Copy the printed text of this page header exactly, as one line. \"\" if none.",
                      schema, self.render(page, 1400, (0, 0, 1, 0.14)))
        return (a or {}).get("text", "")
