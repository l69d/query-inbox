"""Break a query into its steps (CTEs + final SELECT) for the Query inbox viewer, and flag likely mistakes.

analyze(sql, name, lint=None) -> {"dialect", "error", "issues", "steps"}; each step lists what it reads, its joins,
filters, grouping and output columns. Self-check: python sqlviz.py
"""
import re

import sqlglot
from sqlglot import exp


def sql_of(node):
    return node.sql(dialect=DIALECT[0], pretty=True) if node is not None else None


DIALECT = ["bigquery"]  # set per call; sql_of reads it


def branches(query):
    """A UNION is several SELECTs; give them back flat."""
    if isinstance(query, exp.SetOperation):
        return branches(query.left) + branches(query.right)
    if isinstance(query, exp.Subquery):
        return branches(query.this)
    return [query] if isinstance(query, exp.Select) else []


def describe_select(sel, ctes, label):
    issues = []
    from_ = sel.args.get("from_")
    joins = sel.args.get("joins") or []
    sources = [from_.this] if from_ else []
    sources += [j.this for j in joins]
    aliases = [s.alias_or_name for s in sources]

    join_rows = []
    left_joined = set()
    for j in joins:
        kind = " ".join(x for x in (j.side, j.kind) if x) or "INNER"
        on = j.args.get("on")
        join_rows.append({"type": kind + " JOIN", "target": sql_of(j.this), "on": sql_of(on),
                          "using": [c.name for c in j.args.get("using") or []]})
        if on is None and not j.args.get("using") and "CROSS" not in kind.upper() and not isinstance(j.this, exp.Unnest):
            issues.append(f"{label}: {kind} JOIN {j.this.alias_or_name} has no ON condition")
        if isinstance(on, (exp.Tuple, exp.Column, exp.Literal)):  # 'ON a.x, b.x' parses as a tuple
            issues.append(f"{label}: JOIN {j.this.alias_or_name} ON {sql_of(on)} is not a condition -- did you mean '=' instead of ','?")
        if j.side == "LEFT":
            left_joined.add(j.this.alias_or_name)

    where = sel.args.get("where")
    if where is not None and left_joined:
        for col in where.find_all(exp.Column):
            # a filter on the right side of a LEFT JOIN drops its NULL rows, making it an INNER JOIN
            if col.table in left_joined and not isinstance(col.parent, exp.Is) and not col.find_ancestor(exp.Coalesce):
                issues.append(f"{label}: WHERE filters on {col.table}.{col.name}, the right side of a LEFT JOIN -- "
                              "this turns it into an INNER JOIN (move it into ON if that's not intended)")
                break

    if len(sources) > 1:
        unq = sorted({c.name for c in sel.find_all(exp.Column)
                      if not c.table and c.find_ancestor(exp.Select) is sel and c.name not in {e.alias for e in sel.expressions if isinstance(e, exp.Alias)}
                      and not c.find_ancestor(exp.Order)})
        if unq:
            issues.append(f"{label}: unqualified column(s) {', '.join(unq[:6])} with {len(sources)} tables joined -- "
                          "prefix them so it's clear which table they come from")

    for e in sel.expressions:
        if isinstance(e, exp.Alias) and " " in e.alias:
            issues.append(f"{label}: output name '{e.alias}' has a space")

    reads = []
    for t in sel.find_all(exp.Table):
        name = t.name if t.name in ctes and not t.db else ".".join(p for p in (t.db, t.name) if p)
        if name and name not in reads:
            reads.append(name)

    group = sel.args.get("group")
    return {
        "reads": reads,
        "from": sql_of(from_.this) if from_ else None,
        "joins": join_rows,
        "where": sql_of(where.this) if where is not None else None,
        "group_by": [sql_of(g) for g in group.expressions] if group else [],
        "having": sql_of(sel.args["having"].this) if sel.args.get("having") else None,
        "qualify": sql_of(sel.args["qualify"].this) if sel.args.get("qualify") else None,
        "columns": [e.alias_or_name or sql_of(e) for e in sel.expressions],
        "column_sql": [sql_of(e) for e in sel.expressions],
        "distinct": bool(sel.args.get("distinct")),
        "aggregates": sorted({type(a).__name__.upper() for a in sel.find_all(exp.AggFunc)
                              if a.find_ancestor(exp.Select) is sel}),
    }, issues + [f"{label}: aliases {a} used twice" for a in {a for a in aliases if aliases.count(a) > 1}]


def line_of(sql, name):
    m = re.search(r"\b" + re.escape(name) + r"\s+AS\s*\(", sql, re.I)
    return sql[: m.start()].count("\n") + 1 if m else 1


def analyze(sql, name="", lint=None):
    """Dialect is Spark when the file name says spark, else BigQuery. lint: optional fn(sql) -> [str]."""
    dialect = "spark" if "spark" in name.lower() else "bigquery"
    out = {"dialect": dialect, "error": None, "issues": [], "steps": []}
    try:
        trees = [t for t in sqlglot.parse(sql, read=dialect) if t is not None]
    except sqlglot.ParseError as e:
        err = e.errors[0] if e.errors else {}
        out["error"] = {"message": err.get("description") or str(e).splitlines()[0],
                        "line": err.get("line"), "col": err.get("col")}
        return out
    if lint and dialect == "bigquery":
        out["issues"] += ["lint: " + e for e in safe_lint(lint, sql)]
    if not trees:
        out["error"] = {"message": "no statement found", "line": None, "col": None}
        return out
    DIALECT[0] = dialect
    tree = trees[0]
    ctes = {c.alias: c for c in tree.ctes} if hasattr(tree, "ctes") else {}
    for alias, cte in ctes.items():
        out["steps"].append(step(alias, "cte", cte.this, ctes, out["issues"], line_of(sql, alias)))
    final = tree.copy()
    final.set("with_", None)
    last = sql.rfind("\nSELECT") if ctes else 0
    out["steps"].append(step("final", "final", final, ctes, out["issues"],
                             sql[: max(last, 0)].count("\n") + 2 if last > 0 else 1))
    return out


def step(name, kind, query, ctes, issues, line):
    parts = []
    for i, sel in enumerate(branches(query)):
        d, iss = describe_select(sel, ctes, name if i == 0 else f"{name} (union part {i + 1})")
        parts.append(d)
        issues += iss
    if not parts:
        return {"name": name, "kind": kind, "line": line, "reads": [], "parts": [], "union": False}
    reads = []
    for p in parts:
        reads += [r for r in p["reads"] if r not in reads]
    return {"name": name, "kind": kind, "line": line, "reads": reads, "parts": parts, "union": len(parts) > 1}


def safe_lint(lint, sql):
    try:
        return lint(sql)
    except Exception as e:  # the linter re-parses; a parse quirk there shouldn't hide the viewer
        return [f"linter could not run ({type(e).__name__})"]


if __name__ == "__main__":
    bad = analyze("SELECT b.g, age, COUNT(DISTINCT a.id) AS active customers FROM warehouse.t AS a "
                  "LEFT JOIN warehouse.u AS b ON a.id, b.id WHERE b.k = 1 GROUP BY 1, 2")
    txt = " | ".join(bad["issues"]) + str(bad["error"])
    assert bad["error"] or "not a condition" in txt, txt
    r = analyze("SELECT b.g, age, COUNT(DISTINCT a.id) AS n FROM warehouse.t AS a "
                "LEFT JOIN warehouse.u AS b ON a.id, b.id WHERE b.k = 1 GROUP BY 1, 2")
    txt = " | ".join(r["issues"])
    assert "not a condition" in txt and "INNER JOIN" in txt and "unqualified" in txt, txt
    good = analyze("WITH params AS (SELECT 1 AS lo)\n, orders AS (SELECT fuf.id FROM warehouse.t AS fuf "
                   "CROSS JOIN params WHERE fuf.d >= params.lo)\nSELECT COUNT(*) AS n_orders FROM orders")
    assert good["issues"] == [] and [s["name"] for s in good["steps"]] == ["params", "orders", "final"], good
    assert good["steps"][1]["reads"] == ["warehouse.t", "params"], good["steps"][1]
    assert analyze("SELECT FROM WHERE")["error"]
    # a function in ON is a condition; ORDER BY may use output names
    ok = analyze("SELECT zones.store_id, COUNT(*) AS n_orders FROM warehouse.t AS geo "
                 "INNER JOIN warehouse.z AS zones ON ST_CONTAINS(zones.g, geo.g) GROUP BY zones.store_id ORDER BY store_id")
    assert ok["issues"] == [], ok["issues"]
    # an optional linter's notes are passed through, tagged
    assert analyze("SELECT 1 AS one", lint=lambda sql: ["note"])["issues"] == ["lint: note"]
    print("ok")
