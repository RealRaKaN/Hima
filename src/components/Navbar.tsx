import { useState, useEffect } from 'react';
import { Radar, Menu, X } from 'lucide-react';
import { useLang } from '../context/LangContext';

interface NavbarProps {
  currentPage: 'home' | 'gallery';
  onNavigateHome: (sectionId?: string) => void;
  onNavigateGallery: () => void;
}

export default function Navbar({
  currentPage,
  onNavigateHome,
  onNavigateGallery,
}: NavbarProps) {
  const { t, lang, toggleLang } = useLang();
  const [isOpen, setIsOpen] = useState(false);
  const [hidden, setHidden] = useState(false);
  const [lastY, setLastY] = useState(0);

  useEffect(() => {
    const handleScroll = () => {
      const y = window.scrollY;
      setHidden(y > 80 && y > lastY);
      setLastY(y);
    };
    window.addEventListener('scroll', handleScroll, { passive: true });
    return () => window.removeEventListener('scroll', handleScroll);
  }, [lastY]);

  const handleSectionClick = (e: React.MouseEvent, sectionId: string) => {
    e.preventDefault();
    setIsOpen(false);
    onNavigateHome(sectionId);
  };

  const handleGalleryClick = (e: React.MouseEvent) => {
    e.preventDefault();
    setIsOpen(false);
    onNavigateGallery();
  };

  return (
    <nav
      id="navbar"
      style={{ transform: hidden ? 'translateY(-100%)' : 'translateY(0)' }}
    >
      <div className="container nav-container flex justify-between items-center flex-wrap">
        <a
          href="#hero"
          onClick={(e) => handleSectionClick(e, 'hero')}
          className="brand flex items-center gap-2 font-bold text-2xl"
          style={{
            color: 'var(--primary-light)',
            fontFamily: 'var(--font-heading)',
            textShadow: '0 0 10px rgba(16,185,129,0.3)',
          }}
        >
          <Radar size={24} />
          Hima | حِمَى
        </a>

        <button
          className="mobile-menu-btn md:hidden bg-transparent border-none text-white cursor-pointer p-1"
          onClick={() => setIsOpen((v) => !v)}
          aria-label="Toggle navigation"
          aria-expanded={isOpen}
        >
          {isOpen ? <X size={24} /> : <Menu size={24} />}
        </button>

        <div
          className={`nav-links ${
            isOpen ? 'flex' : 'hidden'
          } md:flex flex-col md:flex-row w-full md:w-auto items-center gap-6 md:gap-8 pt-4 md:pt-0`}
        >
          <a
            href="#hero"
            onClick={(e) => handleSectionClick(e, 'hero')}
            className="text-sm font-medium transition-colors duration-300 hover:text-emerald-400"
          >
            {t.nav_live_demo}
          </a>
          <a
            href="#metrics"
            onClick={(e) => handleSectionClick(e, 'metrics')}
            className="text-sm font-medium transition-colors duration-300 hover:text-emerald-400"
          >
            {t.nav_validation}
          </a>
          <a
            href="#species"
            onClick={(e) => handleSectionClick(e, 'species')}
            className="text-sm font-medium transition-colors duration-300 hover:text-emerald-400"
          >
            {t.nav_target_species}
          </a>
          <a
            href="#architecture"
            onClick={(e) => handleSectionClick(e, 'architecture')}
            className="text-sm font-medium transition-colors duration-300 hover:text-emerald-400"
          >
            {t.nav_methodology}
          </a>
          <a
            href="#hardware"
            onClick={(e) => handleSectionClick(e, 'hardware')}
            className="text-sm font-medium transition-colors duration-300 hover:text-emerald-400"
          >
            {t.nav_hardware_setup}
          </a>
          <a
            href="#gallery"
            onClick={handleGalleryClick}
            className={`text-sm font-medium transition-colors duration-300 hover:text-emerald-400 ${
              currentPage === 'gallery' ? 'active' : ''
            }`}
          >
            {t.nav_gallery}
          </a>
          <a
            href="#contact"
            onClick={(e) => handleSectionClick(e, 'contact')}
            className="text-sm font-medium transition-colors duration-300 hover:text-emerald-400 nav-whatsapp"
          >
            {t.nav_contact}
          </a>
          <button
            onClick={toggleLang}
            className="lang-toggle px-3 py-1 rounded-full text-sm font-semibold transition-all duration-200 cursor-pointer"
            style={{
              background: 'rgba(255,255,255,0.1)',
              border: '1px solid rgba(255,255,255,0.3)',
              color: 'white',
            }}
          >
            {lang === 'ar' ? 'English' : 'عربي'}
          </button>
        </div>
      </div>
    </nav>
  );
}
