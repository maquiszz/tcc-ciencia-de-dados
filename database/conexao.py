import os
from contextlib import closing

import psycopg2
from dotenv import load_dotenv


def _conectar():
    senha = os.getenv("DB_PASSWORD")
    if not senha:
        raise RuntimeError("DB_PASSWORD não configurada.")
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "postgres"),
        port=int(os.getenv("DB_PORT", "5432")),
        dbname=os.getenv("DB_NAME", "site_db"),
        user=os.getenv("DB_USER", "site_user"),
        password=senha,
        connect_timeout=10,
        application_name="panaceia-cadastro",
    )


def cadastrar_usuario(nome: str, email: str):
    try:
        with closing(_conectar()) as conn:
            with conn, conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO public.usuarios (nome, email) VALUES (%s, %s) "
                    "RETURNING id, nome, email",
                    (nome, email),
                )
                row = cur.fetchone()
        print(f"Usuário {nome} cadastrado com sucesso!")
        return {"id": row[0], "nome": row[1], "email": row[2]}
    except Exception as exc:
        print(f"Erro ao cadastrar usuário: {exc}")
        return None


def menu_cadastro():
    print("--- Sistema de Cadastro ---")
    while True:
        nome = input("\nDigite o nome do usuário: ").strip()
        email = input("Digite o email do usuário: ").strip()
        if nome and email:
            cadastrar_usuario(nome, email)
        else:
            print("Nome e email não podem estar vazios!")
        if input("\nDeseja realizar um novo cadastro? (s/n): ").lower().strip() != "s":
            print("Encerrando o sistema. Até logo!")
            break


if __name__ == "__main__":
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), "database.env"))
    menu_cadastro()
