"""Zenodo REST 上传器：create_deposit / upload_files / publish。

- 默认 dry-run：只打印将上传的文件清单与元数据，不发起任何请求；
  只有显式 --publish 才真正创建 deposit、上传并发布。
- --sandbox 指向 https://sandbox.zenodo.org/api（先沙箱测试）。
- token 从 .env / 环境变量读取（app.core.config settings：ZENODO_TOKEN，
  沙箱可用 ZENODO_SANDBOX_TOKEN 覆盖），不硬编码。
- 目录内文件递归上传；Zenodo 文件名为扁平键，子目录相对路径以 "__"
  连接（如 02_compilation__observations.csv），保留四层结构信息。
- Zenodo 每 deposit 上限 100 文件：上传前 archive_large_dirs 自动把文件数
  超阈值的子目录打包为同名 zip（幂等）。大文件按大小自适应超时并重试。
- --draft 创建草稿+上传但不公开，生成分享链接供评审；--publish-id 评审后发布。

CLI:
  python -m app.release.zenodo --package output/zenodo_release_v1.1.0 \
      [--title ...] [--metadata metadata.json] [--sandbox] [--dry-run] [--draft] [--publish]
"""
import argparse
import json
import logging
import os
import sys
from pathlib import Path

import requests

from app.core.config import settings

logger = logging.getLogger(__name__)

API_URL = "https://zenodo.org/api"
SANDBOX_API_URL = "https://sandbox.zenodo.org/api"
TIMEOUT = 120


def api_url(sandbox: bool = False) -> str:
    return SANDBOX_API_URL if sandbox else API_URL


def resolve_token(sandbox: bool = False) -> str:
    """settings(.env) 优先，环境变量兜底；沙箱可用独立 token。"""
    if sandbox:
        token = (settings.ZENODO_SANDBOX_TOKEN or settings.ZENODO_TOKEN
                 or os.environ.get("ZENODO_SANDBOX_TOKEN", "")
                 or os.environ.get("ZENODO_TOKEN", ""))
    else:
        token = settings.ZENODO_TOKEN or os.environ.get("ZENODO_TOKEN", "")
    if not token:
        raise SystemExit("缺少 Zenodo token：请在 .env 配置 ZENODO_TOKEN"
                         "（沙箱测试可另配 ZENODO_SANDBOX_TOKEN）")
    return token


def _headers(token: str, content_type: str = "application/json") -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": content_type}


def create_deposit(metadata: dict, token: str, sandbox: bool = False) -> dict:
    """创建草稿 deposit，返回含 id 与 links.bucket 的响应 JSON。"""
    response = requests.post(f"{api_url(sandbox)}/deposit/depositions",
                             json={"metadata": metadata},
                             headers=_headers(token), timeout=TIMEOUT)
    response.raise_for_status()
    deposition = response.json()
    logger.info("deposit 已创建: id=%s", deposition.get("id"))
    return deposition


def iter_package_files(directory):
    """递归列出发布目录内全部文件：(相对路径 Posix, 绝对路径)，按名排序。"""
    root = Path(directory)
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        yield path.relative_to(root).as_posix(), path


def archive_large_dirs(package, threshold=50):
    """Zenodo 每 deposit 默认上限 100 文件。把文件数 >threshold 的子目录打包为
    同名 zip（保留目录内相对路径），原地替换，使总文件数降到上限内。幂等。"""
    import shutil
    import zipfile
    root = Path(package)
    for item in sorted(root.iterdir()):
        if not item.is_dir():
            continue
        files = sorted(p for p in item.rglob("*") if p.is_file())
        if len(files) <= threshold:
            continue
        zip_path = root / f"{item.name}.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in files:
                zf.write(path, path.relative_to(root).as_posix())
        shutil.rmtree(item)
        logger.info("打包 %s（%d 文件）-> %s", item.name, len(files), zip_path.name)


def _upload_timeout(size: int):
    """按文件大小给出 (连接, 读取) 超时。大文件（如 2GB NetCDF）需要更长读取
    超时等待 Zenodo 落盘响应；上限 1 小时，避免无限挂起。"""
    return (30, max(300, min(3600, size // 200_000)))


def upload_files(deposit_id, directory, token: str, sandbox: bool = False) -> list:
    """把目录内文件逐个上传到 deposit 的文件桶，返回上传的文件名列表。

    大文件按大小自适应超时，并对瞬时网络错误重试 3 次（PUT 按文件名幂等）。"""
    base = api_url(sandbox)
    response = requests.get(f"{base}/deposit/depositions/{deposit_id}",
                            headers=_headers(token), timeout=TIMEOUT)
    response.raise_for_status()
    bucket = response.json()["links"]["bucket"]
    uploaded = []
    for relative, path in iter_package_files(directory):
        filename = relative.replace("/", "__")
        size = path.stat().st_size
        timeout = _upload_timeout(size)
        for attempt in range(1, 4):
            try:
                with open(path, "rb") as stream:
                    put = requests.put(f"{bucket}/{filename}", data=stream,
                                       headers=_headers(token, "application/octet-stream"),
                                       timeout=timeout)
                put.raise_for_status()
                break
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError,
                    requests.exceptions.ChunkedEncodingError) as exc:
                if attempt == 3:
                    raise
                logger.warning("上传 %s 第 %d 次失败（%s），重试…", filename, attempt, exc)
        uploaded.append(filename)
        logger.info("已上传 %s (%d bytes)", filename, size)
    return uploaded


def publish(deposit_id, token: str, sandbox: bool = False) -> dict:
    """发布 deposit（不可逆），返回响应 JSON（含 DOI）。"""
    response = requests.post(
        f"{api_url(sandbox)}/deposit/depositions/{deposit_id}/actions/publish",
        headers=_headers(token), timeout=TIMEOUT)
    response.raise_for_status()
    result = response.json()
    logger.info("已发布: %s", result.get("doi_url") or result.get("doi"))
    return result


def create_secret_link(deposit_id, token: str, sandbox: bool = False) -> dict:
    """为草稿 deposit 生成分享链接（preview 权限，供评审匿名访问；不公开）。

    Zenodo（InvenioRDM）草稿的 secret link 走 /api/records/{id}/access/links，
    permission=preview；分享 URL 为 /records/{id}?preview=1&token=<link_token>。
    注意 link_token 是分享链接自带的 JWT（可公开给评审），与 API 鉴权 token 不同。
    若端点不可用则回退提示在 Zenodo UI 的 Share 页手动生成。
    """
    base = api_url(sandbox)
    host = "https://sandbox.zenodo.org" if sandbox else "https://zenodo.org"
    response = requests.post(f"{base}/records/{deposit_id}/access/links",
                             json={"permission": "preview"},
                             headers=_headers(token), timeout=TIMEOUT)
    if response.status_code in (200, 201):
        result = response.json()
        return {"html": f"{host}/records/{deposit_id}?preview=1&token={result['token']}",
                "id": result.get("id")}
    logger.warning("分享链接 API 返回 %s，请在 Zenodo UI 的 Share 页手动生成 secret link",
                   response.status_code)
    return {"html": f"{host}/deposit/{deposit_id}",
            "note": "请在 UI Share 页生成 secret link"}


def set_access_conditions(deposit_id, token: str, access: str, sandbox: bool = False,
                          embargo_date=None) -> dict:
    """设置访问条件：open/restricted/embargoed/closed（发布时生效）。"""
    metadata = {"access_conditions": access}
    if access == "embargoed" and embargo_date:
        metadata["embargo_date"] = embargo_date
    response = requests.put(
        f"{api_url(sandbox)}/deposit/depositions/{deposit_id}",
        json={"metadata": metadata}, headers=_headers(token), timeout=TIMEOUT)
    response.raise_for_status()
    return response.json()


def load_metadata(package: Path, metadata_path=None, title=None) -> dict:
    path = Path(metadata_path) if metadata_path else package / "metadata.json"
    if path.is_file():
        metadata = json.loads(path.read_text(encoding="utf-8"))
    else:
        metadata = {"title": package.name, "upload_type": "dataset",
                    "creators": [{"name": "[AUTHOR_NAME]"}],
                    "description": "[DESCRIPTION]", "license": "cc-by-4.0"}
    if title:
        metadata["title"] = title
    return metadata


def dry_run(package: Path, metadata: dict, sandbox: bool = False) -> dict:
    """只打印文件清单与元数据，不发起任何网络请求。"""
    files = [(relative, path.stat().st_size) for relative, path in iter_package_files(package)]
    total = sum(size for _, size in files)
    print(f"[dry-run] 目标 API: {api_url(sandbox)}（未发起请求）")
    print(f"[dry-run] 发布目录: {package}")
    print(f"[dry-run] 将上传 {len(files)} 个文件，共 {total / 1e6:.1f} MB：")
    for relative, size in files:
        print(f"  {relative.replace('/', '__')}  ({size:,} bytes)")
    print("[dry-run] 元数据：")
    print(json.dumps(metadata, ensure_ascii=False, indent=2))
    return {"files": [{"name": rel.replace("/", "__"), "size": size} for rel, size in files],
            "total_bytes": total, "metadata": metadata}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Zenodo 数据集上传器（默认 dry-run；--draft 上传不发布；--publish 创建+上传+发布）")
    parser.add_argument("--package", help="发布目录（四层结构）")
    parser.add_argument("--title", help="覆盖 metadata 中的 title")
    parser.add_argument("--metadata", help="metadata.json 路径（默认 <package>/metadata.json）")
    parser.add_argument("--sandbox", action="store_true", help="使用 sandbox.zenodo.org 测试")
    parser.add_argument("--dry-run", action="store_true", help="只打印清单与元数据（默认行为）")
    parser.add_argument("--draft", action="store_true", help="创建草稿+上传文件，不发布；生成分享链接供评审")
    parser.add_argument("--publish", action="store_true", help="创建 deposit、上传并立即发布（不可逆）")
    parser.add_argument("--publish-id", help="发布已存在的草稿 deposit（仅发布，不创建/上传）")
    parser.add_argument("--update-id", help="更新已存在草稿的文件与元数据（不新建、不发布，保持 deposit ID）")
    parser.add_argument("--access", choices=["open", "restricted", "embargoed", "closed"],
                        help="发布时的访问条件（默认 open）")
    parser.add_argument("--embargo-date", help="embargoed 访问的解禁日期 YYYY-MM-DD")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    token = resolve_token(args.sandbox)

    # 模式1：发布已有草稿
    if args.publish_id:
        try:
            if args.access:
                set_access_conditions(args.publish_id, token, args.access, args.sandbox, args.embargo_date)
            result = publish(args.publish_id, token, args.sandbox)
        except requests.HTTPError as exc:
            detail = exc.response.text[:500] if exc.response is not None else str(exc)
            print(f"Zenodo API 失败: {exc}\n{detail}", file=sys.stderr)
            return 1
        print(f"发布完成: id={args.publish_id} DOI={result.get('doi')} "
              f"链接={result.get('links', {}).get('html', '')}")
        return 0

    if not args.package:
        parser.error("需要 --package（或 --publish-id 发布已有草稿）")
    package = Path(args.package)
    if not package.is_dir():
        parser.error(f"发布目录不存在: {package}")
    metadata = load_metadata(package, args.metadata, args.title)

    # 模式1b：更新已有草稿（保持 ID，不新建/不发布）
    if args.update_id:
        try:
            archive_large_dirs(package)
            upload_files(args.update_id, package, token, args.sandbox)
            requests.put(f"{api_url(args.sandbox)}/deposit/depositions/{args.update_id}",
                         json={"metadata": metadata}, headers=_headers(token),
                         timeout=TIMEOUT).raise_for_status()
        except requests.HTTPError as exc:
            detail = exc.response.text[:500] if exc.response is not None else str(exc)
            print(f"Zenodo API 失败: {exc}\n{detail}", file=sys.stderr)
            return 1
        print(f"草稿已更新（未公开，ID 不变）: id={args.update_id}")
        print(f"  草稿地址: https://{'sandbox.' if args.sandbox else ''}zenodo.org/deposit/{args.update_id}")
        return 0

    # 模式2：dry-run（默认）
    if args.dry_run or not (args.publish or args.draft):
        dry_run(package, metadata, args.sandbox)
        if not (args.publish or args.draft):
            print("[dry-run] 未上传。--draft 上传为草稿（不公开，可分享链接评审）；--publish 创建+上传+立即发布。")
        return 0

    # 模式3：草稿上传（不发布）或 创建+上传+发布
    try:
        archive_large_dirs(package)
        deposition = create_deposit(metadata, token, args.sandbox)
        deposit_id = deposition["id"]
        upload_files(deposit_id, package, token, args.sandbox)
        if args.draft:
            link = create_secret_link(deposit_id, token, args.sandbox)
            print(f"草稿已上传（未公开）: id={deposit_id}")
            print(f"  草稿地址: https://{'sandbox.' if args.sandbox else ''}zenodo.org/deposit/{deposit_id}")
            print(f"  分享链接: {link.get('html') or link}")
            print(f"  评审确认后发布: python -m app.release.zenodo --publish-id {deposit_id}"
                  f"{' --access ' + args.access if args.access else ''}")
            return 0
        if args.access:
            set_access_conditions(deposit_id, token, args.access, args.sandbox, args.embargo_date)
        result = publish(deposit_id, token, args.sandbox)
    except requests.HTTPError as exc:
        detail = exc.response.text[:500] if exc.response is not None else str(exc)
        print(f"Zenodo API 失败: {exc}\n{detail}", file=sys.stderr)
        return 1
    print(f"发布完成: id={deposit_id} DOI={result.get('doi')} "
          f"链接={result.get('links', {}).get('html', '')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())