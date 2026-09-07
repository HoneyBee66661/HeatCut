import React, { useState, useEffect, useMemo } from 'react';
import { LanguageContext, type Language } from './languageContext';
import { en } from './en';
import { id } from './id';

const STORAGE_KEY = 'cheat_clip_language';

const getInitialLanguage = (): Language => {
  if (typeof window === 'undefined') return 'en';

  const saved = localStorage.getItem(STORAGE_KEY);
  if (saved === 'en' || saved === 'id') {
    return saved;
  }

  // Detect device / browser language (userLanguage is IE/legacy Safari only)
  const nav = navigator as Navigator & { userLanguage?: string };
  const browserLang = (navigator.language || nav.userLanguage || '').toLowerCase();
  if (browserLang.startsWith('id')) {
    return 'id';
  }
  return 'en';
};

export const LanguageProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const [language, setLanguageState] = useState<Language>(getInitialLanguage);

  const setLanguage = (newLang: Language) => {
    setLanguageState(newLang);
    localStorage.setItem(STORAGE_KEY, newLang);
  };

  // Sync state if another tab updates the preference
  useEffect(() => {
    const handleStorageChange = (e: StorageEvent) => {
      if (e.key === STORAGE_KEY && (e.newValue === 'en' || e.newValue === 'id')) {
        setLanguageState(e.newValue);
      }
    };
    window.addEventListener('storage', handleStorageChange);
    return () => window.removeEventListener('storage', handleStorageChange);
  }, []);

  const t = useMemo(() => (language === 'id' ? id : en), [language]);

  return (
    <LanguageContext.Provider value={{ language, setLanguage, t }}>
      {children}
    </LanguageContext.Provider>
  );
};
