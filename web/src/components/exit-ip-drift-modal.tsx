'use client'

import Link from 'next/link'
import { ArrowRight, Globe2, LoaderCircle, Play, ShieldAlert, TriangleAlert } from 'lucide-react'

import { useI18n } from '@/components/i18n-provider'
import { Modal } from '@/components/modal'
import type { ExitIpDriftDetail, Profile } from '@/lib/api'
import { countryName, flag, maskIp } from '@/lib/network-map'

interface Props {
  profile: Profile | null
  drift: ExitIpDriftDetail | null
  maskIps: boolean
  busy: boolean
  onCancel: () => void
  onProceed: () => void
}

/** Warning modal shown when a profile's measured exit IP or country drifted since last launch. */
export function ExitIpDriftModal({ profile, drift, maskIps, busy, onCancel, onProceed }: Props) {
  const { t, locale } = useI18n()
  if (!profile || !drift) return null

  const prevCountry = drift.previous_country
    ? `${flag(drift.previous_country)} ${countryName(drift.previous_country, locale)} (${drift.previous_country})`
    : t('Unknown region')
  const currCountry = drift.current_country
    ? `${flag(drift.current_country)} ${countryName(drift.current_country, locale)} (${drift.current_country})`
    : t('Unknown region')

  return (
    <Modal
      open
      title={t('Exit IP drift blocked')}
      subtitle={profile.name}
      width={560}
      onClose={() => { if (!busy) onCancel() }}
      footer={
        <>
          <Link
            href={`/map/?profile=${encodeURIComponent(profile.id)}`}
            onClick={onCancel}
            className="btn btn-default mr-auto"
          >
            <Globe2 size={13} aria-hidden="true" />
            {t('Switch node on map')}
          </Link>
          <button type="button" className="btn btn-default" disabled={busy} onClick={onCancel}>
            {t('Cancel')}
          </button>
          <button
            type="button"
            className={drift.country_changed ? 'btn btn-danger' : 'btn btn-primary'}
            disabled={busy}
            onClick={onProceed}
          >
            {busy ? <LoaderCircle size={13} className="animate-spin" aria-hidden="true" /> : <Play size={12} aria-hidden="true" />}
            {t('Proceed & update exit record')}
          </button>
        </>
      }
    >
      <div className="space-y-4">
        {drift.country_changed ? (
          <div className="flex items-start gap-2.5 rounded-xl border border-danger/30 bg-danger-shade/60 p-3.5 text-danger" role="alert">
            <ShieldAlert size={16} className="mt-0.5 shrink-0" aria-hidden="true" />
            <div className="text-[12.5px]">
              <p className="font-semibold">{t('Exit country changed — high geo-risk alert')}</p>
              <p className="mt-0.5 text-danger/90">
                {t('The measured exit country differs from the last session. Signing in directly may trigger cross-region risk controls.')}
              </p>
            </div>
          </div>
        ) : (
          <div className="flex items-start gap-2.5 rounded-xl border border-warn/30 bg-warn-shade/70 p-3.5 text-warn" role="status">
            <TriangleAlert size={16} className="mt-0.5 shrink-0" aria-hidden="true" />
            <div className="text-[12.5px]">
              <p className="font-semibold">{t('Measured exit IP changed since last launch')}</p>
              <p className="mt-0.5 text-warn/90">
                {t('Launch was paused by Exit IP Drift Guard so you can verify the new exit address or switch nodes before exposing your session.')}
              </p>
            </div>
          </div>
        )}

        {/* Side-by-side Previous vs Current Exit Comparison */}
        <div className="grid items-center gap-2.5 sm:grid-cols-[1fr_auto_1fr]">
          <div className="rounded-xl border border-line bg-sidebar/70 p-3.5">
            <p className="text-[11px] font-medium uppercase tracking-[0.04em] text-ink-faint">
              {t('Previous exit')}
            </p>
            <p className="mt-1.5 text-[13px] font-medium text-ink">{prevCountry}</p>
            <p className="mt-1 font-mono text-[13px] font-semibold text-ink">
              {maskIp(drift.previous_ip, maskIps)}
            </p>
          </div>

          <div className="flex justify-center text-ink-faint" aria-hidden="true">
            <ArrowRight size={16} />
          </div>

          <div className={`rounded-xl border p-3.5 ${
            drift.country_changed
              ? 'border-danger/35 bg-danger-shade/35'
              : 'border-warn/35 bg-warn-shade/40'
          }`}>
            <p className={`text-[11px] font-medium uppercase tracking-[0.04em] ${
              drift.country_changed ? 'text-danger' : 'text-warn'
            }`}>
              {t('Current measured exit')}
            </p>
            <p className="mt-1.5 text-[13px] font-medium text-ink">{currCountry}</p>
            <p className="mt-1 font-mono text-[13px] font-semibold text-ink">
              {maskIp(drift.current_ip, maskIps)}
            </p>
          </div>
        </div>

        <p className="text-[11.5px] text-ink-dim">
          {t('No browser window was started yet. Choose "Switch node on map" to pick a stable exit, or confirm to continue and record the new exit IP.')}
        </p>
      </div>
    </Modal>
  )
}
