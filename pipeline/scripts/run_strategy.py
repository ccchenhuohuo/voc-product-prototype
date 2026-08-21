#!/usr/bin/env python3
"""独立运行战略轴聚合。

生产运行会连接数据库并调用 LLM；仓库离线验收只导入并 monkeypatch 本入口。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

# 按脚本自身位置解析包根，本机与服务器共用同一份入口。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from voc_analytics import config as C, llm  # noqa: E402
from voc_analytics.strategy import (  # noqa: E402
    MaxPairsExceeded,
    run_strategy,
)


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--type", choices=("通病", "诉求", "all"), default="all")
    parser.add_argument("--generation", choices=("OPP-", "OPP2-"),
                        default=C.STRATEGY_GENERATION)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--run-id")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    llm.reset_usage()
    try:
        result = run_strategy(
            axis_type=args.type,
            generation=args.generation,
            dry_run=args.dry_run,
            run_id=args.run_id,
        )
    except MaxPairsExceeded as error:
        print(
            f"!! 战略召回中止：实际 {error.actual} 对，"
            f"上限 {error.limit} 对",
            file=sys.stderr,
        )
        return 2
    except BaseException as error:
        print(
            f"!! strategy 失败：{type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 1

    metrics = result["metrics"]
    print(
        f"战略聚合完成：召回 {metrics['pairs_recalled']} 对 / "
        f"实际判定 {metrics['pairs_evaluated']} 对 / "
        f"轴 {len(result['axes'])} 根 / 成员 {len(result['members'])} 行"
    )
    if args.dry_run:
        print(json.dumps(result["axes"], ensure_ascii=False, indent=2,
                         default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
