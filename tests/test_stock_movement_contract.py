"""Unit-level SQL adapter contract tests; not PostgreSQL concurrency proof."""

import ast
import copy
import json
import threading
import unittest
import uuid
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STORE_PATH = ROOT / "database" / "postgres_store.py"
STORE_TREE = ast.parse(STORE_PATH.read_text(encoding="utf-8"))
MAX_INTEGER = 2_147_483_647


class StockItemNotFoundError(RuntimeError):
    pass


class InsufficientStockError(RuntimeError):
    pass


class StockOperationConflictError(RuntimeError):
    pass


class Result:
    def __init__(self, data):
        self.data = data


class FakeCursor:
    """Small transaction double for SQL branches used by move_stock only."""

    def __init__(self, database):
        self.database = database
        self.row = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, statement, params=()):
        normalized = " ".join(str(statement).lower().split())
        self.row = None
        if normalized.startswith("insert into public.estoque_movimentacoes"):
            operation_id, item_id, delta = params
            if operation_id not in self.database.ledger:
                self.database.ledger[operation_id] = {
                    "item_id": item_id,
                    "delta": delta,
                    "resultado": {},
                }
                self.row = {"operacao_id": operation_id}
        elif normalized.startswith("select item_id, delta, resultado from public.estoque_movimentacoes"):
            operation_id = params[0]
            self.row = copy.deepcopy(self.database.ledger.get(operation_id))
        elif normalized.startswith("update public.estoque set quantidade = quantidade +"):
            delta, item_id, _checked_delta = params
            item = self.database.items.get(item_id)
            if item and 0 <= item["quantidade"] + delta <= MAX_INTEGER:
                item["quantidade"] += delta
                self.row = copy.deepcopy(item)
        elif normalized.startswith("select 1 from public.estoque"):
            item_id = params[0]
            self.row = {"exists": 1} if item_id in self.database.items else None
        elif normalized.startswith("update public.estoque_movimentacoes set resultado"):
            serialized_result, operation_id = params
            self.database.ledger[operation_id]["resultado"] = json.loads(serialized_result)
        else:
            raise AssertionError(f"SQL não esperado no teste: {statement}")

    def fetchone(self):
        return self.row


class FakeConnection:
    def __init__(self, database):
        self.database = database

    def cursor(self, **_kwargs):
        return FakeCursor(self.database)


class FakeTransactionalDatabase:
    """Serializes this test double; PostgreSQL lock behavior remains unverified."""

    def __init__(self, quantity):
        self.items = {
            7: {
                "id": 7,
                "nome": "Item de teste",
                "quantidade": quantity,
                "quantidade_minima": 0,
                "unidade": "un",
            }
        }
        self.ledger = {}
        self.lock = threading.RLock()

    @contextmanager
    def transaction(self):
        with self.lock:
            items_before = copy.deepcopy(self.items)
            ledger_before = copy.deepcopy(self.ledger)
            try:
                yield FakeConnection(self)
            except Exception:
                self.items = items_before
                self.ledger = ledger_before
                raise


def build_actual_move_stock(database):
    client_class = next(
        node for node in STORE_TREE.body
        if isinstance(node, ast.ClassDef) and node.name == "LocalPostgresClient"
    )
    method = next(
        node for node in client_class.body
        if isinstance(node, ast.FunctionDef) and node.name == "move_stock"
    )
    extracted_class = ast.ClassDef(
        name="ExtractedStockClient",
        bases=[],
        keywords=[],
        body=[method],
        decorator_list=[],
    )
    module = ast.fix_missing_locations(ast.Module(body=[extracted_class], type_ignores=[]))

    @contextmanager
    def fake_session_connection():
        with database.transaction() as connection:
            yield connection

    namespace = {
        "uuid": uuid,
        "json": json,
        "session_connection": fake_session_connection,
        "RealDictCursor": object,
        "Result": Result,
        "InsufficientStockError": InsufficientStockError,
        "StockItemNotFoundError": StockItemNotFoundError,
        "StockOperationConflictError": StockOperationConflictError,
    }
    exec(compile(module, str(STORE_PATH), "exec"), namespace)
    return namespace["ExtractedStockClient"]()


class StockMovementAdapterContractTests(unittest.TestCase):
    def test_same_key_same_parameters_is_idempotent_after_uncertain_result(self):
        database = FakeTransactionalDatabase(quantity=5)
        client = build_actual_move_stock(database)
        operation_id = str(uuid.uuid4())

        first = client.move_stock(7, 2, operation_id)
        # Simulate a response lost after the transaction committed.
        retry = client.move_stock(7, 2, operation_id)

        self.assertEqual(first.data[0]["quantidade"], 7)
        self.assertFalse(first.data[0]["repetida"])
        self.assertTrue(retry.data[0]["repetida"])
        self.assertEqual(database.items[7]["quantidade"], 7)

    def test_same_key_with_different_parameters_is_rejected(self):
        database = FakeTransactionalDatabase(quantity=5)
        client = build_actual_move_stock(database)
        operation_id = str(uuid.uuid4())
        client.move_stock(7, 2, operation_id)

        with self.assertRaises(StockOperationConflictError):
            client.move_stock(7, 3, operation_id)
        self.assertEqual(database.items[7]["quantidade"], 7)

    def test_insufficient_balance_rolls_back_both_stock_and_ledger(self):
        database = FakeTransactionalDatabase(quantity=2)
        client = build_actual_move_stock(database)
        operation_id = str(uuid.uuid4())

        with self.assertRaises(InsufficientStockError):
            client.move_stock(7, -3, operation_id)
        self.assertEqual(database.items[7]["quantidade"], 2)
        self.assertNotIn(operation_id, database.ledger)

    def test_integer_maximum_is_enforced_by_update_contract(self):
        database = FakeTransactionalDatabase(quantity=MAX_INTEGER - 1)
        client = build_actual_move_stock(database)
        client.move_stock(7, 1, str(uuid.uuid4()))
        self.assertEqual(database.items[7]["quantidade"], MAX_INTEGER)

        with self.assertRaises(InsufficientStockError):
            client.move_stock(7, 1, str(uuid.uuid4()))
        self.assertEqual(database.items[7]["quantidade"], MAX_INTEGER)

    def test_concurrent_distinct_operations_preserve_both_deltas_in_test_double(self):
        database = FakeTransactionalDatabase(quantity=10)
        client = build_actual_move_stock(database)
        operations = [
            (4, str(uuid.uuid4())),
            (-3, str(uuid.uuid4())),
            (2, str(uuid.uuid4())),
        ]

        with ThreadPoolExecutor(max_workers=3) as executor:
            results = list(executor.map(lambda op: client.move_stock(7, *op), operations))

        self.assertEqual(database.items[7]["quantidade"], 13)
        self.assertEqual(len(database.ledger), 3)
        self.assertTrue(all(not result.data[0]["repetida"] for result in results))


if __name__ == "__main__":
    unittest.main()
