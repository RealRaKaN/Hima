import { useEffect, useRef } from 'react';
import { Maximize, Images, GraduationCap } from 'lucide-react';
import { useLang } from '../context/LangContext';

interface HeroProps {
  onNavigateGallery: () => void;
}

export default function Hero({ onNavigateGallery }: HeroProps) {
  const { t } = useLang();

  const videoBoxRef = useRef<HTMLDivElement>(null);
  const heroTextRef = useRef<HTMLDivElement>(null);
  const heroOverlayRef = useRef<HTMLDivElement>(null);
  const heroRef = useRef<HTMLElement>(null);
  const videoActionsRef = useRef<HTMLDivElement>(null);
  const fullscreenBtnRef = useRef<HTMLButtonElement>(null);
  const closeBtnRef = useRef<HTMLButtonElement>(null);
  const navbarRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    navbarRef.current = document.getElementById('navbar') as HTMLElement;

    const videoBox = videoBoxRef.current;
    const heroText = heroTextRef.current;
    const heroOverlay = heroOverlayRef.current;
    const hero = heroRef.current;
    const videoActions = videoActionsRef.current;
    const fullscreenButton = fullscreenBtnRef.current;
    const closeButton = closeBtnRef.current;
    const navbar = navbarRef.current;
    const video = videoBox?.querySelector('video') as HTMLVideoElement | null;

    if (!videoBox || !heroText || !heroOverlay || !hero || !video) return;

    const isIOS =
      /iP(hone|od|ad)/.test(navigator.userAgent) ||
      (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);

    const lerp = (a: number, b: number, t: number) => a + (b - a) * t;
    const ease = (t: number) => t * t * (3 - 2 * t);
    let fakeFullscreen = false;
    let previousScrollY = window.scrollY;

    const getTarget = () => {
      const vh = window.innerHeight;
      const heroTop = hero.getBoundingClientRect().top;
      const range = Math.max(hero.offsetHeight - vh, 1);
      return Math.min(Math.max(-heroTop / (range * 0.85), 0), 1);
    };

    const render = (rawP: number) => {
      if (fakeFullscreen) return;
      const vh = window.innerHeight;
      const vw = document.documentElement.clientWidth;
      const isMobile = window.matchMedia('(max-width: 768px)').matches;
      const p = ease(rawP);
      const navH = navbar ? navbar.offsetHeight : 0;
      const textH = heroText.offsetHeight;
      const baseScale = isMobile ? 0.84 : 0.75;
      const endScale = Math.min(baseScale, (vh * 0.4) / textH);
      const scale = lerp(1, endScale, p);
      const textTopStart = (vh - textH) / 2;
      const textTopEnd = navH + 8;
      const textTop = lerp(textTopStart, textTopEnd, p);
      const textBottomEnd = textTopEnd + textH * endScale;
      const gap = 16;
      const bottomPad = 64;
      const availH = Math.max(vh - textBottomEnd - gap - bottomPad, 120);
      const maxW = vw * (isMobile ? 0.95 : 0.6);
      const targetW = Math.min(maxW, (availH * 16) / 9);
      const targetH = (targetW * 9) / 16;
      const videoTopEnd = textBottomEnd + gap;

      heroText.style.position = 'absolute';
      heroText.style.left = '0';
      heroText.style.right = '0';
      heroText.style.top = `${textTop}px`;
      heroText.style.transformOrigin = 'top center';
      heroText.style.transform = `scale(${scale})`;
      heroText.style.opacity = '1';
      heroOverlay.style.opacity = String(Math.max(1 - p * 1.3, 0));

      videoBox.style.left = '50%';
      videoBox.style.transform = 'translateX(-50%)';
      videoBox.style.width = `${lerp(vw, targetW, p)}px`;
      videoBox.style.height = `${lerp(vh, targetH, p)}px`;
      videoBox.style.top = `${lerp(0, videoTopEnd, p)}px`;
      videoBox.style.borderRadius = `${p * (isMobile ? 12 : 20)}px`;
      videoBox.style.boxShadow = `0 20px 50px rgba(0,0,0,.8), 0 0 30px rgba(16,185,129,${p * 0.3})`;

      const isFramed = p > 0.6;
      videoBox.classList.toggle('is-framed', isFramed);
      if (videoActions) {
        videoActions.style.top = `${lerp(0, videoTopEnd, p) + lerp(vh, targetH, p) + 12}px`;
        videoActions.classList.toggle('is-visible', isFramed);
      }
    };

    let current = getTarget();
    let raf: number | null = null;
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

    window.addEventListener('scroll', () => { updateNavigation(); requestUpdate(); }, { passive: true });
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

    const setFakeFullscreen = (on: boolean) => {
      fakeFullscreen = on;
      videoBox.classList.toggle('is-fake-fullscreen', on);
      document.documentElement.classList.toggle('video-fs-open', on);
      document.body.classList.toggle('video-fs-open', on);
      if (on) fitFakeFullscreen();
      else { videoBox.style.removeProperty('height'); requestUpdate(); }
      keepVideoPlaying();
    };

    const nativeEl = () =>
      document.fullscreenElement || (document as any).webkitFullscreenElement || null;
    const nativeExit = () => (document.exitFullscreen || (document as any).webkitExitFullscreen);
    const nativeRequest = () => (video.requestFullscreen || (video as any).webkitRequestFullscreen);

    fullscreenButton?.addEventListener('click', async () => {
      try {
        if (nativeEl()) { const exit = nativeExit(); if (exit) await exit.call(document); return; }
        const req = nativeRequest();
        if (isIOS || !req) setFakeFullscreen(true);
        else await req.call(video);
      } catch { setFakeFullscreen(true); }
    });
    closeButton?.addEventListener('click', () => setFakeFullscreen(false));
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') setFakeFullscreen(false); });

    keepVideoPlaying();
    render(current);
  }, []);

  return (
    <header id="hero" className="hero-container" ref={heroRef}>
      <div className="sticky-wrapper">
        <div className="video-transform-box" id="video-box" ref={videoBoxRef}>
          <video
            className="hero-video"
            autoPlay
            loop
            muted
            playsInline
            preload="auto"
            aria-label="Hima wildlife monitoring demonstration"
          >
            <source src={`${import.meta.env.BASE_URL}all-small.mp4`} type="video/mp4" />
            <source src={`${import.meta.env.BASE_URL}all.mp4`} type="video/mp4" />
          </video>
          <div className="hero-overlay" id="hero-overlay" ref={heroOverlayRef} />
          <button
            className="video-close-btn"
            id="video-close-btn"
            type="button"
            aria-label="Close full screen"
            ref={closeBtnRef}
          >
            ✕
          </button>
        </div>

        <div className="video-actions" id="video-actions" ref={videoActionsRef}>
          <button
            className="video-fullscreen-btn"
            id="video-fullscreen-btn"
            type="button"
            aria-label="Open video in full screen"
            ref={fullscreenBtnRef}
          >
            <Maximize size={16} />
            <span>{t.video_fullscreen}</span>
          </button>
          <button
            className="video-gallery-btn"
            type="button"
            onClick={onNavigateGallery}
          >
            <Images size={16} />
            <span>{t.nav_gallery}</span>
          </button>
        </div>

        <div className="hero-content" id="hero-text" ref={heroTextRef}>
          <h1 className="project-name" aria-label="Hima | حِمَى">
            <span className="project-name__arabic" lang="ar">حِمَى</span>
            <span className="project-name__separator" aria-hidden="true">|</span>
            <span className="project-name__english" lang="en">Hima</span>
          </h1>
          <h2 className="hero-title">{t.hero_title}</h2>
          <p className="subtitle">{t.hero_subtitle}</p>

          <div className="badge-academic">
            <GraduationCap size={16} />
            <span>{t.hero_badge}</span>
          </div>
        </div>
      </div>
    </header>
  );
}
