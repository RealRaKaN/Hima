import { useRef, useState } from 'react';
import { motion, useInView, AnimatePresence } from 'framer-motion';
import { Navigation, Camera, Eye, Cpu } from 'lucide-react';
import { useLang } from '../context/LangContext';

interface Step {
  num: number;
  icon: React.ReactNode;
  iconClass: string;
  titleKey: 'pipe1_title' | 'pipe2_title' | 'pipe3_title' | 'pipe4_title';
  descKey: 'pipe1_desc' | 'pipe2_desc' | 'pipe3_desc' | 'pipe4_desc';
}

const steps: Step[] = [
  { num: 1, icon: <Navigation size={24} />, iconClass: 'anim-fly', titleKey: 'pipe1_title', descKey: 'pipe1_desc' },
  { num: 2, icon: <Camera size={24} />, iconClass: 'anim-pulse', titleKey: 'pipe2_title', descKey: 'pipe2_desc' },
  { num: 3, icon: <Eye size={24} />, iconClass: 'anim-scan', titleKey: 'pipe3_title', descKey: 'pipe3_desc' },
  { num: 4, icon: <Cpu size={24} />, iconClass: 'anim-glow', titleKey: 'pipe4_title', descKey: 'pipe4_desc' },
];

export default function Pipeline() {
  const { t } = useLang();
  const ref = useRef(null);
  const inView = useInView(ref, { once: true, margin: '-100px' });
  const [active, setActive] = useState<number | null>(null);

  return (
    <section id="architecture" className="max-w-6xl mx-auto px-8 py-16" ref={ref}>
      <motion.div
        initial={{ opacity: 0, y: 30 }}
        animate={inView ? { opacity: 1, y: 0 } : {}}
        transition={{ duration: 0.7 }}
        className="text-center mb-12"
      >
        <h2 className="text-4xl font-bold text-white mb-4">{t.arch_title}</h2>
        <div className="section-divider" />
        <p style={{ color: 'var(--text-muted)' }}>{t.arch_sub}</p>
      </motion.div>

      <div className="relative py-8">
        <div className="hidden md:block pipeline-connector" />

        <div className="flex flex-col md:flex-row gap-6 relative z-10">
          {steps.map((step, i) => (
            <motion.div
              key={step.num}
              initial={{ opacity: 0, y: 40 }}
              animate={inView ? { opacity: 1, y: 0 } : {}}
              transition={{ duration: 0.6, delay: i * 0.15 }}
              whileHover={{ y: -6, transition: { duration: 0.2 } }}
              onClick={() => setActive(active === step.num ? null : step.num)}
              className="glass flex-1 p-6 relative z-10 cursor-pointer transition-all duration-300"
              style={{
                borderColor: active === step.num ? 'var(--primary-light)' : undefined,
                boxShadow: active === step.num ? 'var(--glow-shadow)' : undefined,
              }}
            >
              <div
                className="w-8 h-8 rounded-full flex items-center justify-center font-bold text-sm mb-4"
                style={{
                  background: 'var(--primary-accent)',
                  border: '2px solid var(--primary-light)',
                  fontFamily: 'var(--font-heading)',
                }}
              >
                {step.num}
              </div>

              <div className={`mb-2 ${step.iconClass}`} style={{ color: 'var(--primary-light)' }}>
                {step.icon}
              </div>

              <h4 className="text-lg font-bold text-white mb-2">{t[step.titleKey]}</h4>
              <p className="text-sm" style={{ color: 'var(--text-muted)' }}>{t[step.descKey]}</p>

              <AnimatePresence>
                {active === step.num && (
                  <motion.div
                    initial={{ opacity: 0, height: 0 }}
                    animate={{ opacity: 1, height: 'auto' }}
                    exit={{ opacity: 0, height: 0 }}
                    className="mt-3 pt-3"
                    style={{ borderTop: '1px solid var(--glass-border)' }}
                  >
                    <div className="flex items-center gap-2">
                      <div
                        className="w-2 h-2 rounded-full animate-pulse"
                        style={{ background: 'var(--primary-light)' }}
                      />
                      <span className="text-xs" style={{ color: 'var(--primary-light)' }}>
                        Active
                      </span>
                    </div>
                  </motion.div>
                )}
              </AnimatePresence>
            </motion.div>
          ))}
        </div>
      </div>
    </section>
  );
}
