"""CLI：python -m app.release --run-dir <批次> --out <发布目录> [--version 1.0.0] [--no-grid]"""
import argparse
import logging

from .packager import build_release


def main():
    parser = argparse.ArgumentParser(description="把 MOA 提取批次打包为 Zenodo 分层发布数据集")
    parser.add_argument("--run-dir", required=True, help="提取批次目录（含 consolidated/all_data_combined.csv）")
    parser.add_argument("--out", required=True, help="发布包输出目录")
    parser.add_argument("--version", default="1.0.0", help="数据集版本号")
    parser.add_argument("--no-grid", action="store_true", help="不复制 2GB 格点 netCDF")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    build_release(args.run_dir, args.out, args.version, include_grid=not args.no_grid)


if __name__ == "__main__":
    main()