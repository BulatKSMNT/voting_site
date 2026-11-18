// Keep the scoreboard presentation across automatic tour changes.
(() => {
    const button = document.getElementById('screen-toggle');
    const root = document.documentElement;
    const params = new URLSearchParams(location.search);
    let presentation = params.get('screen') === '1';
    let requestedFullscreen = false;
    const browserFullscreen = () => window.innerHeight >= window.screen.height - 2 &&
        Math.abs(window.outerHeight - window.innerHeight) < 8;
    function render() {
        const active = presentation || Boolean(document.fullscreenElement) || browserFullscreen();
        root.classList.toggle('scoreboard-screen', active);
        button.textContent = browserFullscreen() && !presentation && !document.fullscreenElement
            ? 'F11 — выход из полного экрана' : active ? 'Выйти из полного экрана' : 'На весь экран';
        button.setAttribute('aria-pressed', String(active));
    }
    function remember() {
        const url = new URL(location.href);
        if (presentation) url.searchParams.set('screen', '1');
        else url.searchParams.delete('screen');
        history.replaceState(null, '', url);
        render();
    }
    button.addEventListener('click', async () => {
        if (browserFullscreen() && !presentation && !document.fullscreenElement) return;
        if (presentation || document.fullscreenElement) {
            presentation = false;
            requestedFullscreen = false;
            if (document.fullscreenElement) await document.exitFullscreen();
            remember();
            return;
        }
        presentation = true;
        remember();
        if (root.requestFullscreen) {
            try {
                await root.requestFullscreen();
                requestedFullscreen = true;
            } catch (_) {
                // Presentation also works where the browser disallows fullscreen.
            }
        }
    });
    document.addEventListener('fullscreenchange', () => {
        if (!document.fullscreenElement && requestedFullscreen) {
            presentation = false;
            requestedFullscreen = false;
            remember();
        } else render();
    });
    document.addEventListener('keydown', event => {
        if (event.key === 'Escape' && !document.fullscreenElement) {
            presentation = false;
            remember();
        }
    });
    window.addEventListener('resize', render);
    render();
})();
