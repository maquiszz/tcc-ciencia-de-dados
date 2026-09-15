"""Acesso PostgreSQL usado pela aplicação, sem serviços Supabase."""

import os
import re
from contextlib import closing, contextmanager
from dataclasses import dataclass

import psycopg2
from psycopg2 import sql
from psycopg2.extras import RealDictCursor


def connection():
    """Abre uma conexão usando as variáveis DB_* do ambiente."""
    password = os.getenv("DB_PASSWORD")
    if not password:
        raise RuntimeError("DB_PASSWORD não configurada.")
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "postgres"),
        port=int(os.getenv("DB_PORT", "5432")),
        dbname=os.getenv("DB_NAME", "site_db"),
        user=os.getenv("DB_USER", "site_user"),
        password=password,
        connect_timeout=int(os.getenv("DB_CONNECT_TIMEOUT", "10")),
        application_name="panaceia-spa",
    )


@contextmanager
def session_connection():
    with closing(connection()) as conn:
        with conn:
            yield conn


def _ident(name):
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ValueError(f"Identificador SQL inválido: {name!r}")
    return sql.Identifier(name)


@dataclass
class Result:
    data: list


class LocalPostgresClient:
    """Interface mínima para as operações de tabelas e RPCs já usadas pelo app."""

    def table(self, name):
        return Query(name)

    def rpc(self, name, params):
        if name not in {"resgatar_pontos", "utilizar_voucher"}:
            raise ValueError("Função PostgreSQL não permitida.")
        arguments = {
            "resgatar_pontos": (params["p_email"], params["p_recompensa_codigo"]),
            "utilizar_voucher": (params["p_codigo_voucher"], params["p_agendamento_id"]),
        }[name]
        with session_connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql.SQL("SELECT * FROM public.{}(%s, %s)").format(_ident(name)), arguments)
            return Result([dict(row) for row in cur.fetchall()] if cur.description else [])


class Query:
    allowed_tables = {
        "usuarios", "agendamentos", "agendamentos_cancelados",
        "estoque", "resgates_pontos", "servico",
    }

    def __init__(self, table):
        if table not in self.allowed_tables:
            raise ValueError("Tabela PostgreSQL não permitida.")
        self.table_name = table
        self.action = "select"
        self.projection = "*"
        self.payload = None
        self.filters = []
        self.orders = []
        self.row_limit = None

    def select(self, columns="*"):
        self.action, self.projection = "select", columns
        return self

    def insert(self, values):
        self.action, self.payload = "insert", values
        return self

    def update(self, values):
        self.action, self.payload = "update", values
        return self

    def delete(self):
        self.action = "delete"
        return self

    def eq(self, column, value):
        return self._filter(column, "=", value)

    def neq(self, column, value):
        return self._filter(column, "<>", value)

    def gt(self, column, value):
        return self._filter(column, ">", value)

    def _filter(self, column, operator, value):
        self.filters.append((column, operator, value))
        return self

    def order(self, column, desc=False):
        self.orders.append((column, bool(desc)))
        return self

    def limit(self, count):
        self.row_limit = max(0, int(count))
        return self

    def _where(self):
        if not self.filters:
            return sql.SQL(""), []
        conditions, values = [], []
        for column, operator, value in self.filters:
            conditions.append(sql.SQL("{} {} %s").format(_ident(column), sql.SQL(operator)))
            values.append(value)
        return sql.SQL(" WHERE ") + sql.SQL(" AND ").join(conditions), values

    def _add_servico(self, cur, rows):
        # O único relacionamento embutido solicitado pelo backend é agendamentos.servico.
        match = re.search(r"servico\(([^()]*)\)", self.projection or "")
        if self.table_name != "agendamentos" or not match:
            return rows
        fields = [field.strip() for field in match.group(1).split(",") if field.strip()]
        ids = [row.get("servico_id") for row in rows if row.get("servico_id") is not None]
        related = {}
        if ids:
            cur.execute(
                sql.SQL("SELECT id, {} FROM public.servico WHERE id = ANY(%s)").format(
                    sql.SQL(", ").join(map(_ident, fields))
                ),
                (ids,),
            )
            related = {
                row["id"]: {key: value for key, value in row.items() if key != "id"}
                for row in cur.fetchall()
            }
        for row in rows:
            row["servico"] = related.get(row.get("servico_id"))
        return rows

    def execute(self):
        where, values = self._where()
        with session_connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            if self.action == "insert":
                items = self.payload if isinstance(self.payload, list) else [self.payload]
                output = []
                for item in items:
                    if not item:
                        continue
                    columns = list(item)
                    statement = sql.SQL("INSERT INTO public.{} ({}) VALUES ({}) RETURNING *").format(
                        _ident(self.table_name),
                        sql.SQL(", ").join(map(_ident, columns)),
                        sql.SQL(", ").join(sql.Placeholder() for _ in columns),
                    )
                    cur.execute(statement, [item[column] for column in columns])
                    output.extend(dict(row) for row in cur.fetchall())
                return Result(output)

            if self.action == "update":
                columns = list(self.payload or {})
                if not columns:
                    return Result([])
                statement = sql.SQL("UPDATE public.{} SET {}{} RETURNING *").format(
                    _ident(self.table_name),
                    sql.SQL(", ").join(
                        sql.SQL("{} = %s").format(_ident(column)) for column in columns
                    ),
                    where,
                )
                cur.execute(statement, [self.payload[column] for column in columns] + values)
                return Result([dict(row) for row in cur.fetchall()])

            if self.action == "delete":
                statement = sql.SQL("DELETE FROM public.{}{} RETURNING *").format(_ident(self.table_name), where)
                cur.execute(statement, values)
                return Result([dict(row) for row in cur.fetchall()])

            statement = sql.SQL("SELECT * FROM public.{}{}").format(_ident(self.table_name), where)
            parameters = list(values)
            if self.orders:
                statement += sql.SQL(" ORDER BY ") + sql.SQL(", ").join(
                    sql.SQL("{} {}").format(_ident(column), sql.SQL("DESC" if desc else "ASC"))
                    for column, desc in self.orders
                )
            if self.row_limit is not None:
                statement += sql.SQL(" LIMIT %s")
                parameters.append(self.row_limit)
            cur.execute(statement, parameters)
            rows = self._add_servico(cur, [dict(row) for row in cur.fetchall()])
            if self.projection and self.projection.strip() != "*":
                requested = [
                    item.strip() for item in self.projection.split(",")
                    if item.strip() and "(" not in item
                ]
                rows = [
                    {key: value for key, value in row.items() if key in requested or key == "servico"}
                    for row in rows
                ]
            return Result(rows)
