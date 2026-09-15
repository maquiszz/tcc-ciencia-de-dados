# Spa Panaceia

Aplicação Flask para apresentação de serviços, agendamentos, fidelidade,
vouchers, estoque e painéis administrativos. A aplicação usa PostgreSQL local
e não depende do Supabase.

## Arquitetura de produção

- Aplicação Flask servida pelo Gunicorn.
- PostgreSQL no container `postgres`.
- Aplicação e banco na rede Docker `postgres1_database`.
- Cloudflare/HTTPS na entrada pública.
- Um worker Gunicorn com oito threads, pool PostgreSQL e bloqueio adaptativo de
  requisições.
- Credenciais fornecidas em tempo de execução; nenhuma chave é copiada para a
  imagem Docker.

## Variáveis obrigatórias

Mantenha `database.env` somente no servidor e fora do Git:

```env
APP_ENV=production
FLASK_SECRET_KEY=<segredo-aleatorio-longo>

DB_HOST=postgres
DB_PORT=5432
DB_NAME=site_db
DB_USER=site_user
DB_PASSWORD=<senha-exclusiva-do-site>
DB_CONNECT_TIMEOUT=10
DB_POOL_MIN=1
DB_POOL_MAX=10

SPA_TIMEZONE=America/Sao_Paulo
SESSION_COOKIE_SECURE=true
SESSION_COOKIE_SAMESITE=Lax
SESSION_TTL_HOURS=12

CORS_ORIGINS=https://spapanaceia.com.br,https://www.spapanaceia.com.br
TRUSTED_PROXY_IP_HEADER=CF-Connecting-IP

BREVO_API_KEY=<chave-brevo>
BREVO_SENDER_NAME=Spa Panaceia
BREVO_SENDER_EMAIL=<remetente-verificado>

OPENAI_API_KEY=<chave-openai>
OPENAI_MODEL=gpt-4.1-mini
LOG_LEVEL=INFO
```

Não mantenha `SUPABASE_URL`, `SUPABASE_KEY` ou `service_role` depois da
migração. Chaves que tenham sido expostas devem ser revogadas e substituídas no
servidor.

## Atualização no ZimaOS

Execute na pasta do repositório. Ajuste o nome do container e a porta externa
caso a instalação use valores diferentes:

```bash
git pull --ff-only

TAG="$(date +%Y%m%d-%H%M%S)"
docker build --pull \
  -t "spa-panaceia:$TAG" \
  -t spa-panaceia:latest \
  .

docker stop spa-panaceia 2>/dev/null || true
docker rm spa-panaceia 2>/dev/null || true

docker run -d \
  --name spa-panaceia \
  --restart unless-stopped \
  --network postgres1_database \
  --env-file database.env \
  -p 5000:5000 \
  spa-panaceia:latest
```

O build copia apenas o runtime. O `database.env`, backups, datasets e arquivos
de desenvolvimento não entram na imagem.

## Validação após o deploy

```bash
curl -fsS https://spapanaceia.com.br/api/health
docker ps --filter name=spa-panaceia
docker inspect --format '{{json .State.Health}}' spa-panaceia
docker logs --tail 100 spa-panaceia
```

Uma resposta saudável contém:

```json
{
  "status": "ok",
  "database": "ok",
  "restricao_agenda": true
}
```

Teste também:

1. Listagem de serviços.
2. Login de cliente e administrador.
3. Criação, remarcação e cancelamento de um horário de teste.
4. Conclusão administrativa e crédito de pontos.
5. Resgate e utilização de voucher.
6. Exportação de dados do perfil.

## Proteção contra horários duplicados

O backend usa lock transacional do PostgreSQL e grava agendamento e contador do
serviço na mesma transação. Ele também tenta criar este índice automaticamente:

```sql
CREATE UNIQUE INDEX IF NOT EXISTS uq_agendamentos_horario_ativo
ON public.agendamentos (data_atendimento)
WHERE status IS DISTINCT FROM 'Cancelado';
```

Se `/api/health` retornar `"restricao_agenda": false`, o usuário da aplicação
não possui permissão para criar índices. Execute o SQL acima uma única vez com o
usuário administrador do PostgreSQL:

```bash
docker exec -it postgres psql -U postgres -d site_db
```

Antes de criar o índice, confirme que não existem horários ativos duplicados:

```sql
SELECT data_atendimento, COUNT(*)
FROM public.agendamentos
WHERE status IS DISTINCT FROM 'Cancelado'
GROUP BY data_atendimento
HAVING COUNT(*) > 1;
```

O resultado precisa estar vazio.

## Backup diário do PostgreSQL

Crie no ZimaOS uma tarefa diária, por exemplo às 03:00, com o bloco abaixo. O
backup fica fora do container e os arquivos com mais de 14 dias são removidos:

```bash
BACKUP_DIR=/DATA/AppData/spa-panaceia/backups
mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

ARQUIVO="$BACKUP_DIR/spa-$(date +%Y%m%d-%H%M%S).dump"
docker exec postgres sh -lc \
  'pg_dump -U "${POSTGRES_USER:-postgres}" -d "${POSTGRES_DB:-site_db}" --format=custom --no-owner --no-privileges' \
  > "$ARQUIVO"

test -s "$ARQUIVO" || { rm -f "$ARQUIVO"; exit 1; }
find "$BACKUP_DIR" -type f -name 'spa-*.dump' -mtime +14 -delete
```

Guarde também uma cópia periódica em outro disco ou equipamento. Um backup no
mesmo disco não protege contra falha física.

## Teste de restauração

Teste periodicamente em um banco separado; nunca restaure sobre produção para
fazer uma verificação:

```bash
docker exec postgres createdb -U postgres spa_restore_test
docker exec -i postgres pg_restore \
  -U postgres \
  -d spa_restore_test \
  --no-owner \
  --no-privileges \
  < /DATA/AppData/spa-panaceia/backups/<arquivo>.dump
```

Depois compare as quantidades das tabelas e remova o banco de teste somente
quando tiver certeza de que não é o banco de produção.

## Desenvolvimento local

```powershell
python -m pip install -r requirements.txt
$env:APP_ENV = "development"
python app.py
```

O PostgreSQL precisa estar acessível pelas variáveis `DB_*`. Em HTTP local,
defina `SESSION_COOKIE_SECURE=false`; em produção ela deve permanecer `true`.

## Checklist de segurança

- `database.env` não versionado e fora da imagem.
- Chaves OpenAI e Brevo diferentes das que já foram expostas.
- Senha exclusiva para o usuário `site_user`.
- Porta 5432 acessível somente pela rede Docker.
- HTTPS, HSTS, CSP e cookies seguros ativos.
- Backup diário e restauração testada.
- `/api/health` monitorado.
- Imagens antigas mantidas por alguns dias para rollback.
