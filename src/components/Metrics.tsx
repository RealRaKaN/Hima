import { useEffect, useRef, useState } from 'react';
import { motion, useInView } from 'framer-motion';
import { Target, Timer, Zap, ShieldCheck } from 'lucide-react';
import { useLang } from '../context/LangContext';

interface MetricConfig {
  icon: React.ReactNode;
  target: number;
  prefix?: string;
  suffix?: string;
  titleKey: 'metric_map_title' | 'metric_trk_title' | 'metric_fps_title' | 'metric_fpr_title';
  subKey: 'metric_map_sub' | 'metric_trk_sub' | 'metric_fps_sub' | 'metric_fpr_sub';
}

const metrics: MetricConfig[] = [
  { icon: <Target size={32} />, target: 95, suffix: '%', titleKey: 'metric_map_title', subKey: 'metric_map_sub' },
  { icon: <Timer size={32} />, target: 120, titleKey: 'metric_trk_title', subKey: 'metric_trk_sub' },
  { icon: <Zap size={32} />, target: 30, prefix: 'FPS +', titleKey: 'metric_fps_title', subKey: 'metric_fps_sub' },
  { icon: <ShieldCheck size={32} />, target: 10, prefix: '<', suffix: '%', titleKey: 'metric_fpr_title', subKey: 'metric_fpr_sub' },
];

function AnimatedCounter({
  target,
  prefix = '',
  suffix = '',
  inView,
}: {
  target: number;
  prefix?: string;
  suffix?: string;
  inView: boolean;
}) {
  const [value, setValue] = useState(0);
  const doneRef = useRef(false);

  useEffect(() => {
    if (!inView || doneRef.current) return;
    doneRef.current = true;
    let current = 0;
    const increment = target / 40;
    const timer = setInterval(() => {
      current += increment;
      if (current >= target) {
        setValue(target);
        clearInterval(timer);
      } else {
        setValue(Math.ceil(current));
      }
    }, 30);
    return () => clearInterval(timer);
  }, [inView, target]);

  return (
    <span>
      {prefix}
      {value}
      {suffix}
    </span>
  );
}

export default function Metrics() {
  const { t } = useLang();
  const ref = useRef(null);
  const inView = useInView(ref, { once: true, margin: '-100px' });

  return (
    <section id="metrics" className="container fade-in py-16" ref={ref}>
      <motion.div
        initial={{ opacity: 0, y: 30 }}
        animate={inView ? { opacity: 1, y: 0 } : {}}
        transition={{ duration: 0.7 }}
        className="metrics-header text-center mb-10"
      >
        <h2 className="text-4xl font-bold text-white mb-4">{t.nav_validation}</h2>
        <div className="section-divider" />
      </motion.div>

      <div className="metrics-grid grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-6 pb-8">
        {metrics.map((m, i) => (
          <motion.div
            key={m.titleKey}
            initial={{ opacity: 0, y: 40 }}
            animate={inView ? { opacity: 1, y: 0 } : {}}
            transition={{ duration: 0.6, delay: i * 0.12 }}
            whileHover={{ y: -5, transition: { duration: 0.25 } }}
            className="glass metric-card p-8 text-center"
          >
            <div className="metric-icon mb-4 flex justify-center" style={{ color: 'var(--primary-light)' }}>
              {m.icon}
            </div>
            <div className="metric-value counter metric-value-text mb-2">
              <AnimatedCounter
                target={m.target}
                prefix={m.prefix}
                suffix={m.suffix}
                inView={inView}
              />
            </div>
            <div className="metric-title text-lg font-semibold text-white mb-1">
              {t[m.titleKey]}
            </div>
            <div className="metric-subtitle text-sm" style={{ color: 'var(--text-muted)', minHeight: '1.25rem' }}>
              {t[m.subKey]}
            </div>
          </motion.div>
        ))}
      </div>
    </section>
  );
}
