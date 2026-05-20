from database.conexao import cadastrar_usuario

print("--- Testando Conexão com o Supabase ---")

# Teste de inserção (mude os dados para testar)
cadastrar_usuario(nome="Seu Amigo", email="amigo@email.com")