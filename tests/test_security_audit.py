"""Comprehensive security tests for the 8 audited vulnerabilities in Spa Panaceia."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
import os
import re
import sys
import threading
import time
import types
import unittest
from unittest.mock import patch

from werkzeug.security import check_password_hash, generate_password_hash

# Ensure test environment if this file is executed standalone before test_public_routes.py
if "database.cadastro_interface" not in sys.modules:
    os.environ.update({
        "APP_ENV": "test",
        "RENDER": "false",
        "FLASK_SECRET_KEY": "isolated-public-route-test-key",
        "TRUSTED_HOSTS": "localhost,127.0.0.1",
        "CORS_ORIGINS": "http://localhost",
    })
    os.environ.pop("SPA_FRONTEND_FILE", None)
    if "database.postgres_store" not in sys.modules:
        import database.postgres_store as real_store
        real_store.create_database_client = lambda: types.SimpleNamespace(
            ensure_schema=lambda: None,
            table=lambda *_a, **_kw: types.SimpleNamespace(
                select=lambda *a, **k: types.SimpleNamespace(
                    order=lambda *a2, **k2: types.SimpleNamespace(
                        execute=lambda: types.SimpleNamespace(data=[])
                    )
                )
            ),
        )

import database.cadastro_interface as ci
from database.spa_security import RateLimiter


class _InMemoryTableQuery:
    def __init__(self, db_store: "_InMemorySpaDB", table_name: str):
        self._db = db_store
        self._table = table_name
        self._op = "select"
        self._payload = None
        self._filters = []
        self._limit = None

    def select(self, *_args, **_kwargs):
        self._op = "select"
        return self

    def insert(self, payload):
        self._op = "insert"
        self._payload = dict(payload)
        return self

    def update(self, payload):
        self._op = "update"
        self._payload = dict(payload)
        return self

    def delete(self):
        self._op = "delete"
        return self

    def eq(self, column, value):
        self._filters.append(("eq", column, value))
        return self

    def neq(self, column, value):
        self._filters.append(("neq", column, value))
        return self

    def gt(self, column, value):
        self._filters.append(("gt", column, value))
        return self

    def lt(self, column, value):
        self._filters.append(("lt", column, value))
        return self

    def is_(self, column, value):
        self._filters.append(("is", column, value))
        return self

    def order(self, *_args, **_kwargs):
        return self

    def range(self, *_args, **_kwargs):
        return self

    def limit(self, count):
        self._limit = int(count)
        return self

    def _matches(self, row: dict) -> bool:
        for kind, col, val in self._filters:
            rv = row.get(col)
            if kind == "eq" and rv != val:
                return False
            if kind == "neq" and rv == val:
                return False
            if kind == "gt" and (rv is None or str(rv) <= str(val)):
                return False
            if kind == "lt" and (rv is None or str(rv) >= str(val)):
                return False
            if kind == "is":
                if val is None or str(val).lower() == "null":
                    if rv is not None:
                        return False
        return True

    def execute(self):
        with self._db.lock:
            self._db.query_counts[self._table] = self._db.query_counts.get(self._table, 0) + 1
            rows = self._db.tables.setdefault(self._table, [])
            if self._op == "insert":
                new_row = dict(self._payload)
                self._db.next_id += 1
                new_row.setdefault("id", self._db.next_id)
                if self._table == "usuarios":
                    for existing in rows:
                        if existing.get("email") == new_row.get("email"):
                            raise RuntimeError("duplicate key value violates unique constraint (23505)")
                    new_row.setdefault("pontos", 0)
                    new_row.setdefault("sessao_versao", 1)
                    new_row.setdefault("codigo_otp_tentativas", 0)
                    new_row.setdefault("token_recuperacao_tentativas", 0)
                    new_row.setdefault("token_recuperacao_expira_em", None)
                rows.append(new_row)
                return types.SimpleNamespace(data=[dict(new_row)])

            matched = [r for r in rows if self._matches(r)]
            if self._limit is not None:
                matched = matched[: self._limit]

            if self._op == "select":
                return types.SimpleNamespace(data=[dict(r) for r in matched])

            if self._op == "update":
                updated = []
                for r in matched:
                    r.update(self._payload)
                    updated.append(dict(r))
                return types.SimpleNamespace(data=updated)

            if self._op == "delete":
                deleted = [dict(r) for r in matched]
                ids_to_remove = {id(r) for r in matched}
                self._db.tables[self._table] = [r for r in rows if id(r) not in ids_to_remove]
                return types.SimpleNamespace(data=deleted)

            return types.SimpleNamespace(data=[])


class _InMemorySpaDB:
    def __init__(self):
        self.lock = threading.RLock()
        self.next_id = 100
        self.query_counts = {}
        self.tables = {
            "usuarios": [],
            "servico": [
                {"id": 1, "tipo": "Massagem relaxante", "descricao": "Relaxamento.", "valor": 180, "imagem_url": None},
                {"id": 2, "tipo": "Acupuntura", "descricao": "Equilíbrio.", "valor": 160, "imagem_url": None},
            ],
            "agendamentos": [],
            "agenda_lista_espera": [],
            "agenda_auditoria": [],
            "agenda_bloqueios": [],
            "estoque": [],
            "resgates_pontos": [],
        }

    def table(self, name: str):
        return _InMemoryTableQuery(self, name)

    def create_appointment(self, email: str, servico_id: int, data_atendimento: str):
        with self.lock:
            servicos = [s for s in self.tables["servico"] if s["id"] == servico_id]
            if not servicos:
                raise ci.ServiceNotFoundError("Serviço não encontrado.")
            for ag in self.tables["agendamentos"]:
                if ag["data_atendimento"] == data_atendimento and ag.get("status") != "Cancelado":
                    raise ci.ScheduleConflictError("Horário já ocupado.")
            self.next_id += 1
            novo = {
                "id": self.next_id,
                "email_cliente": email,
                "servico_id": servico_id,
                "data_atendimento": data_atendimento,
                "status": "Pendente",
            }
            self.tables["agendamentos"].append(novo)
            return types.SimpleNamespace(data=[dict(novo)])


class SecurityAuditTestSuite(unittest.TestCase):
    def setUp(self):
        self.original_db = ci.db
        self.fake_db = _InMemorySpaDB()
        ci.db = self.fake_db
        ci.rate_limiter.clear()
        ci.spam_blocker.clear()
        ci.session_revocations.clear()
        os.environ.pop("SPA_FRONTEND_FILE", None)
        self.client = ci.app.test_client()
        self.sent_emails = []
        self.email_patcher = patch.object(
            ci,
            "enviar_email_transacional",
            side_effect=self._record_email,
        )
        self.email_patcher.start()

    def tearDown(self):
        deadline = time.monotonic() + 2.0
        while (ci.email_executor.in_flight > 0 or ci.notificacao_executor.in_flight > 0) and time.monotonic() < deadline:
            time.sleep(0.01)
        self.email_patcher.stop()
        ci.db = self.original_db
        ci.rate_limiter.clear()
        ci.spam_blocker.clear()
        ci.session_revocations.clear()
        os.environ.pop("SPA_FRONTEND_FILE", None)

    def _record_email(self, destinatario, assunto, conteudo_html):
        match = re.search(r">(\d{6})<", str(conteudo_html))
        codigo_otp = match.group(1) if match else None
        self.sent_emails.append({
            "email": destinatario,
            "assunto": assunto,
            "codigo": codigo_otp,
            "html": conteudo_html,
        })
        return True

    def _headers(self, csrf_token=None, ip="127.0.0.1"):
        headers = {
            "Origin": "http://localhost",
            "X-Forwarded-For": ip,
        }
        if csrf_token:
            headers["X-CSRF-Token"] = csrf_token
        return headers

    # =========================================================================
    # VULNERABILIDADE 1: Enumeração de contas e sequestro de pré-cadastro
    # =========================================================================
    def test_v1_new_registration_and_otp_validation(self):
        resp = self.client.post(
            "/cadastrar",
            json={"nome": "Ana Silva", "email": "ana@example.test", "senha": "Password!"},
            headers=self._headers(),
        )
        self.assertEqual(resp.status_code, 201)
        data = resp.get_json()
        self.assertEqual(data["email"], "ana@example.test")
        self.assertEqual(len(self.sent_emails), 1)
        otp_code = self.sent_emails[0]["codigo"]

        # Verify OTP is stored as an HMAC hash (v2:...), never plaintext
        user_row = self.fake_db.tables["usuarios"][0]
        self.assertTrue(str(user_row["codigo_otp"]).startswith("v2:"))
        self.assertNotEqual(user_row["codigo_otp"], otp_code)
        self.assertFalse(user_row["email_verificado"])

        # Validate with correct OTP
        val_resp = self.client.post(
            "/api/validar-codigo",
            json={"email": "ana@example.test", "codigo": otp_code},
            headers=self._headers(),
        )
        self.assertEqual(val_resp.status_code, 200)
        self.assertTrue(self.fake_db.tables["usuarios"][0]["email_verificado"])
        self.assertIsNone(self.fake_db.tables["usuarios"][0]["codigo_otp"])

        # Reusing the same OTP must fail with generic 400 (not 404)
        reuse_resp = self.client.post(
            "/api/validar-codigo",
            json={"email": "ana@example.test", "codigo": otp_code},
            headers=self._headers(),
        )
        self.assertEqual(reuse_resp.status_code, 400)

    def test_v1_unverified_account_hijack_recovery(self):
        # Attacker pre-registers victim's email with attacker's password
        resp1 = self.client.post(
            "/cadastrar",
            json={"nome": "Atacante", "email": "vitima@example.test", "senha": "Attacker!"},
            headers=self._headers(ip="10.0.0.1"),
        )
        self.assertEqual(resp1.status_code, 201)
        old_otp = self.sent_emails[-1]["codigo"]

        # Real owner registers their own email before verification completes
        resp2 = self.client.post(
            "/cadastrar",
            json={"nome": "Vitima Real", "email": "vitima@example.test", "senha": "RealOwner!"},
            headers=self._headers(ip="10.0.0.2"),
        )
        # Must succeed with 201 (not 409 Conflict!) and replace name, password hash, and OTP
        self.assertEqual(resp2.status_code, 201)
        new_otp = self.sent_emails[-1]["codigo"]
        self.assertNotEqual(old_otp, new_otp)

        user_row = self.fake_db.tables["usuarios"][0]
        self.assertEqual(user_row["nome"], "Vitima Real")
        self.assertTrue(check_password_hash(user_row["senha"], "RealOwner!"))
        self.assertFalse(check_password_hash(user_row["senha"], "Attacker!"))

        # Old OTP from attacker must be invalid
        bad_val = self.client.post(
            "/api/validar-codigo",
            json={"email": "vitima@example.test", "codigo": old_otp},
            headers=self._headers(ip="10.0.0.1"),
        )
        self.assertEqual(bad_val.status_code, 400)
        self.assertFalse(self.fake_db.tables["usuarios"][0]["email_verificado"])

        # Real owner validates with new OTP
        ok_val = self.client.post(
            "/api/validar-codigo",
            json={"email": "vitima@example.test", "codigo": new_otp},
            headers=self._headers(ip="10.0.0.2"),
        )
        self.assertEqual(ok_val.status_code, 200)
        self.assertTrue(self.fake_db.tables["usuarios"][0]["email_verificado"])

    def test_v1_verified_account_cannot_be_overwritten_or_enumerated(self):
        self.fake_db.tables["usuarios"].append({
            "id": 1,
            "nome": "Cliente Verificado",
            "email": "verificado@example.test",
            "senha": generate_password_hash("OrigPass!", method="pbkdf2:sha256"),
            "email_verificado": True,
            "codigo_otp": None,
            "codigo_expira_em": None,
            "pontos": 50,
            "sessao_versao": 1,
        })

        # Attempting /cadastrar on an already-verified email must NOT return 409 and must NOT overwrite
        resp = self.client.post(
            "/cadastrar",
            json={"nome": "Invasor", "email": "verificado@example.test", "senha": "HackPass!"},
            headers=self._headers(),
        )
        self.assertEqual(resp.status_code, 201)
        user_row = self.fake_db.tables["usuarios"][0]
        self.assertEqual(user_row["nome"], "Cliente Verificado")
        self.assertTrue(check_password_hash(user_row["senha"], "OrigPass!"))
        self.assertTrue(user_row["email_verificado"])
        self.assertEqual(len(self.sent_emails), 0)

        # Validation and resend on non-existent or verified accounts must never return 404 or 409
        val_nonexistent = self.client.post(
            "/api/validar-codigo",
            json={"email": "inexistente@example.test", "codigo": "123456"},
            headers=self._headers(),
        )
        self.assertEqual(val_nonexistent.status_code, 400)

        resend_nonexistent = self.client.post(
            "/api/reenviar-codigo",
            json={"email": "inexistente@example.test"},
            headers=self._headers(),
        )
        self.assertEqual(resend_nonexistent.status_code, 200)

        resend_verified = self.client.post(
            "/api/reenviar-codigo",
            json={"email": "verificado@example.test"},
            headers=self._headers(),
        )
        self.assertEqual(resend_verified.status_code, 200)

    def test_v1_otp_expiration_and_max_attempts(self):
        self.client.post(
            "/cadastrar",
            json={"nome": "Teste OTP", "email": "otp@example.test", "senha": "Password!"},
            headers=self._headers(),
        )
        otp_code = self.sent_emails[-1]["codigo"]

        # Wrong attempts up to OTP_MAX_ATTEMPTS invalidate the OTP
        for _ in range(ci.OTP_MAX_ATTEMPTS):
            r = self.client.post(
                "/api/validar-codigo",
                json={"email": "otp@example.test", "codigo": "000000"},
                headers=self._headers(),
            )
            self.assertEqual(r.status_code, 400)

        # Even the right OTP is now rejected because the code was invalidated in DB
        self.assertIsNone(self.fake_db.tables["usuarios"][0]["codigo_otp"])
        ci.account_rate_limiter.clear()
        r_after = self.client.post(
            "/api/validar-codigo",
            json={"email": "otp@example.test", "codigo": otp_code},
            headers=self._headers(),
        )
        self.assertEqual(r_after.status_code, 400)

        # Resend issues a fresh OTP; test expiration
        self.client.post(
            "/api/reenviar-codigo",
            json={"email": "otp@example.test"},
            headers=self._headers(),
        )
        fresh_otp = self.sent_emails[-1]["codigo"]
        self.fake_db.tables["usuarios"][0]["codigo_expira_em"] = (
            datetime.now(timezone.utc) - timedelta(minutes=1)
        ).isoformat()
        exp_resp = self.client.post(
            "/api/validar-codigo",
            json={"email": "otp@example.test", "codigo": fresh_otp},
            headers=self._headers(),
        )
        self.assertEqual(exp_resp.status_code, 400)

    # =========================================================================
    # VULNERABILIDADE 2: Ausência de TTL persistente na recuperação de senha
    # =========================================================================
    def test_v2_password_recovery_persistent_ttl_invalidation_and_attempts(self):
        self.fake_db.tables["usuarios"].append({
            "id": 2,
            "nome": "Maria",
            "email": "maria@example.test",
            "senha": generate_password_hash("OldPass!", method="pbkdf2:sha256"),
            "email_verificado": True,
            "codigo_otp": None,
            "codigo_expira_em": None,
            "token_recuperacao": None,
            "token_recuperacao_expira_em": None,
            "token_recuperacao_tentativas": 0,
            "sessao_versao": 1,
        })

        # 1. Request recovery token
        r1 = self.client.post(
            "/api/esqueci-senha",
            json={"email": "maria@example.test"},
            headers=self._headers(),
        )
        self.assertEqual(r1.status_code, 200)
        token1 = r1.get_json()["recuperacao_token"]
        code1 = self.sent_emails[-1]["codigo"]

        user = self.fake_db.tables["usuarios"][0]
        self.assertTrue(str(user["token_recuperacao"]).startswith("v2:"))
        self.assertNotIn(code1, str(user["token_recuperacao"]))
        self.assertIsNotNone(user["token_recuperacao_expira_em"])

        # 2. Requesting a second code invalidates the first code and signed token
        r2 = self.client.post(
            "/api/esqueci-senha",
            json={"email": "maria@example.test"},
            headers=self._headers(),
        )
        self.assertEqual(r2.status_code, 200)
        token2 = r2.get_json()["recuperacao_token"]
        code2 = self.sent_emails[-1]["codigo"]

        old_try = self.client.post(
            "/api/redefinir-senha",
            json={
                "email": "maria@example.test",
                "codigo": code1,
                "nova_senha": "NewPass!",
                "recuperacao_token": token1,
            },
            headers=self._headers(),
        )
        self.assertEqual(old_try.status_code, 400)

        # 3. Persistent DB expiration check (even when signed token is still fresh)
        self.fake_db.tables["usuarios"][0]["token_recuperacao_expira_em"] = (
            datetime.now(timezone.utc) - timedelta(seconds=10)
        ).isoformat()
        exp_db_try = self.client.post(
            "/api/redefinir-senha",
            json={
                "email": "maria@example.test",
                "codigo": code2,
                "nova_senha": "NewPass!",
                "recuperacao_token": token2,
            },
            headers=self._headers(),
        )
        self.assertEqual(exp_db_try.status_code, 400)
        self.assertIsNone(self.fake_db.tables["usuarios"][0]["token_recuperacao"])

        # 4. Issue a third token and test valid password reset + single-use enforcement
        r3 = self.client.post(
            "/api/esqueci-senha",
            json={"email": "maria@example.test"},
            headers=self._headers(),
        )
        token3 = r3.get_json()["recuperacao_token"]
        code3 = self.sent_emails[-1]["codigo"]

        ok_reset = self.client.post(
            "/api/redefinir-senha",
            json={
                "email": "maria@example.test",
                "codigo": code3,
                "nova_senha": "NewPass!",
                "recuperacao_token": token3,
            },
            headers=self._headers(),
        )
        self.assertEqual(ok_reset.status_code, 200)
        user_after = self.fake_db.tables["usuarios"][0]
        self.assertIsNone(user_after["token_recuperacao"])
        self.assertIsNone(user_after["token_recuperacao_expira_em"])
        self.assertEqual(user_after["token_recuperacao_tentativas"], 0)
        self.assertTrue(check_password_hash(user_after["senha"], "NewPass!"))

        # Reusing the same token/code fails
        reuse_reset = self.client.post(
            "/api/redefinir-senha",
            json={
                "email": "maria@example.test",
                "codigo": code3,
                "nova_senha": "NextPass!",
                "recuperacao_token": token3,
            },
            headers=self._headers(),
        )
        self.assertEqual(reuse_reset.status_code, 400)

    def test_v2_recovery_excessive_attempts_invalidates_token(self):
        self.fake_db.tables["usuarios"].append({
            "id": 3,
            "nome": "Joao",
            "email": "joao@example.test",
            "senha": generate_password_hash("OldPass!", method="pbkdf2:sha256"),
            "email_verificado": True,
            "token_recuperacao": None,
            "token_recuperacao_expira_em": None,
            "token_recuperacao_tentativas": 0,
            "sessao_versao": 1,
        })
        r = self.client.post(
            "/api/esqueci-senha",
            json={"email": "joao@example.test"},
            headers=self._headers(),
        )
        token = r.get_json()["recuperacao_token"]
        code = self.sent_emails[-1]["codigo"]

        for _ in range(ci.RECOVERY_MAX_ATTEMPTS):
            bad = self.client.post(
                "/api/redefinir-senha",
                json={
                    "email": "joao@example.test",
                    "codigo": "000000",
                    "nova_senha": "NewPass!",
                    "recuperacao_token": token,
                },
                headers=self._headers(),
            )
            self.assertEqual(bad.status_code, 400)

        # Token must now be cleared in DB; even the valid code must fail
        self.assertIsNone(self.fake_db.tables["usuarios"][0]["token_recuperacao"])
        ci.account_rate_limiter.clear()
        after = self.client.post(
            "/api/redefinir-senha",
            json={
                "email": "joao@example.test",
                "codigo": code,
                "nova_senha": "NewPass!",
                "recuperacao_token": token,
            },
            headers=self._headers(),
        )
        self.assertEqual(after.status_code, 400)

    # =========================================================================
    # VULNERABILIDADE 3: DoS por bloqueio global do mutation_lock
    # =========================================================================
    def test_v3_slow_external_email_does_not_block_schedule_or_other_accounts(self):
        slow_started = threading.Event()
        self.fake_db.tables["usuarios"].append({
            "id": 50,
            "nome": "Cliente Agenda",
            "email": "agenda@example.test",
            "senha": generate_password_hash("StrongP!", method="pbkdf2:sha256"),
            "email_verificado": True,
            "pontos": 0,
            "sessao_versao": 1,
        })
        booking_client = ci.app.test_client()
        login_r = booking_client.post(
            "/api/login",
            json={"email": "agenda@example.test", "senha": "StrongP!"},
            headers=self._headers(ip="10.1.0.2"),
        )
        csrf_tok = login_r.get_json()["csrf_token"]
        future_date = (datetime.now(ci.SPA_TIMEZONE) + timedelta(days=6)).replace(
            hour=15, minute=0, second=0, microsecond=0
        )
        if future_date.weekday() == 0:
            future_date += timedelta(days=1)
        slot_str = future_date.strftime("%Y-%m-%dT%H:%M")

        def slow_email(destinatario, assunto, conteudo_html):
            if destinatario == "lento@example.test":
                slow_started.set()
                time.sleep(0.50)
            return True

        with patch.object(ci, "enviar_email_transacional", side_effect=slow_email):
            with ThreadPoolExecutor(max_workers=2) as pool:
                fut_slow = pool.submit(
                    lambda: ci.app.test_client().post(
                        "/api/esqueci-senha",
                        json={"email": "agenda@example.test"},
                        headers=self._headers(ip="10.1.0.1"),
                    )
                )
                # Also trigger slow_email on /cadastrar
                fut_slow_reg = pool.submit(
                    lambda: ci.app.test_client().post(
                        "/cadastrar",
                        json={"nome": "Lento", "email": "lento@example.test", "senha": "Password!"},
                        headers=self._headers(ip="10.1.0.3"),
                    )
                )
                self.assertTrue(slow_started.wait(timeout=3.0))
                t_fast_start = time.monotonic()
                resp_fast = booking_client.post(
                    "/api/agendar",
                    json={"servico_id": 1, "data": slot_str},
                    headers=self._headers(csrf_token=csrf_tok, ip="10.1.0.2"),
                )
                fast_elapsed = time.monotonic() - t_fast_start
                resp_slow = fut_slow_reg.result(timeout=3.0)
                fut_slow.result(timeout=3.0)

            self.assertEqual(resp_fast.status_code, 201)
            self.assertEqual(resp_slow.status_code, 201)
            self.assertLess(fast_elapsed, 0.25)

    # =========================================================================
    # VULNERABILIDADE 4: Esgotamento de memória no RateLimiter
    # =========================================================================
    def test_v4_rate_limiter_lru_eviction_and_expiration_preserve_legitimate_users(self):
        limiter = RateLimiter(max_keys=50)

        for _ in range(5):
            ok, _ = limiter.allow("ip:attacker", limit=5, window_seconds=60)
            self.assertTrue(ok)
        blocked, _ = limiter.allow("ip:attacker", limit=5, window_seconds=60)
        self.assertFalse(blocked)

        for i in range(500):
            allowed, _ = limiter.allow(f"acct:fake-{i}", limit=5, window_seconds=60)
            self.assertTrue(allowed)

        self.assertLessEqual(len(limiter._buckets), 50)

        still_blocked, _ = limiter.allow("ip:attacker", limit=5, window_seconds=60)
        self.assertFalse(still_blocked)

        legit_ok, _ = limiter.allow("ip:legitimate_user", limit=5, window_seconds=60)
        self.assertTrue(legit_ok)

    # =========================================================================
    # VULNERABILIDADE 5: Restrição de SPA_FRONTEND_FILE
    # =========================================================================
    def test_v5_spa_frontend_file_allowlist_enforcement(self):
        for forbidden in (
            "cadastro_interface.py",
            "../database.env",
            "database/spa_security.py",
            "nao_existe.html",
            "config.json",
        ):
            os.environ["SPA_FRONTEND_FILE"] = forbidden
            resp = self.client.get("/")
            self.assertEqual(resp.status_code, 403, f"Expected 403 for {forbidden}")
            self.assertEqual(resp.get_json()["code"], "invalid_frontend_file")

        for allowed in ("servicos.html", "servicos.min.html"):
            os.environ["SPA_FRONTEND_FILE"] = allowed
            resp = self.client.get("/")
            self.assertEqual(resp.status_code, 200, f"Expected 200 for {allowed}")

    # =========================================================================
    # VULNERABILIDADE 6: Invalidação de sessão no logout e proteção CSRF
    # =========================================================================
    def test_v6_server_side_session_revocation_on_logout_and_csrf_handling(self):
        self.fake_db.tables["usuarios"].append({
            "id": 10,
            "nome": "Cliente Sessao",
            "email": "sessao@example.test",
            "senha": generate_password_hash("StrongP!", method="pbkdf2:sha256"),
            "email_verificado": True,
            "pontos": 20,
            "sessao_versao": 1,
        })

        login_resp = self.client.post(
            "/api/login",
            json={"email": "sessao@example.test", "senha": "StrongP!"},
            headers=self._headers(),
        )
        self.assertEqual(login_resp.status_code, 200)

        cookie_obj = self.client.get_cookie(ci.app.config["SESSION_COOKIE_NAME"])
        self.assertIsNotNone(cookie_obj)
        stolen_cookie_value = cookie_obj.value

        future_date = (datetime.now(ci.SPA_TIMEZONE) + timedelta(days=5)).replace(
            hour=14, minute=0, second=0, microsecond=0
        )
        if future_date.weekday() == 0:
            future_date += timedelta(days=1)
        no_csrf_resp = self.client.post(
            "/api/agendar",
            json={"servico_id": 1, "data": future_date.strftime("%Y-%m-%dT%H:%M")},
            headers=self._headers(csrf_token=None),
        )
        self.assertEqual(no_csrf_resp.status_code, 403)

        # Logout WITHOUT CSRF token (desynchronized token) still succeeds when Origin is valid
        logout_resp = self.client.post(
            "/api/logout",
            json={},
            headers=self._headers(csrf_token=None),
        )
        self.assertEqual(logout_resp.status_code, 200)

        # Replaying the stolen signed session cookie must now be rejected by the server (401)
        attacker_client = ci.app.test_client()
        attacker_client.set_cookie(
            ci.app.config["SESSION_COOKIE_NAME"],
            stolen_cookie_value,
            domain="localhost",
        )
        replay_me = attacker_client.get("/api/me", headers=self._headers())
        self.assertEqual(replay_me.status_code, 401)

        replay_points = attacker_client.get("/api/usuario/pontos", headers=self._headers())
        self.assertEqual(replay_points.status_code, 401)

    def test_v6_global_session_revocation_invalidates_other_devices(self):
        self.fake_db.tables["usuarios"].append({
            "id": 11,
            "nome": "Multi Device",
            "email": "multi@example.test",
            "senha": generate_password_hash("StrongP!", method="pbkdf2:sha256"),
            "email_verificado": True,
            "pontos": 10,
            "sessao_versao": 1,
        })

        client_a = ci.app.test_client()
        client_b = ci.app.test_client()

        r_a = client_a.post(
            "/api/login",
            json={"email": "multi@example.test", "senha": "StrongP!"},
            headers=self._headers(ip="10.2.0.1"),
        )
        r_b = client_b.post(
            "/api/login",
            json={"email": "multi@example.test", "senha": "StrongP!"},
            headers=self._headers(ip="10.2.0.2"),
        )
        self.assertEqual(r_a.status_code, 200)
        self.assertEqual(r_b.status_code, 200)

        logout_all = client_a.post(
            "/api/logout",
            json={"revogar_todas": True},
            headers=self._headers(ip="10.2.0.1"),
        )
        self.assertEqual(logout_all.status_code, 200)

        b_points = client_b.get("/api/usuario/pontos", headers=self._headers(ip="10.2.0.2"))
        self.assertEqual(b_points.status_code, 401)

    # =========================================================================
    # VULNERABILIDADE 7: Content Security Policy com nonce e sem unsafe-inline
    # =========================================================================
    def test_v7_csp_header_uses_per_request_nonce_without_unsafe_inline_script(self):
        r1 = self.client.get("/")
        r2 = self.client.get("/")
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)

        csp1 = r1.headers.get("Content-Security-Policy", "")
        csp2 = r2.headers.get("Content-Security-Policy", "")

        m1 = re.search(r"script-src ([^;]+);", csp1)
        m2 = re.search(r"script-src ([^;]+);", csp2)
        self.assertIsNotNone(m1)
        self.assertIsNotNone(m2)
        script_src_1 = m1.group(1)
        script_src_2 = m2.group(1)

        self.assertNotIn("'unsafe-inline'", script_src_1)
        self.assertIn("script-src-attr 'none'", csp1)

        n1 = re.search(r"'nonce-([^']+)'", script_src_1).group(1)
        n2 = re.search(r"'nonce-([^']+)'", script_src_2).group(1)
        self.assertNotEqual(n1, n2)
        self.assertGreaterEqual(len(n1), 20)

        html1 = r1.data.decode("utf-8")
        self.assertNotIn("__CSP_NONCE__", html1)
        self.assertTrue(
            f'nonce="{n1}"' in html1 or f"nonce={n1}" in html1,
            "Expected CSP nonce attribute in rendered HTML script tags",
        )

        for fname in ("servicos.html", "servicos.min.html"):
            content = (Path(ci.BASE_DIR) / fname).read_text(encoding="utf-8")
            raw_handlers = re.findall(r'\s(on(?:click|submit|input|change|keypress))=', content)
            self.assertEqual(raw_handlers, [], f"Found raw inline handlers in {fname}: {raw_handlers}")

    # =========================================================================
    # VULNERABILIDADE 8: Excesso de threads não gerenciadas
    # =========================================================================
    def test_v8_bounded_executor_and_waitlist_batch_resilience(self):
        slot = "2026-12-10T14:00"
        for i in range(6):
            self.fake_db.tables["agenda_lista_espera"].append({
                "id": i + 1,
                "email_cliente": f"espera{i}@example.test",
                "servico_id": 1,
                "data_atendimento": slot,
                "status": "Aguardando",
                "criado_em": f"2026-11-01T10:0{i}:00",
                "notificado_em": None,
            })

        sent_to = []
        done_event = threading.Event()

        def mock_waitlist_email(destinatario, assunto, conteudo_html):
            if destinatario == "espera2@example.test":
                return False
            sent_to.append(destinatario)
            if len(sent_to) == 5:
                done_event.set()
            return True

        with patch.object(ci, "enviar_email_transacional", side_effect=mock_waitlist_email):
            self.fake_db.query_counts.clear()
            notified_count = ci.notificar_lista_espera_horario(slot)
            self.assertEqual(notified_count, 6)
            self.assertTrue(done_event.wait(timeout=3.0))

            deadline = time.monotonic() + 2.0
            while ci.email_executor.in_flight > 0 and time.monotonic() < deadline:
                time.sleep(0.01)

            self.assertEqual(self.fake_db.query_counts.get("servico", 0), 1)
            self.assertEqual(len(sent_to), 5)

            # Recipient 2 failed delivery so its status reverted to "Aguardando"; the other 5 are "Notificado"
            statuses = {r["email_cliente"]: r["status"] for r in self.fake_db.tables["agenda_lista_espera"]}
            self.assertEqual(statuses["espera2@example.test"], "Aguardando")
            self.assertEqual(statuses["espera0@example.test"], "Notificado")

    def test_v8_bounded_executor_queue_overflow_reverts_waitlist_status(self):
        slot = "2026-12-11T15:00"
        self.fake_db.tables["agenda_lista_espera"].append({
            "id": 99,
            "email_cliente": "fila_cheia@example.test",
            "servico_id": 1,
            "data_atendimento": slot,
            "status": "Aguardando",
            "criado_em": "2026-11-01T10:00:00",
            "notificado_em": None,
        })

        with patch.object(ci.email_executor, "submit", return_value=False):
            count = ci.notificar_lista_espera_horario(slot)
            self.assertEqual(count, 0)
            row = self.fake_db.tables["agenda_lista_espera"][0]
            self.assertEqual(row["status"], "Aguardando")
            self.assertIsNone(row["notificado_em"])

    # =========================================================================
    # TELEGRAM NOTIFICATIONS: Local & ZimaOS/Docker Production Reliability
    # =========================================================================
    def test_telegram_env_sanitization_and_production_delivery(self):
        fake_token = "123456789:ABCDEF_secret_token_xyz"
        fake_chat = "-100987654321"
        captured_requests = []

        def fake_post(url, json=None, timeout=None):
            captured_requests.append({"url": url, "json": json, "timeout": timeout})
            return types.SimpleNamespace(
                ok=True,
                status_code=200,
                content=b'{"ok":true,"result":{"message_id":777}}',
                json=lambda: {"ok": True, "result": {"message_id": 777}},
            )

        # Simulate Docker --env-file on ZimaOS with quotes, CRLF (\r\n), and redundant "bot" prefix
        with patch.dict(
            os.environ,
            {
                "TELEGRAM_BOT_TOKEN": f'  "bot{fake_token}\r\n" ',
                "TELEGRAM_CHAT_ID": f" '{fake_chat}\r\n' ",
            },
            clear=False,
        ):
            token, chat_id = ci.obter_config_telegram()
            self.assertEqual(token, fake_token)
            self.assertEqual(chat_id, fake_chat)

        with patch.dict(
            os.environ,
            {
                "TELEGRAM_BOT_TOKEN": f'"{fake_token}\r\n"',
                "TELEGRAM_CHAT_ID": f"'{fake_chat}\r\n'",
            },
            clear=False,
        ), patch.object(ci.requests, "post", side_effect=fake_post):
            ci.telegram_limiter.clear()
            with self.assertLogs("spa.telegram", level="INFO") as cm:
                ok = ci.alertar_servidor_telegram(
                    "SISTEMA",
                    "Teste Produção ZimaOS",
                    {"Ambiente": "Produção"},
                    chave_cooldown="test_zima_prod",
                    sincrono=True,
                )
            self.assertTrue(ok)
            self.assertEqual(len(captured_requests), 1)
            self.assertEqual(
                captured_requests[0]["url"],
                f"https://api.telegram.org/bot{fake_token}/sendMessage",
            )
            self.assertEqual(captured_requests[0]["json"]["chat_id"], fake_chat)
            joined_logs = "\n".join(cm.output)
            self.assertIn("message_id=777", joined_logs)
            self.assertNotIn(fake_token, joined_logs)

    def test_telegram_error_logging_redacts_token_and_avoids_recursion(self):
        fake_token = "999888777:SECRET_BOT_KEY_DO_NOT_LEAK"
        fake_chat = "1234567890"
        calls = []

        def failing_post(url, json=None, timeout=None):
            calls.append(url)
            raise ci.requests.ConnectionError(f"Failed to connect to {url}")

        with patch.dict(
            os.environ,
            {"TELEGRAM_BOT_TOKEN": fake_token, "TELEGRAM_CHAT_ID": fake_chat},
            clear=False,
        ), patch.object(ci.requests, "post", side_effect=failing_post):
            with self.assertLogs("spa.telegram", level="WARNING") as cm:
                ok = ci.enviar_alerta_telegram("<b>Teste falha</b>")
            self.assertFalse(ok)
            # Retried once (2 attempts total)
            self.assertEqual(len(calls), 2)
            joined_logs = "\n".join(cm.output)
            self.assertIn("Erro de conexão ao enviar alerta ao Telegram", joined_logs)
            self.assertIn("<REDACTED_TOKEN>", joined_logs)
            self.assertNotIn(fake_token, joined_logs)

    def test_telegram_executor_fork_safety_and_boot_alert_in_production(self):
        fake_token = "111222333:AAABBBCCC_test_token"
        fake_chat = "555666777"
        delivered = threading.Event()

        def fake_post(url, json=None, timeout=None):
            delivered.set()
            return types.SimpleNamespace(
                ok=True,
                status_code=200,
                content=b'{"ok":true,"result":{"message_id":101}}',
                json=lambda: {"ok": True, "result": {"message_id": 101}},
            )

        # Simulate a Gunicorn fork by setting _pid to a different parent PID
        ci.notificacao_executor._pid = -999
        ci.telegram_limiter.clear()

        with patch.dict(
            os.environ,
            {"TELEGRAM_BOT_TOKEN": fake_token, "TELEGRAM_CHAT_ID": fake_chat},
            clear=False,
        ), patch.object(ci, "APP_ENVIRONMENT", "production"), patch.object(
            ci, "IS_PRODUCTION", True
        ), patch.dict(ci.app.config, {"TESTING": False}), patch.object(
            ci.requests, "post", side_effect=fake_post
        ):
            scheduled = ci.diagnosticar_e_notificar_boot_telegram()
            self.assertTrue(scheduled)
            self.assertTrue(delivered.wait(timeout=2.0))
            self.assertEqual(ci.notificacao_executor._pid, os.getpid())


if __name__ == "__main__":
    unittest.main()
