-- Migração 001: Hardening de Segurança (Recuperação de Senha, OTP e Sessões)
-- Compatível com PostgreSQL local e Supabase.
-- Operação idempotente e não destrutiva: preserva todos os registros existentes.

BEGIN;

DO $$
BEGIN
    IF to_regclass('public.usuarios') IS NOT NULL THEN
        ALTER TABLE public.usuarios
            ADD COLUMN IF NOT EXISTS token_recuperacao_expira_em TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS token_recuperacao_tentativas INTEGER NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS codigo_otp_tentativas INTEGER NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS sessao_versao INTEGER NOT NULL DEFAULT 1;
    END IF;
END
$$;

COMMIT;
