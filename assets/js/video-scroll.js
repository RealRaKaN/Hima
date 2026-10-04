(() => {
    const videoBox = document.getElementById('video-box');
    const heroText = document.getElementById('hero-text');
    const heroOverlay = document.getElementById('hero-overlay');
    const hero = document.getElementById('hero');
    const video = videoBox?.querySelector('video');
    const fullscreenButton = document.getElementById('video-fullscreen-btn');
    const videoActions = document.getElementById('video-actions');
    const closeButton = document.getElementById('video-close-btn');
    const navbar = document.getElementById('navbar');
    let previousScrollY = window.scrollY;

    if (!videoBox || !heroText || !heroOverlay || !hero || !video) return;

    const isIOS = /iP(hone|od|ad)/.test(navigator.userAgent) ||
        (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);

    const lerp = (a, b, t) => a + (b - a) * t;
    const ease = (t) => t * t * (3 - 2 * t); // smoothstep
    let fakeFullscreen = false;

    const getTarget = () => {
        const vh = window.innerHeight;
        const heroTop = hero.getBoundingClientRect().top;
        const range = Math.max(hero.offsetHeight - vh, 1);
        return Math.min(Math.max(-heroTop / (range * 0.85), 0), 1);
    };

    const render = (rawP) => {
        if (fakeFullscreen) return;

        const vh = window.innerHeight;
        const vw = document.documentElement.clientWidth;
        const isMobile = window.matchMedia('(max-width: 768px)').matches;
        const p = ease(rawP);

        const navH = navbar ? navbar.offsetHeight : 0;
        const textH = heroText.offsetHeight;
        const baseScale = isMobile ? 0.84 : 0.75;
        const endScale = Math.min(baseScale, (vh * 0.40) / textH);
        const scale = lerp(1, endScale, p);

        const textTopStart = (vh - textH) / 2;
        const textTopEnd = navH + 8;
        const textTop = lerp(textTopStart, textTopEnd, p);
        const textBottomEnd = textTopEnd + textH * endScale;

        const gap = 16;
        const bottomPad = 64;
        const availH = Math.max(vh - textBottomEnd - gap - bottomPad, 120);
        const maxW = vw * (isMobile ? 0.95 : 0.60);
        const targetW = Math.min(maxW, availH * 16 / 9);
        const targetH = targetW * 9 / 16;
        const videoTopEnd = textBottomEnd + gap;

        heroText.style.position = 'absolute';
        heroText.style.left = '0';
        heroText.style.right = '0';
        heroText.style.top = `${textTop}px`;
        heroText.style.transformOrigin = 'top center';
        heroText.style.transform = `scale(${scale})`;
        heroText.style.opacity = 1;
        heroOverlay.style.opacity = Math.max(1 - p * 1.3, 0);

        videoBox.style.left = '50%';
        videoBox.style.transform = 'translateX(-50%)';
        videoBox.style.width = `${lerp(vw, targetW, p)}px`;
        videoBox.style.height = `${lerp(vh, targetH, p)}px`;
        videoBox.style.top = `${lerp(0, videoTopEnd, p)}px`;
        videoBox.style.borderRadius = `${p * (isMobile ? 12 : 20)}px`;
        videoBox.style.boxShadow = `0 20px 50px rgba(0,0,0,.8), 0 0 30px rgba(16,185,129,${p * 0.3})`;
        videoBox.classList.toggle('is-framed', p > 0.6);
        if (videoActions) {
            videoActions.style.top = `${lerp(0, videoTopEnd, p) + lerp(vh, targetH, p) + 12}px`;
        }
    };

    let current = getTarget();
    let raf = null;
    const tick = () => {
        const target = getTarget();
        current += (target - current) * 0.14;
        if (Math.abs(target - current) < 0.0005) current = target;
        try { render(current); } catch (e) { console.error(e); }
        raf = current !== target ? requestAnimationFrame(tick) : null;
    };
    const requestUpdate = () => { if (!raf) raf = requestAnimationFrame(tick); };

    const updateNavigation = () => {
        const y = window.scrollY;
        navbar?.classList.toggle('is-hidden', y > 80 && y > previousScrollY);
        previousScrollY = y;
    };

    window.addEventListener('scroll', () => {
        updateNavigation();
        requestUpdate();
    }, { passive: true });
    window.addEventListener('resize', requestUpdate);
    window.addEventListener('orientationchange', () => setTimeout(requestUpdate, 300));
    window.addEventListener('load', requestUpdate);
    document.fonts?.ready.then(requestUpdate);
    if ('ResizeObserver' in window) new ResizeObserver(requestUpdate).observe(heroText);

    const keepVideoPlaying = () => {
        if (!document.hidden && !video.ended) {
            video.muted = true;
            video.play().catch(() => {});
        }
    };
    video.addEventListener('pause', () => setTimeout(keepVideoPlaying, 100));
    ['fullscreenchange', 'webkitfullscreenchange', 'mozfullscreenchange', 'MSFullscreenChange']
        .forEach(ev => document.addEventListener(ev, () => setTimeout(keepVideoPlaying, 100)));
    document.addEventListener('visibilitychange', () => { if (!document.hidden) keepVideoPlaying(); });

    const fitFakeFullscreen = () => {
        if (!fakeFullscreen) return;
        videoBox.style.setProperty('height', `${window.innerHeight}px`, 'important');
    };
    window.addEventListener('resize', fitFakeFullscreen);
    window.addEventListener('orientationchange', () => setTimeout(fitFakeFullscreen, 300));

    const setFakeFullscreen = (on) => {
        fakeFullscreen = on;
        videoBox.classList.toggle('is-fake-fullscreen', on);
        document.documentElement.classList.toggle('video-fs-open', on);
        document.body.classList.toggle('video-fs-open', on);
        if (on) fitFakeFullscreen();
        else { videoBox.style.removeProperty('height'); requestUpdate(); }
        keepVideoPlaying();
    };

    const nativeElement = () => document.fullscreenElement || document.webkitFullscreenElement ||
        document.mozFullScreenElement || document.msFullscreenElement || null;
    const nativeExit = () => (document.exitFullscreen || document.webkitExitFullscreen ||
        document.mozCancelFullScreen || document.msExitFullscreen);
    const nativeRequest = () => (video.requestFullscreen || video.webkitRequestFullscreen ||
        video.mozRequestFullScreen || video.msRequestFullscreen);

    fullscreenButton?.addEventListener('click', async () => {
        try {
            if (nativeElement()) {
                const exit = nativeExit();
                if (exit) await exit.call(document);
                return;
            }
            const request = nativeRequest();
            if (isIOS || !request) setFakeFullscreen(true);
            else await request.call(video);
        } catch (e) {
            console.warn('Native full screen failed, using fallback.', e);
            setFakeFullscreen(true);
        }
    });
    closeButton?.addEventListener('click', () => setFakeFullscreen(false));
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') setFakeFullscreen(false);
    });

    keepVideoPlaying();
    render(current);
})();