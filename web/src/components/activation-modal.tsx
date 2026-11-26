'use client'

import { useEffect, useRef, useState } from 'react'
import { Eye, EyeOff, KeyRound, LoaderCircle, ShieldCheck, TriangleAlert, X } from 'lucide-react'

import { useI18n } from '@/components/i18n-provider'
import { useToast } from '@/components/toast'
import {
  cloudAPI,
  formatCloudAuthError,
  isEntitlementBlocked,
  type CloudStatus,
} from '@/lib/cloud-api'
import { acquireViewportOverlay } from '@/lib/desktop-bridge'

interface ActivationModalProps {
  open: boolean
  brandName?: string
  requireActivation?: boolean
  onDismiss: () => void
  onActivated: (status: CloudStatus) => void
}

/**
 * First-run license activation dialog for customer edition clients.
 * When requireActivation is false, users can skip and continue using local nodes.
 */
export function ActivationModal({
  open,
  brandName,
  requireActivation = false,
  onDismiss,
  onActivated,
}: ActivationModalProps) {
  const { t } = useI18n()
  const toast = useToast()

  const [token, setToken] = useState('')
  const [showToken, setShowToken] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const panelRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const onDismissRef = useRef(onDismiss)

  useEffect(() => {
    onDismissRef.current = onDismiss
  }, [onDismiss])

  useEffect(() => {
    if (!open) return

    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    const releaseOverlay = acquireViewportOverlay()

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !requireActivation && !submitting) {
        onDismissRef.current()
      }
    }
    document.addEventListener('keydown', onKeyDown)
    inputRef.current?.focus()

    return () => {
      releaseOverlay()
      document.removeEventListener('keydown', onKeyDown)
      document.body.style.overflow = previousOverflow
    }
  }, [open, requireActivation, submitting])

  if (!open) return null

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault()
    const trimmed = token.trim()
    if (!trimmed || submitting) return

    setSubmitting(true)
    setError(null)
    try {
      const result = await cloudAPI.activate(trimmed)
      const nextStatus = result.status
      if (isEntitlementBlocked(nextStatus?.entitlement_status)) {
        setError(
          nextStatus.last_error
            ? t(nextStatus.last_error)
            : t('Activation succeeded, but your license is currently unavailable.'),
        )
        return
      }
      if (result.report?.errors && result.report.errors.length > 0 && !nextStatus?.has_token) {
        setError(t(result.report.errors[0]))
        return
      }
      setToken('')
      toast(
        'ok',
        t('License activated'),
        nextStatus?.tenant_name
          ? t('Welcome, {name}. Your exclusive nodes have been synced.', { name: nextStatus.tenant_name })
          : t('Your exclusive nodes have been synced.'),
      )
      onActivated(nextStatus)
    } catch (err) {
      setError(t(formatCloudAuthError(err)))
    } finally {
      setSubmitting(false)
    }
  }

  const displayBrand = brandName?.trim() || t('Prism')

  return (
    <div
      className="overlay-in modal-scrim fixed inset-0 z-50 flex items-center justify-center p-5 backdrop-blur-[6px]"
      onMouseDown={event => {
        if (event.target === event.currentTarget && !requireActivation && !submitting) {
          onDismiss()
        }
      }}
    >
      <div
        ref={panelRef}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        aria-label={t('Activate exclusive license')}
        className="dialog-in modal-sheet relative w-full max-w-[460px] overflow-hidden rounded-2xl outline-none"
      >
        <div className="prism-hairline h-[2px] w-full" aria-hidden="true" />
        {!requireActivation && (
          <button
            type="button"
            onClick={onDismiss}
            disabled={submitting}
            aria-label={t('Close')}
            className="btn btn-ghost absolute right-3 top-3.5 h-7 w-7 p-0"
          >
            <X size={15} strokeWidth={2} />
          </button>
        )}

        <div className="px-6 pt-7 pb-6">
          <div className="flex flex-col items-center text-center">
            <span className="flex h-12 w-12 items-center justify-center rounded-2xl bg-signal-shade text-signal shadow-[0_8px_24px_-6px_rgba(124,123,255,0.42)] ring-1 ring-signal/25">
              <KeyRound size={22} strokeWidth={1.9} aria-hidden="true" />
            </span>
            <p className="mt-3 text-[11px] font-semibold tracking-wide text-signal uppercase">
              {displayBrand}
            </p>
            <h2 className="mt-1 text-[17px] font-semibold tracking-tight text-ink">
              {t('Activate exclusive node license')}
            </h2>
            <p className="mt-1.5 text-[12.5px] leading-relaxed text-ink-dim">
              {requireActivation
                ? t('Enter the exclusive activation code provided by your administrator to activate and sync your dedicated proxy nodes.')
                : t('Enter the exclusive activation code provided by your administrator to automatically sync dedicated proxy nodes. You can still freely add your own local nodes in Subscriptions & nodes.')}
            </p>
          </div>

          <form onSubmit={handleSubmit} className="mt-5 space-y-4">
            <div>
              <label className="field-label" htmlFor="activation-modal-token">
                {t('Activation code')}
              </label>
              <div className="relative">
                <input
                  ref={inputRef}
                  id="activation-modal-token"
                  type={showToken ? 'text' : 'password'}
                  autoComplete="off"
                  spellCheck={false}
                  value={token}
                  onChange={event => {
                    setToken(event.target.value)
                    if (error) setError(null)
                  }}
                  placeholder={t('Paste your activation code (e.g. prism_…)')}
                  className="field pr-9 font-mono text-[12.5px]"
                  disabled={submitting}
                />
                <button
                  type="button"
                  onClick={() => setShowToken(prev => !prev)}
                  aria-label={showToken ? t('Hide activation code') : t('Show activation code')}
                  className="absolute right-2 top-1/2 -translate-y-1/2 cursor-pointer rounded p-1 text-ink-faint hover:bg-raised hover:text-ink"
                >
                  {showToken ? <EyeOff size={14} aria-hidden="true" /> : <Eye size={14} aria-hidden="true" />}
                </button>
              </div>
            </div>

            {error && (
              <div
                className="flex items-start gap-2 rounded-lg border border-danger/25 bg-danger-shade/60 px-3 py-2 text-[12px] text-danger"
                role="alert"
              >
                <TriangleAlert size={14} className="mt-0.5 shrink-0" aria-hidden="true" />
                <span>{error}</span>
              </div>
            )}

            <button
              type="submit"
              className="btn btn-primary h-[38px] w-full text-[13px]"
              disabled={!token.trim() || submitting}
            >
              {submitting ? (
                <LoaderCircle size={14} className="animate-spin" aria-hidden="true" />
              ) : (
                <ShieldCheck size={14} aria-hidden="true" />
              )}
              {submitting ? t('Activating & syncing…') : t('Activate & sync nodes now')}
            </button>

            {!requireActivation && (
              <button
                type="button"
                onClick={onDismiss}
                disabled={submitting}
                className="btn btn-ghost h-[34px] w-full text-[12.5px] text-ink-dim"
              >
                {t('Skip for now, use local nodes')}
              </button>
            )}
          </form>
        </div>
      </div>
    </div>
  )
}
