import React, { createContext, useContext, useState, useEffect, useCallback } from 'react';
import { translations } from '../data/translations';
import type { Lang, Translations } from '../data/translations';

interface LangContextValue {
  lang: Lang;
  t: Translations;
  toggleLang: () => void;
}

const LangContext = createContext<LangContextValue | null>(null);

export function LangProvider({ children }: { children: React.ReactNode }) {
  const getInitialLang = (): Lang => {
    const saved = localStorage.getItem('hima-lang') as Lang | null;
    if (saved === 'en' || saved === 'ar') return saved;
    return navigator.language.startsWith('ar') ? 'ar' : 'en';
  };

  const [lang, setLang] = useState<Lang>(getInitialLang);

  useEffect(() => {
    document.documentElement.lang = lang;
    document.documentElement.dir = lang === 'ar' ? 'rtl' : 'ltr';
    localStorage.setItem('hima-lang', lang);
  }, [lang]);

  const toggleLang = useCallback(() => {
    setLang(prev => (prev === 'en' ? 'ar' : 'en'));
  }, []);

  return (
    <LangContext.Provider value={{ lang, t: translations[lang], toggleLang }}>
      {children}
    </LangContext.Provider>
  );
}

// eslint-disable-next-line react-refresh/only-export-components
export function useLang() {
  const ctx = useContext(LangContext);
  if (!ctx) throw new Error('useLang must be used inside LangProvider');
  return ctx;
}
