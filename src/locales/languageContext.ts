import { createContext, useContext } from 'react';
import type { Translations } from './en';

export type Language = 'en' | 'id';

export interface LanguageContextType {
  language: Language;
  setLanguage: (lang: Language) => void;
  t: Translations;
}

// Separated from the provider component so this module holds no JSX —
// keeps react-refresh fast-refresh happy for the provider file.
export const LanguageContext = createContext<LanguageContextType | undefined>(undefined);

export const useLanguage = (): LanguageContextType => {
  const context = useContext(LanguageContext);
  if (!context) {
    throw new Error('useLanguage must be used within a LanguageProvider');
  }
  return context;
};
