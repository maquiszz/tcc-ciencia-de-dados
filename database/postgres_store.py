"""Acesso PostgreSQL usado pela aplicação, sem serviços Supabase."""

import os
import re
from contextlib import contextmanager
from dataclasses import dataclass
from threading import Lock

import psycopg2
from psycopg2 import errors, sql
from psycopg2.extras import RealDictCursor
from psycopg2.pool import ThreadedConnectionPool


_pool = None
_pool_pid = None
_pool_lock = Lock()


class ScheduleConflictError(RuntimeError):
    """O horário solicitado já possui um agendamento ativo."""


class ServiceNotFoundError(RuntimeError):
    """O serviço solicitado não existe."""


def _connection_kwargs():
    password = os.getenv("DB_PASSWORD")
    if not password:
        raise RuntimeError("DB_PASSWORD não configurada.")
    return {
        "host": os.getenv("DB_HOST", "postgres"),
        "port": int(os.getenv("DB_PORT", "5432")),
        "dbname": os.getenv("DB_NAME", "site_db"),
        "user": os.getenv("DB_USER", "site_user"),
        "password": password,
        "connect_timeout": int(os.getenv("DB_CONNECT_TIMEOUT", "10")),
        "application_name": "panaceia-spa",
        "keepalives": 1,
        "keepalives_idle": 30,
        "keepalives_interval": 10,
        "keepalives_count": 3,
    }


def connection():
    """Abre uma conexão dedicada; operações web usam o pool compartilhado."""
    return psycopg2.connect(**_connection_kwargs())


def _get_pool():
    """Cria um pool por processo, seguro para as threads do Gunicorn."""
    global _pool, _pool_pid
    current_pid = os.getpid()
    if _pool is not None and _pool_pid == current_pid:
        return _pool
    with _pool_lock:
        if _pool is not None and _pool_pid != current_pid:
            _pool.closeall()
            _pool = None
        if _pool is None:
            minimum = max(1, int(os.getenv("DB_POOL_MIN", "1")))
            maximum = max(minimum, int(os.getenv("DB_POOL_MAX", "10")))
            _pool = ThreadedConnectionPool(minimum, maximum, **_connection_kwargs())
            _pool_pid = current_pid
    return _pool


@contextmanager
def session_connection():
    pool = _get_pool()
    conn = pool.getconn()
    try:
        with conn:
            yield conn
    finally:
        pool.putconn(conn, close=bool(conn.closed))


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

    def healthcheck(self):
        """Confirma conexão e informa se a proteção estrutural da agenda existe."""
        with session_connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT 1 AS ok, "
                "to_regclass('public.uq_agendamentos_horario_ativo') IS NOT NULL "
                "AS booking_constraint"
            )
            return dict(cur.fetchone())

    def ensure_schema(self):
        """Cria, quando permitido, a restrição contra horários ativos duplicados."""
        with session_connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT data_atendimento FROM public.agendamentos "
                "WHERE status IS DISTINCT FROM 'Cancelado' "
                "GROUP BY data_atendimento HAVING COUNT(*) > 1 LIMIT 1"
            )
            if cur.fetchone():
                raise RuntimeError(
                    "Existem agendamentos ativos duplicados; o índice de horário não foi criado."
                )
            cur.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_agendamentos_horario_ativo "
                "ON public.agendamentos (data_atendimento) "
                "WHERE status IS DISTINCT FROM 'Cancelado'"
            )
        return True

    @staticmethod
    def _lock_schedule(cur, data_atendimento):
        cur.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (str(data_atendimento),),
        )

    def create_appointment(self, email, servico_id, data_atendimento):
        """Reserva e contabiliza o serviço em uma única transação."""
        try:
            with session_connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
                self._lock_schedule(cur, data_atendimento)
                cur.execute("SELECT 1 FROM public.servico WHERE id = %s", (servico_id,))
                if not cur.fetchone():
                    raise ServiceNotFoundError("Serviço não encontrado.")
                cur.execute(
                    "SELECT 1 FROM public.agendamentos "
                    "WHERE data_atendimento = %s "
                    "AND status IS DISTINCT FROM 'Cancelado' LIMIT 1",
                    (data_atendimento,),
                )
                if cur.fetchone():
                    raise ScheduleConflictError("Este horário já está reservado por outro cliente.")
                cur.execute(
                    "INSERT INTO public.agendamentos "
                    "(email_cliente, servico_id, data_atendimento, status) "
                    "VALUES (%s, %s, %s, 'Pendente') RETURNING *",
                    (email, servico_id, data_atendimento),
                )
                appointment = dict(cur.fetchone())
                cur.execute(
                    "UPDATE public.servico SET contratos = COALESCE(contratos, 0) + 1 "
                    "WHERE id = %s",
                    (servico_id,),
                )
                return Result([appointment])
        except errors.UniqueViolation as error:
            raise ScheduleConflictError("Este horário já está reservado por outro cliente.") from error

    def reschedule_appointment(self, agendamento_id, data_atendimento):
        """Altera um horário sob lock transacional compartilhado pelo banco."""
        try:
            with session_connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
                self._lock_schedule(cur, data_atendimento)
                cur.execute(
                    "SELECT 1 FROM public.agendamentos "
                    "WHERE data_atendimento = %s AND id <> %s "
                    "AND status IS DISTINCT FROM 'Cancelado' LIMIT 1",
                    (data_atendimento, agendamento_id),
                )
                if cur.fetchone():
                    raise ScheduleConflictError("Este horário já está reservado por outro cliente.")
                cur.execute(
                    "UPDATE public.agendamentos SET data_atendimento = %s "
                    "WHERE id = %s RETURNING *",
                    (data_atendimento, agendamento_id),
                )
                row = cur.fetchone()
                return Result([dict(row)] if row else [])
        except errors.UniqueViolation as error:
            raise ScheduleConflictError("Este horário já está reservado por outro cliente.") from error

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
