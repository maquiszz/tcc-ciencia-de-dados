import os
import random
from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory
from dotenv import load_dotenv
from supabase import create_client, Client
from werkzeug.security import generate_password_hash, check_password_hash
from flask_cors import CORS  # 1. Importe o CORS
import requests
from itsdangerous import URLSafeTimedSerializer

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": ["http://127.0.0.1:5500", "http://localhost:5500", "https://seu-site-hospedado.com"]}})

# ==============================================================================
# --- 🔑 CONFIGURAÇÃO INTELIGENTE DO BANCO (LOCAL VS PRODUÇÃO) ---
# ==============================================================================

diretorio_do_script = os.path.dirname(os.path.abspath(__file__))
raiz_do_projeto = os.path.dirname(diretorio_do_script)
caminho_env = os.path.join(raiz_do_projeto, "database.env")

if os.path.exists(caminho_env):
    load_dotenv(dotenv_path=caminho_env)
    print(f"👉 Chaves carregadas localmente de: {caminho_env}")
else:
    print("👉 Arquivo 'database.env' não encontrado. Usando variáveis de ambiente do Render.")

url = os.environ.get("SUPABASE_URL")
key = os.environ.get("SUPABASE_KEY")

if not url or not key:
    print("\n❌ ERRO CRÍTICO: As variáveis SUPABASE_URL ou SUPABASE_KEY não foram encontradas!")
    exit()

supabase: Client = create_client(url, key)

# Configurações de Tokens
serializer = URLSafeTimedSerializer(key)

# ==============================================================================
# --- 📧 NOVO SISTEMA DE E-MAIL VIA BREVO API (ENVIA PARA QUALQUER UM) ---
# ==============================================================================
def enviar_email_transacional(destinatario, assunto, conteudo_html):
    api_key = os.environ.get("BREVO_API_KEY")
    
    # ⚠️ Mude para o e-mail exato que você usou para criar a conta no Brevo:
    email_remetente = "spapanaceia@gmail.com" 
    nome_remetente = "Spa Panaceia"
    
    if not api_key:
        print("❌ Erro: Chave BREVO_API_KEY não encontrada nas variáveis de ambiente.")
        return False

    url = "https://api.brevo.com/v3/smtp/email"
    
    headers = {
        "accept": "application/json",
        "api-key": api_key,
        "content-type": "application/json"
    }
    
    payload = {
        "sender": {"name": nome_remetente, "email": email_remetente},
        "to": [{"email": destinatario}],
        "subject": assunto,
        "htmlContent": conteudo_html
    }
    
    try:
        resposta = requests.post(url, headers=headers, json=payload)
        
        # O Brevo retorna 201 quando cria o e-mail com sucesso
        if resposta.status_code in [200, 201]:
            print(f"✅ E-mail enviado com sucesso para {destinatario}!")
            return True
        else:
            print(f"❌ Erro ao enviar via Brevo: {resposta.text}")
            return False
            
    except Exception as e:
        print(f"❌ Erro interno na API do Brevo: {e}")
        return False

# ==============================================================================
# --- 🌍 ROTAS DE PÁGINAS VISUAIS (HTML) ---
# ==============================================================================

@app.route('/')
def pagina_principal():
    return send_from_directory(diretorio_do_script, 'servicos.html')


@app.route('/cadastro')
def pagina_cadastro():
    return send_from_directory(diretorio_do_script, 'index.html')


# ==============================================================================
# --- ⚙️ ROTAS DE PROCESSAMENTO DE DADOS (API) ---
# ==============================================================================

@app.route('/cadastrar', methods=['POST'])
def cadastrar():
    dados = request.json
    nome = dados.get('nome')
    email = dados.get('email')
    senha_limpa = dados.get('senha')

    if not nome or not email or not senha_limpa:
        return jsonify({"error": "Nome, e-mail e senha são obrigatórios."}), 400

    if "@" not in email or "." not in email.split("@")[-1]:
        return jsonify({"error": "Por favor, insira um e-mail real e válido."}), 400

    senha_criptografada = generate_password_hash(senha_limpa, method='pbkdf2:sha256')

    try:
        # Cadastra o usuário como não verificado inicialmente
        supabase.table("usuarios").insert({
            "nome": nome, 
            "email": email, 
            "senha": senha_criptografada,
            "verificado": False
        }).execute()

        # Gera link de confirmação válido por 1 hora
        token = serializer.dumps(email, salt='confirmar-email')
        link_confirmacao = f"http://127.0.0.1:5000/api/confirmar?token={token}"

        html_msg = f"""
        <div style="font-family: Arial, sans-serif; padding: 20px; color: #333;">
            <h2 style="color: #6a11cb;">Bem-vindo(a) ao Spa Panaceia, {nome}! 🌿</h2>
            <p>Falta apenas um clique para ativar sua conta de bem-estar.</p>
            <a href="{link_confirmacao}" style="display: inline-block; padding: 12px 25px; background: #6a11cb; color: white; text-decoration: none; border-radius: 8px; font-weight: bold; margin-top: 15px;">Confirmar Meu E-mail</a>
            <p style="margin-top: 20px; font-size: 12px; color: #777;">Se você não se cadastrou, ignore esta mensagem.</p>
        </div>
        """
        enviar_email_transacional(email, "Confirme sua conta - Spa Panaceia", html_msg)

        return jsonify({"message": "Cadastro realizado! Verifique sua caixa de entrada para ativar a conta."}), 201
    except Exception as e:
        print(f"❌ Erro ao salvar no banco: {e}")
        return jsonify({"error": "Erro ao salvar usuário. O e-mail já pode estar em uso."}), 500


@app.route('/api/confirmar', methods=['GET'])
def confirmar_email():
    token = request.args.get('token')
    try:
        email = serializer.loads(token, salt='confirmar-email', max_age=3600)
        supabase.table("usuarios").update({"verificado": True}).eq("email", email).execute()
        return "<h3>Conta confirmada com sucesso! ✨ Você já pode fechar esta aba e fazer login no site.</h3>", 200
    except Exception:
        return "<h3>Link inválido ou expirado. Tente se cadastrar novamente.</h3>", 400


@app.route('/api/login', methods=['POST'])
def login():
    dados = request.json
    email = dados.get('email')
    senha_digitada = dados.get('senha')

    if not email or not senha_digitada:
        return jsonify({"error": "E-mail e senha são obrigatórios."}), 400

    try:
        usuario = supabase.table("usuarios").select("*").eq("email", email).execute()
        
        if not usuario.data:
            return jsonify({"error": "E-mail não encontrado. Por favor, crie uma conta primeiro."}), 404
        
        user_db = usuario.data[0]
        
        # 🔒 Trava de verificação de e-mail
        if not user_db.get('verificado', True):
            return jsonify({"error": "Conta não verificada. Por favor, verifique seu e-mail para ativar o cadastro antes de entrar."}), 403

        senha_banco = user_db.get('senha')
        
        if not check_password_hash(senha_banco, senha_digitada):
            return jsonify({"error": "Senha incorreta. Tente novamente."}), 401
        
        # Remove a senha hash do objeto, mantendo o campo 'is_admin' intacto para o frontend
        user_db.pop('senha', None)
        
        return jsonify({"message": "Login realizado com sucesso!", "usuario": user_db}), 200
    except Exception as e:
        print(f"❌ Erro interno no login: {e}")
        return jsonify({"error": "Erro interno no servidor de autenticação."}), 500


@app.route('/api/esqueci-senha', methods=['POST'])
def esqueci_senha():
    dados = request.json
    email = dados.get('email')

    if not email:
        return jsonify({"error": "Informe seu e-mail cadastrado."}), 400

    try:
        usuario = supabase.table("usuarios").select("*").eq("email", email).execute()
        if not usuario.data:
            return jsonify({"error": "E-mail não encontrado em nossa base."}), 404

        codigo = str(random.randint(100000, 999999))
        supabase.table("usuarios").update({"token_recuperacao": codigo}).eq("email", email).execute()

        html_msg = f"""
        <div style="font-family: Arial, sans-serif; padding: 20px; color: #333;">
            <h3 style="color: #6a11cb;">Recuperação de Senha - Spa Panaceia 🌸</h3>
            <p>Utilize o código de verificação abaixo para redefinir sua senha:</p>
            <h1 style="background: #f8fafc; padding: 15px; border-radius: 8px; letter-spacing: 5px; color: #2575fc; display: inline-block;">{codigo}</h1>
            <p>Se você não solicitou isso, ignore este e-mail.</p>
        </div>
        """
        enviar_email_transacional(email, "Código de Recuperação de Senha", html_msg)
        return jsonify({"message": "Código de recuperação enviado para o seu e-mail!"}), 200
    except Exception as e:
        print(f"❌ Erro no esqueci senha: {e}")
        return jsonify({"error": "Erro interno ao processar recuperação."}), 500


@app.route('/api/redefinir-senha', methods=['POST'])
def redefinir_senha():
    dados = request.json
    email = dados.get('email')
    codigo = dados.get('codigo')
    nova_senha = dados.get('nova_senha')

    if not email or not codigo or not nova_senha:
        return jsonify({"error": "Preencha todos os campos."}), 400

    try:
        usuario = supabase.table("usuarios").select("*").eq("email", email).execute()
        if not usuario.data:
            return jsonify({"error": "Usuário não encontrado."}), 404

        user_db = usuario.data[0]
        if user_db.get('token_recuperacao') != codigo:
            return jsonify({"error": "Código de verificação incorreto."}), 400

        senha_criptografada = generate_password_hash(nova_senha, method='pbkdf2:sha256')
        
        supabase.table("usuarios").update({
            "senha": senha_criptografada,
            "token_recuperacao": None
        }).eq("email", email).execute()

        return jsonify({"message": "Senha redefinida com sucesso! Faça login com a nova senha."}), 200
    except Exception as e:
        return jsonify({"error": "Erro ao redefinir senha."}), 500


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
    data_atendimento_str = dados.get('data')

    if not email or not servico_id or not data_atendimento_str:
        return jsonify({"error": "Todos os campos são obrigatórios."}), 400

    try:
        data_selecionada = datetime.fromisoformat(data_atendimento_str)
        if data_selecionada < datetime.now():
            return jsonify({"error": "Não é possível agendar numa data ou horário que já passou."}), 400
            
        if data_selecionada.minute != 0:
            return jsonify({"error": "Os agendamentos devem ser feitos em horários cheios (ex: 09:00, 10:00)."}), 400

        usuario_existe = supabase.table("usuarios").select("email").eq("email", email).execute()
        if not usuario_existe.data:
            return jsonify({"error": "Usuário Inexistente. Crie uma conta antes de agendar."}), 404

        conflito = supabase.table("agendamentos").select("id").eq("data_atendimento", data_atendimento_str).execute()
        if conflito.data:
            return jsonify({"error": "Este horário já está reservado por outro cliente. Por favor, escolha outra opção."}), 409

        supabase.table("agendamentos").insert({
            "email_cliente": email,
            "servico_id": servico_id,
            "data_atendimento": data_atendimento_str
        }).execute()

        servico_atual = supabase.table("servico").select("contratos").eq("id", servico_id).execute()
        if servico_atual.data:
            contratos_atuais = servico_atual.data[0].get('contratos', 0)
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
        resposta = supabase.table("agendamentos").select("id, data_atendimento, status, avaliacao, servico_id, servico(tipo, valor)").eq("email_cliente", email).execute()
        return jsonify(resposta.data), 200
    except Exception as e:
        print(f"❌ Erro ao buscar agendamentos: {e}")
        return jsonify({"error": "Erro ao carregar a lista de agendamentos."}), 500


@app.route('/api/agendamentos/<int:id>', methods=['PUT'])
def alterar_horario(id):
    dados = request.json
    nova_data_str = dados.get('data')

    if not nova_data_str:
        return jsonify({"error": "Nova data/hora é obrigatória."}), 400

    try:
        data_selecionada = datetime.fromisoformat(nova_data_str)
        if data_selecionada < datetime.now():
            return jsonify({"error": "Não é possível remarcar para uma data no passado."}), 400
            
        if data_selecionada.minute != 0:
            return jsonify({"error": "Os agendamentos devem ser feitos em horários cheios (ex: 09:00, 10:00)."}), 400

        conflito = supabase.table("agendamentos").select("id").eq("data_atendimento", nova_data_str).neq("id", id).execute()
        if conflito.data:
            return jsonify({"error": "Este horário já está reservado. Escolha outro momento para a sua sessão."}), 409

        supabase.table("agendamentos").update({"data_atendimento": nova_data_str}).eq("id", id).execute()
        return jsonify({"message": "Horário updated com sucesso!"}), 200
    except Exception as e:
        print(f"❌ Erro ao atualizar horário: {e}")
        return jsonify({"error": "Erro ao atualizar horário no banco."}), 500


@app.route('/api/agendamentos/<int:id>', methods=['DELETE'])
def cancelar_agendamento(id):
    try:
        agendamento_busca = supabase.table("agendamentos").select("*").eq("id", id).execute()
        
        if not agendamento_busca.data:
            return jsonify({"error": "Agendamento não encontrado no banco de dados."}), 404
            
        dados_agendamento = agendamento_busca.data[0]
        servico_id = dados_agendamento.get('servico_id')

        try:
            supabase.table("agendamentos_cancelados").insert({
                "agendamento_id": dados_agendamento.get('id'),
                "email_cliente": dados_agendamento.get('email_cliente'),
                "servico_id": servico_id,
                "data_atendimento_original": dados_agendamento.get('data_atendimento')
            }).execute()
        except Exception as hist_err:
            print(f"⚠️ Nota: Não foi possível salvar na tabela histórica: {hist_err}")

        if servico_id:
            try:
                servico_atual = supabase.table("servico").select("contratos").eq("id", servico_id).execute()
                if servico_atual.data:
                    contratos_atuais = servico_atual.data[0].get('contratos', 0)
                    novos_contratos = max(0, contratos_atuais - 1)
                    supabase.table("servico").update({"contratos": novos_contratos}).eq("id", servico_id).execute()
            except Exception as serv_err:
                print(f"⚠️ Nota: Falha ao atualizar contador na tabela 'servico': {serv_err}")

        supabase.table("agendamentos").delete().eq("id", id).execute()
        return jsonify({"message": "Agendamento cancelado com sucesso!"}), 200

    except Exception as e:
        print(f"❌ Erro crítico ao processar exclusão do agendamento: {e}")
        return jsonify({"error": "Erro interno ao processar o cancelamento no servidor."}), 500


@app.route('/api/agendamentos/<int:id>/avaliar', methods=['POST'])
def avaliar_agendamento(id):
    dados = request.json
    nota = dados.get('avaliacao')

    if nota not in ["Bom", "Médio", "Ruim"]:
        return jsonify({"error": "Avaliação inválida. Use apenas Bom, Médio ou Ruim."}), 400

    try:
        busca = supabase.table("agendamentos").select("status").eq("id", id).execute()
        if not busca.data or busca.data[0].get('status') != 'Concluido':
            return jsonify({"error": "Você só pode avaliar serviços já finalizados."}), 400

        supabase.table("agendamentos").update({"avaliacao": nota}).eq("id", id).execute()
        return jsonify({"message": "Obrigado pela sua avaliação!"}), 200
    except Exception as e:
        print(f"❌ Erro ao salvar avaliação: {e}")
        return jsonify({"error": "Erro interno ao salvar avaliação."}), 500


# ==============================================================================
# --- 👑 ROTAS EXCLUSIVAS DO PAINEL ADMINISTRATIVO (PROTEGIDAS) ---
# ==============================================================================

@app.route('/api/admin/agendamentos', methods=['GET'])
def admin_listar_todos_agendamentos():
    admin_email = request.args.get('admin_email')
    
    if not admin_email:
        return jsonify({"error": "Identificação administrativa ausente."}), 400
        
    try:
        checagem = supabase.table("usuarios").select("is_admin").eq("email", admin_email).execute()
        if not checagem.data or not checagem.data[0].get('is_admin'):
            return jsonify({"error": "Acesso negado. Rota exclusiva para administradores."}), 403
    except Exception as e:
        return jsonify({"error": f"Erro na checagem de admin: {str(e)}"}), 500

    try:
        servicos_req = supabase.table("servico").select("id, tipo, valor").execute()
        lista_servicos = servicos_req.data or []
        mapa_servicos = {s['id']: s for s in lista_servicos}
    except Exception as e:
        return jsonify({"error": f"Erro ao ler tabela 'servicos'. Detalhes: {str(e)}"}), 500

    try:
        resposta_ativos = supabase.table("agendamentos").select("id, data_atendimento, email_cliente, status, avaliacao, servico_id").execute()
        agendamentos_ativos = resposta_ativos.data or []
    except Exception as e:
        return jsonify({"error": f"Erro ao ler tabela 'agendamentos'. Detalhes: {str(e)}"}), 500

    try:
        resposta_cancelados = supabase.table("agendamentos_cancelados").select("id, data_atendimento_original, email_cliente, servico_id").execute()
        agendamentos_cancelados = resposta_cancelados.data or []
    except Exception as e:
        return jsonify({"error": f"Erro ao ler tabela 'agendamentos_cancelados'. Detalhes: {str(e)}"}), 500

    try:
        for ag in agendamentos_cancelados:
            ag['data_atendimento'] = ag.pop('data_atendimento_original', None)
            ag['status'] = 'Cancelado'
            ag['avaliacao'] = None
            
            s_id = ag.get('servico_id')
            if s_id and s_id in mapa_servicos:
                ag['servico'] = {"tipo": mapa_servicos[s_id]['tipo'], "valor": mapa_servicos[s_id]['valor']}
            else:
                ag['servico'] = {"tipo": "Cancelado", "valor": 0}

        for ag in agendamentos_ativos:
            s_id = ag.get('servico_id')
            if s_id and s_id in mapa_servicos:
                ag['servico'] = {"tipo": mapa_servicos[s_id]['tipo'], "valor": mapa_servicos[s_id]['valor']}
            else:
                ag['servico'] = {"tipo": "N/A", "valor": 0}
                
        lista_completa = agendamentos_ativos + agendamentos_cancelados
        return jsonify(lista_completa), 200

    except Exception as e:
        return jsonify({"error": f"Erro ao processar mapeamento dos dados: {str(e)}"}), 500


@app.route('/api/admin/usuarios', methods=['GET'])
def admin_listar_todos_usuarios():
    admin_email = request.args.get('admin_email')
    
    if not admin_email:
        return jsonify({"error": "Identificação administrativa ausente."}), 400
        
    try:
        checagem = supabase.table("usuarios").select("is_admin").eq("email", admin_email).execute()
        if not checagem.data or not checagem.data[0].get('is_admin'):
            return jsonify({"error": "Acesso negado. Rota exclusiva para administradores."}), 403
            
        usuarios_req = supabase.table("usuarios").select("nome, email, is_admin").execute()
        usuarios_banco = usuarios_req.data or []
        
        ativos_req = supabase.table("agendamentos").select("email_cliente, status, servico(valor)").execute()
        agendamentos_ativos = ativos_req.data or []
        
        cancelados_req = supabase.table("agendamentos_cancelados").select("email_cliente").execute()
        agendamentos_cancelados = cancelados_req.data or []
        
        lista_resposta = []
        
        for usuario in usuarios_banco:
            if usuario.get('is_admin'):
                continue
                
            email_user = usuario.get('email')
            user_ativos = [ag for ag in agendamentos_ativos if ag.get('email_cliente') == email_user]
            user_cancelados = [ag for ag in agendamentos_cancelados if ag.get('email_cliente') == email_user]
            
            total_cancelados = len(user_cancelados)
            total_ativos = len(user_ativos)
            
            pendentes = sum(1 for ag in user_ativos if ag.get('status') != 'Concluido')
            concluidos = sum(1 for ag in user_ativos if ag.get('status') == 'Concluido')
            
            total_gasto = 0.0
            for ag in user_ativos:
                if ag.get('status') == 'Concluido' and ag.get('servico'):
                    total_gasto += float(ag.get('servico', {}).get('valor', 0))
            
            lista_resposta.append({
                "nome": usuario.get('nome') or email_user.split('@')[0],
                "email": email_user,
                "total": total_ativos + total_cancelados,
                "pendentes": pendentes,
                "concluidos": concluidos,
                "cancelados": total_cancelados,
                "gastos": total_gasto
            })
            
        return jsonify(lista_resposta), 200

    except Exception as e:
        print(f"❌ Erro na consulta de administração de usuários: {e}")
        return jsonify({"error": "Erro ao listar usuários do sistema."}), 500


@app.route('/api/admin/agendamentos/<int:id>/concluir', methods=['POST'])
def admin_concluir_agendamento(id):
    dados = request.json
    admin_email = dados.get('admin_email')
    
    if not admin_email:
        return jsonify({"error": "Identificação administrativa ausente."}), 400
        
    try:
        # Validação de segurança
        checagem = supabase.table("usuarios").select("is_admin").eq("email", admin_email).execute()
        if not checagem.data or not checagem.data[0].get('is_admin'):
            return jsonify({"error": "Acesso negado. Rota exclusiva para administradores."}), 403
            
        # Pega os dados do agendamento para saber quem é o cliente
        ag_busca = supabase.table("agendamentos").select("email_cliente, servico(valor)").eq("id", id).execute()
        if not ag_busca.data:
            return jsonify({"error": "Agendamento não encontrado."}), 404
        
        info_ag = ag_busca.data[0]
        email_cliente = info_ag.get('email_cliente')
        
        # Atualiza o status para 'Concluido'
        supabase.table("agendamentos").update({"status": "Concluido"}).eq("id", id).execute()
        
        # 🌟 SISTEMA DE FIDELIDADE: Adiciona 10 pontos ao cliente
        user_busca = supabase.table("usuarios").select("pontos").eq("email", email_cliente).execute()
        if user_busca.data:
            pontos_atuais = user_busca.data[0].get('pontos') or 0
            novos_pontos = pontos_atuais + 10 # Cada serviço concluído dá 10 pontos
            supabase.table("usuarios").update({"pontos": novos_pontos}).eq("email", email_cliente).execute()

        return jsonify({"message": "Agendamento concluído e pontos de fidelidade creditados!"}), 200
    except Exception as e:
        print(f"❌ Erro ao concluir agendamento: {e}")
        return jsonify({"error": "Erro ao atualizar status do agendamento."}), 500


@app.after_request
def adicionar_cabecalhos_sem_cache(resposta):
    resposta.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resposta.headers["Pragma"] = "no-cache"
    resposta.headers["Expires"] = "0"
    return resposta


@app.route('/api/chat', methods=['POST'])
def chat_assistente():
    dados = request.json
    mensagem_usuario = dados.get('mensagem')

    if not mensagem_usuario:
        return jsonify({"error": "Mensagem vazia."}), 400

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return jsonify({"error": "Chave da OpenAI não configurada no servidor."}), 500

    try:
        servicos_req = supabase.table("servico").select("tipo, valor").execute()
        lista_servicos = servicos_req.data or []
        
        texto_servicos = ", ".join([f"{s['tipo']} (R$ {s['valor']})" for s in lista_servicos])
        
        prompt_sistema = f"""Você é o(a) recepcionista super carismático(a) e humano(a) do Spa Panaceia.
        Aja como um amigo acolhedor: seja caloroso, demonstre empatia real e use uma linguagem muito natural, leve e coloquial. 
        Fale pouco! Responda em no MÁXIMO 3 a 4 frases curtas. Pareça uma conversa de WhatsApp. Use emojis para dar vida ao texto.
        O cliente dirá o que está sentindo. Acolha a dor dele rapidamente e sugira apenas 1 (ou no máximo 2) serviços que realmente ajudem.
        Nossos serviços e preços reais são: {texto_servicos}.
        NUNCA invente serviços. Vá direto ao ponto de forma gentil."""

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        
        payload = {
            "model": "gpt-3.5-turbo", 
            "messages": [
                {"role": "system", "content": prompt_sistema},
                {"role": "user", "content": mensagem_usuario}
            ],
            "max_tokens": 150,
            "temperature": 0.85
        }

        resposta_openai = requests.post("https://api.openai.com/v1/chat/completions", json=payload, headers=headers)
        dados_ai = resposta_openai.json()

        if resposta_openai.status_code == 200:
            texto_resposta = dados_ai['choices'][0]['message']['content'].strip()
            return jsonify({"resposta": texto_resposta}), 200
        else:
            print("Erro da OpenAI:", dados_ai)
            return jsonify({"error": "Nossa IA está meditando agora. Tente novamente mais tarde."}), 500

    except Exception as e:
        print(f"❌ Erro no chat: {e}")
        return jsonify({"error": "Falha na comunicação neural do Spa."}), 500
    

@app.route('/api/usuario/dados', methods=['GET'])
def dados_usuario():
    email = request.args.get('email')
    if not email:
        return jsonify({"error": "E-mail não informado."}), 400
    try:
        res = supabase.table("usuarios").select("nome, email, is_admin, pontos").eq("email", email).execute()
        if not res.data:
            return jsonify({"error": "Usuário não encontrado."}), 404
        return jsonify(res.data[0]), 200
    except Exception as e:
        return jsonify({"error": "Erro ao buscar dados."}), 500    


# ==============================================================================
# --- 🚀 INICIALIZAÇÃO DINÂMICA COMPATÍVEL COM RENDER ---
# ==============================================================================
if __name__ == '__main__':
    print("\n" + "═"*50)
    print(" 🌿 SERVIDOR PANACEIA SPA CONFIGURADO 🌿")
    print("═"*50)
    
    porta = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=porta)