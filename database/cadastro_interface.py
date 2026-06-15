import os
from flask import Flask, request, jsonify, send_from_directory
from dotenv import load_dotenv
from supabase import create_client, Client

app = Flask(__name__)

# ==============================================================================
# --- 🔑 CONFIGURAÇÃO FIXA DO BANCO VIA TCC-CIENCIA-DE-DADOS ---
# ==============================================================================

# Pega o caminho da pasta 'database' (onde este script está)
diretorio_do_script = os.path.dirname(os.path.abspath(__file__))

# Sobe um nível para chegar na pasta principal 'tcc-ciencia-de-dados'
raiz_do_projeto = os.path.dirname(diretorio_do_script)

# Aponta direto para o arquivo database.env na raiz correta
caminho_env = os.path.join(raiz_do_projeto, "database.env")

if not os.path.exists(caminho_env):
    print("\n❌ ERRO CRÍTICO: Arquivo 'database.env' não existe na raiz do projeto!")
    print(f"👉 Caminho procurado: {caminho_env}")
    exit()

# Carrega o arquivo correto
load_dotenv(dotenv_path=caminho_env)

url = os.environ.get("SUPABASE_URL")
key = os.environ.get("service_role") or os.environ.get("SUPABASE_KEY")

# Validação das credenciais antes de iniciar o servidor
if not url or not key:
    print("❌ ERRO CRÍTICO: Credenciais ausentes no database.env!")
    exit()

# Cria a conexão oficial com o Supabase
supabase: Client = create_client(url, key)


# ==============================================================================
# --- 🌍 ROTAS DE PÁGINAS VISUAIS (HTML) ---
# ==============================================================================

@app.route('/')
def pagina_principal():
    """A PÁGINA INICIAL DO LINK ENTREGA O CATÁLOGO (servicos.html)"""
    return send_from_directory('.', 'servicos.html')


@app.route('/cadastro')
def pagina_cadastro():
    """A PÁGINA DE CADASTRO FICA NO CAMINHO /cadastro (index.html)"""
    return send_from_directory('.', 'index.html')


# ==============================================================================
# --- ⚙️ ROTAS DE PROCESSAMENTO DE DADOS (API) ---
# ==============================================================================

@app.route('/cadastrar', methods=['POST'])
def cadastrar():
    """Recebe os dados do formulário e grava na tabela 'usuarios' do Supabase"""
    dados = request.json
    nome = dados.get('nome')
    email = dados.get('email')

    if not nome or not email:
        return jsonify({"error": "Nome e e-mail são obrigatórios."}), 400

    try:
        # Executa a inserção no Supabase
        supabase.table("usuarios").insert({"nome": nome, "email": email}).execute()
        print(f"✅ Usuário gravado com sucesso no Supabase: {nome}")
        return jsonify({"message": "Cadastrado com sucesso!"}), 201
        
    except Exception as e:
        print(f"❌ Erro interno ao salvar no banco: {e}")
        return jsonify({"error": "Não foi possível completar o cadastro no banco de dados."}), 500


@app.route('/api/servicos', methods=['GET'])
def listar_servicos():
    """Busca os dados da tabela 'servico' (no singular) do Supabase"""
    try:
        resposta = supabase.table("servico").select("*").execute()
        return jsonify(resposta.data), 200
    except Exception as e:
        print(f"❌ Erro ao buscar serviços no Supabase: {e}")
        return jsonify({"error": "Erro ao ler a tabela de serviços do banco de dados."}), 500


# ==============================================================================
# --- 🚀 INICIALIZAÇÃO DO SERVIDOR ---
# ==============================================================================
if __name__ == '__main__':
    print("🚀 Servidor do Spa iniciado na nuvem!")
    # O Render define a porta automaticamente através da variável 'PORT'
    port = int(os.environ.get('PORT', 5000))
    # host='0.0.0.0' permite que o servidor receba acessos externos
    app.run(host='0.0.0.0', port=port)