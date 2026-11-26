'use client'

import { useEffect, useRef } from 'react'
import { X } from 'lucide-react'
import { useI18n } from '@/components/i18n-provider'
import { acquireViewportOverlay } from '@/lib/desktop-bridge'

interface ModalProps {
  open: boolean
  title: string
  subtitle?: string
  onClose: () => void
  children: React.ReactNode
  footer?: React.ReactNode
  width?: number
  bodyClassName?: string
  panelClassName?: string
}

/**
 * Dialog shell with focus trap, Escape dismissal, backdrop click, specular glass rim,
 * and pinned header/footer so content scrolls smoothly inside the sheet.
 */
export function Modal({
  open,
  title,
  subtitle,
  onClose,
  children,
  footer,
  width = 620,
  bodyClassName,
  panelClassName,
}: ModalProps) {
  const { t } = useI18n()
  const panelRef = useRef<HTMLDivElement>(null)
  const onCloseRef = useRef(onClose)

  useEffect(() => {
    onCloseRef.current = onClose
  }, [onClose])

  useEffect(() => {
    if (!open) return

    const panel = panelRef.current
    const previouslyFocused = document.activeElement as HTMLElement | null

    const focusable = () =>
      Array.from(
        panel?.querySelectorAll<HTMLElement>(
          'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
        ) ?? [],
      ).filter((element) => element.offsetParent !== null)

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        onCloseRef.current()
        return
      }
      if (event.key !== 'Tab') return
      const items = focusable()
      if (items.length === 0) return
      const first = items[0]
      const last = items[items.length - 1]
      const active = document.activeElement
      if (!panel?.contains(active)) {
        event.preventDefault()
        const target = event.shiftKey ? last : first
        target.focus()
        return
      }
      if (event.shiftKey && (active === first || active === panel)) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && active === last) {
        event.preventDefault()
        first.focus()
      }
    }
    document.addEventListener('keydown', onKeyDown)

    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    const releaseOverlay = acquireViewportOverlay()
    const initialFocus = panel?.querySelector<HTMLElement>('[data-modal-autofocus]:not([disabled])')
    if (!panel?.contains(document.activeElement)) (initialFocus ?? panel)?.focus()

    return () => {
      releaseOverlay()
      document.removeEventListener('keydown', onKeyDown)
      document.body.style.overflow = previousOverflow
      previouslyFocused?.focus?.()
    }
  }, [open])

  if (!open) return null

  return (
    <div
      className="overlay-in modal-scrim fixed inset-0 z-40 flex items-center justify-center p-5 backdrop-blur-[6px]"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose()
      }}
    >
      <div
        ref={panelRef}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        style={{ width }}
        className={`dialog-in modal-sheet relative flex max-h-full max-w-full flex-col overflow-hidden rounded-2xl bg-surface outline-none ${panelClassName ?? ''}`}
      >
        <div className="prism-hairline h-[1.5px] w-full shrink-0 opacity-75" aria-hidden="true" />
        <header className="flex shrink-0 items-start justify-between gap-4 border-b border-line px-5 py-3.5">
          <div className="min-w-0">
            <h2 className="text-[14.5px] font-semibold tracking-tight text-ink">{title}</h2>
            {subtitle && <p className="mt-0.5 text-[12px] text-ink-dim">{subtitle}</p>}
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label={t('Close')}
            className="btn btn-ghost -mr-2 h-7 w-7 p-0"
          >
            <X size={15} strokeWidth={2} />
          </button>
        </header>

        <div className={bodyClassName ?? 'min-h-0 flex-1 overflow-y-auto px-5 py-4'}>{children}</div>

        {footer && (
          <footer className="flex shrink-0 items-center justify-end gap-2 border-t border-line bg-sidebar/75 px-5 py-3">
            {footer}
          </footer>
        )}
      </div>
    </div>
  )
}

interface ConfirmProps {
  open: boolean
  title: string
  body: string
  confirmLabel?: string
  destructive?: boolean
  onConfirm: () => void
  onCancel: () => void
}

/** Replaces window.confirm so destructive actions match the rest of the UI. */
export function ConfirmDialog({
  open,
  title,
  body,
  confirmLabel,
  destructive = false,
  onConfirm,
  onCancel,
}: ConfirmProps) {
  const { t } = useI18n()
  return (
    <Modal
      open={open}
      title={title}
      onClose={onCancel}
      width={420}
      footer={
        <>
          <button type="button" className="btn btn-default" onClick={onCancel}>
            {t('Cancel')}
          </button>
          <button
            type="button"
            className={destructive ? 'btn btn-danger' : 'btn btn-primary'}
            onClick={onConfirm}
            data-modal-autofocus
          >
            {confirmLabel ?? t('Confirm')}
          </button>
        </>
      }
    >
      <p className="leading-relaxed text-ink-dim">{body}</p>
    </Modal>
  )
}
