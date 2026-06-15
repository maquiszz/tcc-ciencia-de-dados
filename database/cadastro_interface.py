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
key = os.environ.get("SUPABASE_KEY")

if not url or not key:
    print("\n❌ ERRO CRÍTICO: O arquivo 'database.env' foi aberto na raiz, mas as variáveis estão vazias!")
    print("👉 Abra o arquivo no VS Code, preencha as chaves e lembre-se de salvar com Ctrl+S.")
    exit()

# Conecta ao Supabase
supabase: Client = create_client(url, key)


# ==============================================================================
# --- 🌍 ROTAS DE PÁGINAS VISUAIS (HTML) ---
# ==============================================================================

@app.route('/')
def pagina_principal():
    """Busca o servicos.html obrigatoriamente na pasta database"""
    return send_from_directory(diretorio_do_script, 'servicos.html')


@app.route('/cadastro')
def pagina_cadastro():
    """Busca o index.html obrigatoriamente na pasta database"""
    return send_from_directory(diretorio_do_script, 'index.html')


# ==============================================================================
# --- ⚙️ ROTAS DE PROCESSAMENTO DE DADOS (API) ---
# ==============================================================================

@app.route('/cadastrar', methods=['POST'])
def cadastrar():
    dados = request.json
    nome = dados.get('nome')
    email = dados.get('email')
    senha = dados.get('senha')

    if not nome or not email or not senha:
        return jsonify({"error": "Nome, e-mail e senha são obrigatórios."}), 400

    try:
        # Insere o usuário com nome, email e senha no Supabase
        supabase.table("usuarios").insert({"nome": nome, "email": email, "senha": senha}).execute()
        return jsonify({"message": "Cadastrado com sucesso!"}), 201
    except Exception as e:
        print(f"❌ Erro ao salvar no banco: {e}")
        return jsonify({"error": "Erro ao salvar usuário. O e-mail já pode estar em uso."}), 500


@app.route('/api/login', methods=['POST'])
def login():
    dados = request.json
    email = dados.get('email')
    senha = dados.get('senha')

    if not email or not senha:
        return jsonify({"error": "E-mail e senha são obrigatórios."}), 400

    try:
        # Busca o usuário pelo e-mail informado
        usuario = supabase.table("usuarios").select("*").eq("email", email).execute()
        
        if not usuario.data:
            return jsonify({"error": "E-mail não encontrado. Por favor, crie uma conta primeiro."}), 404
        
        user_db = usuario.data[0]
        
        # Validação de senha textual simples
        if user_db.get('senha') != senha:
            return jsonify({"error": "Senha incorreta. Tente novamente."}), 401
        
        return jsonify({"message": "Login realizado com sucesso!", "usuario": user_db}), 200
    except Exception as e:
        print(f"❌ Erro interno no login: {e}")
        return jsonify({"error": "Erro interno no servidor de autenticação."}), 500


@app.route('/api/servicos', methods=['GET'])
def listar_servicos():
    try:
        resposta = supabase.table("servico").select("*").execute()
        return jsonify(resposta.data), 200
    except Exception as e:
        return jsonify({"error": "Erro ao ler a tabela de serviços."}), 500


@app.route('/api/agendar', methods=['POST'])
def criar_agendamento():
    dados = request.json
    email = dados.get('email')
    servico_id = dados.get('servico_id')
    data_atendimento = dados.get('data')

    if not email or not servico_id or not data_atendimento:
        return jsonify({"error": "Todos os campos são obrigatórios."}), 400

    try:
        usuario_existe = supabase.table("usuarios").select("email").eq("email", email).execute()
        if not usuario_existe.data:
            return jsonify({"error": "Usuário Inexistente. Crie uma conta antes de agendar."}), 404

        supabase.table("agendamentos").insert({
            "email_cliente": email,
            "servico_id": servico_id,
            "data_atendimento": data_atendimento
        }).execute()

        # Atualiza a contagem de contratos do serviço (+1)
        servico_atual = supabase.table("servico").select("contratos").eq("id", servico_id).single().execute()
        contratos_atuais = servico_atual.data.get('contratos', 0) if servico_atual.data else 0
        supabase.table("servico").update({"contratos": contratos_atuais + 1}).eq("id", servico_id).execute()

        return jsonify({"message": "Agendamento realizado com sucesso!"}), 201
    except Exception as e:
        print(f"❌ Erro ao processar agendamento: {e}")
        return jsonify({"error": "Erro ao salvar o agendamento no banco."}), 500


@app.route('/api/meus-agendamentos', methods=['GET'])
def meus_agendamentos():
    email = request.args.get('email')
    if not email:
        return jsonify({"error": "E-mail do usuário não informado."}), 400

    try:
        # Busca os agendamentos trazendo informações acopladas da tabela de serviços (tipo, valor)
        resposta = supabase.table("agendamentos").select("id, data_atendimento, servico_id, servico(tipo, valor)").eq("email_cliente", email).execute()
        return jsonify(resposta.data), 200
    except Exception as e:
        print(f"❌ Erro ao buscar agendamentos: {e}")
        return jsonify({"error": "Erro ao carregar a lista de agendamentos."}), 500


@app.route('/api/agendamentos/<int:id>', methods=['PUT'])
def alterar_horario(id):
    dados = request.json
    nova_data = dados.get('data')

    if not nova_data:
        return jsonify({"error": "Nova data/hora é obrigatória."}), 400

    try:
        supabase.table("agendamentos").update({"data_atendimento": nova_data}).eq("id", id).execute()
        return jsonify({"message": "Horário atualizado!"}), 200
    except Exception as e:
        return jsonify({"error": "Erro ao atualizar horário no banco."}), 500


@app.route('/api/agendamentos/<int:id>', methods=['DELETE'])
def cancelar_agendamento(id):
    try:
        # 1. Busca os dados do agendamento antes de removê-lo da tabela ativa
        agendamento_busca = supabase.table("agendamentos").select("*").eq("id", id).execute()
        
        if not agendamento_busca.data:
            return jsonify({"error": "Agendamento não encontrado."}), 404
            
        dados_agendamento = agendamento_busca.data[0]
        servico_id = dados_agendamento.get('servico_id')

        # 2. Salva o registro histórico na tabela de 'agendamentos_cancelados' para auditoria
        supabase.table("agendamentos_cancelados").insert({
            "agendamento_id": dados_agendamento.get('id'),
            "email_cliente": dados_agendamento.get('email_cliente'),
            "servico_id": servico_id,
            "data_atendimento_original": dados_agendamento.get('data_atendimento')
        }).execute()

        # 3. Decrementa o número de contratos do serviço correspondente (-1)
        if servico_id:
            servico_atual = supabase.table("servico").select("contratos").eq("id", servico_id).single().execute()
            if servico_atual.data:
                contratos_atuais = servico_atual.data.get('contratos', 0)
                novos_contratos = max(0, contratos_atuais - 1)  # Impede valores negativos por segurança
                supabase.table("servico").update({"contratos": novos_contratos}).eq("id", servico_id).execute()

        # 4. Deleta permanentemente o agendamento da tabela operacional ativa
        supabase.table("agendamentos").delete().eq("id", id).execute()
        
        return jsonify({"message": "Agendamento cancelado e arquivado no histórico com sucesso!"}), 200

    except Exception as e:
        print(f"❌ Erro ao cancelar e arquivar agendamento: {e}")
        return jsonify({"error": "Erro ao processar o cancelamento no banco."}), 500


# Bloqueia o cache para desenvolvimento
@app.after_request
def adicionar_cabecalhos_sem_cache(resposta):
    resposta.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resposta.headers["Pragma"] = "no-cache"
    resposta.headers["Expires"] = "0"
    return resposta


if __name__ == '__main__':
    print("\n" + "═"*50)
    print(" 🌿 SERVIDOR PANACEIA SPA INICIADO COM SUCESSO! 🌿")
    print("═"*50)
    print(f" 👉 Carregando chaves de: {caminho_env}")
    print(" 👉 Link da Plataforma: http://127.0.0.1:5002")
    print("═"*50 + "\n")
    
    app.run(host='0.0.0.0', port=5002, debug=True)