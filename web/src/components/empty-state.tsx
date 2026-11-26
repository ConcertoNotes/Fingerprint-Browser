interface Props {
  icon: React.ReactNode
  title: string
  body: string
  action?: React.ReactNode
}

/** Elevated empty-state panel with subtle obsidian/porcelain focal icon plate. */
export function EmptyState({ icon, title, body, action }: Props) {
  return (
    <div className="flex flex-col items-center justify-center px-6 py-20 text-center">
      <div className="relative mb-4 flex h-12 w-12 items-center justify-center rounded-2xl border border-line-strong bg-surface text-signal shadow-[0_8px_24px_-8px_rgba(124,123,255,0.28)]">
        {icon}
      </div>
      <h2 className="text-[14.5px] font-semibold tracking-tight text-ink">{title}</h2>
      <p className="mt-1.5 max-w-[44ch] text-[12.5px] leading-relaxed text-ink-dim">{body}</p>
      {action && <div className="mt-5">{action}</div>}
    </div>
  )
}
