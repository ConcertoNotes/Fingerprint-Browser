'use client'

import { Moon, Sun } from 'lucide-react'

import { useI18n } from '@/components/i18n-provider'
import { useTheme, type ThemeMode } from '@/components/theme-provider'

const OPTIONS: { value: ThemeMode; labelKey: string; Icon: typeof Moon }[] = [
  { value: 'dark', labelKey: 'Obsidian dark', Icon: Moon },
  { value: 'light', labelKey: 'Porcelain light', Icon: Sun },
]

export function ThemeToggle({ compact = true }: { compact?: boolean }) {
  const { theme, setTheme } = useTheme()
  const { t } = useI18n()

  if (!compact) {
    return (
      <div className="grid gap-3 sm:grid-cols-2" role="radiogroup" aria-label={t('Appearance')}>
        {OPTIONS.map(({ value, labelKey, Icon }) => {
          const active = theme === value
          const isDarkOption = value === 'dark'
          return (
            <button
              key={value}
              type="button"
              role="radio"
              aria-checked={active}
              onClick={() => setTheme(value)}
              className={`group flex cursor-pointer items-center gap-3.5 rounded-xl border p-3.5 text-left transition-all ${
                active
                  ? 'border-signal/60 bg-signal-shade/60 ring-1 ring-signal/25'
                  : 'border-line bg-canvas/70 hover:border-line-strong hover:bg-raised/60'
              }`}
            >
              {/* Miniature desktop preview swatch */}
              <div
                aria-hidden="true"
                className={`relative flex h-11 w-16 shrink-0 overflow-hidden rounded-lg border ${
                  isDarkOption
                    ? 'border-white/15 bg-[#090b10]'
                    : 'border-slate-900/12 bg-[#f7f8fa]'
                }`}
              >
                <div
                  className={`h-full w-4 border-r ${
                    isDarkOption ? 'border-white/10 bg-[#0c0e14]' : 'border-slate-900/10 bg-[#f2f4f7]'
                  }`}
                />
                <div className="flex flex-1 flex-col gap-1 p-1.5">
                  <div
                    className={`h-1.5 w-6 rounded-full ${
                      isDarkOption ? 'bg-[#7c7bff]' : 'bg-[#4f46e5]'
                    }`}
                  />
                  <div
                    className={`h-4 w-full rounded ${
                      isDarkOption ? 'border border-white/10 bg-[#12151e]' : 'border border-slate-900/8 bg-white'
                    }`}
                  />
                </div>
              </div>

              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-1.5">
                  <Icon
                    size={13}
                    className={active ? 'text-signal' : 'text-ink-dim'}
                    aria-hidden="true"
                  />
                  <span className="text-[13px] font-semibold text-ink">{t(labelKey)}</span>
                </div>
                <p className="mt-0.5 text-[11.5px] text-ink-dim">
                  {isDarkOption
                    ? t('Deep obsidian surfaces with luminous prism highlights.')
                    : t('Crisp alabaster paper canvas with refined graphite contrast.')}
                </p>
              </div>
            </button>
          )
        })}
      </div>
    )
  }

  return (
    <div
      role="group"
      aria-label={t('Appearance')}
      className="inline-flex items-center rounded-lg border border-line bg-canvas/75 p-0.5 text-[11px]"
    >
      {OPTIONS.map(({ value, labelKey, Icon }) => {
        const active = theme === value
        return (
          <button
            key={value}
            type="button"
            aria-pressed={active}
            aria-label={t(labelKey)}
            title={t(labelKey)}
            onClick={() => setTheme(value)}
            className={`inline-flex h-[22px] w-[26px] cursor-pointer items-center justify-center rounded-md transition-all ${
              active
                ? 'bg-surface text-signal shadow-[0_1px_3px_rgba(0,0,0,0.2)] ring-1 ring-line'
                : 'text-ink-faint hover:text-ink'
            }`}
          >
            <Icon size={12} strokeWidth={2.1} aria-hidden="true" />
          </button>
        )
      })}
    </div>
  )
}
