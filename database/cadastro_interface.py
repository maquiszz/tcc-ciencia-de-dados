import os
from flask import Flask, request, jsonify, send_from_directory
from dotenv import load_dotenv
from supabase import create_client, Client

app = Flask(__name__)

# 📌 Carrega o arquivo de configuração diretamente da raiz do projeto
load_dotenv(dotenv_path="database.env")

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


@app.route('/api/agendar', methods=['POST'])
def criar_agendamento():
    dados = request.json
    email = dados.get('email')
    servico_id = dados.get('servico_id')
    data_atendimento = dados.get('data')

    if not email or not servico_id or not data_atendimento:
        return jsonify({"error": "Todos os campos são obrigatórios."}), 400

    try:
        # 🔍 1. VALIDAÇÃO: Verifica se o e-mail existe na tabela 'usuarios'
        usuario_existe = supabase.table("usuarios").select("email").eq("email", email).execute()
        
        # Se a lista de dados retornar vazia, significa que o e-mail não está cadastrado
        if not usuario_existe.data:
            return jsonify({
                "error": "Usuário Inexistente. Por favor, faça o seu cadastro na aba 'Cadastro' antes de agendar um atendimento."
            }), 404

        # 📅 2. Se o usuário existir, prossegue com o agendamento normal
        supabase.table("agendamentos").insert({
            "email_cliente": email,
            "servico_id": servico_id,
            "data_atendimento": data_atendimento
        }).execute()

        # 📊 3. Busca a quantidade atual de contratos do serviço para atualizar
        servico_atual = supabase.table("servico").select("contratos").eq("id", servico_id).single().execute()
        contratos_atuais = servico_atual.data.get('contratos', 0) if servico_atual.data else 0
        
        # 📈 4. Atualiza a tabela 'servico' somando +1 contrato
        supabase.table("servico").update({"contratos": contratos_atuais + 1}).eq("id", servico_id).execute()

        print(f"🎉 Novo agendamento realizado por {email} para o serviço ID {servico_id}")
        return jsonify({"message": "Agendamento realizado com sucesso!"}), 201

    except Exception as e:
        print(f"❌ Erro ao processar agendamento: {e}")
        return jsonify({"error": "Erro ao salvar o agendamento no banco de dados."}), 500
# ==============================================================================
# --- 🚀 INICIALIZAÇÃO DO SERVIDOR ---
# ==============================================================================
if __name__ == '__main__':
    print("\n" + "="*50)
    print("🚀 SERVIDOR ZENITH SPA INICIADO COM SUCESSO!")
    print("="*50)
    print("👉 Página Inicial (Catálogo): http://127.0.0.1:5000")
    print("👉 Página Secundária (Cadastro): http://127.0.0.1:5000/cadastro")
    print("="*50 + "\n")
    
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)