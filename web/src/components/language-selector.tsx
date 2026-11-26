'use client'

import { useI18n } from '@/components/i18n-provider'
import type { Locale } from '@/lib/i18n'

const OPTIONS: { value: Locale; label: string; lang: string }[] = [
  { value: 'zh-CN', label: '中文', lang: 'zh-CN' },
  { value: 'en', label: 'EN', lang: 'en' },
]

export function LanguageSelector() {
  const { locale, setLocale, t } = useI18n()
  return (
    <div
      role="group"
      aria-label={t('Language')}
      className="inline-flex items-center rounded-lg border border-line bg-canvas/75 p-0.5 text-[11px]"
    >
      {OPTIONS.map(option => {
        const active = locale === option.value
        return (
          <button
            key={option.value}
            type="button"
            lang={option.lang}
            aria-pressed={active}
            onClick={() => setLocale(option.value)}
            className={`h-[22px] cursor-pointer rounded-md px-2 font-medium transition-all ${
              active
                ? 'bg-surface text-ink shadow-[0_1px_3px_rgba(0,0,0,0.2)] ring-1 ring-line'
                : 'text-ink-faint hover:text-ink'
            }`}
          >
            {option.label}
          </button>
        )
      })}
    </div>
  )
}
