'use client'

import { useEffect, useState } from 'react'
import { CheckCircle2 } from 'lucide-react'

import { useI18n } from '@/components/i18n-provider'
import { useWorkspace } from '@/components/workspace-provider'
import { systemAPI, type BrowserReadiness } from '@/lib/api'

/** Built-in isolation browser kernel status card (never shows download/install nags). */
export function BrowserSetup({ onCreate }: { firstProfile?: boolean; onCreate?: () => void }) {
  const { t } = useI18n()
  const { engineInfo } = useWorkspace()
  const [fallbackBrowser, setFallbackBrowser] = useState<BrowserReadiness | null>(null)

  useEffect(() => {
    // Only fetch backend fallback version when rendered in Settings and desktop engine info isn't present yet.
    if (onCreate || engineInfo?.chromiumVersion) return
    let active = true
    systemAPI
      .browser()
      .then(data => { if (active) setFallbackBrowser(data) })
      .catch(() => {})
    return () => { active = false }
  }, [onCreate, engineInfo?.chromiumVersion])

  // On the main browser workspace (`onCreate` provided), the built-in kernel is always ready — render nothing.
  if (onCreate) return null

  const versionLabel = engineInfo?.chromiumVersion
    ? t('Chromium {version}', { version: engineInfo.chromiumVersion })
    : fallbackBrowser?.version ?? null

  return (
    <section aria-label={t('Isolation browser kernel')}>
      <h2 className="mb-2 text-[11px] font-semibold uppercase tracking-[0.06em] text-ink-faint">
        {t('Isolation browser kernel')}
      </h2>
      <div className="panel flex flex-wrap items-center justify-between gap-3 p-4">
        <div className="flex items-start gap-3">
          <span className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-xl bg-ok-shade text-ok" aria-hidden="true">
            <CheckCircle2 size={17} />
          </span>
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <p className="font-semibold text-ink">
                {t('Built-in isolation browser kernel · Ready')}
              </p>
              {versionLabel && (
                <span className="rounded-full bg-ok-shade px-2 py-0.5 font-mono text-[11px] font-medium text-ok">
                  {versionLabel}
                </span>
              )}
            </div>
            <p className="mt-0.5 text-[12px] text-ink-dim">
              {t('Zero external downloads required. Each profile runs in its own isolated storage partition, pinned hardware fingerprint, and dedicated exit tunnel inside the unified workspace.')}
            </p>
          </div>
        </div>
      </div>
    </section>
  )
}
