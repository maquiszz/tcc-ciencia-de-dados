from flask import Flask, render_template, request
import os
from database.conexao import cadastrar_usuario # Importa a função que criamos antes

app = Flask(__name__)

# Rota principal: Abre a página do site com o formulário limpo
@app.route("/")
def home():
    return render_template("index.html")

# Rota de cadastro: Recebe os dados do formulário e joga no Supabase
@app.route("/cadastrar", methods=["POST"])
def cadastrar():
    # Pega o que o usuário digitou nas caixinhas do site
    nome_digitado = request.form.get("nome")
    email_digitado = request.form.get("email")
    
    # Envia automaticamente para a função do Supabase em database/conexao.py
    resultado = cadastrar_usuario(nome=nome_digitado, email=email_digitado)
    
    if resultado:
        msg = f"🎉 {nome_digitado} subiu automaticamente pro Supabase!"
    else:
        msg = "❌ Erro ao enviar. Verifique se o e-mail já existe ou olhe o terminal."

    # Recarrega a página mostrando a mensagem de sucesso ou erro
    return render_template("index.html", mensagem=msg)

import os

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)