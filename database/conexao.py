import os
from supabase import create_client, Client
from dotenv import load_dotenv

# Carrega o arquivo procurando na raiz do projeto
load_dotenv(dotenv_path="database.env")

url: str = os.environ.get("SUPABASE_URL")
key: str = os.environ.get("SUPABASE_KEY")

if not url or not key:
    raise ValueError(
        "❌ ERRO: Não foi possível ler as credenciais do arquivo database.env. "
        "Verifique se o arquivo está na raiz do projeto e se os nomes estão corretos!"
    )

supabase: Client = create_client(url, key)

def cadastrar_usuario(nome: str, email: str):
    dados_usuario = {"nome": nome, "email": email}
    try:
        # Nota: Certifique-se de que a tabela no Supabase se chama 'usuarios'
        resposta = supabase.table("usuarios").insert(dados_usuario).execute()
        print(f"🎉 Usuário {nome} cadastrado com sucesso!")
        return resposta.data
    except Exception as e:
        print(f"❌ Erro ao cadastrar usuário: {e}")
        return None

# --- Nova parte: Interface de repetição no terminal ---

def menu_cadastro():
    print("--- 📝 Sistema de Cadastro Supabase ---")
    
    while True:
        nome_input = input("\nDigite o nome do usuário: ").strip()
        email_input = input("Digite o email do usuário: ").strip()

        if nome_input and email_input:
            cadastrar_usuario(nome_input, email_input)
        else:
            print("⚠️ Nome e email não podem estar vazios!")

        # Pergunta se o usuário deseja continuar
        continuar = input("\nDeseja realizar um novo cadastro? (s/n): ").lower().strip()
        
        if continuar != 's':
            print("\nEncerrando o sistema. Até logo! 👋")
            break

if __name__ == "__main__":
    menu_cadastro()