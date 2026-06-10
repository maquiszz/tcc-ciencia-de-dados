import os
from flask import Flask, request, jsonify, send_from_directory
from dotenv import load_dotenv
from supabase import create_client, Client

# Inicializa o servidor web Flask
app = Flask(__name__)

# --- CARREGAMENTO DO BACKEND (SUPABASE) ---
load_dotenv(dotenv_path="database.env")

url = os.environ.get("SUPABASE_URL")
# Usa a service_role (ou SUPABASE_KEY com RLS liberado) para evitar erro 42501
key = os.environ.get("service_role")
if not url or not key:
    print("❌ ERRO: Verifique se as credenciais estão corretas no database.env!")
    exit()

supabase: Client = create_client(url, key)

# --- ROTA 1: Serve a página visual (HTML/CSS) ---
@app.route('/')
def pagina_principal():
    # Busca o index.html na mesma pasta do script
    return send_from_directory('.', 'index.html')

# --- ROTA 2: Recebe os dados do formulário e envia ao Supabase ---
@app.route('/cadastrar', methods=['POST'])
def cadastrar():
    dados_recebidos = request.json
    nome = dados_recebidos.get('nome')
    email = dados_recebidos.get('email')
    servico = dados_recebidos.get('servico')

    if not nome or not email:
        return jsonify({"error": "Nome e e-mail são campos obrigatórios."}), 400

    # Estrutura para salvar no banco
    dados_usuario = {
        "nome": nome,
        "email": email
        # Se sua tabela no Supabase aceitar o campo de serviço, descomente a linha abaixo:
        # "servico": servico
    }

    try:
        # Executa o insert no Supabase
        supabase.table("usuarios").insert(dados_usuario).execute()
        return jsonify({"message": "Usuário cadastrado com sucesso!"}), 201
    except Exception as e:
        print(f"Erro no banco: {e}")
        return jsonify({"error": "Erro ao salvar no banco de dados. Verifique a política RLS."}), 500

# Inicializa o servidor local
if __name__ == '__main__':
    print("🚀 Servidor do Spa iniciado!")
    print("👉 Acesse no seu navegador: http://127.0.0.1:5000")
    app.run(debug=True, port=5000)
    