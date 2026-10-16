import { useState, useCallback, useEffect } from 'react';
import { ArrowLeft } from 'lucide-react';
import { useLang } from '../context/LangContext';

interface GalleryItem {
  src: string;
  en?: string;
  ar?: string;
}

const galleryItems: GalleryItem[] = [
  { src: '/assets/images/gallery/3.jpg' },
  { src: '/assets/images/gallery/11.jpg' },
  { src: '/assets/images/gallery/4.jpg' },
  { src: '/assets/images/gallery/5.jpg' },
  { src: '/assets/images/gallery/7.jpg' },
  { src: '/assets/images/gallery/8.jpg' },
  { src: '/assets/images/gallery/9.jpg' },
  { src: '/assets/images/gallery/10.jpg' },
  { src: '/assets/images/gallery/12.jpg' },
  { src: '/assets/images/gallery/6.jpg' },
  { src: '/assets/images/gallery/13.jpg' },
  { src: '/assets/images/gallery/1.jpg' },
  { src: '/assets/images/gallery/2.jpg' },
];

interface GalleryPageProps {
  onNavigateHome: () => void;
}

export default function GalleryPage({ onNavigateHome }: GalleryPageProps) {
  const { t, lang } = useLang();
  const [lightboxIndex, setLightboxIndex] = useState<number | null>(null);

  const getCaption = (item: GalleryItem) => {
    return lang === 'ar' ? item.ar || item.en || '' : item.en || item.ar || '';
  };

  const openLightbox = (i: number) => {
    setLightboxIndex(i);
    document.body.style.overflow = 'hidden';
  };

  const closeLightbox = () => {
    setLightboxIndex(null);
    document.body.style.overflow = '';
  };

  const prev = useCallback(() => {
    setLightboxIndex((i) =>
      i !== null ? (i - 1 + galleryItems.length) % galleryItems.length : null
    );
  }, []);

  const next = useCallback(() => {
    setLightboxIndex((i) =>
      i !== null ? (i + 1) % galleryItems.length : null
    );
  }, []);

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (lightboxIndex === null) return;
      if (e.key === 'Escape') closeLightbox();
      if (e.key === 'ArrowLeft') prev();
      if (e.key === 'ArrowRight') next();
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [lightboxIndex, prev, next]);

  return (
    <main className="container gallery-page">
      <div className="section-header">
        <h2>{t.gallery_title}</h2>
        <p>{t.gallery_sub}</p>
      </div>

      {galleryItems.length === 0 ? (
        <p className="gallery-empty">{t.gallery_empty}</p>
      ) : (
        <div className="gallery-grid" id="gallery-grid">
          {galleryItems.map((item, i) => {
            const caption = getCaption(item);
            return (
              <button
                key={item.src}
                type="button"
                className="gallery-item glass"
                onClick={() => openLightbox(i)}
              >
                <img loading="lazy" src={item.src} alt={caption || `Gallery ${i + 1}`} />
                {caption && <span className="gallery-caption">{caption}</span>}
              </button>
            );
          })}
        </div>
      )}

      <div className="gallery-back">
        <button
          type="button"
          onClick={onNavigateHome}
          className="btn btn-primary"
        >
          <ArrowLeft size={16} />
          <span>{t.gallery_back}</span>
        </button>
      </div>

      {lightboxIndex !== null && (
        <div
          className="lightbox"
          role="dialog"
          aria-modal="true"
          aria-label="Image viewer"
          onClick={(e) => {
            if (e.target === e.currentTarget) closeLightbox();
          }}
        >
          <button
            className="lightbox-btn lightbox-close"
            type="button"
            aria-label="Close"
            onClick={closeLightbox}
          >
            ✕
          </button>
          <button
            className="lightbox-btn lightbox-prev"
            type="button"
            aria-label="Previous"
            onClick={prev}
          >
            ‹
          </button>
          <figure className="lightbox-figure">
            <img
              src={galleryItems[lightboxIndex].src}
              alt={getCaption(galleryItems[lightboxIndex])}
            />
            {getCaption(galleryItems[lightboxIndex]) && (
              <figcaption>
                {getCaption(galleryItems[lightboxIndex])}
              </figcaption>
            )}
          </figure>
          <button
            className="lightbox-btn lightbox-next"
            type="button"
            aria-label="Next"
            onClick={next}
          >
            ›
          </button>
        </div>
      )}
    </main>
  );
}
