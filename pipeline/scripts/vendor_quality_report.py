#!/usr/bin/env python3
"""云听数据质量报告：量化上游归属缺口，供对服务商提要求。

只读，不调 LLM，不写库。

背景：社媒的产品归属完全依赖云听打的 SPU 标签。评论本身很少提产品名
（「我的电池才12分钟就用完了」），身份在原帖里；我们靠组内继承把原帖的
SPU 传给评论。029 起继承只从原帖/视频这一级采集——同级继承会让一条评论
被识别出的 SPU 灌满整个评论区（实测单组最多注入 196 条）。

因此上游有两个必须达标的点：
  1. 原帖/视频必须打上 SPU——它是整个评论区归属的唯一来源
  2. 归属必须准确——错一个原帖，污染的是它下面所有评论

原帖漏标的直接后果：整组无归属。这些反馈内容质量往往不差，但没有产品
身份就无法进入老品迭代，只能落「缺陷不可归属」终态。我们不从正文自行
提取型号来补——那会引入一层不可控误差，并掩盖上游的质量问题。

用法：
  cd /home/sdy/voc-analytics
  set -a; . ./.env; set +a
  .venv/bin/python scripts/vendor_quality_report.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from voc_analytics import db  # noqa: E402

ROOT_COVERAGE = """
SELECT count(*) AS roots,
       count(*) FILTER (WHERE COALESCE(cardinality(spu),0) > 0) AS tagged,
       count(*) FILTER (WHERE COALESCE(cardinality(spu),0) = 0) AS untagged
  FROM voc_message
 WHERE src_line = '社媒' AND message_type IN ('帖子','视频')
"""

ORPHAN_GROUPS = """
SELECT count(*) AS groups_total,
       count(*) FILTER (WHERE NOT has_root) AS groups_without_root,
       count(*) FILTER (WHERE has_root AND NOT root_tagged) AS groups_root_untagged,
       sum(comments) FILTER (WHERE NOT has_root OR NOT root_tagged) AS comments_unattributable
  FROM (
    SELECT m.message_group_id,
           bool_or(m.message_type IN ('帖子','视频')) AS has_root,
           bool_or(m.message_type IN ('帖子','视频')
                   AND COALESCE(cardinality(m.spu),0) > 0) AS root_tagged,
           count(*) FILTER (WHERE m.message_type NOT IN ('帖子','视频')) AS comments
      FROM voc_message m
     WHERE m.src_line = '社媒' AND m.message_group_id IS NOT NULL
     GROUP BY 1) g
"""

# 有价值但无归属：过了 G4 价值门、却因上游没给 SPU 而无法进老品的缺陷。
LOST_DEFECTS = """
SELECT count(*) AS defects_unattributable
  FROM voc_message m
  JOIN voc_social_gate g ON g.message_id = m.message_id
  LEFT JOIN LATERAL (
    SELECT bool_or(COALESCE(cardinality(r.spu),0) > 0) AS root_tagged
      FROM voc_message r
     WHERE r.message_group_id = m.message_group_id
       AND r.message_type IN ('帖子','视频')) rt ON true
 WHERE m.src_line = '社媒'
   AND g.cls = '产品缺陷'
   AND COALESCE(cardinality(m.spu),0) = 0
   AND COALESCE(rt.root_tagged, false) = false
"""

PARENT_CHAIN = """
SELECT count(*) AS social_messages,
       count(*) FILTER (WHERE parent_id IS NOT NULL) AS with_parent_id,
       count(*) FILTER (WHERE parent_id IS NOT NULL AND EXISTS (
         SELECT 1 FROM voc_message p WHERE p.message_id = m.parent_id)) AS parent_resolvable
  FROM voc_message m
 WHERE m.src_line = '社媒'
"""


def _pct(part: int, whole: int) -> str:
    return f"{100.0 * part / whole:.1f}%" if whole else "—"


def main() -> None:
    root = db.q(ROOT_COVERAGE)[0]
    grp = db.q(ORPHAN_GROUPS)[0]
    lost = db.q(LOST_DEFECTS)[0]
    par = db.q(PARENT_CHAIN)[0]

    print("=" * 62)
    print("云听社媒数据质量报告（只读）")
    print("=" * 62)

    print("\n【缺口一】原帖/视频未打 SPU —— 整个评论区因此失去归属")
    print(f"  原帖总数            {root['roots']:>7}")
    print(f"  已打 SPU            {root['tagged']:>7}  ({_pct(root['tagged'], root['roots'])})")
    print(f"  未打 SPU            {root['untagged']:>7}  ({_pct(root['untagged'], root['roots'])})  ← 未达标")

    print("\n【缺口二】评论组无法归属")
    print(f"  社媒评论组总数      {grp['groups_total']:>7}")
    print(f"  组内无原帖          {grp['groups_without_root']:>7}  （原帖未导出）")
    print(f"  有原帖但原帖无标    {grp['groups_root_untagged']:>7}")
    print(f"  受影响的评论条数    {grp['comments_unattributable'] or 0:>7}")

    print("\n【缺口三】父链不可用 —— 无法按层级继承，只能退到组级")
    print(f"  社媒消息            {par['social_messages']:>7}")
    print(f"  带 parent_id        {par['with_parent_id']:>7}")
    print(f"  parent_id 能解析    {par['parent_resolvable']:>7}  ← 期望等于上一行")

    print("\n【业务后果】")
    print(f"  有价值但无法归属的产品缺陷    {lost['defects_unattributable']:>5} 条")
    print("  这些反馈已通过价值门（确认是真实产品缺陷），仅因上游未给出")
    print("  产品归属而无法进入老品迭代，落「缺陷不可归属」终态。")

    print("\n【对服务商的要求】")
    print("  1. 帖子/视频层的 SPU 标注必须覆盖 —— 它是整个评论区归属的唯一来源")
    print("  2. 评论所属的帖子/视频本体必须一并导出")
    print("  3. parent_id 需与 message_id 同命名空间，否则父链继承无法实现")
    print()


if __name__ == "__main__":
    main()
