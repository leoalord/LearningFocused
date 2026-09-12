"""Normalize provider-specific message content into readable text.

Gemini returns message content as a list of blocks like
`[{"type": "text", "text": "...", "extras": {"signature": "<kilobytes of base64>"}}]`.
Interpolating that into an f-string prints a Python repr, so callers that show
content to a human must go through `format_message_content` first.
"""

from __future__ import annotations

from typing import Any


def format_message_content(content: Any) -> str:
    """Extract the user-visible text from a message content payload."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(text)
            elif isinstance(item, str) and item.strip():
                parts.append(item)
        return "\n".join(parts).strip()
    if isinstance(content, dict):
        text = content.get("text")
        if isinstance(text, str):
            return text
    return str(content)
