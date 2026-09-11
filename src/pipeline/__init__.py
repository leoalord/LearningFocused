"""
Pipeline package.

- `src.pipeline.audio`: canonical audio/podcast pipeline implementation
- `src.pipeline.substack`: Substack/article pipeline implementation
- `src.pipeline.youtube`: YouTube ingest (TASK-4 stub; TASK-5 implements)

All audio implementations live under `src.pipeline.audio`.
"""

from src.pipeline import audio, substack

__all__ = ["audio", "substack"]


