"""命令行入口：python -m quota_service --host 127.0.0.1 --port 8080 --db data/quota.json"""
from __future__ import annotations

import argparse

from .api import serve


def main() -> None:
    parser = argparse.ArgumentParser(description="国际培训名额管理服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--db", default="data/quota.json", help="JSON 数据文件路径")
    args = parser.parse_args()
    serve(args.host, args.port, args.db)


if __name__ == "__main__":
    main()
