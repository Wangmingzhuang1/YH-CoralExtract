"""人工复核工作台：把复核队列、源文件与大模型建议串联，产出 --review-decisions 兼容裁决。

入口：python -m app.review_workbench --run-dir <run> [--source-root .] [--port 8765]
"""

from .server import serve

__all__ = ["serve"]