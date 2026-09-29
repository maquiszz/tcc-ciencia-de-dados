# Spa Panaceia

Aplicação Flask para apresentação de serviços, agendamentos, fidelidade,
vouchers, estoque e painéis administrativos. A aplicação pode operar com o
PostgreSQL local ou com o Supabase. O failover entre eles é opt-in para evitar
que duas bases independentes recebam gravações divergentes.

## Arquitetura de produção

- Aplicação Flask servida pelo Gunicorn.
- PostgreSQL no container `postgres`.
- Aplicação e banco na rede Docker `postgres1_database`.
- Supabase configurável como banco principal ou contingência pela Data API.
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

# Banco principal e tempo de espera antes de testar novamente um banco offline.
DATABASE_PRIMARY=postgres
DATABASE_FAILOVER_ENABLED=false
DB_FAILOVER_COOLDOWN_SECONDS=60


# Contingência Supabase. A service role fica somente no backend.
SUPABASE_URL=https://SEU-PROJETO.supabase.co
SUPABASE_SERVICE_ROLE_KEY=<service-role-do-servidor>

SPA_TIMEZONE=America/Sao_Paulo
SESSION_COOKIE_SECURE=true
SESSION_COOKIE_SAMESITE=Lax
SESSION_TTL_HOURS=12

CORS_ORIGINS=https://spapanaceia.com.br,https://www.spapanaceia.com.br
TRUSTED_HOSTS=spapanaceia.com.br,www.spapanaceia.com.br,localhost,127.0.0.1
TRUSTED_PROXY_IP_HEADER=CF-Connecting-IP
# CIDRs dos proxies que realmente chegam ao container (mantenha a lista atualizada).
TRUSTED_PROXY_IPS=<cidr-do-proxy-1>,<cidr-do-proxy-2>
# Opcional: IPs/redes a bloquear diretamente no backend.
BLOCKED_IP_CIDRS=<ip-ou-cidr-bloqueado>
# Opcional: restringe o site aos países informados; só funciona atrás do proxy confiável.
# ALLOWED_COUNTRIES=BR
# Opcional: cabeçalho definido pelo proxy confiável para tráfego VPN/anônimo.
# TRUSTED_VPN_HEADER=X-Spa-VPN

BREVO_API_KEY=<chave-brevo>
BREVO_SENDER_NAME=Spa Panaceia
BREVO_SENDER_EMAIL=<remetente-verificado>
SPA_ALERT_EMAIL=<caixa-da-equipe-que-recebe-alertas>

OPENAI_API_KEY=<chave-openai>
OPENAI_MODEL=gpt-4.1-mini
LOG_LEVEL=INFO
```

Nunca coloque `SUPABASE_SERVICE_ROLE_KEY` no HTML, JavaScript ou Git. O backend
aceita temporariamente o nome legado `service_role`, mas o nome recomendado é
`SUPABASE_SERVICE_ROLE_KEY`. Chaves que tenham sido expostas devem ser revogadas
e substituídas no servidor.

O backend não usa uma chave publicável/anon como credencial do servidor. O IP de
cliente vindo de `TRUSTED_PROXY_IP_HEADER` só é aceito quando `REMOTE_ADDR` do
proxy corresponde a um endereço em `TRUSTED_PROXY_IPS`; sem essa lista, o app
usa o IP da conexão direta. Restrinja o acesso ao container para que somente o
proxy esperado possa alcançá-lo e mantenha os CIDRs desse proxy atualizados. Se
o cabeçalho for `CF-Connecting-IP`, use as redes de origem publicadas pelo
Cloudflare ([faixas oficiais](https://developers.cloudflare.com/fundamentals/concepts/cloudflare-ip-addresses/))
e aplique a mesma restrição de entrada no firewall do servidor. O Cloudflare
documenta o cabeçalho [CF-Connecting-IP](https://developers.cloudflare.com/fundamentals/reference/http-headers/)
como enviado da edge para a origem.

`BLOCKED_IP_CIDRS` aceita IPs individuais ou redes CIDR separados por vírgula e
bloqueia-os em todas as rotas. `ALLOWED_COUNTRIES=BR` é opcional e recusa países
fora da lista, inclusive quando a localização não puder ser confirmada; antes de
ativá-lo, configure `TRUSTED_PROXY_IPS` e habilite o cabeçalho `CF-IPCountry` no
Cloudflare. Para reduzir a velocidade de tráfego VPN, `TRUSTED_VPN_HEADER` só é
aceito vindo de um proxy confiável; configure uma regra no proxy/WAF para definir
esse cabeçalho para VPNs identificadas. O Cloudflare mantém listas de VPNs e
anonimizadores, mas a disponibilidade das listas gerenciadas depende do plano
([documentação oficial](https://developers.cloudflare.com/waf/tools/lists/managed-lists/)).

`TRUSTED_HOSTS` restringe os valores de `Host` aceitos pelo Flask. Inclua todos
os domínios públicos usados e os hosts locais necessários para o healthcheck;
não use curingas amplos. O serviço retorna `400` para hosts fora da lista.
Em produção, o cookie de sessão permanece `Secure` independentemente do host
usado na requisição. Para testes HTTP em IP privado, use `APP_ENV=development`
somente em ambiente local isolado.

O bloqueio adaptativo e os limites de autenticação vivem na memória de um
processo. Em produção o Gunicorn está configurado com um worker; mantenha também
as regras de WAF/limite de requisições e a proteção contra acesso direto à origem
no proxy/firewall. Um bloqueio dentro do Flask não consegue absorver um ataque
que sature a conexão antes de chegar ao container.

## Agenda, lista de espera e auditoria

O PostgreSQL local cria as tabelas operacionais ao iniciar. Na Data API do
Supabase, execute a migração abaixo uma vez no SQL Editor. As tabelas são novas;
nenhuma reserva ou conta existente é alterada. O RLS fica habilitado e sem
políticas para usuários públicos; somente o backend com service role deve acessá-las.

```sql
CREATE TABLE IF NOT EXISTS public.agenda_lista_espera (
    id BIGSERIAL PRIMARY KEY,
    email_cliente TEXT NOT NULL,
    servico_id BIGINT NOT NULL,
    data_atendimento TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    status TEXT NOT NULL DEFAULT 'Aguardando'
        CHECK (status IN ('Aguardando', 'Notificando', 'Notificado', 'Cancelado')),
    criado_em TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    notificado_em TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_agenda_lista_espera_email_horario
    ON public.agenda_lista_espera (email_cliente, data_atendimento)
    WHERE status IN ('Aguardando', 'Notificando', 'Notificado');
CREATE INDEX IF NOT EXISTS ix_agenda_lista_espera_horario_status
    ON public.agenda_lista_espera (data_atendimento, status);

CREATE TABLE IF NOT EXISTS public.agenda_auditoria (
    id BIGSERIAL PRIMARY KEY,
    agendamento_id BIGINT NOT NULL,
    ator_email TEXT NOT NULL,
    ator_tipo TEXT NOT NULL CHECK (ator_tipo IN ('admin', 'cliente')),
    acao TEXT NOT NULL,
    data_anterior TEXT,
    data_nova TEXT,
    status_anterior TEXT,
    status_novo TEXT,
    criado_em TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE public.agenda_auditoria
    ADD COLUMN IF NOT EXISTS status_anterior TEXT,
    ADD COLUMN IF NOT EXISTS status_novo TEXT;
CREATE INDEX IF NOT EXISTS ix_agenda_auditoria_agendamento_data
    ON public.agenda_auditoria (agendamento_id, criado_em DESC);

ALTER TABLE public.agenda_lista_espera ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.agenda_auditoria ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.agenda_lista_espera, public.agenda_auditoria FROM PUBLIC, anon, authenticated;
GRANT ALL ON public.agenda_lista_espera, public.agenda_auditoria TO service_role;
GRANT USAGE, SELECT ON SEQUENCE public.agenda_lista_espera_id_seq, public.agenda_auditoria_id_seq TO service_role;
```

`SPA_ALERT_EMAIL` deve ser uma caixa monitorada pela equipe. Se não for definida,
os alertas usam `BREVO_SENDER_EMAIL`. Ambos dependem de `BREVO_API_KEY` válido.

## Bloqueio de horários

O PostgreSQL local cria a tabela de bloqueios ao iniciar. Para o Supabase,
execute o SQL abaixo no SQL Editor uma vez antes de habilitar essa função. Ele
cria a tabela sem alterar reservas ou serviços existentes. Caso a tabela antiga
`servicos_favoritos` ainda exista no Supabase, ela e seus dados permanecem
intactos e não são mais utilizados pelo aplicativo. O gatilho
confere o bloqueio dentro da própria transação de reserva/remarcação; o outro
gatilho impede bloquear uma hora que tenha reserva ativa, inclusive sob acesso
simultâneo. O índice único também impede duas reservas ativas no mesmo horário.
O mesmo lock é usado nas duas direções.

```sql
CREATE TABLE IF NOT EXISTS public.agenda_bloqueios (
    id BIGSERIAL PRIMARY KEY,
    data_atendimento TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    motivo TEXT NOT NULL,
    criado_por TEXT NOT NULL,
    criado_em TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_agenda_bloqueios_horario
    ON public.agenda_bloqueios (data_atendimento);
-- Garante uma única reserva ativa por horário mesmo sob requisições simultâneas.
CREATE UNIQUE INDEX IF NOT EXISTS uq_agendamentos_horario_ativo
    ON public.agendamentos (data_atendimento)
    WHERE status IS DISTINCT FROM 'Cancelado';

CREATE OR REPLACE FUNCTION public.verificar_bloqueio_ao_reservar()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = public, pg_temp AS $$
BEGIN
    IF NEW.status IS DISTINCT FROM 'Cancelado' THEN
        PERFORM pg_advisory_xact_lock(hashtextextended(
            to_char(NEW.data_atendimento, 'YYYY-MM-DD"T"HH24:MI'), 0));
        IF EXISTS (
            SELECT 1 FROM public.agenda_bloqueios
            WHERE data_atendimento = NEW.data_atendimento
        ) THEN
            RAISE EXCEPTION 'AGENDA_BLOQUEADA' USING ERRCODE = 'P0001';
        END IF;
        IF EXISTS (
            SELECT 1 FROM public.agendamentos
            WHERE data_atendimento = NEW.data_atendimento
              AND id IS DISTINCT FROM NEW.id
              AND status IS DISTINCT FROM 'Cancelado'
        ) THEN
            RAISE EXCEPTION 'AGENDA_RESERVADA' USING ERRCODE = 'P0001';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgname = 'trg_agendamentos_respeitar_bloqueio'
          AND tgrelid = 'public.agendamentos'::regclass
    ) THEN
        CREATE TRIGGER trg_agendamentos_respeitar_bloqueio
        BEFORE INSERT OR UPDATE OF data_atendimento, status
        ON public.agendamentos FOR EACH ROW
        EXECUTE FUNCTION public.verificar_bloqueio_ao_reservar();
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION public.verificar_reserva_ao_bloquear()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = public, pg_temp AS $$
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended(
        to_char(NEW.data_atendimento, 'YYYY-MM-DD"T"HH24:MI'), 0));
    IF EXISTS (
        SELECT 1 FROM public.agendamentos
        WHERE data_atendimento = NEW.data_atendimento
          AND status IS DISTINCT FROM 'Cancelado'
    ) THEN
        RAISE EXCEPTION 'AGENDA_RESERVADA' USING ERRCODE = 'P0001';
    END IF;
    RETURN NEW;
END;
$$;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgname = 'trg_agenda_bloqueios_sem_reservas'
          AND tgrelid = 'public.agenda_bloqueios'::regclass
    ) THEN
        CREATE TRIGGER trg_agenda_bloqueios_sem_reservas
        BEFORE INSERT OR UPDATE OF data_atendimento
        ON public.agenda_bloqueios FOR EACH ROW
        EXECUTE FUNCTION public.verificar_reserva_ao_bloquear();
    END IF;
END;
$$;

ALTER TABLE public.agenda_bloqueios ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.agenda_bloqueios FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.verificar_bloqueio_ao_reservar(),
    public.verificar_reserva_ao_bloquear() FROM PUBLIC, anon, authenticated;
GRANT ALL ON public.agenda_bloqueios TO service_role;
GRANT USAGE, SELECT ON SEQUENCE public.agenda_bloqueios_id_seq TO service_role;
```

Repetir uma reserva usa `POST /api/agendar` com o serviço anterior e um horário
novo. O administrador pode listar, criar e remover
bloqueios em `/api/admin/bloqueios`. Na criação, envie
`{"data_inicio":"2026-10-01T09:00","data_fim":"2026-10-01T10:00","motivo":"Pausa da equipe"}`;
o fim é exclusivo. `GET /api/horarios-ocupados?detalhes=1` separa as horas
bloqueadas, para não oferecer lista de espera nelas.

## Comportamento dos dois bancos

- `DATABASE_FAILOVER_ENABLED=false` (padrão): só o banco primário recebe
  leituras e gravações. Use este modo quando as bases não forem réplicas
  sincronizadas; se ele cair, o site sinaliza indisponibilidade em vez de gravar
  silenciosamente na outra base.
- `DATABASE_FAILOVER_ENABLED=true`: permite contingência entre bancos. Só ative
  se houver procedimento para manter/reconciliar os dados; o failover não replica
  nem sincroniza as bases.
- `DATABASE_PRIMARY=postgres`: tenta o PostgreSQL primeiro e usa o Supabase se
  `DATABASE_FAILOVER_ENABLED=true` e o banco local estiver offline.
- `DATABASE_PRIMARY=supabase`: seleciona o Supabase como origem primária.
- Com failover habilitado, leituras com falha de conexão podem ser repetidas no
  banco de contingência. Antes de uma gravação, o backend confirma que o banco
  ativo responde. Uma
  gravação que falha depois de enviada não é repetida automaticamente, evitando
  reservas ou movimentações duplicadas.
- Erros de negócio, como horário ocupado, voucher inválido ou saldo
  insuficiente, nunca provocam failover.

Mesmo com failover habilitado, as duas bases precisam ter o mesmo esquema,
funções RPC e dados coerentes. Faça backup e reconciliação antes de mudar o banco
primário ou reativar uma base que ficou offline.

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
  "database_backend": "postgres",
  "databases": {
    "postgres": {"status": "ok"},
    "supabase": {"status": "ok"}
  },
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

No PostgreSQL, se `/api/health` retornar `"restricao_agenda": false`, o índice
único não existe. Confira duplicatas e crie-o uma única vez com um usuário que
tenha permissão de DDL. No Supabase, `restricao_agenda` retorna `null` e
`restricao_agenda_verificavel` retorna `false`: a Data API não permite ao
healthcheck confirmar triggers/índices. Nesse caso, confira no SQL Editor se o
índice e os dois triggers descritos acima foram instalados; não interprete
`null` como proteção ausente nem como confirmação de que ela existe.

Para o PostgreSQL, o comando abaixo abre o `psql` com o usuário administrador
do container (substitua usuário/banco se forem diferentes):

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

Pelo menos um banco precisa estar configurado: PostgreSQL pelas variáveis
`DB_*`, ou Supabase por `SUPABASE_URL` e uma chave de servidor. Em HTTP local,
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
