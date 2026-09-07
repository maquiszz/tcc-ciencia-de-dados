import os
import random
import secrets
import traceback
from datetime import datetime, timedelta, timezone
import sys
import os
import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from itsdangerous import URLSafeTimedSerializer
from supabase import Client, create_client
from werkzeug.security import check_password_hash, generate_password_hash
from openai import OpenAI

app = Flask(__name__)
CORS(
    app,
    resources={
        r"/api/*": {
            "origins": [
                "http://127.0.0.1:5500",
                "http://localhost:5500",
                "https://seu-site-hospedado.com",
            ]
        }
    },
)




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
    print(
        "👉 Arquivo 'database.env' não encontrado. Usando variáveis de ambiente do Render."
    )

url = os.environ.get("SUPABASE_URL")
key = os.environ.get("SUPABASE_KEY")

if not url or not key:
    print(
        "\n❌ ERRO CRÍTICO: As variáveis SUPABASE_URL ou SUPABASE_KEY não foram encontradas!"
    )
    exit()

supabase: Client = create_client(url, key)

# Configurações de Tokens
serializer = URLSafeTimedSerializer(key)


#SISTEMA DE CHAT BOT

# 1. Carrega as variáveis do seu arquivo de configuração específico
load_dotenv('database.env')

# 2. Inicializa o cliente da OpenAI puxando a chave do .env
chave_api = os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=chave_api)

@app.route('/api/chat', methods=['POST'])
def chat():
    try:
        dados = request.get_json()
        mensagem_usuario = dados.get('mensagem')

        if not mensagem_usuario:
            return jsonify({"error": "Sinto o vácuo. Me diga como se sente."}), 400

        # Contexto do Sistema (A nova personalidade Premium do Orientador)
        contexto_spa = (
            "Você é o Concierge Virtual de bem-estar do Spa Panaceia, um ambiente de alto luxo e serenidade. "
            "Seu tom deve ser sofisticado, empático, acolhedor e muito conciso (máximo de 2 a 3 frases). "
            "Regras de ouro do seu atendimento:\n"
            "1. Se o cliente relatar dores, estresse ou cansaço: Mostre empatia e sugira uma experiência do spa (ex: Massagem Relaxante, Aromaterapia, Banho Termal) focada naquele sintoma.\n"
            "2. Se o cliente disser que quer AGENDAR, RESERVAR, ou que GOSTOU da sugestão: NUNCA faça novas perguntas sobre sentimentos. Apenas celebre a decisão de autocuidado de forma elegante e diga que está abrindo a agenda na tela para ele escolher o horário.\n"
            "3. Evite parecer um robô. Use vocabulário premium: jornada, refúgio, serenidade, vitalidade, renovação."
        )

        # Chamada real para a API da OpenAI
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": contexto_spa},
                {"role": "user", "content": mensagem_usuario}
            ],
            max_tokens=150,
            temperature=0.7
        )

        # Extrai a resposta gerada pela IA
        resposta_ia = response.choices[0].message.content.strip()

        return jsonify({"resposta": resposta_ia}), 200

    except Exception as e:
        print(f"Erro na OpenAI: {e}")
        return jsonify({"error": "Nossa conexão neural falhou momentaneamente. Respire fundo e tente novamente."}), 500



# ==============================================================================
# --- 📧 NOVO SISTEMA DE E-MAIL VIA BREVO API (ENVIA PARA QUALQUER UM) ---
# ==============================================================================
def enviar_email_transacional(destinatario, assunto, conteudo_html):
    api_key = os.environ.get("BREVO_API_KEY")

    email_remetente = "spapanaceia@gmail.com"
    nome_remetente = "Spa Panaceia"

    if not api_key:
        print(
            "❌ Erro: Chave BREVO_API_KEY não encontrada nas variáveis de ambiente."
        )
        return False

    url = "https://api.brevo.com/v3/smtp/email"

    headers = {
        "accept": "application/json",
        "api-key": api_key,
        "content-type": "application/json",
    }

    payload = {
        "sender": {"name": nome_remetente, "email": email_remetente},
        "to": [{"email": destinatario}],
        "subject": assunto,
        "htmlContent": conteudo_html,
    }

    try:
        resposta = requests.post(url, headers=headers, json=payload)

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


@app.route("/")
def pagina_principal():
    return send_from_directory(diretorio_do_script, "servicos.html")


@app.route("/cadastro")
def pagina_cadastro():
    return send_from_directory(diretorio_do_script, "index.html")


# ==============================================================================
# --- ⚙️ ROTAS DE PROCESSAMENTO DE DADOS (API) ---
# ==============================================================================


@app.route("/cadastrar", methods=["POST"])
def cadastrar():
    dados = request.json or {}
    nome = dados.get("nome")
    email = dados.get("email", "").strip().lower()
    senha_limpa = dados.get("senha")

    if not nome or not email or not senha_limpa:
        return jsonify({"error": "Nome, e-mail e senha são obrigatórios."}), 400

    if "@" not in email or "." not in email.split("@")[-1]:
        return (
            jsonify({"error": "Por favor, insira um e-mail real e válido."}),
            400,
        )

    senha_criptografada = generate_password_hash(
        senha_limpa, method="pbkdf2:sha256"
    )

    try:
        checagem = (
            supabase.table("usuarios")
            .select("id")
            .eq("email", email)
            .execute()
        )
        if checagem.data:
            return (
                jsonify(
                    {"error": "Este e-mail já está cadastrado no sistema."}
                ),
                400,
            )

        codigo_otp = f"{secrets.randbelow(1000000):06d}"
        expiracao = (
            datetime.now(timezone.utc) + timedelta(minutes=15)
        ).isoformat()

        supabase.table("usuarios").insert(
            {
                "nome": nome,
                "email": email,
                "senha": senha_criptografada,
                "email_verificado": False,
                "codigo_otp": codigo_otp,
                "codigo_expira_em": expiracao,
                "is_admin": False,
            }
        ).execute()

        html_msg = f"""
        <div style="font-family: Arial, sans-serif; max-width: 500px; margin: 0 auto; padding: 20px; border: 1px solid #e0e0e0; border-radius: 10px; color: #333;">
            <h2 style="color: #6a11cb; text-align: center;">Bem-vindo(a) ao Spa Panaceia, {nome}! 🌿</h2>
            <p>Seu código de verificação para ativar a sua conta é:</p>
            
            <div style="background-color: #f0f4ff; text-align: center; padding: 15px; border-radius: 8px; margin: 20px 0;">
                <span style="font-size: 32px; font-weight: bold; letter-spacing: 8px; color: #6a11cb;">{codigo_otp}</span>
            </div>
            
            <p style="font-size: 13px; color: #666; text-align: center;">Este código é válido por 15 minutos.</p>
            <p style="margin-top: 20px; font-size: 12px; color: #999; text-align: center;">Se você não realizou este cadastro, desconsidere esta mensagem.</p>
        </div>
        """

        enviar_email_transacional(
            email, "Seu código de ativação - Spa Panaceia", html_msg
        )

        return (
            jsonify(
                {
                    "message": "Cadastro realizado! Digite o código de 6 dígitos enviado ao seu e-mail para ativar a conta.",
                    "email": email,
                }
            ),
            201,
        )

    except Exception as e:
        print(f"❌ Erro ao salvar no banco: {e}")
        return (
            jsonify(
                {"error": "Erro ao criar conta. Tente novamente mais tarde."}
            ),
            500,
        )


@app.route("/api/confirmar", methods=["GET"])
def confirmar_email():
    token = request.args.get("token")
    try:
        email = serializer.loads(token, salt="confirmar-email", max_age=3600)
        supabase.table("usuarios").update({"verificado": True}).eq(
            "email", email
        ).execute()
        return (
            "<h3>Conta confirmada com sucesso! ✨ Você já pode fechar esta aba e fazer login no site.</h3>",
            200,
        )
    except Exception:
        return (
            "<h3>Link inválido ou expirado. Tente se cadastrar novamente.</h3>",
            400,
        )


@app.route("/api/validar-codigo", methods=["POST"])
def validar_codigo():
    dados = request.json or {}
    email = dados.get("email")
    codigo_digitado = dados.get("codigo")

    if not email or not codigo_digitado:
        return jsonify({"error": "E-mail e código são obrigatórios."}), 400

    try:
        res = (
            supabase.table("usuarios").select("*").eq("email", email).execute()
        )

        if not res.data:
            return jsonify({"error": "Usuário não encontrado."}), 404

        usuario = res.data[0]

        if usuario.get("email_verificado"):
            return (
                jsonify(
                    {
                        "message": "Conta já verificada! Faça login para continuar."
                    }
                ),
                200,
            )

        codigo_salvo = usuario.get("codigo_otp")
        if str(codigo_salvo) != str(codigo_digitado):
            return jsonify({"error": "Código de verificação incorreto."}), 400

        expiracao_str = usuario.get("codigo_expira_em")
        if expiracao_str:
            expiracao = datetime.fromisoformat(
                expiracao_str.replace("Z", "+00:00")
            )
            if datetime.now(timezone.utc) > expiracao:
                return (
                    jsonify(
                        {
                            "error": "Código expirado. Solicite um novo código."
                        }
                    ),
                    400,
                )

        supabase.table("usuarios").update(
            {
                "email_verificado": True,
                "codigo_otp": None,
                "codigo_expira_em": None,
            }
        ).eq("email", email).execute()

        return jsonify({"message": "E-mail verificado com sucesso!"}), 200

    except Exception as e:
        print(f"❌ Erro ao validar código: {e}")
        return jsonify({"error": "Erro interno ao validar o código."}), 500


@app.route("/api/login", methods=["POST"])
def login():
    dados = request.json or {}
    email = dados.get("email", "").strip().lower()
    senha = dados.get("senha")

    if not email or not senha:
        return jsonify({"error": "E-mail e senha são obrigatórios."}), 400

    try:
        res = (
            supabase.table("usuarios").select("*").eq("email", email).execute()
        )

        if not res.data:
            return jsonify({"error": "Usuário não encontrado."}), 404

        usuario = res.data[0]

        if not usuario.get("email_verificado"):
            return (
                jsonify(
                    {
                        "error": "Conta não verificada. Verifique seu e-mail antes de entrar."
                    }
                ),
                403,
            )

        senha_hash_banco = usuario.get("senha")

        if not senha_hash_banco or not check_password_hash(
            senha_hash_banco, senha
        ):
            return jsonify({"error": "E-mail ou senha incorretos."}), 401

        return (
            jsonify(
                {
                    "message": "Login realizado com sucesso!",
                    "usuario": {
                        "id": usuario.get("id"),
                        "nome": usuario.get("nome"),
                        "email": usuario.get("email"),
                        "is_admin": usuario.get("is_admin", False),
                        "pontos": usuario.get("pontos", 0),
                    },
                }
            ),
            200,
        )

    except Exception as e:
        print(f"❌ Erro no login: {e}")
        return jsonify({"error": "Erro interno no servidor."}), 500


@app.route("/api/esqueci-senha", methods=["POST"])
def esqueci_senha():
    dados = request.json or {}
    email = dados.get("email", "").strip().lower()

    if not email:
        return jsonify({"error": "Informe seu e-mail cadastrado."}), 400

    try:
        usuario = (
            supabase.table("usuarios")
            .select("id")
            .eq("email", email)
            .execute()
        )

        if not usuario.data:
            return (
                jsonify(
                    {
                        "message": "Se o e-mail estiver cadastrado, você receberá o código de recuperação."
                    }
                ),
                200,
            )

        codigo = f"{random.randint(100000, 999999):06d}"

        supabase.table("usuarios").update({"token_recuperacao": codigo}).eq(
            "email", email
        ).execute()

        html_msg = f"""
        <div style="font-family: Arial, sans-serif; padding: 20px; color: #333;">
            <h3 style="color: #6a11cb;">Recuperação de Senha - Spa Panaceia 🌸</h3>
            <p>Utilize o código de verificação abaixo para redefinir sua senha:</p>
            <h1 style="background: #f8fafc; padding: 15px; border-radius: 8px; letter-spacing: 5px; color: #2575fc; display: inline-block;">{codigo}</h1>
            <p>Se você não solicitou isso, ignore este e-mail.</p>
        </div>
        """
        enviar_email_transacional(
            email, "Código de Recuperação de Senha", html_msg
        )

        return (
            jsonify(
                {
                    "message": "Código de recuperação enviado para o seu e-mail!"
                }
            ),
            200,
        )

    except Exception as e:
        print("❌ Erro no /api/esqueci-senha:")
        traceback.print_exc()
        return jsonify({"error": "Erro ao processar a solicitação."}), 500


@app.route("/api/redefinir-senha", methods=["POST"])
def redefinir_senha():
    dados = request.json or {}
    email = dados.get("email", "").strip().lower()
    codigo = str(dados.get("codigo", "")).strip()
    nova_senha = dados.get("nova_senha", "").strip()

    if not email or not codigo or not nova_senha:
        return jsonify({"error": "Preencha todos os campos."}), 400

    if len(nova_senha) < 6:
        return (
            jsonify({"error": "A senha deve ter no mínimo 6 caracteres."}),
            400,
        )

    try:
        usuario = (
            supabase.table("usuarios")
            .select("token_recuperacao")
            .eq("email", email)
            .execute()
        )

        if not usuario.data:
            return jsonify({"error": "Usuário não encontrado."}), 404

        token_salvo = str(usuario.data[0].get("token_recuperacao") or "")

        if not token_salvo or token_salvo != codigo:
            return jsonify({"error": "Código de verificação incorreto."}), 400

        senha_hash = generate_password_hash(
            nova_senha, method="pbkdf2:sha256"
        )

        supabase.table("usuarios").update(
            {"senha": senha_hash, "token_recuperacao": None}
        ).eq("email", email).execute()

        return (
            jsonify(
                {
                    "message": "Senha redefinida com sucesso! Faça login para continuar."
                }
            ),
            200,
        )

    except Exception as e:
        print("❌ Erro no /api/redefinir-senha:")
        traceback.print_exc()
        return jsonify({"error": "Erro ao redefinir a senha."}), 500


@app.route("/api/servicos", methods=["GET"])
def listar_servicos():
    try:
        resposta = supabase.table("servico").select("*").execute()
        return jsonify(resposta.data), 200
    except Exception as e:
        return jsonify({"error": "Erro ao ler a tabela de serviços."}), 500


@app.route("/api/agendar", methods=["POST"])
def criar_agendamento():
    dados = request.json or {}
    email = dados.get("email")
    servico_id = dados.get("servico_id")
    data_atendimento_str = dados.get("data")

    if not email or not servico_id or not data_atendimento_str:
        return jsonify({"error": "Todos os campos são obrigatórios."}), 400

    try:
        data_selecionada = datetime.fromisoformat(data_atendimento_str)
        agora = datetime.now(data_selecionada.tzinfo) if data_selecionada.tzinfo else datetime.now()
        
        if data_selecionada < agora:
            return jsonify({"error": "Não é possível agendar numa data ou horário que já passou."}), 400

        if data_selecionada.minute != 0:
            return jsonify({"error": "Os agendamentos devem ser feitos em horários cheios (ex: 09:00, 10:00)."}), 400

        # ==============================================================
        # 🔒 NOVAS REGRAS DE NEGÓCIO: BLOQUEIO DE DIAS E HORÁRIOS
        # ==============================================================
        dia_semana = data_selecionada.weekday() # 0 = Segunda, 6 = Domingo
        hora = data_selecionada.hour

        # Bloqueia Segunda-feira (dia 0)
        if dia_semana == 0:
            return jsonify({"error": "O Spa é fechado às segundas-feiras para manutenção. Por favor, escolha de terça a domingo."}), 400
        
        # Bloqueia horários fora da janela 09:00 - 20:00
        if not (9 <= hora <= 20):
            return jsonify({"error": "O horário de atendimento é das 09:00 às 20:00."}), 400
        # ==============================================================

        usuario_existe = (
            supabase.table("usuarios")
            .select("email")
            .eq("email", email)
            .execute()
        )
        if not usuario_existe.data:
            return jsonify({"error": "Usuário Inexistente. Crie uma conta antes de agendar."}), 404

        conflito = (
            supabase.table("agendamentos")
            .select("id")
            .eq("data_atendimento", data_atendimento_str)
            .neq("status", "Cancelado")
            .execute()
        )
        if conflito.data:
            return jsonify({"error": "Este horário já está reservado por outro cliente. Por favor, escolha outra opção."}), 409

        supabase.table("agendamentos").insert(
            {
                "email_cliente": email,
                "servico_id": servico_id,
                "data_atendimento": data_atendimento_str,
                "status": "Pendente",
            }
        ).execute()

        servico_atual = (
            supabase.table("servico")
            .select("contratos")
            .eq("id", servico_id)
            .execute()
        )
        if servico_atual.data:
            contratos_atuais = servico_atual.data[0].get("contratos", 0) or 0
            supabase.table("servico").update(
                {"contratos": contratos_atuais + 1}
            ).eq("id", servico_id).execute()

        return jsonify({"message": "Agendamento realizado com sucesso!"}), 201
    except Exception as e:
        print(f"❌ Erro ao processar agendamento: {e}")
        return jsonify({"error": "Erro ao salvar o agendamento no banco."}), 500


@app.route("/api/meus-agendamentos", methods=["GET"])
def meus_agendamentos():
    email = request.args.get("email")
    if not email:
        return jsonify({"error": "E-mail do usuário não informado."}), 400

    try:
        resposta = (
            supabase.table("agendamentos")
            .select(
                "id, data_atendimento, status, avaliacao, servico_id, servico(tipo, valor)"
            )
            .eq("email_cliente", email)
            .execute()
        )
        return jsonify(resposta.data), 200
    except Exception as e:
        print(f"❌ Erro ao buscar agendamentos: {e}")
        return (
            jsonify({"error": "Erro ao carregar a lista de agendamentos."}),
            500,
        )

    
@app.route('/api/horarios-ocupados', methods=['GET'])
def horarios_ocupados():
    try:
        # Busca todas as datas já reservadas que não foram canceladas
        res = supabase.table("agendamentos").select("data_atendimento").neq("status", "Cancelado").execute()
        # Retorna uma lista só com os textos das datas e horas
        ocupados = [ag["data_atendimento"] for ag in res.data]
        return jsonify(ocupados), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/agendamentos/<int:id>", methods=["PUT"])
def alterar_horario(id):
    dados = request.json or {}
    nova_data_str = dados.get("data")

    if not nova_data_str:
        return jsonify({"error": "Nova data/hora é obrigatória."}), 400

    try:
        nova_data = datetime.fromisoformat(nova_data_str)
        agora = datetime.now(nova_data.tzinfo) if nova_data.tzinfo else datetime.now()

        if nova_data < agora:
            return jsonify({"error": "A nova data não pode estar no passado."}), 400

        if nova_data.minute != 0:
            return jsonify({"error": "Agendamentos apenas em horários cheios."}), 400

        conflito = (
            supabase.table("agendamentos")
            .select("id")
            .eq("data_atendimento", nova_data_str)
            .neq("id", id)
            .neq("status", "Cancelado")
            .execute()
        )
        if conflito.data:
            return jsonify({"error": "Este horário já está reservado por outro cliente."}), 409

        supabase.table("agendamentos").update(
            {"data_atendimento": nova_data_str}
        ).eq("id", id).execute()

        return jsonify({"message": "Horário atualizado com sucesso!"}), 200
    except Exception as e:
        print(f"❌ Erro ao alterar horário: {e}")
        return jsonify({"error": "Erro interno ao reagendar."}), 500


@app.route("/api/agendamentos/<int:id>", methods=["DELETE"])
def cancelar_agendamento(id):
    try:
        supabase.table("agendamentos").update({"status": "Cancelado"}).eq("id", id).execute()
        return jsonify({"message": "Agendamento cancelado com sucesso."}), 200
    except Exception as e:
        print(f"❌ Erro ao cancelar agendamento: {e}")
        return jsonify({"error": "Erro ao cancelar o agendamento."}), 500


@app.route("/api/agendamentos/<int:id>/avaliar", methods=["POST"])
def avaliar_agendamento(id):
    dados = request.json or {}
    avaliacao = dados.get("avaliacao")

    if avaliacao not in ["Bom", "Médio", "Ruim"]:
        return jsonify({"error": "Avaliação inválida."}), 400

    try:
        supabase.table("agendamentos").update({"avaliacao": avaliacao}).eq("id", id).execute()
        return jsonify({"message": "Avaliação registrada!"}), 200
    except Exception as e:
        print(f"❌ Erro ao avaliar agendamento: {e}")
        return jsonify({"error": "Erro ao salvar avaliação."}), 500


# ==============================================================================
# --- 👑 ROTAS DE ADMINISTRAÇÃO E ESTOQUE ---
# ==============================================================================


@app.route("/api/admin/agendamentos", methods=["GET"])
def admin_agendamentos():
    admin_email = request.args.get("admin_email")
    if not admin_email:
        return jsonify({"error": "Email do administrador não informado."}), 400

    try:
        admin_check = supabase.table("usuarios").select("is_admin").eq("email", admin_email).execute()
        if not admin_check.data or not admin_check.data[0].get("is_admin"):
            return jsonify({"error": "Acesso não autorizado."}), 403

        resposta = supabase.table("agendamentos").select("id, email_cliente, data_atendimento, status, avaliacao, servico(tipo, valor)").execute()
        return jsonify(resposta.data), 200
    except Exception as e:
        print(f"❌ Erro no admin agendamentos: {e}")
        return jsonify({"error": "Erro ao buscar dados globais."}), 500


@app.route("/api/admin/agendamentos/<int:id>/concluir", methods=["POST"])
def concluir_agendamento(id):
    dados = request.json or {}
    admin_email = dados.get("admin_email")

    if not admin_email:
        return jsonify({"error": "Email do administrador é obrigatório."}), 400

    try:
        admin_check = supabase.table("usuarios").select("is_admin").eq("email", admin_email).execute()
        if not admin_check.data or not admin_check.data[0].get("is_admin"):
            return jsonify({"error": "Acesso não autorizado."}), 403

        # ADICIONADO: Puxando também o servico_id para sabermos qual foi o serviço feito
        agendamento = supabase.table("agendamentos").select("email_cliente, status, servico_id").eq("id", id).execute()
        if not agendamento.data:
            return jsonify({"error": "Agendamento não encontrado."}), 404

        cliente_email = agendamento.data[0].get("email_cliente")
        servico_id = agendamento.data[0].get("servico_id")

        supabase.table("agendamentos").update({"status": "Concluido"}).eq("id", id).execute()

        # ADICIONADO: Buscando o valor do serviço e calculando 10%
        pontos_ganhos = 0
        if servico_id:
            servico = supabase.table("servico").select("valor").eq("id", servico_id).execute()
            if servico.data:
                valor_servico = float(servico.data[0].get("valor", 0))
                pontos_ganhos = int(valor_servico * 0.10) # 10% do valor

        cliente = supabase.table("usuarios").select("pontos").eq("email", cliente_email).execute()
        if cliente.data:
            pontos_atuais = cliente.data[0].get("pontos", 0) or 0
            # ATUALIZADO: Agora soma os 10% calculados em vez do número 10 fixo
            supabase.table("usuarios").update({"pontos": pontos_atuais + pontos_ganhos}).eq("email", cliente_email).execute()

        return jsonify({"message": f"Agendamento concluído e {pontos_ganhos} pontos creditados ao cliente!"}), 200
    except Exception as e:
        print(f"❌ Erro ao concluir agendamento: {e}")
        return jsonify({"error": "Erro ao concluir o serviço."}), 500


@app.route("/api/usuario/pontos", methods=["GET"])
def obter_pontos():
    email = request.args.get("email")
    if not email:
        return jsonify({"error": "Email é obrigatório."}), 400

    try:
        cliente = supabase.table("usuarios").select("pontos").eq("email", email).execute()
        if cliente.data:
            pontos = cliente.data[0].get("pontos", 0) or 0
            return jsonify({"pontos": pontos}), 200
        return jsonify({"error": "Usuário não encontrado."}), 404
    except Exception as e:
        print(f"❌ Erro ao buscar pontos: {e}")
        return jsonify({"error": "Erro ao buscar os pontos do usuário."}), 500    


@app.route("/api/admin/usuarios", methods=["GET"])
def admin_usuarios():
    admin_email = request.args.get("admin_email")
    if not admin_email:
        return jsonify({"error": "Email do administrador não informado."}), 400

    try:
        admin_check = supabase.table("usuarios").select("is_admin").eq("email", admin_email).execute()
        if not admin_check.data or not admin_check.data[0].get("is_admin"):
            return jsonify({"error": "Acesso não autorizado."}), 403

        usuarios_res = supabase.table("usuarios").select("id, nome, email").execute()
        agendamentos_res = supabase.table("agendamentos").select("email_cliente, status, servico(valor)").execute()

        lista_final = []
        agendamentos_dados = agendamentos_res.data or []

        for user in usuarios_res.data or []:
            user_email = user.get("email")
            user_agends = [a for a in agendamentos_dados if a.get("email_cliente") == user_email]

            total = len(user_agends)
            pendentes = len([a for a in user_agends if a.get("status") == "Pendente"])
            concluidos = len([a for a in user_agends if a.get("status") == "Concluido"])
            cancelados = len([a for a in user_agends if a.get("status") == "Cancelado"])

            gastos = sum(
                float(a.get("servico", {}).get("valor", 0) or 0)
                for a in user_agends
                if a.get("status") == "Concluido" and a.get("servico")
            )

            lista_final.append({
                "nome": user.get("nome"),
                "email": user_email,
                "total": total,
                "pendentes": pendentes,
                "concluidos": concluidos,
                "cancelados": cancelados,
                "gastos": gastos
            })

        return jsonify(lista_final), 200
    except Exception as e:
        print(f"❌ Erro ao listar usuários no admin: {e}")
        return jsonify({"error": "Erro interno ao carregar relatório de clientes."}), 500


@app.route("/api/estoque", methods=["GET"])
def listar_estoque():
    try:
        res = supabase.table("estoque").select("*").order("id").execute()
        return jsonify(res.data), 200
    except Exception as e:
        print(f"❌ Erro ao listar estoque: {e}")
        return jsonify({"error": "Erro ao carregar o estoque."}), 500


@app.route("/api/estoque", methods=["POST"])
def adicionar_estoque():
    dados = request.json or {}
    nome = dados.get("nome")
    quantidade = dados.get("quantidade", 0)
    quantidade_minima = dados.get("quantidade_minima", 5)
    unidade = dados.get("unidade", "un")

    if not nome:
        return jsonify({"error": "O nome do produto é obrigatório."}), 400

    try:
        supabase.table("estoque").insert({
            "nome": nome,
            "quantidade": quantidade,
            "quantidade_minima": quantidade_minima,
            "unidade": unidade
        }).execute()

        return jsonify({"message": "Item adicionado ao estoque!"}), 201
    except Exception as e:
        print(f"❌ Erro ao inserir item no estoque: {e}")
        return jsonify({"error": "Erro ao cadastrar o produto no estoque."}), 500


@app.route("/api/estoque/<int:id>", methods=["PUT"])
def atualizar_estoque(id):
    dados = request.json or {}
    quantidade = dados.get("quantidade")

    if quantidade is None:
        return jsonify({"error": "Quantidade é obrigatória."}), 400

    try:
        supabase.table("estoque").update({"quantidade": quantidade}).eq("id", id).execute()
        return jsonify({"message": "Estoque atualizado!"}), 200
    except Exception as e:
        print(f"❌ Erro ao atualizar item do estoque: {e}")
        return jsonify({"error": "Erro ao alterar quantidade."}), 500


@app.route("/api/estoque/<int:id>", methods=["DELETE"])
def deletar_estoque(id):
    try:
        supabase.table("estoque").delete().eq("id", id).execute()
        return jsonify({"message": "Item removido do estoque!"}), 200
    except Exception as e:
        print(f"❌ Erro ao deletar item do estoque: {e}")
        return jsonify({"error": "Erro ao remover item do estoque."}), 500


@app.route("/api/chat", methods=["POST"])
def chat_assistente():
    dados = request.json or {}
    mensagem = dados.get("mensagem", "")

    if not mensagem:
        return jsonify({"error": "Mensagem vazia."}), 400

    msg_lc = mensagem.lower()
    if any(k in msg_lc for k in ["dor", "costas", "tenso", "tensão", "muscular"]):
        resposta = "Para alívio de tensões profundas e dores musculares, recomendo nossa Massagem Terapêutica ou Massagem com Pedras Quentes. Elas ajudam a relaxar a musculatura acumulada pelo estresse."
    elif any(k in msg_lc for k in ["estresse", "cansaço", "cansado", "ansiedade", "mente"]):
        resposta = "Se você está buscando desacelerar a mente e renovar as energias, recomendo nossa Aromaterapia com Óleos Essenciais ou o Banho de Imersão Panaceia."
    elif any(k in msg_lc for k in ["pele", "rosto", "facial", "esfoliação"]):
        resposta = "Para cuidados estéticos e revitalização da pele, nossa Limpeza de Pele Profunda e o Ritual Facial Anti-aging são as opções ideais!"
    else:
        resposta = "Sinta-se à vontade para explorar nosso catálogo completo de serviços de bem-estar. Se precisar de uma recomendação específica para dores, estresse ou cuidados faciais, é só me chamar!"

    return jsonify({"resposta": resposta}), 200


if __name__ == '__main__':
    # Pega a porta definida pelo Render (ou usa 5000 como padrão para testes locais)
    port = int(os.environ.get('PORT', 5000))

    # O segredo é o host='0.0.0.0'
    app.run(host='0.0.0.0', port=port)