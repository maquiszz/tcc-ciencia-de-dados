import os
from supabase import create_client, Client
from dotenv import load_dotenv

# Carrega o arquivo procurando na raiz do projeto
load_dotenv(dotenv_path="database.env")

url: str = os.environ.get("SUPABASE_URL")
key: str = os.environ.get("SUPABASE_KEY")

# Esse IF serve para te avisar no terminal se ele falhar em ler as variáveis de novo
if not url or not key:
    raise ValueError(
        "❌ ERRO: Não foi possível ler as credenciais do arquivo database.env. "
        "Verifique se o arquivo está na raiz do projeto e se os nomes estão corretos!"
    )

# Inicializa o cliente se estiver tudo OK
supabase: Client = create_client(url, key)

def cadastrar_usuario(nome: str, email: str):
    dados_usuario = {"nome": nome, "email": email}
    try:
        resposta = supabase.table("usuarios").insert(dados_usuario).execute()
        print(f"🎉 Usuário {nome} cadastrado com sucesso!")
        return resposta.data
    except Exception as e:
        print(f"❌ Erro ao cadastrar usuário: {e}")
        return None