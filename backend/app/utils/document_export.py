import io
from typing import Optional, Dict, Any
from backend.app.utils.docx_skill import build_docx_document

def markdown_to_docx(markdown_text: str, title: str = "MIMIR Oracle Research Report", session_meta: Optional[Dict[str, Any]] = None) -> io.BytesIO:
    """
    Converts structured markdown into a styled, professional Word (.docx) document
    using the Create DOCX Skill Engine.
    """
    return build_docx_document(title=title, markdown_content=markdown_text, session_meta=session_meta)

