"""Acesso resiliente ao PostgreSQL local, com Supabase como contingência."""

import json
import logging
import os
import re
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from threading import Lock, RLock

import psycopg2
from psycopg2 import errors, sql
from psycopg2.extras import RealDictCursor
from psycopg2.pool import ThreadedConnectionPool

try:
    import httpx
except ImportError:  # pragma: no cover - instalado como dependência do supabase-py
    httpx = None

try:
    from supabase import create_client as create_supabase_client
except ImportError:  # Permite usar somente PostgreSQL quando o pacote ainda não foi instalado.
    create_supabase_client = None


logger = logging.getLogger(__name__)


_pool = None
_pool_pid = None
_pool_lock = Lock()


class ScheduleConflictError(RuntimeError):
    """O horário solicitado já possui um agendamento ativo."""


class ServiceNotFoundError(RuntimeError):
    """O serviço solicitado não existe."""


class StockItemNotFoundError(RuntimeError):
    """O item solicitado não existe no estoque."""


class InsufficientStockError(RuntimeError):
    """A movimentação deixaria o saldo do item negativo."""


class StockOperationConflictError(RuntimeError):
    """A chave de idempotência foi reutilizada com dados diferentes."""


class StockQuantityConflictError(RuntimeError):
    """O saldo mudou antes da substituição manual ser aplicada."""


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

    def execute(self):
        """Mantém compatibilidade com builders do Supabase usados pelo backend."""
        return self


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
            cur.execute(
                "CREATE TABLE IF NOT EXISTS public.agenda_lista_espera ("
                "id BIGSERIAL PRIMARY KEY, email_cliente TEXT NOT NULL, servico_id BIGINT NOT NULL, "
                "data_atendimento TIMESTAMP WITHOUT TIME ZONE NOT NULL, "
                "status TEXT NOT NULL DEFAULT 'Aguardando' "
                "CHECK (status IN ('Aguardando', 'Notificando', 'Notificado', 'Cancelado')), "
                "criado_em TIMESTAMPTZ NOT NULL DEFAULT NOW(), notificado_em TIMESTAMPTZ)"
            )
            cur.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_agenda_lista_espera_email_horario "
                "ON public.agenda_lista_espera (email_cliente, data_atendimento) "
                "WHERE status IN ('Aguardando', 'Notificando', 'Notificado')"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS ix_agenda_lista_espera_horario_status "
                "ON public.agenda_lista_espera (data_atendimento, status)"
            )
            cur.execute(
                "CREATE TABLE IF NOT EXISTS public.agenda_auditoria ("
                "id BIGSERIAL PRIMARY KEY, agendamento_id BIGINT NOT NULL, ator_email TEXT NOT NULL, "
                "ator_tipo TEXT NOT NULL CHECK (ator_tipo IN ('admin', 'cliente')), "
                "acao TEXT NOT NULL, data_anterior TEXT, data_nova TEXT, "
                "status_anterior TEXT, status_novo TEXT, "
                "criado_em TIMESTAMPTZ NOT NULL DEFAULT NOW())"
            )
            cur.execute(
                "ALTER TABLE public.agenda_auditoria "
                "ADD COLUMN IF NOT EXISTS status_anterior TEXT, "
                "ADD COLUMN IF NOT EXISTS status_novo TEXT"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS ix_agenda_auditoria_agendamento_data "
                "ON public.agenda_auditoria (agendamento_id, criado_em DESC)"
            )
            cur.execute(
                "CREATE TABLE IF NOT EXISTS public.agenda_bloqueios ("
                "id BIGSERIAL PRIMARY KEY, "
                "data_atendimento TIMESTAMP WITHOUT TIME ZONE NOT NULL UNIQUE, "
                "motivo TEXT NOT NULL, criado_por TEXT NOT NULL, "
                "criado_em TIMESTAMPTZ NOT NULL DEFAULT NOW())"
            )
            cur.execute(
                "SELECT to_regclass('public.estoque') IS NOT NULL"
            )
            if cur.fetchone()[0]:
                cur.execute(
                    "CREATE TABLE IF NOT EXISTS public.estoque_movimentacoes ("
                    "operacao_id UUID PRIMARY KEY, item_id BIGINT NOT NULL, "
                    "delta INTEGER NOT NULL CHECK (delta <> 0), "
                    "resultado JSONB NOT NULL, criado_em TIMESTAMPTZ NOT NULL DEFAULT NOW())"
                )
                cur.execute(
                    "SELECT 1 FROM pg_constraint "
                    "WHERE conrelid = 'public.estoque'::regclass "
                    "AND conname = 'estoque_quantidade_nao_negativa'"
                )
                if not cur.fetchone():
                    cur.execute(
                        "ALTER TABLE public.estoque ADD CONSTRAINT "
                        "estoque_quantidade_nao_negativa CHECK (quantidade >= 0) NOT VALID"
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
                    "SELECT 1 FROM public.agenda_bloqueios "
                    "WHERE data_atendimento = %s LIMIT 1",
                    (data_atendimento,),
                )
                if cur.fetchone():
                    raise ScheduleConflictError("Este horário está indisponível na agenda do Spa.")
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
                    "SELECT 1 FROM public.agenda_bloqueios "
                    "WHERE data_atendimento = %s LIMIT 1",
                    (data_atendimento,),
                )
                if cur.fetchone():
                    raise ScheduleConflictError("Este horário está indisponível na agenda do Spa.")
                cur.execute(
                    "UPDATE public.agendamentos SET data_atendimento = %s "
                    "WHERE id = %s AND status = 'Pendente' RETURNING *",
                    (data_atendimento, agendamento_id),
                )
                row = cur.fetchone()
                if not row:
                    raise ScheduleConflictError("Esta reserva não está mais pendente para remarcação.")
                return Result([dict(row)])
        except errors.UniqueViolation as error:
            raise ScheduleConflictError("Este horário já está reservado por outro cliente.") from error

    def create_schedule_block(self, slots, motivo, criado_por):
        """Bloqueia todas as horas na mesma transação usada pela reserva."""
        try:
            with session_connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
                for slot in sorted(set(slots)):
                    self._lock_schedule(cur, slot)
                    cur.execute(
                        "SELECT 1 FROM public.agendamentos "
                        "WHERE data_atendimento = %s AND status IS DISTINCT FROM 'Cancelado' LIMIT 1",
                        (slot,),
                    )
                    if cur.fetchone():
                        raise ScheduleConflictError("O período inclui um horário com reserva ativa.")
                    cur.execute(
                        "SELECT 1 FROM public.agenda_bloqueios "
                        "WHERE data_atendimento = %s LIMIT 1",
                        (slot,),
                    )
                    if cur.fetchone():
                        raise ScheduleConflictError("O período inclui um horário já bloqueado.")
                rows = []
                for slot in sorted(set(slots)):
                    cur.execute(
                        "INSERT INTO public.agenda_bloqueios "
                        "(data_atendimento, motivo, criado_por) "
                        "VALUES (%s, %s, %s) RETURNING *",
                        (slot, motivo, criado_por),
                    )
                    rows.append(dict(cur.fetchone()))
                return Result(rows)
        except errors.UniqueViolation as error:
            raise ScheduleConflictError("O período inclui um horário já bloqueado.") from error

    def move_stock(self, item_id, delta, operation_id):
        """Movimenta o saldo uma vez por operação, dentro da transação local."""
        operation_id = str(uuid.UUID(str(operation_id)))
        delta = int(delta)
        if delta == 0:
            raise ValueError("A quantidade da movimentação deve ser maior que zero.")
        with session_connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "INSERT INTO public.estoque_movimentacoes "
                "(operacao_id, item_id, delta, resultado) "
                "VALUES (%s, %s, %s, '{}'::jsonb) "
                "ON CONFLICT (operacao_id) DO NOTHING RETURNING operacao_id",
                (operation_id, item_id, delta),
            )
            if not cur.fetchone():
                cur.execute(
                    "SELECT item_id, delta, resultado FROM public.estoque_movimentacoes "
                    "WHERE operacao_id = %s FOR UPDATE",
                    (operation_id,),
                )
                previous = cur.fetchone()
                if not previous or previous["item_id"] != item_id or previous["delta"] != delta:
                    raise StockOperationConflictError(
                        "A chave da movimentação já foi usada com outros dados."
                    )
                result = previous["resultado"]
                if isinstance(result, str):
                    result = json.loads(result)
                return Result([{**result, "repetida": True}])

            cur.execute(
                "UPDATE public.estoque SET quantidade = quantidade + %s "
                "WHERE id = %s AND quantidade::BIGINT + %s BETWEEN 0 AND 2147483647 "
                "RETURNING id, nome, quantidade, quantidade_minima, unidade",
                (delta, item_id, delta),
            )
            item = cur.fetchone()
            if not item:
                cur.execute("SELECT 1 FROM public.estoque WHERE id = %s", (item_id,))
                if cur.fetchone():
                    raise InsufficientStockError("A movimentação excede o saldo permitido.")
                raise StockItemNotFoundError("Item do estoque não encontrado.")
            result = {**dict(item), "repetida": False}
            cur.execute(
                "UPDATE public.estoque_movimentacoes SET resultado = %s::jsonb "
                "WHERE operacao_id = %s",
                (json.dumps(result, ensure_ascii=False), operation_id),
            )
            return Result([result])

    def set_stock_quantity(self, item_id, quantity, expected_quantity):
        """Substitui o saldo somente se ele ainda corresponder ao valor lido."""
        with session_connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "UPDATE public.estoque SET quantidade = %s WHERE id = %s "
                "AND quantidade = %s "
                "RETURNING id, nome, quantidade, quantidade_minima, unidade",
                (quantity, item_id, expected_quantity),
            )
            item = cur.fetchone()
            if not item:
                cur.execute("SELECT 1 FROM public.estoque WHERE id = %s", (item_id,))
                if cur.fetchone():
                    raise StockQuantityConflictError(
                        "O saldo mudou. Atualize a lista antes de definir um novo valor."
                    )
                raise StockItemNotFoundError("Item do estoque não encontrado.")
            return Result([dict(item)])

    def complete_appointment(self, appointment_id):
        """Conclui e credita pontos na mesma transação, sob lock da reserva."""
        with session_connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT email_cliente, servico_id, status FROM public.agendamentos "
                "WHERE id = %s FOR UPDATE",
                (appointment_id,),
            )
            appointment = cur.fetchone()
            if not appointment:
                return Result([{"resultado": "nao_encontrado", "pontos": 0}])
            if appointment["status"] == "Concluido":
                return Result([{"resultado": "ja_concluido", "pontos": 0}])
            if appointment["status"] != "Pendente":
                return Result([{"resultado": "status_invalido", "pontos": 0}])

            cur.execute(
                "SELECT valor FROM public.servico WHERE id = %s",
                (appointment["servico_id"],),
            )
            service = cur.fetchone()
            if not service or service["valor"] is None or service["valor"] < 0:
                raise RuntimeError("Serviço sem valor válido para crédito de pontos.")
            points = int(service["valor"] / 10)
            cur.execute(
                "UPDATE public.usuarios SET pontos = COALESCE(pontos, 0) + %s "
                "WHERE email = %s RETURNING id",
                (points, appointment["email_cliente"]),
            )
            if not cur.fetchone():
                raise RuntimeError("Cliente não encontrado para crédito de pontos.")
            cur.execute(
                "UPDATE public.agendamentos SET status = 'Concluido' "
                "WHERE id = %s AND status = 'Pendente' RETURNING id",
                (appointment_id,),
            )
            if not cur.fetchone():
                raise RuntimeError("Falha ao concluir agendamento após o crédito.")
            return Result([{"resultado": "concluido", "pontos": points}])

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
        "estoque", "resgates_pontos", "servico", "agenda_lista_espera",
        "agenda_auditoria", "agenda_bloqueios",
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
        self.row_offset = None

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

    def range(self, start, end):
        """Paginação inclusiva, compatível com a Data API do Supabase."""
        start, end = int(start), int(end)
        if start < 0 or end < start:
            raise ValueError("Intervalo de paginação inválido.")
        self.row_offset = start
        self.row_limit = end - start + 1
        return self

    def _selected_columns(self):
        """Retorna as colunas da projeção que podem ser selecionadas com segurança."""
        projection = (self.projection or "*").strip()
        if projection == "*":
            return None

        parts = []
        depth = 0
        start = 0
        for index, character in enumerate(projection):
            if character in {'"', "'"}:
                # Aliases/expressões não são usados pelas rotas e ficam no caminho compatível.
                return None
            if character == "(":
                depth += 1
            elif character == ")":
                depth -= 1
                if depth < 0:
                    return None
            elif character == "," and depth == 0:
                parts.append(projection[start:index].strip())
                start = index + 1
        if depth != 0:
            return None
        parts.append(projection[start:].strip())

        columns = []
        includes_service = False
        for part in parts:
            if self.table_name == "agendamentos" and re.fullmatch(r"servico\([^()]+\)", part):
                includes_service = True
                continue
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", part):
                return None
            columns.append(part)

        if includes_service and "servico_id" not in columns:
            columns.append("servico_id")
        return columns or None

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

            selected_columns = self._selected_columns()
            projection = (
                sql.SQL("*")
                if selected_columns is None
                else sql.SQL(", ").join(map(_ident, selected_columns))
            )
            statement = sql.SQL("SELECT {} FROM public.{}{}").format(
                projection, _ident(self.table_name), where
            )
            parameters = list(values)
            if self.orders:
                statement += sql.SQL(" ORDER BY ") + sql.SQL(", ").join(
                    sql.SQL("{} {}").format(_ident(column), sql.SQL("DESC" if desc else "ASC"))
                    for column, desc in self.orders
                )
            if self.row_limit is not None:
                statement += sql.SQL(" LIMIT %s")
                parameters.append(self.row_limit)
            if self.row_offset is not None:
                statement += sql.SQL(" OFFSET %s")
                parameters.append(self.row_offset)
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


def _json_value(value):
    """Converte valores do PostgreSQL para o formato aceito pela Data API."""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _unique_violation(error):
    text = str(error).lower()
    code = str(getattr(error, "code", "") or "").lower()
    return "23505" in code or "23505" in text or "duplicate key" in text or "unique constraint" in text


def _schedule_block_violation(error):
    return any(marker in str(error).upper() for marker in (
        "AGENDA_BLOQUEADA", "AGENDA_RESERVADA", "AGENDA_JA_BLOQUEADA",
    ))


class SupabaseDatabaseClient:
    """Adaptador Supabase compatível com a interface usada pela aplicação."""

    def __init__(self, url, key):
        if create_supabase_client is None:
            raise RuntimeError("A dependência 'supabase' não está instalada.")
        self._client = create_supabase_client(url, key)

    def table(self, name):
        if name not in Query.allowed_tables:
            raise ValueError("Tabela Supabase não permitida.")
        return self._client.table(name)

    def rpc(self, name, params):
        if name not in {"resgatar_pontos", "utilizar_voucher"}:
            raise ValueError("Função Supabase não permitida.")
        return self._client.rpc(name, _json_value(params))

    def complete_appointment(self, appointment_id):
        return self._client.rpc(
            "concluir_agendamento_creditar_pontos",
            {"p_agendamento_id": int(appointment_id)},
        ).execute()

    def healthcheck(self):
        self.table("servico").select("id").limit(1).execute()
        return {"ok": 1, "booking_constraint": None, "backend": "supabase"}

    def ensure_schema(self):
        # A Data API não executa DDL. O esquema e as funções devem existir no projeto.
        self.healthcheck()
        return True

    def create_appointment(self, email, servico_id, data_atendimento):
        service_rows = (
            self.table("servico")
            .select("id, contratos")
            .eq("id", servico_id)
            .limit(1)
            .execute()
            .data or []
        )
        if not service_rows:
            raise ServiceNotFoundError("Serviço não encontrado.")

        occupied = (
            self.table("agendamentos")
            .select("id")
            .eq("data_atendimento", _json_value(data_atendimento))
            .neq("status", "Cancelado")
            .limit(1)
            .execute()
            .data or []
        )
        if occupied:
            raise ScheduleConflictError("Este horário já está reservado por outro cliente.")

        blocked = (
            self.table("agenda_bloqueios")
            .select("id")
            .eq("data_atendimento", _json_value(data_atendimento))
            .limit(1)
            .execute()
            .data or []
        )
        if blocked:
            raise ScheduleConflictError("Este horário está indisponível na agenda do Spa.")

        try:
            response = self.table("agendamentos").insert({
                "email_cliente": email,
                "servico_id": servico_id,
                "data_atendimento": _json_value(data_atendimento),
                "status": "Pendente",
            }).execute()
        except Exception as error:
            if _schedule_block_violation(error):
                raise ScheduleConflictError("Este horário está indisponível na agenda do Spa.") from error
            if _unique_violation(error):
                raise ScheduleConflictError("Este horário já está reservado por outro cliente.") from error
            raise

        service = service_rows[0]
        try:
            self.table("servico").update({
                "contratos": int(service.get("contratos") or 0) + 1,
            }).eq("id", servico_id).execute()
        except Exception:
            # A reserva é o dado crítico; o contador pode ser reconciliado depois.
            logger.exception("Reserva criada no Supabase, mas o contador do serviço não foi atualizado")
        return response

    def reschedule_appointment(self, agendamento_id, data_atendimento):
        target = _json_value(data_atendimento)
        occupied = (
            self.table("agendamentos")
            .select("id")
            .eq("data_atendimento", target)
            .neq("id", agendamento_id)
            .neq("status", "Cancelado")
            .limit(1)
            .execute()
            .data or []
        )
        if occupied:
            raise ScheduleConflictError("Este horário já está reservado por outro cliente.")
        blocked = (
            self.table("agenda_bloqueios")
            .select("id")
            .eq("data_atendimento", target)
            .limit(1)
            .execute()
            .data or []
        )
        if blocked:
            raise ScheduleConflictError("Este horário está indisponível na agenda do Spa.")
        try:
            resposta = self.table("agendamentos").update({
                "data_atendimento": target,
            }).eq("id", agendamento_id).eq("status", "Pendente").execute()
            if not resposta.data:
                raise ScheduleConflictError("Esta reserva não está mais pendente para remarcação.")
            return resposta
        except Exception as error:
            if _schedule_block_violation(error):
                raise ScheduleConflictError("Este horário está indisponível na agenda do Spa.") from error
            if _unique_violation(error):
                raise ScheduleConflictError("Este horário já está reservado por outro cliente.") from error
            raise

    def create_schedule_block(self, slots, motivo, criado_por):
        try:
            return self.table("agenda_bloqueios").insert([
                {
                    "data_atendimento": slot,
                    "motivo": motivo,
                    "criado_por": criado_por,
                }
                for slot in sorted(set(slots))
            ]).execute()
        except Exception as error:
            if _schedule_block_violation(error) or _unique_violation(error):
                raise ScheduleConflictError("O período inclui um horário reservado ou já bloqueado.") from error
            raise

    def move_stock(self, item_id, delta, operation_id):
        try:
            operation_id = str(uuid.UUID(str(operation_id)))
        except (ValueError, TypeError, AttributeError) as error:
            raise ValueError("Identificador da movimentação inválido.") from error
        try:
            response = self._client.rpc(
                "movimentar_estoque",
                {
                    "p_item_id": int(item_id),
                    "p_delta": int(delta),
                    "p_operacao_id": operation_id,
                },
            ).execute()
        except Exception as error:
            message = str(error)
            if "ESTOQUE_INSUFICIENTE" in message:
                raise InsufficientStockError(
                    "A movimentação excede o saldo permitido."
                ) from error
            if "ESTOQUE_NAO_ENCONTRADO" in message:
                raise StockItemNotFoundError("Item do estoque não encontrado.") from error
            if "ESTOQUE_OPERACAO_DIVERGENTE" in message:
                raise StockOperationConflictError(
                    "A chave da movimentação já foi usada com outros dados."
                ) from error
            raise
        data = response.data
        if isinstance(data, list):
            data = data[0] if data else None
        if not isinstance(data, dict):
            raise RuntimeError("Resposta inválida da movimentação de estoque.")
        return Result([data])

    def set_stock_quantity(self, item_id, quantity, expected_quantity):
        response = (
            self.table("estoque")
            .update({"quantidade": int(quantity)})
            .eq("id", item_id)
            .eq("quantidade", int(expected_quantity))
            .select("id, nome, quantidade, quantidade_minima, unidade")
            .execute()
        )
        if not (response.data or []):
            rows = self.table("estoque").select("id").eq("id", item_id).limit(1).execute().data or []
            if rows:
                raise StockQuantityConflictError(
                    "O saldo mudou. Atualize a lista antes de definir um novo valor."
                )
            raise StockItemNotFoundError("Item do estoque não encontrado.")
        return response


def _connection_error(error):
    """Distingue indisponibilidade de banco de erros válidos da aplicação."""
    if isinstance(error, (psycopg2.OperationalError, psycopg2.InterfaceError, OSError, TimeoutError)):
        return True
    if httpx is not None and isinstance(error, (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.NetworkError)):
        return True
    text = str(error).lower()
    markers = (
        "connection refused", "connection reset", "connection timed out", "timeout expired",
        "temporary failure in name resolution", "name or service not known", "network is unreachable",
        "server disconnected", "nodename nor servname", "no route to host",
    )
    return any(marker in text for marker in markers)


class RoutedQuery:
    """Grava uma cadeia de consulta e a executa no banco selecionado."""

    def __init__(self, router, source, name, params=None):
        self.router = router
        self.source = source
        self.name = name
        self.params = params
        self.operations = []
        self.mutating = source == "rpc"

    def _operation(self, method, *args, **kwargs):
        if method in {"insert", "update", "delete"}:
            self.mutating = True
        self.operations.append((method, args, kwargs))
        return self

    def select(self, *args, **kwargs):
        return self._operation("select", *args, **kwargs)

    def insert(self, *args, **kwargs):
        return self._operation("insert", *args, **kwargs)

    def update(self, *args, **kwargs):
        return self._operation("update", *args, **kwargs)

    def delete(self, *args, **kwargs):
        return self._operation("delete", *args, **kwargs)

    def eq(self, *args, **kwargs):
        return self._operation("eq", *args, **kwargs)

    def neq(self, *args, **kwargs):
        return self._operation("neq", *args, **kwargs)

    def gt(self, *args, **kwargs):
        return self._operation("gt", *args, **kwargs)

    def order(self, *args, **kwargs):
        return self._operation("order", *args, **kwargs)

    def limit(self, *args, **kwargs):
        return self._operation("limit", *args, **kwargs)

    def range(self, *args, **kwargs):
        return self._operation("range", *args, **kwargs)

    def _execute_on(self, backend):
        query = backend.table(self.name) if self.source == "table" else backend.rpc(self.name, self.params)
        for method, args, kwargs in self.operations:
            query = getattr(query, method)(*(_json_value(args)), **_json_value(kwargs))
        return query.execute()

    def execute(self):
        return self.router._run(self._execute_on, mutating=self.mutating)


class ResilientDatabaseClient:
    """Seleciona PostgreSQL ou Supabase e evita repetir mutações incertas."""

    def __init__(self, backends, primary="postgres"):
        if not backends:
            raise RuntimeError("Nenhum banco de dados foi configurado.")
        self.backends = dict(backends)
        self.primary = primary if primary in self.backends else next(iter(self.backends))
        # Independent databases are not replicas. Fail over only by explicit
        # operator choice; otherwise separate writes can silently split data.
        self.failover_enabled = os.getenv("DATABASE_FAILOVER_ENABLED", "false").strip().lower() in {
            "1", "true", "yes", "on",
        }
        self.cooldown_seconds = max(5, int(os.getenv("DB_FAILOVER_COOLDOWN_SECONDS", "60")))
        self._active = None
        self._unavailable_until = {name: 0.0 for name in self.backends}
        self._last_health = {}
        self._lock = RLock()

    @property
    def active_backend(self):
        return self._active

    def table(self, name):
        if name not in Query.allowed_tables:
            raise ValueError("Tabela não permitida.")
        return RoutedQuery(self, "table", name)

    def rpc(self, name, params):
        return RoutedQuery(self, "rpc", name, params)

    def _order(self):
        names = [self.primary]
        if self.failover_enabled:
            names.extend(name for name in self.backends if name != self.primary)
        if self._active in names:
            names.remove(self._active)
            names.insert(0, self._active)
        return names

    def _mark_unavailable(self, name, error):
        with self._lock:
            self._unavailable_until[name] = time.monotonic() + self.cooldown_seconds
            self._last_health[name] = {"status": "indisponivel", "erro": type(error).__name__}
            if self._active == name:
                self._active = None
        logger.warning("Banco %s indisponível; ativando contingência por %ss", name, self.cooldown_seconds)

    def _select(self, validate_current=False):
        with self._lock:
            current = self._active
        if current and not validate_current and (self.failover_enabled or current == self.primary):
            return current, self.backends[current]

        now = time.monotonic()
        errors_found = []
        for name in self._order():
            if now < self._unavailable_until.get(name, 0):
                continue
            backend = self.backends[name]
            try:
                health = backend.healthcheck()
                with self._lock:
                    previous = self._active
                    self._active = name
                    self._last_health[name] = {"status": "ok", **(health or {})}
                if previous != name:
                    logger.warning("Banco ativo alterado para %s", name)
                return name, backend
            except Exception as error:
                errors_found.append(error)
                if not _connection_error(error):
                    raise
                self._mark_unavailable(name, error)

        if self.failover_enabled and current and current in self.backends:
            return current, self.backends[current]
        detail = str(errors_found[-1]) if errors_found else "todos os bancos estão em espera de reconexão"
        raise RuntimeError(f"Nenhum banco de dados está disponível: {detail}")

    def _run(self, operation, mutating=False):
        name, backend = self._select(validate_current=mutating)
        try:
            return operation(backend)
        except Exception as error:
            if not _connection_error(error):
                raise
            self._mark_unavailable(name, error)
            if mutating:
                # Uma falha depois do envio pode ter resultado incerto. Não repetimos escrita.
                raise RuntimeError(
                    f"O banco {name} ficou indisponível durante a gravação; a operação não foi repetida."
                ) from error
            fallback_name, fallback = self._select()
            logger.warning("Repetindo leitura no banco de contingência %s", fallback_name)
            return operation(fallback)

    def healthcheck(self):
        active_name, _ = self._select(validate_current=True)
        active_health = self._last_health.get(active_name, {"ok": 1})
        statuses = {
            name: {
                "status": self._last_health.get(name, {}).get(
                    "status",
                    "ok" if name == active_name else "nao_testado",
                )
            }
            for name in self.backends
        }
        statuses[active_name] = {"status": "ok"}
        return {
            **active_health,
            "backend": active_name,
            "backends": statuses,
        }

    def ensure_schema(self):
        name, backend = self._select()
        result = backend.ensure_schema()
        logger.info("Banco inicializado: %s", name)
        return result

    def create_appointment(self, email, servico_id, data_atendimento):
        return self._run(
            lambda backend: backend.create_appointment(email, servico_id, data_atendimento),
            mutating=True,
        )

    def reschedule_appointment(self, agendamento_id, data_atendimento):
        return self._run(
            lambda backend: backend.reschedule_appointment(agendamento_id, data_atendimento),
            mutating=True,
        )

    def create_schedule_block(self, slots, motivo, criado_por):
        return self._run(
            lambda backend: backend.create_schedule_block(slots, motivo, criado_por),
            mutating=True,
        )

    def move_stock(self, item_id, delta, operation_id):
        return self._run(lambda backend: backend.move_stock(item_id, delta, operation_id), mutating=True)

    def set_stock_quantity(self, item_id, quantity, expected_quantity):
        return self._run(
            lambda backend: backend.set_stock_quantity(item_id, quantity, expected_quantity),
            mutating=True,
        )

    def complete_appointment(self, appointment_id):
        return self._run(lambda backend: backend.complete_appointment(appointment_id), mutating=True)


def create_database_client():
    """Monta o roteador apenas com bancos cujas credenciais estão completas."""
    backends = {}
    if os.getenv("DB_PASSWORD"):
        backends["postgres"] = LocalPostgresClient()

    supabase_url = (os.getenv("SUPABASE_URL") or "").strip()
    supabase_key = (
        os.getenv("SUPABASE_SERVICE_ROLE_KEY")
        or os.getenv("service_role")
        or ""
    ).strip()
    if supabase_url and supabase_key:
        if create_supabase_client is None:
            logger.warning("Supabase configurado, mas a dependência 'supabase' não está instalada")
        else:
            backends["supabase"] = SupabaseDatabaseClient(supabase_url, supabase_key)

    primary = (os.getenv("DATABASE_PRIMARY") or "postgres").strip().lower()
    return ResilientDatabaseClient(backends, primary=primary)
