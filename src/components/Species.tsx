import { useRef } from 'react';
import { motion, useInView } from 'framer-motion';
import { Info, MapPin, Eye } from 'lucide-react';
import { useLang } from '../context/LangContext';

interface SpeciesData {
  emoji: string;
  titleKey: 'oryx_title' | 'gazelle_title';
  latinName: string;
  details: Array<{
    icon: React.ReactNode;
    key: 'oryx_detail_1' | 'oryx_detail_2' | 'oryx_detail_3' | 'gazelle_detail_1' | 'gazelle_detail_2' | 'gazelle_detail_3';
  }>;
}

const speciesData: SpeciesData[] = [
  {
    emoji: '🦌',
    titleKey: 'oryx_title',
    latinName: 'Oryx leucoryx',
    details: [
      { icon: <Info size={16} />, key: 'oryx_detail_1' },
      { icon: <MapPin size={16} />, key: 'oryx_detail_2' },
      { icon: <Eye size={16} />, key: 'oryx_detail_3' },
    ],
  },
  {
    emoji: '🐐',
    titleKey: 'gazelle_title',
    latinName: 'Gazella marica',
    details: [
      { icon: <Info size={16} />, key: 'gazelle_detail_1' },
      { icon: <MapPin size={16} />, key: 'gazelle_detail_2' },
      { icon: <Eye size={16} />, key: 'gazelle_detail_3' },
    ],
  },
];

export default function Species() {
  const { t } = useLang();
  const ref = useRef(null);
  const inView = useInView(ref, { once: true, margin: '-100px' });

  return (
    <section id="species" className="max-w-6xl mx-auto px-8 py-16" ref={ref}>
      <motion.div
        initial={{ opacity: 0, y: 30 }}
        animate={inView ? { opacity: 1, y: 0 } : {}}
        transition={{ duration: 0.7 }}
        className="text-center mb-12"
      >
        <h2 className="text-4xl font-bold text-white mb-4">{t.species_section_title}</h2>
        <div className="section-divider" />
        <p style={{ color: 'var(--text-muted)' }}>{t.species_section_sub}</p>
      </motion.div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-8 pb-16">
        {speciesData.map((species, i) => (
          <motion.div
            key={species.titleKey}
            initial={{ opacity: 0, x: i === 0 ? -40 : 40 }}
            animate={inView ? { opacity: 1, x: 0 } : {}}
            transition={{ duration: 0.7, delay: i * 0.15 }}
            whileHover={{ y: -5, transition: { duration: 0.25 } }}
            className="glass p-8"
          >
            <div className="species-silhouette">{species.emoji}</div>

            <div className="flex justify-between items-start mb-6">
              <div>
                <h3 className="text-2xl font-bold text-white">{t[species.titleKey]}</h3>
                <em className="text-sm font-light" style={{ color: 'var(--text-muted)' }}>
                  {species.latinName}
                </em>
              </div>
              <span
                className="px-3 py-1 rounded-full text-xs font-semibold"
                style={{
                  background: 'rgba(212,175,55,0.1)',
                  color: 'var(--secondary-accent)',
                  border: '1px solid rgba(212,175,55,0.3)',
                }}
              >
                {t.species_vulnerable}
              </span>
            </div>

            <ul className="list-none space-y-3">
              {species.details.map((d) => (
                <li key={d.key} className="flex items-center gap-3 text-sm">
                  <span style={{ color: 'var(--primary-light)', flexShrink: 0 }}>{d.icon}</span>
                  <span>{t[d.key]}</span>
                </li>
              ))}
            </ul>
          </motion.div>
        ))}
      </div>
    </section>
  );
}
