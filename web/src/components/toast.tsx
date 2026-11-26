'use client'

import { createContext, useCallback, useContext, useEffect, useState } from 'react'
import { Check, Info, TriangleAlert, X } from 'lucide-react'
import { useI18n } from '@/components/i18n-provider'

type ToastKind = 'ok' | 'error' | 'info'

interface Toast {
  id: number
  kind: ToastKind
  title: string
  detail?: string
}

const ToastContext = createContext<{
  toast: (kind: ToastKind, title: string, detail?: string) => void
}>({ toast: () => {} })

/** Non-blocking, stacked, self-dismissing notifications positioned cleanly above the bottom status bar. */
export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([])

  const dismiss = useCallback((id: number) => {
    setToasts((current) => current.filter((t) => t.id !== id))
  }, [])

  const toast = useCallback((kind: ToastKind, title: string, detail?: string) => {
    const id = Date.now() + Math.random()
    setToasts((current) => [...current, { id, kind, title, detail }])
  }, [])

  return (
    <ToastContext.Provider value={{ toast }}>
      {children}
      <div
        className="pointer-events-none fixed bottom-10 right-4 z-50 flex w-[356px] flex-col gap-2"
        role="status"
        aria-live="polite"
      >
        {toasts.map((t) => (
          <ToastRow key={t.id} toast={t} onDismiss={() => dismiss(t.id)} />
        ))}
      </div>
    </ToastContext.Provider>
  )
}

function ToastRow({ toast, onDismiss }: { toast: Toast; onDismiss: () => void }) {
  const { t } = useI18n()
  useEffect(() => {
    const timer = setTimeout(onDismiss, toast.kind === 'error' ? 7500 : 3800)
    return () => clearTimeout(timer)
  }, [onDismiss, toast.kind])

  const Icon = toast.kind === 'ok' ? Check : toast.kind === 'error' ? TriangleAlert : Info
  const badge =
    toast.kind === 'ok'
      ? 'bg-ok-shade text-ok ring-1 ring-ok/25'
      : toast.kind === 'error'
        ? 'bg-danger-shade text-danger ring-1 ring-danger/25'
        : 'bg-signal-shade text-signal ring-1 ring-signal/25'
  const accentBar =
    toast.kind === 'ok'
      ? 'bg-ok-dot'
      : toast.kind === 'error'
        ? 'bg-danger-dot'
        : 'bg-signal'

  return (
    <div className="dialog-in modal-sheet pointer-events-auto relative flex items-start gap-2.5 overflow-hidden rounded-xl p-3 pl-3.5 backdrop-blur-xl">
      <span className={`absolute bottom-2 left-0 top-2 w-[3px] rounded-r-full ${accentBar}`} aria-hidden="true" />
      <span className={`mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full ${badge}`}>
        <Icon size={11.5} strokeWidth={2.4} />
      </span>
      <div className="min-w-0 flex-1">
        <div className="text-[12.5px] font-medium text-ink">{toast.title}</div>
        {toast.detail && (
          <div className="mt-0.5 whitespace-pre-wrap break-words text-[11.5px] text-ink-dim">{toast.detail}</div>
        )}
      </div>
      <button
        type="button"
        onClick={onDismiss}
        aria-label={t('Dismiss')}
        className="h-fit cursor-pointer rounded p-0.5 text-ink-faint transition-colors hover:bg-raised hover:text-ink"
      >
        <X size={13} strokeWidth={2} />
      </button>
    </div>
  )
}

export function useToast() {
  return useContext(ToastContext).toast
}
