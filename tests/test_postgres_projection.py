"""Unit tests for the local PostgreSQL adapter's narrow SELECT projections."""

import unittest

from database.postgres_store import Query


class PostgresProjectionTests(unittest.TestCase):
    def test_plain_projection_selects_only_requested_columns(self):
        query = Query("servico").select("id, tipo, descricao, valor, imagem_url")
        self.assertEqual(
            query._selected_columns(),
            ["id", "tipo", "descricao", "valor", "imagem_url"],
        )

    def test_embedded_service_projection_keeps_only_required_foreign_key(self):
        query = Query("agendamentos").select(
            "id, data_atendimento, status, servico(tipo, valor)"
        )
        self.assertEqual(
            query._selected_columns(),
            ["id", "data_atendimento", "status", "servico_id"],
        )

    def test_wildcard_and_unsupported_projection_keep_legacy_fallback(self):
        self.assertIsNone(Query("usuarios").select("*")._selected_columns())
        self.assertIsNone(Query("servico").select("id AS service_id")._selected_columns())


if __name__ == "__main__":
    unittest.main()
