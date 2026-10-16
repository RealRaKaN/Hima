import { useRef } from 'react';
import { motion, useInView } from 'framer-motion';
import { useLang } from '../context/LangContext';

export default function DroneViewer() {
  const { t } = useLang();
  const ref = useRef(null);
  const inView = useInView(ref, { once: true, margin: '-100px' });

  return (
    <section id="hardware" className="container fade-in py-16" ref={ref}>
      <motion.div
        initial={{ opacity: 0, y: 30 }}
        animate={inView ? { opacity: 1, y: 0 } : {}}
        transition={{ duration: 0.7 }}
        className="section-header"
      >
        <h2>{t.drone_3d_title}</h2>
        <div className="section-divider" />
      </motion.div>

      <motion.div
        initial={{ opacity: 0, scale: 0.96 }}
        animate={inView ? { opacity: 1, scale: 1 } : {}}
        transition={{ duration: 0.8, delay: 0.2 }}
        className="hardware-split"
      >
        <div className="glass hardware-card p-8">
          <div
            style={{ width: '100%', height: '480px', position: 'relative' }}
            dangerouslySetInnerHTML={{
              __html: `
                <model-viewer
                  src="${import.meta.env.BASE_URL}DRONE.glb"
                  alt="A 3D model of the Hima drone"
                  auto-rotate
                  camera-controls
                  class="drone-model"
                  style="width: 100%; height: 480px; display: block; margin: 0 auto; background-color: rgba(0, 0, 0, 0.2); border-radius: 8px; outline: none;"
                  ar
                  ar-modes="webxr scene-viewer quick-look"
                  tone-mapping="neutral"
                  shadow-intensity="1"
                  environment-image="legacy"
                  exposure="0.28"
                  interaction-prompt="none"
                >
                  <div class="progress-bar hide" slot="progress-bar">
                    <div class="update-bar"></div>
                  </div>
                </model-viewer>
              `,
            }}
          />
          <p className="hardware-description mt-4 text-center">
            {t.drone_3d_desc}
          </p>
        </div>
      </motion.div>
    </section>
  );
}
