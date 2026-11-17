import os
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret-key-analytics-favorites")

import database.analytics_service as analytics_service
import database.cadastro_interface as ci


class _FakeResult:
    def __init__(self, data=None):
        self.data = data or []


class _FakeTableQuery:
    def __init__(self, rows, table_name, storage):
        self._rows = rows
        self._table = table_name
        self._storage = storage
        self._filters = []
        self._action = "select"
        self._payload = None

    def select(self, *_args, **_kwargs):
        self._action = "select"
        return self

    def insert(self, payload):
        self._action = "insert"
        self._payload = payload
        return self

    def delete(self):
        self._action = "delete"
        return self

    def eq(self, col, val):
        self._filters.append(("eq", col, val))
        return self

    def gt(self, col, val):
        self._filters.append(("gt", col, val))
        return self

    def order(self, *_args, **_kwargs):
        return self

    def limit(self, *_args, **_kwargs):
        return self

    def execute(self):
        if self._action == "insert":
            novo = dict(self._payload)
            novo.setdefault("id", len(self._storage[self._table]) + 1)
            self._storage[self._table].append(novo)
            return _FakeResult([novo])

        filtrados = list(self._rows)
        for op, col, val in self._filters:
            if op == "eq":
                filtrados = [r for r in filtrados if r.get(col) == val]
            elif op == "gt":
                filtrados = [r for r in filtrados if str(r.get(col) or "") > str(val)]

        if self._action == "delete":
            restantes = [r for r in self._rows if r not in filtrados]
            self._storage[self._table] = restantes
            return _FakeResult(filtrados)

        return _FakeResult(filtrados)


class _FakeDB:
    def __init__(self, storage):
        self._storage = storage

    def table(self, name):
        return _FakeTableQuery(self._storage[name], name, self._storage)


class AnalyticsServiceTests(unittest.TestCase):
    def test_resolver_intervalo_analytics_presets_e_custom(self):
        hoje = date(2026, 11, 17)
        ini, fim, chave = analytics_service.resolver_intervalo_analytics("7d", None, None, hoje)
        self.assertEqual((ini, fim, chave), (date(2026, 11, 11), hoje, "7d"))

        ini, fim, chave = analytics_service.resolver_intervalo_analytics(
            "custom", "2026-11-01", "2026-11-10", hoje
        )
        self.assertEqual((ini, fim, chave), (date(2026, 11, 1), date(2026, 11, 10), "custom"))

        with self.assertRaises(ValueError):
            analytics_service.resolver_intervalo_analytics("custom", "2026-11-15", "2026-11-10", hoje)

    def test_calcular_dashboard_analitico_com_dados_reais(self):
        servicos = {
            1: {"id": 1, "tipo": "Massagem", "valor": 350.0},
            2: {"id": 2, "tipo": "Acupuntura", "valor": 280.0},
        }
        agendamentos = [
            {"id": 1, "servico_id": 1, "data_atendimento": "2026-11-10T10:00:00", "status": "Concluido"},
            {"id": 2, "servico_id": 1, "data_atendimento": "2026-11-10T14:00:00", "status": "Cancelado"},
            {"id": 3, "servico_id": 2, "data_atendimento": "2026-11-11T10:00:00", "status": "Pendente"},
            {"id": 4, "servico_id": 2, "data_atendimento": "2026-10-01T10:00:00", "status": "Concluido"},
        ]
        res = analytics_service.calcular_dashboard_analitico(
            agendamentos, servicos, date(2026, 11, 10), date(2026, 11, 12)
        )
        self.assertEqual(res["resumo"]["total"], 3)
        self.assertEqual(res["resumo"]["concluidos"], 1)
        self.assertEqual(res["resumo"]["cancelados"], 1)
        self.assertEqual(res["resumo"]["pendentes"], 1)
        self.assertEqual(res["resumo"]["taxa_comparecimento"], 50.0)
        self.assertEqual(res["resumo"]["taxa_cancelamento"], 33.3)
        self.assertEqual(res["resumo"]["faturamento_estimado"], 350.0)
        self.assertEqual(res["servicos_populares"][0]["nome"], "Massagem")
        self.assertEqual(res["servicos_populares"][0]["quantidade"], 2)
        self.assertEqual(len(res["evolucao"]), 3)
        self.assertEqual(len(res["por_dia_semana"]), 7)

    def test_calcular_dashboard_sem_dados_exibe_limitacao(self):
        res = analytics_service.calcular_dashboard_analitico(
            [], {1: {"id": 1, "tipo": "Massagem", "valor": 350.0}}, date(2026, 11, 1), date(2026, 11, 7)
        )
        self.assertEqual(res["resumo"]["total"], 0)
        self.assertIsNone(res["resumo"]["taxa_comparecimento"])
        self.assertTrue(any("Nenhum agendamento" in msg for msg in res["limitacoes"]))


class AnalyticsAndFavoritesRouteTests(unittest.TestCase):
    def setUp(self):
        self.storage = {
            "usuarios": [
                {
                    "id": 10,
                    "email": "cliente@spa.com",
                    "nome": "Cliente",
                    "senha": "hash-cliente",
                    "is_admin": False,
                    "email_verificado": True,
                    "sessao_versao": 1,
                },
                {
                    "id": 11,
                    "email": "admin@spa.com",
                    "nome": "Admin",
                    "senha": "hash-admin",
                    "is_admin": True,
                    "email_verificado": True,
                    "sessao_versao": 1,
                },
            ],
            "servico": [
                {"id": 1, "tipo": "Massagem", "valor": 350.0},
                {"id": 2, "tipo": "Acupuntura", "valor": 280.0},
            ],
            "agendamentos": [
                {"id": 1, "servico_id": 1, "data_atendimento": "2026-11-16T10:00:00", "status": "Concluido"},
                {"id": 2, "servico_id": 2, "data_atendimento": "2026-11-16T14:00:00", "status": "Pendente"},
            ],
            "servicos_favoritos": [],
        }
        self.fake_db = _FakeDB(self.storage)

    def _autenticar(self, client, user):
        with client.session_transaction() as sess:
            sess["usuario_email"] = user["email"]
            sess["usuario_id"] = user["id"]
            sess["sid"] = f"sid-{user['id']}"
            sess["iat"] = 1700000000
            sess["password_version"] = ci.security.fingerprint(user["senha"])
            sess["sessao_versao"] = user["sessao_versao"]
            sess["csrf_token"] = "csrf-ok"

    def test_favoritos_crud_e_autorizacao(self):
        client = ci.app.test_client()
        with patch.object(ci, "db", self.fake_db):
            self.assertEqual(client.get("/api/favoritos").status_code, 401)
            self._autenticar(client, self.storage["usuarios"][0])

            r_add = client.post("/api/favoritos/1", headers={"X-CSRF-Token": "csrf-ok"})
            self.assertEqual(r_add.status_code, 200)
            self.assertTrue(r_add.get_json()["favorito"])

            # Idempotente
            self.assertEqual(client.post("/api/favoritos/1", headers={"X-CSRF-Token": "csrf-ok"}).status_code, 200)
            r_list = client.get("/api/favoritos")
            self.assertEqual(r_list.status_code, 200)
            self.assertEqual(r_list.get_json()["favoritos"], [1])

            r_del = client.delete("/api/favoritos/1", headers={"X-CSRF-Token": "csrf-ok"})
            self.assertEqual(r_del.status_code, 200)
            self.assertEqual(client.get("/api/favoritos").get_json()["favoritos"], [])

    def test_admin_analytics_requer_admin_e_retorna_metricas(self):
        client = ci.app.test_client()
        with patch.object(ci, "db", self.fake_db):
            self._autenticar(client, self.storage["usuarios"][0])
            self.assertEqual(client.get("/api/admin/analytics?periodo=30d").status_code, 403)

            self._autenticar(client, self.storage["usuarios"][1])
            r = client.get("/api/admin/analytics?periodo=30d")
            self.assertEqual(r.status_code, 200)
            body = r.get_json()
            self.assertIn("resumo", body)
            self.assertIn("evolucao", body)
            self.assertIn("servicos_populares", body)
            self.assertIn("por_dia_semana", body)
            self.assertIn("horarios_pico", body)


if __name__ == "__main__":
    unittest.main()
