import { useState, useEffect } from 'react';
import './index.css';
import { LangProvider } from './context/LangContext';
import ShaderBackground from './components/ShaderBackground';
import Navbar from './components/Navbar';
import Hero from './components/Hero';
import Metrics from './components/Metrics';
import Species from './components/Species';
import Pipeline from './components/Pipeline';
import DroneViewer from './components/DroneViewer';
import Toolchain from './components/Toolchain';
import GalleryPage from './components/GalleryPage';
import Footer from './components/Footer';
import { useIdleTour } from './hooks/useIdleTour';

function MainContent({
  currentPage,
  onNavigateGallery,
  onNavigateHome,
}: {
  currentPage: 'home' | 'gallery';
  onNavigateGallery: () => void;
  onNavigateHome: (sectionId?: string) => void;
}) {
  useIdleTour({
    enabled: currentPage === 'home',
    idleTimeoutMs: 30000,
    sectionPauseMs: 3500,
  });

  return (
    <main>
      {currentPage === 'home' ? (
        <>
          <Hero onNavigateGallery={onNavigateGallery} />
          <Metrics />
          <Species />
          <Pipeline />
          <DroneViewer />
          <Toolchain />
        </>
      ) : (
        <GalleryPage onNavigateHome={() => onNavigateHome('hero')} />
      )}
    </main>
  );
}

export default function App() {
  const [currentPage, setCurrentPage] = useState<'home' | 'gallery'>(() => {
    return window.location.hash === '#gallery' ? 'gallery' : 'home';
  });

  useEffect(() => {
    const handleHashChange = () => {
      if (window.location.hash === '#gallery') {
        setCurrentPage('gallery');
        window.scrollTo({ top: 0, behavior: 'smooth' });
      } else {
        setCurrentPage('home');
      }
    };

    window.addEventListener('hashchange', handleHashChange);
    return () => window.removeEventListener('hashchange', handleHashChange);
  }, []);

  const navigateToGallery = () => {
    window.location.hash = 'gallery';
    setCurrentPage('gallery');
    window.scrollTo({ top: 0, behavior: 'smooth' });
  };

  const navigateToHome = (sectionId?: string) => {
    if (currentPage !== 'home') {
      window.location.hash = sectionId ? `#${sectionId}` : '';
      setCurrentPage('home');
      if (sectionId) {
        setTimeout(() => {
          document.getElementById(sectionId)?.scrollIntoView({ behavior: 'smooth' });
        }, 100);
      } else {
        window.scrollTo({ top: 0, behavior: 'smooth' });
      }
    } else if (sectionId) {
      document.getElementById(sectionId)?.scrollIntoView({ behavior: 'smooth' });
    } else {
      window.scrollTo({ top: 0, behavior: 'smooth' });
    }
  };

  return (
    <LangProvider>
      <ShaderBackground />
      <Navbar
        currentPage={currentPage}
        onNavigateHome={navigateToHome}
        onNavigateGallery={navigateToGallery}
      />
      <MainContent
        currentPage={currentPage}
        onNavigateGallery={navigateToGallery}
        onNavigateHome={navigateToHome}
      />
      <Footer />
    </LangProvider>
  );
}
