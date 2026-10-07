(() => {
    const storageKey = 'spa_privacy_notice_seen_v1';

    function inicializarAvisoPrivacidade() {
        const aviso = document.getElementById('aviso-cookies');
        const botao = document.getElementById('aviso-cookies-fechar');
        if (!aviso || !botao) return;

        try {
            if (window.localStorage.getItem(storageKey) === '1') {
                aviso.hidden = true;
            }
        } catch (_) {
            // O aviso continua disponível mesmo quando o armazenamento local é bloqueado.
        }

        botao.addEventListener('click', () => {
            aviso.hidden = true;
            try {
                window.localStorage.setItem(storageKey, '1');
            } catch (_) {
                // A escolha vale para esta visualização mesmo sem persistência local.
            }
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', inicializarAvisoPrivacidade, { once: true });
    } else {
        inicializarAvisoPrivacidade();
    }
})();
