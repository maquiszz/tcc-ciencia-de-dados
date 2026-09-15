import os
from contextlib import closing

import pandas as pd
import psycopg2
import tkinter as tk
from dotenv import load_dotenv
from psycopg2.extras import execute_values
from tkinter import filedialog


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
        application_name="panaceia-importacao-csv",
    )


def selecionar_arquivo_csv():
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    caminho = filedialog.askopenfilename(
        title="Selecione o arquivo CSV de clientes",
        filetypes=[("Arquivos CSV", "*.csv")],
    )
    root.destroy()
    return caminho


def cadastrar_via_csv(caminho_csv):
    try:
        df = pd.read_csv(caminho_csv)
        if not {"nome", "email"}.issubset(df.columns):
            print("Erro: o CSV deve ter as colunas 'nome' e 'email'.")
            return False
        registros = list(df[["nome", "email"]].itertuples(index=False, name=None))
        if not registros:
            print("O CSV não contém registros para importar.")
            return True
        with closing(_conectar()) as conn:
            with conn, conn.cursor() as cur:
                execute_values(
                    cur,
                    "INSERT INTO public.usuarios (nome, email) VALUES %s",
                    registros,
                    page_size=500,
                )
        print(f"Sucesso! {len(registros)} clientes cadastrados.")
        return True
    except Exception as exc:
        print(f"Erro na importação: {exc}")
        return False


if __name__ == "__main__":
    raiz = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    load_dotenv(os.path.join(raiz, "database.env"))
    caminho = selecionar_arquivo_csv()
    if caminho:
        cadastrar_via_csv(caminho)
