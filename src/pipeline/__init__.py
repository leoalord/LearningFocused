"""
Pipeline package.

- `src.pipeline.audio`: canonical audio/podcast pipeline implementation
- `src.pipeline.substack`: Substack/article pipeline implementation
- `src.pipeline.youtube`: YouTube unique ingest (TASK-5)

All audio implementations live under `src.pipeline.audio`.
"""

from src.pipeline import audio, substack

__all__ = ["audio", "substack"]


