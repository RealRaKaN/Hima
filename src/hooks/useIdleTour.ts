import { useEffect, useRef } from 'react';

interface IdleTourOptions {
  enabled: boolean;
  idleTimeoutMs?: number;
  sectionPauseMs?: number;
}

function easeInOutCubic(t: number): number {
  return t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
}

export function useIdleTour({
  enabled,
  idleTimeoutMs = 30000,
  sectionPauseMs = 3500,
}: IdleTourOptions) {
  const isTourActiveRef = useRef(false);
  const animFrameIdRef = useRef<number | null>(null);
  const pauseTimerRef = useRef<number | null>(null);
  const idleTimerRef = useRef<number | null>(null);

  useEffect(() => {
    const cleanup = () => {
      if (idleTimerRef.current) {
        clearTimeout(idleTimerRef.current);
        idleTimerRef.current = null;
      }
      if (pauseTimerRef.current) {
        clearTimeout(pauseTimerRef.current);
        pauseTimerRef.current = null;
      }
      if (animFrameIdRef.current) {
        cancelAnimationFrame(animFrameIdRef.current);
        animFrameIdRef.current = null;
      }
      isTourActiveRef.current = false;
    };

    if (!enabled) {
      cleanup();
      return;
    }

    const stopTour = () => {
      if (animFrameIdRef.current) {
        cancelAnimationFrame(animFrameIdRef.current);
        animFrameIdRef.current = null;
      }
      if (pauseTimerRef.current) {
        clearTimeout(pauseTimerRef.current);
        pauseTimerRef.current = null;
      }
      isTourActiveRef.current = false;
    };

    const smoothScrollTo = (
      targetY: number,
      durationMs: number,
      onComplete: () => void
    ) => {
      if (!isTourActiveRef.current) return;

      const startY = window.scrollY;
      const distance = targetY - startY;
      const startTime = performance.now();

      const step = (currentTime: number) => {
        if (!isTourActiveRef.current) return;

        const elapsed = currentTime - startTime;
        const progress = Math.min(elapsed / durationMs, 1);
        const ease = easeInOutCubic(progress);

        window.scrollTo(0, startY + distance * ease);

        if (progress < 1) {
          animFrameIdRef.current = requestAnimationFrame(step);
        } else {
          window.scrollTo(0, targetY);
          onComplete();
        }
      };

      animFrameIdRef.current = requestAnimationFrame(step);
    };

    const startTour = () => {
      stopTour();
      isTourActiveRef.current = true;

      const navOffset = 60;
      const maxScroll = Math.max(
        document.documentElement.scrollHeight - window.innerHeight,
        0
      );

      const framedHeroPos = Math.round(window.innerHeight * 0.85);

      const sectionIds = [
        'metrics',
        'species',
        'architecture',
        'hardware',
        'tools',
        'contact',
      ];

      const sectionTargets: { id: string; targetY: number }[] = [];

      for (const id of sectionIds) {
        const el = document.getElementById(id);
        if (el) {
          const rect = el.getBoundingClientRect();
          const targetY = Math.min(
            Math.max(window.scrollY + rect.top - navOffset, 0),
            maxScroll
          );
          sectionTargets.push({ id, targetY });
        }
      }

      if (
        sectionTargets.length === 0 ||
        sectionTargets[sectionTargets.length - 1].targetY < maxScroll - 50
      ) {
        sectionTargets.push({ id: 'bottom', targetY: maxScroll });
      }

      const currentScrollY = window.scrollY;

      let remainingTargets = sectionTargets.filter(
        (st) => st.targetY > currentScrollY + 60
      );

      if (remainingTargets.length === 0) {
        remainingTargets = [{ id: 'bottom', targetY: maxScroll }];
      }

      let stepIndex = 0;

      const visitNextSection = () => {
        if (!isTourActiveRef.current) return;

        if (stepIndex < remainingTargets.length) {
          const nextTarget = remainingTargets[stepIndex];
          stepIndex++;

          const distance = Math.abs(nextTarget.targetY - window.scrollY);
          const duration = Math.min(Math.max(distance * 1.5, 1600), 2800);

          smoothScrollTo(nextTarget.targetY, duration, () => {
            if (!isTourActiveRef.current) return;

            pauseTimerRef.current = window.setTimeout(() => {
              visitNextSection();
            }, sectionPauseMs);
          });
        } else {
          const returnDistance = Math.abs(window.scrollY - framedHeroPos);
          const returnDuration = Math.min(
            Math.max(returnDistance * 1.2, 2200),
            3500
          );

          smoothScrollTo(framedHeroPos, returnDuration, () => {
            if (!isTourActiveRef.current) return;
            isTourActiveRef.current = false;
            resetIdleTimer();
          });
        }
      };

      visitNextSection();
    };

    const resetIdleTimer = () => {
      if (isTourActiveRef.current) {
        stopTour();
      }
      if (idleTimerRef.current) {
        clearTimeout(idleTimerRef.current);
      }
      idleTimerRef.current = window.setTimeout(() => {
        startTour();
      }, idleTimeoutMs);
    };

    const onUserActivity = () => {
      resetIdleTimer();
    };

    const onWheelOrTouch = () => {
      if (isTourActiveRef.current) {
        stopTour();
      }
      resetIdleTimer();
    };

    window.addEventListener('mousemove', onUserActivity, { passive: true });
    window.addEventListener('mousedown', onUserActivity, { passive: true });
    window.addEventListener('keydown', onUserActivity, { passive: true });
    window.addEventListener('touchstart', onWheelOrTouch, { passive: true });
    window.addEventListener('touchmove', onWheelOrTouch, { passive: true });
    window.addEventListener('wheel', onWheelOrTouch, { passive: true });

    resetIdleTimer();

    return () => {
      cleanup();
      window.removeEventListener('mousemove', onUserActivity);
      window.removeEventListener('mousedown', onUserActivity);
      window.removeEventListener('keydown', onUserActivity);
      window.removeEventListener('touchstart', onWheelOrTouch);
      window.removeEventListener('touchmove', onWheelOrTouch);
      window.removeEventListener('wheel', onWheelOrTouch);
    };
  }, [enabled, idleTimeoutMs, sectionPauseMs]);
}
