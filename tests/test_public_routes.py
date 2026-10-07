"""Read-only Flask route checks using an isolated database adapter double."""

import os
import sys
import types
import unittest
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo


class _FakeQuery:
    def __init__(self, name):
        self.name = name

    def select(self, *_args, **_kwargs):
        return self

    def order(self, *_args, **_kwargs):
        return self

    def eq(self, *_args, **_kwargs):
        return self

    def limit(self, *_args, **_kwargs):
        return self

    def neq(self, *_args, **_kwargs):
        return self

    def gt(self, *_args, **_kwargs):
        return self

    def range(self, *_args, **_kwargs):
        return self

    def execute(self):
        rows = [
            {"id": 1, "tipo": "Massagem relaxante", "descricao": "Pausa e relaxamento.", "valor": 180, "imagem_url": None},
            {"id": 2, "tipo": "Acupuntura", "descricao": "Cuidado individualizado.", "valor": 160, "imagem_url": None},
        ] if self.name == "servico" else []
        return types.SimpleNamespace(data=rows)


class _FakeDatabase:
    def ensure_schema(self):
        return None

    def table(self, *_args, **_kwargs):
        return _FakeQuery(_args[0])


def _load_app_without_database():
    os.environ.update({
        "APP_ENV": "test",
        "RENDER": "false",
        "FLASK_SECRET_KEY": "isolated-public-route-test-key",
        "TRUSTED_HOSTS": "localhost,127.0.0.1",
        "CORS_ORIGINS": "http://localhost",
    })
    os.environ.pop("SPA_FRONTEND_FILE", None)
    os.environ.pop("PUBLIC_SITE_URL", None)

    fake_store = types.ModuleType("database.postgres_store")
    for name in (
        "InsufficientStockError",
        "ScheduleConflictError",
        "ServiceNotFoundError",
        "StockItemNotFoundError",
        "StockOperationConflictError",
        "StockQuantityConflictError",
    ):
        setattr(fake_store, name, type(name, (Exception,), {}))
    fake_store.create_database_client = lambda: _FakeDatabase()
    sys.modules["database.postgres_store"] = fake_store

    from database.cadastro_interface import app

    app.config.update(TESTING=True)
    return app


APP = _load_app_without_database()


class PublicRouteTests(unittest.TestCase):
    def setUp(self):
        self.client = APP.test_client()
        os.environ.pop("PUBLIC_SITE_URL", None)

    def test_registration_route_renders_real_form_and_is_not_indexed(self):
        response = self.client.get("/cadastro")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"id=cadastroForm", response.data)
        self.assertIn(b"name=senha", response.data)
        self.assertIn(b"id=verificacaoForm", response.data)
        self.assertEqual(response.headers.get("X-Robots-Tag"), "noindex, nofollow")

    def test_home_and_favicon_use_public_assets(self):
        home = self.client.get("/")
        self.assertEqual(home.status_code, 200)
        self.assertIn(b"Um tempo para cuidar de voc", home.data)
        self.assertIn(b"/app-icon.svg", home.data)
        self.assertNotIn(b"__SPA_CANONICAL_METADATA__", home.data)
        self.assertIn(b"Custos e despesas", home.data)
        self.assertIn(b"Mensagem para o assistente virtual", home.data)
        self.assertEqual(home.headers.get("Cache-Control"), "no-cache, must-revalidate")
        icon = self.client.get("/favicon.ico")
        self.assertEqual(icon.status_code, 308)
        self.assertEqual(icon.headers.get("Location"), "/app-icon.svg")
        self.assertEqual(self.client.get("/app-icon.svg").status_code, 200)
        stylesheet = self.client.get("/static/css/privacy-notice.min.css")
        script = self.client.get("/static/js/privacy-notice.min.js")
        self.assertEqual(stylesheet.status_code, 200)
        self.assertEqual(script.status_code, 200)
        stylesheet.close()
        script.close()

        for path in (
            "/manifest.webmanifest",
            "/spa-sw.js",
            "/static/js/registration.min.js",
            "/static/img/logo_panaceia.png",
        ):
            with self.subTest(path=path):
                asset = self.client.get(path)
                self.assertEqual(asset.status_code, 200)
                asset.close()

    def test_public_privacy_copy_matches_confirmed_account_behavior(self):
        registration = self.client.get("/cadastro")
        self.assertIn(b"confirmar seu e-mail", registration.data)
        self.assertIn(b"recursos da conta", registration.data)
        self.assertNotIn(b"somente para atendimento e gest\xc3\xa3o das reservas", registration.data)
        self.assertIn(b"--danger:#ff8b83", registration.data)
        self.assertIn(b"--success:#62ddb0", registration.data)

        privacy = self.client.get("/privacidade")
        self.assertIn(b"o Spa precisa indicar um canal oficial", privacy.data)
        self.assertIn(b"n\xc3\xa3o declara conformidade", privacy.data)

    def test_canonical_metadata_is_emitted_only_for_an_exact_trusted_https_host(self):
        os.environ["PUBLIC_SITE_URL"] = "https://localhost"
        response = self.client.get("/")
        self.assertIn(b'<link rel="canonical" href="https://localhost/">', response.data)

        os.environ["PUBLIC_SITE_URL"] = "https://outside.example"
        response = self.client.get("/")
        self.assertNotIn(b"rel=canonical", response.data)
        self.assertNotIn(b"https://outside.example", response.data)

    def test_canonical_url_rejects_non_https_and_nonstandard_authorities(self):
        from database.cadastro_interface import obter_url_publica_canonica

        for candidate in (
            "http://localhost",
            "https://outside.example",
            "https://localhost:8443",
            "https://user@localhost",
            "https://localhost/path",
            "https://localhost/?q=1",
        ):
            with self.subTest(candidate=candidate):
                os.environ["PUBLIC_SITE_URL"] = candidate
                self.assertIsNone(obter_url_publica_canonica())

    def test_html_and_api_not_found_keep_their_status_and_representation(self):
        missing = self.client.get("/caminho-inexistente")
        self.assertEqual(missing.status_code, 404)
        self.assertIn(b"Esta p\xc3\xa1gina n\xc3\xa3o foi encontrada", missing.data)
        self.assertEqual(missing.headers.get("X-Robots-Tag"), "noindex, nofollow")
        api_missing = self.client.get("/api/rota-inexistente")
        self.assertEqual(api_missing.status_code, 404)
        self.assertEqual(api_missing.get_json()["code"], "route_not_found")

    def test_robots_excludes_private_api_and_sitemap_needs_configured_trusted_host(self):
        robots = self.client.get("/robots.txt")
        self.assertEqual(robots.status_code, 200)
        self.assertIn(b"Disallow: /api/", robots.data)
        self.assertIn(b"Disallow: /cadastrar", robots.data)
        self.assertNotIn(b"Sitemap:", robots.data)
        self.assertEqual(self.client.get("/sitemap.xml").status_code, 503)

        os.environ["PUBLIC_SITE_URL"] = "https://localhost"
        robots = self.client.get("/robots.txt")
        sitemap = self.client.get("/sitemap.xml")
        self.assertIn(b"Sitemap: https://localhost/sitemap.xml", robots.data)
        self.assertEqual(sitemap.status_code, 200)
        self.assertIn(b"https://localhost/privacidade", sitemap.data)
        self.assertNotIn(b"/api/", sitemap.data)
        self.assertNotIn(b"/cadastro", sitemap.data)
        self.assertNotIn(b"/perfil", sitemap.data)
        self.assertNotIn(b"/admin", sitemap.data)

        os.environ["PUBLIC_SITE_URL"] = "https://example.invalid"
        self.assertEqual(self.client.get("/sitemap.xml").status_code, 503)

    def test_invalid_registration_payload_is_rejected_before_database_access(self):
        response = self.client.post("/cadastrar", json={"nome": "Cliente", "email": "cliente@example.test"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("senha", response.get_json()["error"].lower())


def _simulate_expiry(records, local_now, activation_date, locked_ids=(), fail_audit=False):
    """In-memory transaction model for the SQL job; it is not a PostgreSQL test."""
    local_today = local_now.astimezone(ZoneInfo("America/Sao_Paulo")).date()
    staged = [dict(record) for record in records]
    audit = []

    for record in sorted(staged, key=lambda item: (item["data_atendimento"], item["id"])):
        if (
            record["status"] != "Pendente"
            or record["data_atendimento"].date() >= local_today
            or record["data_atendimento"].date() < activation_date
            or record["id"] in locked_ids
        ):
            continue

        record["status"] = "Cancelado"
        if fail_audit:
            raise RuntimeError("falha simulada na auditoria")
        audit.append({
            "agendamento_id": record["id"],
            "ator_tipo": "sistema",
            "acao": "cancelamento_automatico",
            "status_anterior": "Pendente",
            "status_novo": "Cancelado",
        })

    records[:] = staged
    return len(audit), audit


class AppointmentExpiryModelTests(unittest.TestCase):
    def setUp(self):
        self.sao_paulo = ZoneInfo("America/Sao_Paulo")
        self.activation = date(2026, 10, 6)

    def test_booking_remains_valid_until_the_local_day_ends(self):
        rows = [{
            "id": 1,
            "data_atendimento": datetime(2026, 10, 7, 20, 0),
            "status": "Pendente",
        }]

        count, _ = _simulate_expiry(
            rows, datetime(2026, 10, 7, 23, 59, tzinfo=self.sao_paulo), self.activation
        )
        self.assertEqual(count, 0)
        self.assertEqual(rows[0]["status"], "Pendente")

        count, audit = _simulate_expiry(
            rows, datetime(2026, 10, 8, 0, 0, tzinfo=self.sao_paulo), self.activation
        )
        self.assertEqual(count, 1)
        self.assertEqual(rows[0]["status"], "Cancelado")
        self.assertEqual(audit[0]["ator_tipo"], "sistema")

    def test_repeat_and_non_pending_statuses_do_not_change_or_duplicate_audit(self):
        rows = [
            {"id": 1, "data_atendimento": datetime(2026, 10, 6, 10), "status": "Pendente"},
            {"id": 2, "data_atendimento": datetime(2026, 10, 6, 11), "status": "Concluido"},
            {"id": 3, "data_atendimento": datetime(2026, 10, 6, 12), "status": "Cancelado"},
        ]
        now = datetime(2026, 10, 8, 12, tzinfo=self.sao_paulo)

        first_count, first_audit = _simulate_expiry(rows, now, self.activation)
        second_count, second_audit = _simulate_expiry(rows, now, self.activation)

        self.assertEqual(first_count, 1)
        self.assertEqual(second_count, 0)
        self.assertEqual(len(first_audit) + len(second_audit), 1)
        self.assertEqual([row["status"] for row in rows], ["Cancelado", "Concluido", "Cancelado"])

    def test_a_completion_holding_the_row_lock_wins_without_cancellation(self):
        rows = [{
            "id": 1,
            "data_atendimento": datetime(2026, 10, 6, 10),
            "status": "Pendente",
            "points": 0,
        }]
        now = datetime(2026, 10, 8, 12, tzinfo=self.sao_paulo)

        count, audit = _simulate_expiry(rows, now, self.activation, locked_ids={1})
        self.assertEqual((count, audit), (0, []))
        rows[0]["status"] = "Concluido"  # simulated completion commits first
        rows[0]["points"] += 18
        count, audit = _simulate_expiry(rows, now, self.activation)

        self.assertEqual((count, audit), (0, []))
        self.assertEqual(rows[0]["status"], "Concluido")
        self.assertEqual(rows[0]["points"], 18)

    def test_cancellation_winning_the_lock_prevents_a_later_points_credit(self):
        rows = [{
            "id": 1,
            "data_atendimento": datetime(2026, 10, 6, 10),
            "status": "Pendente",
            "points": 0,
        }]

        count, audit = _simulate_expiry(
            rows, datetime(2026, 10, 8, 12, tzinfo=self.sao_paulo), self.activation
        )

        self.assertEqual(count, 1)
        self.assertEqual(len(audit), 1)
        self.assertEqual(rows[0]["status"], "Cancelado")
        if rows[0]["status"] == "Pendente":
            rows[0]["status"] = "Concluido"
            rows[0]["points"] += 18
        self.assertEqual(rows[0]["status"], "Cancelado")
        self.assertEqual(rows[0]["points"], 0)

    def test_audit_failure_rolls_back_the_mocked_status_change(self):
        rows = [{
            "id": 1,
            "data_atendimento": datetime(2026, 10, 6, 10),
            "status": "Pendente",
        }]
        before = [dict(row) for row in rows]

        with self.assertRaisesRegex(RuntimeError, "auditoria"):
            _simulate_expiry(
                rows,
                datetime(2026, 10, 8, 12, tzinfo=self.sao_paulo),
                self.activation,
                fail_audit=True,
            )

        self.assertEqual(rows, before)

    def test_documented_sql_keeps_timezone_lock_audit_and_safe_activation_contract(self):
        readme = Path(__file__).resolve().parents[1] / "README.md"
        text = readme.read_text(encoding="utf-8")
        section = text.split("## Cancelamento automático de agendamentos vencidos", 1)[1]
        migration = section.split("```sql", 1)[1].split("```", 1)[0].lower()

        for fragment in (
            "create extension if not exists pg_cron",
            "at time zone 'america/sao_paulo'",
            "a.status = 'pendente'",
            "a.data_atendimento < v_hoje::timestamp",
            "a.data_atendimento >= p_desde::timestamp",
            "for update of a skip locked",
            "and a.status = 'pendente'",
            "insert into public.agenda_auditoria",
            "'sistema', 'sistema',",
            "'cancelamento_automatico'",
            "'*/15 * * * *'",
            "from public, anon, authenticated, service_role",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, migration)


if __name__ == "__main__":
    unittest.main()
