import { useRef } from 'react';
import { motion, useInView } from 'framer-motion';
import { useLang } from '../context/LangContext';

interface ToolLogo {
  type: 'logo';
  title: string;
  src: string;
}

interface ToolText {
  type: 'text';
  title: string;
}

interface ToolChannel {
  type: 'channel';
  title: string;
  src: string;
  href: string;
}

type ToolItem = ToolLogo | ToolText | ToolChannel;

const base = import.meta.env.BASE_URL;

const tools: ToolItem[] = [
  { type: 'logo', title: 'PyTorch', src: `${base}assets/images/tools/pytorch.svg` },
  { type: 'logo', title: 'YOLO', src: `${base}assets/images/tools/yolo.svg` },
  { type: 'logo', title: 'OpenCV', src: `${base}assets/images/tools/opencv.svg` },
  { type: 'logo', title: 'Python', src: `${base}assets/images/tools/python.svg` },
  { type: 'logo', title: 'Arduino', src: `${base}assets/images/tools/arduino.svg` },
  { type: 'logo', title: 'Kaggle', src: `${base}assets/images/tools/kaggle.svg` },
  { type: 'logo', title: 'Ultralytics', src: `${base}assets/images/tools/ultralytics.svg` },
  { type: 'logo', title: 'iNaturalist API', src: `${base}assets/images/tools/image.png` },
  { type: 'logo', title: 'jetson nano', src: `${base}assets/images/tools/nvd.png` },
  { type: 'logo', title: 'ArduPilot', src: `${base}assets/images/tools/ArduPilot_logo.svg` },
  { type: 'logo', title: 'Roboflow', src: `${base}assets/images/tools/roboflow.svg` },
  { type: 'logo', title: 'Ollama', src: `${base}assets/images/tools/ollama.svg` },
  { type: 'logo', title: 'Llama 3', src: `${base}assets/images/tools/meta.svg` },
  { type: 'logo', title: 'React', src: `${base}assets/images/tools/react.svg` },
  { type: 'logo', title: 'Vite', src: `${base}assets/images/tools/vite.svg` },
  { type: 'logo', title: 'Tailwind CSS', src: `${base}assets/images/tools/tailwindcss.svg` },
  { type: 'logo', title: 'Framer Motion', src: `${base}assets/images/tools/framer.svg` },
  { type: 'logo', title: 'Lucide Icons', src: `${base}assets/images/tools/lucide.svg` },
  { type: 'logo', title: 'Google Fonts', src: `${base}assets/images/tools/googlefonts.svg` },
  { type: 'logo', title: 'Wikimedia Commons', src: `${base}assets/images/tools/wikimediacommons.svg` },
  { type: 'text', title: 'SAM 2' },
  { type: 'text', title: 'Grounding DINO' },
  { type: 'text', title: 'BoT-SORT' },
  { type: 'text', title: 'ByteTrack' },
  { type: 'text', title: 'Gemma 3' },
  { type: 'text', title: 'MAVLink' },
  { type: 'text', title: 'Mission Planner' },
  { type: 'text', title: 'WDPA (Protected Planet)' },
  { type: 'text', title: 'Sentinel-2 (EOxCloudless)' },
  { type: 'text', title: 'model-viewer' },
  {
    type: 'channel',
    title: 'محمد اليوسفي',
    src: `${base}assets/images/tools/youtube.svg`,
    href: 'https://www.youtube.com/@MohdAlyousefi',
  },
];

export default function Toolchain() {
  const { t } = useLang();
  const ref = useRef(null);
  const inView = useInView(ref, { once: true, margin: '-80px' });

  return (
    <section id="tools" className="container tools-section fade-in" ref={ref}>
      <motion.h3
        initial={{ opacity: 0, y: 20 }}
        animate={inView ? { opacity: 1, y: 0 } : {}}
        transition={{ duration: 0.6 }}
        className="text-2xl font-bold text-white mb-8"
      >
        {t.tools_title}
      </motion.h3>

      <div className="tools-grid">
        {tools.map((tool, i) => {
          if (tool.type === 'logo') {
            return (
              <motion.div
                key={tool.title}
                initial={{ opacity: 0, scale: 0.8 }}
                animate={inView ? { opacity: 1, scale: 1 } : {}}
                transition={{ duration: 0.4, delay: i * 0.04 }}
                className="tool-badge tool-badge--logo glass"
                title={tool.title}
              >
                <img src={tool.src} alt={tool.title} />
              </motion.div>
            );
          }

          if (tool.type === 'text') {
            return (
              <motion.div
                key={tool.title}
                initial={{ opacity: 0, scale: 0.8 }}
                animate={inView ? { opacity: 1, scale: 1 } : {}}
                transition={{ duration: 0.4, delay: i * 0.04 }}
                className="tool-badge glass"
              >
                {tool.title}
              </motion.div>
            );
          }

          if (tool.type === 'channel') {
            return (
              <motion.a
                key={tool.title}
                initial={{ opacity: 0, scale: 0.8 }}
                animate={inView ? { opacity: 1, scale: 1 } : {}}
                transition={{ duration: 0.4, delay: i * 0.04 }}
                className="tool-badge tool-badge--channel glass"
                href={tool.href}
                target="_blank"
                rel="noopener noreferrer"
              >
                <img src={tool.src} alt="YouTube" />
                <span>{tool.title}</span>
              </motion.a>
            );
          }

          return null;
        })}
      </div>
    </section>
  );
}
