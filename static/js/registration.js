(() => {
    const timeoutMs = 15000;
    const passwordPattern = /^(?=.*[a-z])(?=.*[A-Z])(?=.*[@$!%*?&#,.]).{8,}$/;
    const emailStorageKey = 'email_em_verificacao';
    let emailToVerify = '';

    const element = (id) => document.getElementById(id);
    const setStatus = (message, kind = '', busy = false) => {
        const status = element('status');
        status.className = kind;
        status.textContent = message;
        status.setAttribute('aria-busy', String(busy));
    };

    async function requestWithTimeout(url, options) {
        const controller = new AbortController();
        let timer;
        try {
            return await Promise.race([
                fetch(url, { ...options, signal: controller.signal }),
                new Promise((_, reject) => {
                    timer = window.setTimeout(() => {
                        const error = new Error('A solicitação demorou mais que o esperado.');
                        error.name = 'TimeoutError';
                        reject(error);
                        controller.abort();
                    }, timeoutMs);
                })
            ]);
        } finally {
            window.clearTimeout(timer);
        }
    }

    async function readResponse(response) {
        let payload;
        try {
            payload = await response.json();
        } catch (_) {
            const error = new Error('O servidor retornou uma resposta inválida.');
            error.name = 'InvalidResponseError';
            throw error;
        }
        if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
            const error = new Error('O servidor retornou uma resposta inválida.');
            error.name = 'InvalidResponseError';
            throw error;
        }
        if (!response.ok) throw new Error(payload.error || 'Não foi possível concluir. Tente novamente.');
        return payload;
    }

    async function enviarCadastro(event) {
        event.preventDefault();
        const form = event.currentTarget;
        const button = element('botaoCadastro');
        if (button.disabled) return;
        const nome = element('nome').value.trim();
        const email = element('email').value.trim().toLowerCase();
        const senha = element('senha').value;

        if (!form.reportValidity()) return;
        if (!passwordPattern.test(senha) || senha.length > 256) {
            setStatus('Use uma senha com pelo menos 8 caracteres, uma letra maiúscula, uma minúscula e um símbolo.', 'erro');
            element('senha').focus();
            return;
        }

        button.disabled = true;
        button.setAttribute('aria-busy', 'true');
        button.textContent = 'Enviando…';
        setStatus('Enviando seus dados…', '', true);
        try {
            const response = await requestWithTimeout('/cadastrar', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ nome, email, senha })
            });
            const result = await readResponse(response);
            if (typeof result.email !== 'string' || result.email.trim().toLowerCase() !== email) {
                const error = new Error('O servidor não confirmou o e-mail do cadastro.');
                error.name = 'InvalidResponseError';
                throw error;
            }
            emailToVerify = result.email;
            try { window.localStorage.setItem(emailStorageKey, emailToVerify); } catch (_) { /* O fluxo segue na página atual se o armazenamento estiver bloqueado. */ }
            element('emailVerificacao').textContent = emailToVerify;
            element('senha').value = '';
            form.hidden = true;
            element('verificacaoForm').hidden = false;
            setStatus('Cadastro iniciado. Confira seu e-mail e digite o código para ativar a conta.', 'sucesso');
            element('codigoVerificacao').focus();
        } catch (error) {
            const message = error.name === 'TimeoutError'
                ? 'Não recebemos a confirmação. O cadastro pode ter sido iniciado; confira seu e-mail antes de tentar novamente. Seus dados foram mantidos.'
                : error.name === 'InvalidResponseError'
                    ? 'A resposta do servidor não pôde ser confirmada. O cadastro pode ter sido iniciado; confira seu e-mail antes de tentar novamente. Seus dados foram mantidos.'
                : error.name === 'AbortError'
                    ? 'A conexão foi interrompida. Seus dados foram mantidos; confira seu e-mail antes de tentar novamente.'
                    : error.message || 'Não foi possível conectar. Seus dados foram mantidos; tente novamente.';
            setStatus(message, 'erro');
        } finally {
            button.disabled = false;
            button.removeAttribute('aria-busy');
            button.textContent = 'Confirmar cadastro';
        }
    }

    async function confirmarEmail(event) {
        event.preventDefault();
        const form = event.currentTarget;
        const button = element('botaoVerificacao');
        if (button.disabled) return;
        const codigo = element('codigoVerificacao').value.trim();
        if (!form.reportValidity()) return;
        if (!emailToVerify || !/^\d{6}$/.test(codigo)) {
            setStatus('Informe o código de 6 números enviado ao seu e-mail.', 'erro');
            element('codigoVerificacao').focus();
            return;
        }

        button.disabled = true;
        button.setAttribute('aria-busy', 'true');
        button.textContent = 'Confirmando…';
        setStatus('Validando seu código…', '', true);
        try {
            const response = await requestWithTimeout('/api/validar-codigo', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ email: emailToVerify, codigo })
            });
            await readResponse(response);
            try {
                if (window.localStorage.getItem(emailStorageKey) === emailToVerify) window.localStorage.removeItem(emailStorageKey);
            } catch (_) { /* A confirmação no servidor continua válida sem acesso ao armazenamento. */ }
            form.hidden = true;
            element('loginAfterVerify').hidden = false;
            setStatus('E-mail confirmado. Sua conta está ativa; agora você pode entrar.', 'sucesso');
            element('loginAfterVerify').focus();
        } catch (error) {
            const message = error.name === 'TimeoutError'
                ? 'A confirmação demorou e o resultado é incerto. Seus dados foram mantidos; confira a mensagem antes de reenviar o código.'
                : error.name === 'InvalidResponseError'
                    ? 'A resposta do servidor não pôde ser confirmada. A ativação pode ter sido concluída; confira se já consegue entrar antes de reenviar o código.'
                : error.name === 'AbortError'
                    ? 'A conexão foi interrompida. O código continua preenchido; confira sua conexão e tente novamente.'
                    : error.message || 'Não foi possível validar o código. Seus dados foram mantidos.';
            setStatus(message, 'erro');
        } finally {
            button.disabled = false;
            button.removeAttribute('aria-busy');
            button.textContent = 'Confirmar e-mail';
        }
    }

    async function reenviarCodigo() {
        const button = element('botaoReenviar');
        if (button.disabled || !emailToVerify) return;
        button.disabled = true;
        button.setAttribute('aria-busy', 'true');
        setStatus('Solicitando outro código…', '', true);
        try {
            const response = await requestWithTimeout('/api/reenviar-codigo', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ email: emailToVerify })
            });
            const result = await readResponse(response);
            setStatus(result.message || 'Se a conta estiver aguardando ativação, um novo código será enviado.', 'sucesso');
        } catch (error) {
            const message = error.name === 'TimeoutError'
                ? 'Não recebemos a confirmação do reenvio. Confira seu e-mail antes de solicitar outro código.'
                : error.name === 'InvalidResponseError'
                    ? 'A resposta do servidor não pôde ser confirmada. Confira seu e-mail antes de solicitar outro código.'
                : error.message || 'Não foi possível solicitar outro código. Tente novamente mais tarde.';
            setStatus(message, 'erro');
        } finally {
            button.disabled = false;
            button.removeAttribute('aria-busy');
        }
    }

    element('cadastroForm')?.addEventListener('submit', enviarCadastro);
    element('verificacaoForm')?.addEventListener('submit', confirmarEmail);
    element('botaoReenviar')?.addEventListener('click', reenviarCodigo);

    try {
        const emailSalvo = window.localStorage.getItem(emailStorageKey);
        if (typeof emailSalvo === 'string' && emailSalvo.length <= 254 && emailSalvo.includes('@')) {
            emailToVerify = emailSalvo;
            element('emailVerificacao').textContent = emailToVerify;
            element('cadastroForm').hidden = true;
            element('verificacaoForm').hidden = false;
            setStatus('Retomamos a confirmação do e-mail. Digite o código recebido ou solicite outro.', '');
        }
    } catch (_) { /* O cadastro permanece disponível se o armazenamento local estiver bloqueado. */ }
})();
