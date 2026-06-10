from django.db import connection


def _q(sql, params=None, one=False):
    with connection.cursor() as cur:
        cur.execute(sql, params or [])
        cols = [c[0] for c in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    return (rows[0] if rows else None) if one else rows


def _has_table_column(table_name: str, column_name: str) -> bool:
    try:
        if connection.vendor == "postgresql":
            row = _q(
                """
                SELECT 1
                  FROM information_schema.columns
                 WHERE table_name=%s
                   AND column_name=%s
                   AND table_schema = ANY(current_schemas(true))
                 LIMIT 1
                """,
                [table_name, column_name],
                one=True,
            )
            return bool(row)
        row = _q(
            """
            SELECT 1
              FROM information_schema.columns
             WHERE table_name=%s
               AND column_name=%s
             LIMIT 1
            """,
            [table_name, column_name],
            one=True,
        )
        return bool(row)
    except Exception:
        return False


def has_rejected_budget_charge_schema() -> bool:
    return _has_table_column("ingresos", "presupuesto_rechazado_cobro_neto") and _has_table_column(
        "ingresos", "presupuesto_rechazado_quote_id"
    )


def get_stored_rejected_budget_fields(ingreso_id: int) -> dict:
    if not has_rejected_budget_charge_schema():
        return {
            "presupuesto_rechazado_cobro_neto": None,
            "presupuesto_rechazado_quote_id": None,
        }
    row = _q(
        """
        SELECT
          presupuesto_rechazado_cobro_neto,
          presupuesto_rechazado_quote_id
        FROM ingresos
        WHERE id=%s
        """,
        [ingreso_id],
        one=True,
    )
    return row or {
        "presupuesto_rechazado_cobro_neto": None,
        "presupuesto_rechazado_quote_id": None,
    }


def get_rejected_quote_summary(ingreso_id: int, preferred_quote_id: int | None = None) -> dict | None:
    base_sql = """
        SELECT
          q.id AS quote_id,
          COALESCE(q.version_num, 1) AS version_num,
          q.estado,
          COALESCE(ROUND(SUM(qi.qty * qi.precio_u), 2), 0) AS subtotal,
          ROUND(COALESCE(SUM(qi.qty * qi.precio_u), 0) * 0.21, 2) AS iva_21,
          ROUND(COALESCE(SUM(qi.qty * qi.precio_u), 0) * 1.21, 2) AS total
        FROM quotes q
        LEFT JOIN quote_items qi ON qi.quote_id = q.id
        WHERE q.ingreso_id=%s
          AND q.estado='rechazado'
    """
    group_order_sql = """
        GROUP BY q.id, q.version_num, q.estado
        ORDER BY COALESCE(q.version_num, 1) DESC, q.id DESC
    """
    if preferred_quote_id:
        preferred = _q(
            base_sql + " AND q.id=%s " + group_order_sql,
            [ingreso_id, preferred_quote_id],
            one=True,
        )
        if preferred:
            return preferred
    return _q(base_sql + group_order_sql, [ingreso_id], one=True)


def get_rejected_budget_context(ingreso_id: int) -> dict:
    stored = get_stored_rejected_budget_fields(ingreso_id)
    stored_quote_id = stored.get("presupuesto_rechazado_quote_id")
    rejected_quote = get_rejected_quote_summary(ingreso_id, preferred_quote_id=stored_quote_id)
    resolved_quote_id = rejected_quote.get("quote_id") if rejected_quote else None
    return {
        **stored,
        "rejected_quote": rejected_quote,
        "resolved_quote_id": resolved_quote_id,
        "resolved_from_fallback": bool(stored_quote_id and resolved_quote_id and int(stored_quote_id) != int(resolved_quote_id)),
    }
