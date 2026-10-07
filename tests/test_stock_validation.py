"""Safe unit tests for stock quantity validation and route pre-write guards."""

import ast
import unittest
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE_PATH = ROOT / "database" / "cadastro_interface.py"
TREE = ast.parse(SOURCE_PATH.read_text(encoding="utf-8"))


def load_stock_functions():
    names = {
        "ESTOQUE_QUANTIDADE_MAXIMA",
        "quantidade_inteira",
        "adicionar_estoque",
        "atualizar_estoque",
    }
    selected = []
    for node in TREE.body:
        name = None
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            name = node.name
        elif isinstance(node, ast.Assign):
            name = next(
                (target.id for target in node.targets if isinstance(target, ast.Name)),
                None,
            )
        if name in names:
            if isinstance(node, ast.FunctionDef):
                node.decorator_list = []
            selected.append(node)

    module = ast.fix_missing_locations(ast.Module(body=selected, type_ignores=[]))
    namespace = {
        "uuid": uuid,
        "jsonify": lambda payload: payload,
        "json_body": lambda: {},
        "logger": type("Logger", (), {"exception": lambda *args, **kwargs: None})(),
        "StockItemNotFoundError": type("StockItemNotFoundError", (Exception,), {}),
        "InsufficientStockError": type("InsufficientStockError", (Exception,), {}),
        "StockOperationConflictError": type("StockOperationConflictError", (Exception,), {}),
        "StockQuantityConflictError": type("StockQuantityConflictError", (Exception,), {}),
    }
    exec(compile(module, str(SOURCE_PATH), "exec"), namespace)
    return namespace


FUNCTIONS = load_stock_functions()


class FakeTable:
    def __init__(self, calls):
        self.calls = calls

    def insert(self, data):
        self.calls.append(("insert", data))
        return self

    def execute(self):
        self.calls.append(("execute",))
        return type("Response", (), {"data": []})()


class FakeDatabase:
    def __init__(self):
        self.calls = []

    def table(self, name):
        self.calls.append(("table", name))
        return FakeTable(self.calls)

    def move_stock(self, *args):
        self.calls.append(("move_stock", args))
        return type("Response", (), {"data": [{"quantidade": 10}]})()

    def set_stock_quantity(self, *args):
        self.calls.append(("set_stock_quantity", args))
        return type("Response", (), {"data": [{"quantidade": 10}]})()


class StockQuantityValidationTests(unittest.TestCase):
    def setUp(self):
        self.validate = FUNCTIONS["quantidade_inteira"]

    def test_accepts_integer_and_integer_string_at_database_limit(self):
        self.assertEqual(self.validate(0, "Quantidade"), 0)
        self.assertEqual(self.validate("00012", "Quantidade"), 12)
        self.assertEqual(self.validate(2_147_483_647, "Quantidade"), 2_147_483_647)
        self.assertEqual(self.validate("2147483647", "Quantidade"), 2_147_483_647)

    def test_rejects_invalid_or_out_of_range_values(self):
        for value in (2.9, 2.0, True, False, -1, "-1", "2.9", "", None, "１２", "9" * 10000):
            with self.subTest(value=repr(value)[:40]):
                with self.assertRaises(ValueError):
                    self.validate(value, "Quantidade")

        with self.assertRaisesRegex(ValueError, "limite máximo"):
            self.validate(2_147_483_648, "Quantidade")
        with self.assertRaisesRegex(ValueError, "limite máximo"):
            self.validate("2147483648", "Quantidade")

    def test_invalid_create_payload_does_not_call_database(self):
        for body in (
            {"nome": "Óleo", "quantidade": 2.9},
            {"nome": "Óleo", "quantidade": 2, "quantidade_minima": True},
            {"nome": "Óleo", "quantidade": 2_147_483_648},
        ):
            with self.subTest(body=body):
                database = FakeDatabase()
                FUNCTIONS.update(db=database, json_body=lambda body=body: body)
                response, status = FUNCTIONS["adicionar_estoque"]()
                self.assertEqual(status, 400)
                self.assertIn("error", response)
                self.assertEqual(database.calls, [])

    def test_invalid_movement_and_manual_adjustment_do_not_call_database(self):
        invalid_bodies = (
            {"quantidade": 2.9, "acao": "adicionar", "operacao_id": str(uuid.uuid4())},
            {"quantidade": True, "acao": "adicionar", "operacao_id": str(uuid.uuid4())},
            {"quantidade": 1, "quantidade_atual": -1},
            {"quantidade": 1.5, "quantidade_atual": 10},
            {"quantidade": 1, "quantidade_atual": 10.5},
            {"quantidade": 1, "quantidade_atual": 2_147_483_648},
            {"quantidade": 2_147_483_648, "quantidade_atual": 10},
        )
        for body in invalid_bodies:
            with self.subTest(body=body):
                database = FakeDatabase()
                FUNCTIONS.update(db=database, json_body=lambda body=body: body)
                response, status = FUNCTIONS["atualizar_estoque"](item_id=7)
                self.assertEqual(status, 400)
                self.assertIn("error", response)
                self.assertEqual(database.calls, [])


if __name__ == "__main__":
    unittest.main()
