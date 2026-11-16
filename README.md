# Spa Panaceia

Aplicação Flask para apresentação de serviços, agendamentos, fidelidade,
vouchers, estoque e painéis administrativos. A aplicação pode operar com o
PostgreSQL local ou com o Supabase. O failover entre eles é opt-in para evitar
que duas bases independentes recebam gravações divergentes.

## Arquitetura de produção

- O deploy ativo verificado está no Render, com a aplicação Flask servida pelo
  Gunicorn. O `Procfile` define um worker e oito threads.
- O Supabase é o banco ativo confirmado pela resposta mais recente do healthcheck.
- O PostgreSQL local em Docker/ZimaOS é uma alternativa operacional; não é a
  configuração ativa confirmada. O `Dockerfile` e as instruções de ZimaOS abaixo
  documentam essa alternativa.
- PostgreSQL e Supabase são bancos independentes: não são réplicas nem recebem
  sincronização automática. Mantenha o failover desativado e não o habilite sem
  um plano testado de reconciliação dos dados.
- O backend lê credenciais do ambiente de execução. Segredos nunca devem ser
  incluídos neste README, em outros arquivos versionados ou no Git.

## Estado do deploy verificado

Verificação pública pré-publicação feita em 07/10/2026:

- `https://spa-panaceia.onrender.com/` respondeu HTTP 200 com TLS válido. O
  healthcheck e o catálogo responderam HTTP 200; o healthcheck informou
  Supabase como banco ativo.
- `https://spapanaceia.com.br/` e `https://www.spapanaceia.com.br/` responderam
  HTTP 530 do Cloudflare. A conexão TLS até a borda Cloudflare foi válida, mas
  os domínios próprios não serviram o site.
- `https://panaceia.onrender.com/` pertence ao serviço Render separado chamado
  `PANACEIA`; a leitura expirou após 25 segundos. Ele não é o serviço
  `Spa Panaceia`, e esse resultado não confirma o estado do serviço separado.
- No código que ainda estava no ar durante essa verificação,
  `/robots.txt` e `/privacidade` respondiam 404. As rotas novas entram em vigor
  somente depois da publicação deste commit.

Esse retrato não confirma DNS, origem Cloudflare ou configuração atual do
Render. Confira os endpoints novamente depois da publicação.

## Frontend público, privacidade e publicação de ativos

- A página principal é servida pelo Flask a partir de `database/servicos.html`;
  cadastro, privacidade e página 404 são templates em `templates/`. O Dockerfile
  precisa copiar essas duas pastas para a imagem.
- Os fontes permanecem legíveis. Depois de editá-los, gere as cópias otimizadas
  com `python -m pip install -r requirements-build.txt` e
  `python scripts/build_frontend.py`; inclua fontes e arquivos `.min.html`,
  `.min.css` e `.min.js` juntos.
  A etapa usa uma dependência só de build, sem adicionar minificador ao runtime.
- O catálogo usa dimensões explícitas e carregamento tardio para as fotos dos
  tratamentos. O favicon usa o SVG leve já servido pelo aplicativo. As fotos
  externas mantêm seus provedores e qualidade atuais.
- Analytics permanece desativado porque não há ID nem preferência de consentimento
  confirmados. O aviso de cookies descreve apenas armazenamento funcional visto
  no código; não representa declaração de conformidade legal.
- `PUBLIC_SITE_URL` é opcional e só gera canonical, `og:url` e entradas de sitemap
  quando a URL HTTPS aponta para um host já confiável pelo backend. Não configure
  essa variável até confirmar o domínio canônico e seu estado público. Sem essa
  confirmação, `/sitemap.xml` responde 503 deliberadamente e `robots.txt` não
  anuncia um sitemap incompleto.
- O aviso em `/privacidade` descreve o comportamento observado e lista as
  informações legais e operacionais que ainda precisam de confirmação humana.

## Variáveis obrigatórias

No Render, configure as variáveis de execução no serviço. Para manter o backend
apontando explicitamente para o banco ativo, use `DATABASE_PRIMARY=supabase` e
`DATABASE_FAILOVER_ENABLED=false`; configure `SUPABASE_URL`,
`SUPABASE_SERVICE_ROLE_KEY` e `FLASK_SECRET_KEY` no ambiente do Render, sem
copiar seus valores para documentação ou Git.

O bloco abaixo é um exemplo com PostgreSQL local para a alternativa Docker/ZimaOS,
não a configuração ativa confirmada do Render. Mantenha `database.env` somente
no servidor local, fora do Git e fora da imagem Docker. Os valores entre sinais
de menor/maior são placeholders; nunca os substitua por segredos neste README.

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

# Alertas operacionais e de segurança no Telegram
TELEGRAM_BOT_TOKEN=<token-do-bot-telegram>
TELEGRAM_CHAT_ID=<id-do-chat-telegram>

OPENAI_API_KEY=<chave-openai>
OPENAI_MODEL=gpt-4.1-mini
LOG_LEVEL=INFO
```

Nunca coloque valores de `SUPABASE_SERVICE_ROLE_KEY`, `FLASK_SECRET_KEY`,
`DB_PASSWORD`, `BREVO_API_KEY`, `TELEGRAM_BOT_TOKEN`, `OPENAI_API_KEY` ou qualquer outro segredo no
README, em arquivos versionados, no HTML/JavaScript ou no Git. Configure-os
somente no ambiente do provedor ou no arquivo local ignorado pelo Git. O backend
aceita temporariamente o nome legado `service_role`; o nome recomendado é
`SUPABASE_SERVICE_ROLE_KEY`. Segredos expostos devem ser revogados e substituídos
no ambiente de execução.

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
- `DATABASE_FAILOVER_ENABLED=true`: permite contingência entre bancos. Não
  habilite sem um plano testado para manter e reconciliar os dados; o failover
  não replica nem sincroniza as bases.
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

## Atualização e validação em produção no ZimaOS

Como `database.env` fica fora do Git e da imagem Docker (por segurança), sempre
confirme que `TELEGRAM_BOT_TOKEN` e `TELEGRAM_CHAT_ID` estão presentes no
`database.env` do servidor ZimaOS e sem quebras de linha Windows (`\r\n`):

```bash
cd /DATA/AppData/projetos/tcc-ciencia-de-dados && git stash && git pull origin main && sudo docker --config /DATA/.docker image build --no-cache -t tcc-panaceia . && (sudo docker stop panaceia-spa; sudo docker rm panaceia-spa) && sudo docker run -d --name panaceia-spa --restart unless-stopped -p 5000:5000 --env-file /DATA/AppData/projetos/tcc-ciencia-de-dados/database.env tcc-panaceia
```

Para confirmar que o container subiu com as variáveis do Telegram ativas e que o
alerta de inicialização foi entregue:

```bash
curl -fsS http://127.0.0.1:5000/api/health
sudo docker logs --tail 50 panaceia-spa
```

O endpoint `/api/health` retornará `"telegram_configurado": true`, e os logs do
container exibirão `INFO:spa.telegram:Notificações do Telegram configuradas` e
`INFO:spa.telegram:Alerta entregue ao Telegram com sucesso`.

## Estado operacional

A produção usa Render + Supabase; o PostgreSQL local/ZimaOS está indisponível.
As bases não são réplicas: mantenha `DATABASE_FAILOVER_ENABLED=false`. O último
healthcheck registrado apontou Supabase `ok`; ele não confirma índices ou
triggers. Verifique `/api/health` e os objetos da agenda no Supabase após mudanças.
O domínio oficial ainda teve erros Cloudflare na última verificação; confirme-o
antes de considerá-lo ativo.

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

## Crédito transacional ao concluir atendimentos

O administrador conclui uma reserva por `POST /api/admin/agendamentos/<id>/concluir`.
O backend valida sessão, papel administrativo, origem e CSRF; o navegador não
informa saldo nem quantidade de pontos. No Supabase, a função abaixo bloqueia a
linha do agendamento, calcula `trunc(valor / 10)` a partir de `servico.valor`,
soma os pontos em `usuarios` e altera o status na mesma transação. No PostgreSQL
local, o adaptador executa essas mesmas etapas em uma transação com `FOR UPDATE`.
Uma reserva já `Concluido` retorna zero pontos, inclusive se sua conclusão for
anterior a esta mudança: não há crédito retroativo automático.

Após exportar as definições das tabelas, índices, gatilhos, funções e permissões
envolvidos, este é o SQL de instalação da função no Supabase. Não altera registros
existentes nem muda tabelas ou políticas RLS. Execute-o como uma única transação;
publique o backend atualizado somente depois de confirmar que a função existe e
que apenas `service_role` (além do proprietário) pode executá-la.

```sql
BEGIN;

CREATE OR REPLACE FUNCTION public.concluir_agendamento_creditar_pontos(p_agendamento_id integer)
RETURNS TABLE(resultado text, pontos integer)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO pg_catalog, public
AS $function$
DECLARE
    v_email text;
    v_servico_id integer;
    v_status text;
    v_valor numeric;
    v_pontos integer;
BEGIN
    SELECT a.email_cliente, a.servico_id, a.status
      INTO v_email, v_servico_id, v_status
      FROM public.agendamentos AS a
     WHERE a.id = p_agendamento_id
     FOR UPDATE;

    IF NOT FOUND THEN
        RETURN QUERY SELECT 'nao_encontrado'::text, 0;
        RETURN;
    END IF;
    IF v_status = 'Concluido' THEN
        RETURN QUERY SELECT 'ja_concluido'::text, 0;
        RETURN;
    END IF;
    IF v_status IS DISTINCT FROM 'Pendente' THEN
        RETURN QUERY SELECT 'status_invalido'::text, 0;
        RETURN;
    END IF;

    SELECT s.valor INTO v_valor
      FROM public.servico AS s
     WHERE s.id = v_servico_id;
    IF NOT FOUND OR v_valor IS NULL OR v_valor < 0 THEN
        RAISE EXCEPTION 'Servico sem valor valido para credito de pontos';
    END IF;
    v_pontos := trunc(v_valor / 10)::integer;

    UPDATE public.usuarios AS u
       SET pontos = coalesce(u.pontos, 0) + v_pontos
     WHERE u.email = v_email;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Cliente nao encontrado para credito de pontos';
    END IF;

    UPDATE public.agendamentos
       SET status = 'Concluido'
     WHERE id = p_agendamento_id AND status = 'Pendente';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Falha ao concluir agendamento apos credito';
    END IF;

    RETURN QUERY SELECT 'concluido'::text, v_pontos;
END;
$function$;

REVOKE ALL ON FUNCTION public.concluir_agendamento_creditar_pontos(integer)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.concluir_agendamento_creditar_pontos(integer)
    TO service_role;
NOTIFY pgrst, 'reload schema';

COMMIT;
```

Instalação conferida em 01/10/2026 no Supabase ativo: a primeira definição foi
criada, mas o teste sintético detectou que `coalesce(pontos, 0)` era ambíguo com
a coluna de retorno `pontos` (erro PostgreSQL 42702). A transação de teste foi
revertida. A função foi substituída pelo SQL corrigido acima, qualificando
`u.pontos`; a execução retornou sucesso. A inspeção de privilégios confirmou
`EXECUTE` para `service_role` e negou `anon` e `authenticated`. Um teste com
usuário, serviço e agendamento sintéticos, encerrado com `ROLLBACK`, confirmou
19 pontos na primeira conclusão, zero na repetição e saldo/status coerentes.
Após o rollback, nenhum registro sintético permaneceu e as contagens de
agendamentos e usuários continuaram iguais. O backup estrutural pré-mudança foi
exportado em CSV fora do repositório, sem dados de clientes nem credenciais.

O SQL acima cria apenas uma função e sua permissão de execução. Uma falha em
qualquer gravação aborta toda a chamada. Duas conclusões simultâneas da mesma
reserva são serializadas pelo bloqueio de linha; atualizações de saldo são somas
atômicas, inclusive quando há resgate de voucher simultâneo. Se for necessário
reverter a instalação antes de usar a nova versão do backend, remova apenas essa
função; depois de publicar o backend, planeje a reversão junto com o código para
não reintroduzir a atualização separada de status e saldo.

## Cancelamento automático de agendamentos vencidos

No Supabase, um job `pg_cron` roda a cada 15 minutos e muda para `Cancelado` os
agendamentos ainda `Pendente` cuja data já terminou em `America/Sao_Paulo`.
Atualização e auditoria são atômicas; a função é idempotente e não pode ser
chamada pelos papéis da API. O job guarda a data de ativação para não alterar
reservas antigas automaticamente. O PostgreSQL local está indisponível e não
tem scheduler configurado.

SQL aplicado no Supabase:

```sql
BEGIN;

CREATE EXTENSION IF NOT EXISTS pg_cron;

ALTER TABLE public.agenda_auditoria
    DROP CONSTRAINT agenda_auditoria_ator_tipo_check;
ALTER TABLE public.agenda_auditoria
    ADD CONSTRAINT agenda_auditoria_ator_tipo_check
    CHECK (ator_tipo IN ('admin', 'cliente', 'sistema'));

CREATE OR REPLACE FUNCTION public.cancelar_agendamentos_vencidos(p_desde date)
RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO pg_catalog, public
AS $function$
DECLARE
    v_hoje date := (statement_timestamp() AT TIME ZONE 'America/Sao_Paulo')::date;
    v_agendamento record;
    v_data_anterior text;
    v_cancelados integer := 0;
BEGIN
    IF p_desde IS NULL THEN
        RAISE EXCEPTION 'Data de ativação obrigatória';
    END IF;

    FOR v_agendamento IN
        SELECT a.id
          FROM public.agendamentos AS a
         WHERE a.status = 'Pendente'
           AND a.data_atendimento < v_hoje::timestamp
           AND a.data_atendimento >= p_desde::timestamp
         ORDER BY a.data_atendimento, a.id
         FOR UPDATE OF a SKIP LOCKED
    LOOP
        UPDATE public.agendamentos AS a
           SET status = 'Cancelado'
         WHERE a.id = v_agendamento.id
           AND a.status = 'Pendente'
        RETURNING a.data_atendimento::text INTO v_data_anterior;

        IF FOUND THEN
            INSERT INTO public.agenda_auditoria (
                agendamento_id, ator_email, ator_tipo, acao,
                data_anterior, status_anterior, status_novo
            ) VALUES (
                v_agendamento.id, 'sistema', 'sistema',
                'cancelamento_automatico', v_data_anterior,
                'Pendente', 'Cancelado'
            );
            v_cancelados := v_cancelados + 1;
        END IF;
    END LOOP;

    RETURN v_cancelados;
END;
$function$;

REVOKE ALL ON FUNCTION public.cancelar_agendamentos_vencidos(date)
    FROM PUBLIC, anon, authenticated, service_role;

DO $schedule$
DECLARE
    v_ativacao date := (statement_timestamp() AT TIME ZONE 'America/Sao_Paulo')::date;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM cron.job
         WHERE jobname = 'spa-cancelar-agendamentos-vencidos'
    ) THEN
        PERFORM cron.schedule(
            'spa-cancelar-agendamentos-vencidos',
            '*/15 * * * *',
            format(
                'SELECT public.cancelar_agendamentos_vencidos(%L::date)',
                v_ativacao
            )
        );
    END IF;
END;
$schedule$;

COMMIT;
```

Verificação rápida do job e das execuções:

```sql
SELECT jobid, schedule, active, username, command
FROM cron.job WHERE jobname = 'spa-cancelar-agendamentos-vencidos';

SELECT status, start_time, return_message
FROM cron.job_run_details
WHERE jobid = (SELECT jobid FROM cron.job
               WHERE jobname = 'spa-cancelar-agendamentos-vencidos')
ORDER BY start_time DESC LIMIT 5;
```

Verifique também se há pendências vencidas desde a ativação:

```sql
SELECT count(*)
FROM public.agendamentos
WHERE status = 'Pendente'
  AND data_atendimento < (now() AT TIME ZONE 'America/Sao_Paulo')::date
  AND data_atendimento >= DATE '2026-10-07';
```

Na instalação, a consulta encontrou 16 reservas antigas pendentes e uma entrada
vencida na lista de espera; nenhuma foi alterada. Esses casos precisam de
revisão manual antes de qualquer acerto retroativo. O primeiro ciclo do job
terminou com sucesso e não encontrou reservas elegíveis para cancelar; ainda
falta observar um cancelamento real pelo cron.

Para reverter, remova somente o job e a função:

```sql
BEGIN;
SELECT cron.unschedule(jobid)
FROM cron.job
WHERE jobname = 'spa-cancelar-agendamentos-vencidos';
DROP FUNCTION IF EXISTS public.cancelar_agendamentos_vencidos(date);
COMMIT;
```

Mantenha `pg_cron` se outros jobs o utilizarem. Não restaure a restrição antiga
da auditoria enquanto houver registros com `ator_tipo='sistema'`.

## Movimentação atômica de estoque

O endpoint administrativo `PUT /api/estoque/<id>/movimentar` envia quantidade,
ação e um `operacao_id` UUID. A interface mantém essa chave no armazenamento
local do navegador até receber sucesso ou um erro definitivo do cliente; depois
de timeout ou erro de servidor, a equipe pode repetir a mesma ação e o backend
reenvia a mesma chave. O banco guarda o resultado da operação na mesma transação
do saldo. Reutilizar a chave com outro item ou delta é rejeitado.

No Supabase, uma única função executa o incremento/decremento aritmético e só
atualiza o saldo se o resultado ficar entre zero e o limite de `integer`. O
bloqueio de linha do `UPDATE` serializa movimentações diferentes do mesmo item;
a chave única serializa repetições. A constraint também impede saldo negativo
por outras gravações diretas. A tabela de idempotência tem RLS habilitado, sem
políticas e sem privilégios diretos; somente `service_role` pode executar a
RPC. No PostgreSQL local, o adaptador usa uma tabela equivalente, deduplicação
transacional e atualização SQL sob o lock da linha. A substituição manual de
saldo exige a quantidade anteriormente observada para recusar gravações com
valor desatualizado. A chave vale dentro de cada banco; como os bancos não são
sincronizados, mantenha o failover entre eles desligado até existir um plano de
reconciliação. Não apague automaticamente registros de
`estoque_movimentacoes`: sem uma janela máxima para novas tentativas, remover a
chave pode permitir que uma repetição antiga movimente o saldo outra vez.

Antes da mudança, foram exportadas 41 definições estruturais das tabelas,
índices, gatilhos, funções e permissões relevantes para um CSV fora do
repositório, sem dados de clientes ou credenciais. Em 02/10/2026, após confirmar
que não havia saldo negativo e que os objetos ainda não existiam, foi aplicada
a transação abaixo no projeto Supabase ativo:

```sql
BEGIN;

ALTER TABLE public.estoque
    ADD CONSTRAINT estoque_quantidade_nao_negativa CHECK (quantidade >= 0);

CREATE TABLE public.estoque_movimentacoes (
    operacao_id uuid PRIMARY KEY,
    item_id bigint NOT NULL,
    delta integer NOT NULL CHECK (delta <> 0),
    resultado jsonb NOT NULL,
    criado_em timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE public.estoque_movimentacoes ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE public.estoque_movimentacoes
    FROM PUBLIC, anon, authenticated, service_role;

CREATE OR REPLACE FUNCTION public.movimentar_estoque(
    p_item_id bigint,
    p_delta integer,
    p_operacao_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO pg_catalog, public
AS $function$
DECLARE
    v_inserted integer;
    v_previous_item_id bigint;
    v_previous_delta integer;
    v_result jsonb;
BEGIN
    IF p_operacao_id IS NULL OR p_item_id IS NULL OR p_delta IS NULL OR p_delta = 0 THEN
        RAISE EXCEPTION 'ESTOQUE_ARGUMENTO_INVALIDO' USING ERRCODE = '22023';
    END IF;

    INSERT INTO public.estoque_movimentacoes (operacao_id, item_id, delta, resultado)
    VALUES (p_operacao_id, p_item_id, p_delta, '{}'::jsonb)
    ON CONFLICT (operacao_id) DO NOTHING;
    GET DIAGNOSTICS v_inserted = ROW_COUNT;

    IF v_inserted = 0 THEN
        SELECT m.item_id, m.delta, m.resultado
          INTO v_previous_item_id, v_previous_delta, v_result
          FROM public.estoque_movimentacoes AS m
         WHERE m.operacao_id = p_operacao_id
         FOR UPDATE;

        IF v_previous_item_id IS DISTINCT FROM p_item_id
           OR v_previous_delta IS DISTINCT FROM p_delta THEN
            RAISE EXCEPTION 'ESTOQUE_OPERACAO_DIVERGENTE' USING ERRCODE = 'P0001';
        END IF;
        IF v_result IS NULL OR v_result = '{}'::jsonb THEN
            RAISE EXCEPTION 'ESTOQUE_OPERACAO_INCOMPLETA' USING ERRCODE = 'P0001';
        END IF;
        RETURN v_result || jsonb_build_object('repetida', true);
    END IF;

    UPDATE public.estoque AS e
       SET quantidade = (e.quantidade::bigint + p_delta)::integer
     WHERE e.id = p_item_id
       AND e.quantidade::bigint + p_delta BETWEEN 0 AND 2147483647
    RETURNING jsonb_build_object(
        'id', e.id,
        'nome', e.nome,
        'quantidade', e.quantidade,
        'quantidade_minima', e.quantidade_minima,
        'unidade', e.unidade
    ) INTO v_result;

    IF v_result IS NULL THEN
        IF EXISTS (SELECT 1 FROM public.estoque AS e WHERE e.id = p_item_id) THEN
            RAISE EXCEPTION 'ESTOQUE_INSUFICIENTE' USING ERRCODE = 'P0001';
        ELSE
            RAISE EXCEPTION 'ESTOQUE_NAO_ENCONTRADO' USING ERRCODE = 'P0001';
        END IF;
    END IF;

    v_result := v_result || jsonb_build_object('repetida', false);
    UPDATE public.estoque_movimentacoes AS m
       SET resultado = v_result
     WHERE m.operacao_id = p_operacao_id;

    RETURN v_result;
END;
$function$;

REVOKE ALL ON FUNCTION public.movimentar_estoque(bigint, integer, uuid)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.movimentar_estoque(bigint, integer, uuid)
    TO service_role;
NOTIFY pgrst, 'reload schema';

COMMIT;
```

A instalação retornou sucesso. A verificação confirmou a constraint validada,
RLS habilitado sem políticas, ausência de leitura direta da tabela de operações
e execução da função apenas por `service_role` (negada a `anon` e
`authenticated`). Um teste no Supabase usou um item e duas chaves UUID
sintéticas dentro de um bloco com rollback: a primeira chamada elevou 5 para 8,
a repetição não alterou o saldo, e a retirada insuficiente foi rejeitada sem
gravar a chave. O teste foi revertido; uma consulta posterior confirmou zero
linhas sintéticas e zero saldos negativos. O SQL de teste não alterou dados
reais. Mocks isolados cobriram conclusão de pontos normal/repetida, duas
conclusões concorrentes e falha no crédito; para estoque, cobriram deltas
simultâneos, duas chamadas com a mesma chave, repetição após timeout simulado e
saldo insuficiente. Esses mocks validam a integração do adaptador, não substituem
um teste de concorrência contra duas sessões reais do Supabase. Esse teste não
foi executado porque a interface SQL disponível não permite coordenar duas
sessões mantendo os dados sintéticos reversíveis.

## Backups

O procedimento `pg_dump` do antigo servidor local foi removido porque esse
servidor está indisponível. Confirme no painel Supabase que os backups e a
retenção atendem à necessidade do Spa; este README não confirma o estado atual.

## Desenvolvimento local

```powershell
python -m pip install -r requirements.txt
$env:APP_ENV = "development"
python app.py
```

Pelo menos um banco precisa estar configurado: PostgreSQL pelas variáveis
`DB_*`, ou Supabase por `SUPABASE_URL` e uma chave de servidor. Em HTTP local,
defina `SESSION_COOKIE_SECURE=false`; em produção ela deve permanecer `true`.

## Migração de endurecimento de segurança (`migrations/001_security_hardening.sql`)

O script idempotente `migrations/001_security_hardening.sql` adiciona colunas não
destrutivas à tabela `public.usuarios` para expiração persistente e contagem de
tentativas de recuperação de senha, contagem de tentativas de OTP e versão de
sessão por usuário (`token_recuperacao_expira_em`, `token_recuperacao_tentativas`,
`codigo_otp_tentativas`, `sessao_versao`):

- **PostgreSQL local**: aplicado automaticamente no boot por `ensure_schema()`, ou
  manualmente via `psql -h <host> -U <user> -d <db> -f migrations/001_security_hardening.sql`.
- **Supabase**: cole e execute o conteúdo de `migrations/001_security_hardening.sql`
  no **SQL Editor** do painel do Supabase. O backend possui fallback compatível
  (`atualizar_usuario_compativel`) caso a migração ainda não tenha sido executada.

## Checklist de segurança

- Nenhum segredo incluído no README ou no Git; `database.env` local não
  versionado e fora da imagem.
- Segredos de produção configurados no ambiente do Render; segredos expostos
  revogados e substituídos.
- Senha exclusiva para o usuário `site_user`.
- HTTPS, HSTS, CSP e cookies seguros ativos.
- `/api/health` monitorado.
- Imagens antigas mantidas por alguns dias para rollback.
