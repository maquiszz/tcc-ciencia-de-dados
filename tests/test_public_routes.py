"""Read-only Flask route checks using an isolated database adapter double."""

import os
import sys
import types
import unittest


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
        self.assertNotIn(b"Sitemap:", robots.data)
        self.assertEqual(self.client.get("/sitemap.xml").status_code, 503)

        os.environ["PUBLIC_SITE_URL"] = "https://localhost"
        robots = self.client.get("/robots.txt")
        sitemap = self.client.get("/sitemap.xml")
        self.assertIn(b"Sitemap: https://localhost/sitemap.xml", robots.data)
        self.assertEqual(sitemap.status_code, 200)
        self.assertIn(b"https://localhost/privacidade", sitemap.data)
        self.assertNotIn(b"/api/", sitemap.data)

        os.environ["PUBLIC_SITE_URL"] = "https://example.invalid"
        self.assertEqual(self.client.get("/sitemap.xml").status_code, 503)

    def test_invalid_registration_payload_is_rejected_before_database_access(self):
        response = self.client.post("/cadastrar", json={"nome": "Cliente", "email": "cliente@example.test"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("senha", response.get_json()["error"].lower())


if __name__ == "__main__":
    unittest.main()
