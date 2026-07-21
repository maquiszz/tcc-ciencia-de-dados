import os
import pandas as pd
import tkinter as tk
from tkinter import filedialog
from dotenv import load_dotenv
from supabase import create_client, Client

# --- CONFIGURAÇÃO AUTOMÁTICA ---
NOME_ARQUIVO_ENV = "database.env"

def carregar_configuracoes():
    # Localiza a pasta onde o script está
    diretorio_atual = os.path.dirname(os.path.abspath(__file__))
    # Sobe para a pasta raiz onde o .env deve estar
    pasta_raiz = os.path.dirname(diretorio_atual)
    
    caminho_env = os.path.join(pasta_raiz, NOME_ARQUIVO_ENV)

    if os.path.exists(caminho_env):
        # O override=True garante que ele atualize as variáveis se você mudou o arquivo
        load_dotenv(dotenv_path=caminho_env, override=True)
        print(f"✅ Configurações carregadas de: {caminho_env}")
        return True
    else:
        print(f"❌ Erro: Arquivo '{NOME_ARQUIVO_ENV}' não encontrado em: {pasta_raiz}")
        return False

def selecionar_arquivo_csv():
    root = tk.Tk()
    root.withdraw()
    root.attributes('-topmost', True)
    caminho = filedialog.askopenfilename(
        title="Selecione o arquivo CSV de clientes", 
        filetypes=[("Arquivos CSV", "*.csv")]
    )
    root.destroy()
    return caminho

def cadastrar_via_csv(caminho_csv, supabase_client):
    try:
        df = pd.read_csv(caminho_csv)
        
        if 'nome' not in df.columns or 'email' not in df.columns:
            print("❌ Erro: O CSV deve ter as colunas 'nome' e 'email'.")
            return

        # Bulk Insert: transformando DataFrame em lista de dicionários
        lista_usuarios = df[['nome', 'email']].to_dict('records')
        total = len(lista_usuarios)

        print(f"\n⏳ Enviando {total} registros para o Supabase...")
        
        # Inserção única (mais eficiente e ignora RLS com a service_role)
        supabase_client.table("usuarios").insert(lista_usuarios).execute()
        
        print(f"🎉 Sucesso! {total} clientes cadastrados.")

    except Exception as e:
        print(f"❌ Erro na importação: {e}")

if __name__ == "__main__":
    if not carregar_configuracoes():
        exit()

    # Buscando os nomes conforme você configurou no .env
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("service_role")  # Nome atualizado conforme seu pedido

    if not url or not key:
        print(f"❌ Erro: Verifique se SUPABASE_URL e service_role estão preenchidos no {NOME_ARQUIVO_ENV}")
        exit()

    try:
        supabase: Client = create_client(url, key)
        caminho_csv = selecionar_arquivo_csv()
        if caminho_csv:
            cadastrar_via_csv(caminho_csv, supabase)
    except Exception as e:
        print(f"❌ Falha no cliente Supabase: {e}")