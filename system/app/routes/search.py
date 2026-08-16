from __future__ import annotations

from fastapi import APIRouter, Query, Request

from .. import queries as Q
from ..db import db
from ..viewmodels import normalize_spus
from ..web import templates

router = APIRouter()
SORT_KEYS = frozenset(("grade", "ratio", "evidence", "star", "issues"))


def _keyword_params(keyword: str) -> tuple[str, str, str, str]:
    pattern = f"%{keyword}%"
    return keyword, pattern, pattern, pattern


def _sort_state(sort: str, direction: str) -> tuple[str, str]:
    key = sort.strip()
    value = direction.strip().lower()
    return (key, value) if key in SORT_KEYS and value in ("asc", "desc") else ("", "")


def _tag_options(rows, domain: str, sub: str, leaf: str):
    domain_rows = [row for row in rows if row.get("level") == "domain"]
    sub_rows = [row for row in rows if row.get("level") == "sub"]
    leaf_rows = [row for row in rows if row.get("level") == "leaf"]

    domains = {str(row.get("domain") or "") for row in domain_rows}
    selected_domain = domain if domain in domains else ""
    subs = {
        (str(row.get("domain") or ""), str(row.get("sub") or ""))
        for row in sub_rows
    }
    selected_sub = (
        sub if selected_domain and (selected_domain, sub) in subs else ""
    )
    leaves = {
        (
            str(row.get("domain") or ""),
            str(row.get("sub") or ""),
            str(row.get("leaf") or ""),
        )
        for row in leaf_rows
    }
    selected_leaf = (
        leaf
        if selected_sub and (selected_domain, selected_sub, leaf) in leaves
        else ""
    )

    visible_subs = [
        row for row in sub_rows
        if not selected_domain or row.get("domain") == selected_domain
    ]
    visible_leaves = [
        row for row in leaf_rows
        if (not selected_domain or row.get("domain") == selected_domain)
        and (not selected_sub or row.get("sub") == selected_sub)
    ]
    return (
        selected_domain,
        selected_sub,
        selected_leaf,
        {"domains": domain_rows, "subs": visible_subs, "leaves": visible_leaves},
    )


@router.get("/search")
def product_search(
    request: Request,
    q: str = Query(""),
    category: str = Query(""),
    domain: str = Query(""),
    sub: str = Query(""),
    leaf: str = Query(""),
    sort: str = Query(""),
    dir: str = Query(""),
):
    keyword = q.strip()
    selected_category = category.strip()
    keyword_params = _keyword_params(keyword)
    facet_rows = db.query(Q.SEARCH_FACETS)
    filter_params = (*keyword_params, selected_category, selected_category)
    tag_rows = db.query(Q.SEARCH_TAG_FACETS, filter_params)
    selected_domain, selected_sub, selected_leaf, tag_options = _tag_options(
        tag_rows, domain.strip(), sub.strip(), leaf.strip()
    )
    sort_key, sort_dir = _sort_state(sort, dir)

    result_query = Q.SEARCH_SPUS
    tag_params: tuple[str, ...] = ()
    if selected_leaf:
        result_query = Q.SEARCH_SPUS_BY_LEAF
        tag_params = (selected_domain, selected_sub, selected_leaf)
    elif selected_sub:
        result_query = Q.SEARCH_SPUS_BY_SUB
        tag_params = (selected_domain, selected_sub)
    elif selected_domain:
        result_query = Q.SEARCH_SPUS_BY_DOMAIN
        tag_params = (selected_domain,)
    rows = normalize_spus(db.query(
        result_query,
        (*filter_params, *tag_params, sort_key, sort_dir),
    ))

    category_counts = {
        str(row.get("value") or ""): int(row.get("count") or 0)
        for row in facet_rows
    }
    categories = sorted(value for value in category_counts if value != "__all__")
    if selected_category and selected_category not in categories:
        categories.append(selected_category)

    return templates.TemplateResponse(request, "product-search.html", {
        "rows": rows,
        "filters": {
            "q": keyword,
            "category": selected_category,
            "domain": selected_domain,
            "sub": selected_sub,
            "leaf": selected_leaf,
        },
        "facets": {"category": category_counts},
        "categories": categories,
        "tag_options": tag_options,
        "selected_path": [
            part for part in (selected_domain, selected_sub, selected_leaf) if part
        ],
        "sorting": {"key": sort_key, "dir": sort_dir},
        "max_negative": max(
            (
                int(
                    row.get("max_negative_evi_count")
                    or row.get("negative_evi_count")
                    or 0
                )
                for row in rows
            ),
            default=0,
        ),
    })
