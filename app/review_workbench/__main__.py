"""CLI：python -m app.review_workbench --run-dir <run> [--source-root .] [--port 8765] [--open]"""

import argparse
from pathlib import Path

from .server import serve


def main():
    parser = argparse.ArgumentParser(description="珊瑚提取人工复核工作台")
    parser.add_argument("--run-dir", help="提取批次目录（默认使用最新的全量 CSV 批次）")
    parser.add_argument("--source-root", default=".", help="源文件根目录（默认当前目录）")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    args = parser.parse_args()
    run = args.run_dir
    if run is None:
        batches = Path(args.source_root) / "output" / "moa_output"
        run = max((path for path in batches.glob("run_*") if (path / "review_queue.csv").is_file()
                   and (path / "consolidated" / "all_data_combined.csv").is_file()), default=None)
        if run is None:
            parser.error("找不到可用批次，请通过 --run-dir 指定")
    serve(run, args.source_root, args.port, args.open)


if __name__ == "__main__":
    main()
