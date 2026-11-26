'use client'

import { useState } from 'react'
import { LoaderCircle, Plus, ShieldCheck, Sparkles } from 'lucide-react'

import { useI18n } from '@/components/i18n-provider'
import { Modal } from '@/components/modal'
import { useToast } from '@/components/toast'
import {
  groupsAPI,
  isStaleWrite,
  OS_LABELS,
  profileFingerprintHash,
  profilesAPI,
  shortGpuName,
  type Group,
  type Profile,
} from '@/lib/api'

interface Props {
  profile: Profile | null
  groups: Group[]
  onClose: () => void
  onSaved: () => void | Promise<void>
  onGroupCreated?: (group: Group) => void
}

/** Focused profile metadata & isolation policy editor for existing browsers. */
export function EditBrowserModal({ profile, groups, onClose, onSaved, onGroupCreated }: Props) {
  const { t } = useI18n()
  const toast = useToast()
  const [name, setName] = useState(profile?.name ?? '')
  const [group, setGroup] = useState(profile?.group ?? '')
  const [tagsText, setTagsText] = useState((profile?.tags ?? []).join(', '))
  const [notes, setNotes] = useState(profile?.notes ?? '')
  const [ipDriftGuard, setIpDriftGuard] = useState(profile?.ip_drift_guard ?? true)
  const [creatingGroup, setCreatingGroup] = useState(false)
  const [newGroupName, setNewGroupName] = useState('')
  const [saving, setSaving] = useState(false)
  const [reoptimizing, setReoptimizing] = useState(false)
  const [error, setError] = useState<string | null>(null)

  if (!profile) return null

  const iso = profile.isolation
  const summary = profile.isolation_summary
  const fpHash = profileFingerprintHash(profile)
  const osName = summary?.os ?? iso?.os ?? profile.browser_settings?.os ?? 'macos'
  const screen = summary?.screen ?? profile.fingerprint?.screen ?? profile.browser_settings?.screen ?? '—'
  const gpu = shortGpuName(summary?.gpu ?? profile.fingerprint?.gpu)
  const policyVer = summary?.policy_version ?? iso?.policy_version ?? 'builtin'
  const tzMode = summary?.timezone_mode ?? iso?.timezone_mode ?? 'fixed'
  const tzValue = summary?.timezone ?? iso?.timezone ?? 'America/Los_Angeles'
  const localeMode = summary?.locale_mode ?? iso?.locale_mode ?? 'fixed'
  const langs = (summary?.languages ?? iso?.languages ?? ['en-US', 'en']).join(', ')
  const guardOn = summary?.install_webrtc_guard ?? iso?.install_webrtc_guard ?? true

  async function createGroupInline() {
    const trimmed = newGroupName.trim()
    if (!trimmed) return
    try {
      const created = await groupsAPI.create({ name: trimmed })
      onGroupCreated?.(created)
      setGroup(created.id)
      setNewGroupName('')
      setCreatingGroup(false)
      toast('ok', t('Group created'), created.name)
    } catch (err) {
      toast('error', t('Could not create group'), t(err instanceof Error ? err.message : String(err)))
    }
  }

  async function save(event: React.FormEvent) {
    event.preventDefault()
    if (!profile || !name.trim() || saving) return
    setSaving(true)
    setError(null)
    try {
      const tags = tagsText
        .split(/[,，]/)
        .map(tag => tag.trim())
        .filter(Boolean)
      await profilesAPI.updateProfile(profile.id, {
        name: name.trim(),
        group: group || null,
        tags,
        notes: notes.trim() || null,
        ip_drift_guard: ipDriftGuard,
        row_version: profile.row_version,
      })
      toast('ok', t('Browser saved'), name.trim())
      await onSaved()
      onClose()
    } catch (err) {
      setError(
        isStaleWrite(err)
          ? t('This browser changed elsewhere. Close and reload before saving.')
          : t(err instanceof Error ? err.message : String(err)),
      )
    } finally {
      setSaving(false)
    }
  }

  async function handleReoptimize() {
    if (!profile || reoptimizing) return
    setReoptimizing(true)
    try {
      await profilesAPI.reoptimizeProfile(profile.id)
      toast('ok', t('Cloud isolation policy applied'), profile.name)
      await onSaved()
    } catch (err) {
      toast('error', t('Action failed'), t(err instanceof Error ? err.message : String(err)))
    } finally {
      setReoptimizing(false)
    }
  }

  return (
    <Modal
      open
      title={t('Edit {name}', { name: profile.name })}
      subtitle={fpHash ? `${fpHash} · ${OS_LABELS[osName] ?? osName}` : OS_LABELS[osName] ?? osName}
      width={540}
      onClose={() => { if (!saving) onClose() }}
      footer={
        <>
          <button type="button" className="btn btn-default" disabled={saving} onClick={onClose}>
            {t('Cancel')}
          </button>
          <button
            type="submit"
            form="edit-browser-form"
            className="btn btn-primary"
            disabled={saving || !name.trim()}
          >
            {saving && <LoaderCircle size={13} className="animate-spin" aria-hidden="true" />}
            {t('Save changes')}
          </button>
        </>
      }
    >
      <form id="edit-browser-form" onSubmit={save} className="space-y-4">
        <div>
          <label className="field-label" htmlFor="edit-browser-name">{t('Browser name')}</label>
          <input
            id="edit-browser-name"
            className="field"
            value={name}
            maxLength={100}
            onChange={event => setName(event.target.value)}
            data-modal-autofocus
          />
        </div>

        <div>
          <div className="mb-1 flex items-center justify-between">
            <label className="field-label mb-0" htmlFor="edit-browser-group">{t('Group')}</label>
            <button
              type="button"
              className="inline-flex items-center gap-1 text-[11px] font-medium text-signal hover:underline"
              onClick={() => setCreatingGroup(v => !v)}
            >
              <Plus size={11} aria-hidden="true" />
              {t('New group')}
            </button>
          </div>
          {creatingGroup ? (
            <div className="flex gap-2">
              <input
                className="field"
                value={newGroupName}
                onChange={event => setNewGroupName(event.target.value)}
                placeholder={t('Group name')}
                maxLength={60}
              />
              <button type="button" className="btn btn-primary shrink-0" onClick={createGroupInline} disabled={!newGroupName.trim()}>
                {t('Create')}
              </button>
              <button type="button" className="btn btn-ghost shrink-0" onClick={() => setCreatingGroup(false)}>
                {t('Cancel')}
              </button>
            </div>
          ) : (
            <select
              id="edit-browser-group"
              className="field"
              value={group}
              onChange={event => setGroup(event.target.value)}
            >
              <option value="">{t('Ungrouped')}</option>
              {groups.map(item => (
                <option key={item.id} value={item.id}>{item.name}</option>
              ))}
            </select>
          )}
        </div>

        <div>
          <label className="field-label" htmlFor="edit-browser-tags">{t('Tags')}</label>
          <input
            id="edit-browser-tags"
            className="field"
            value={tagsText}
            onChange={event => setTagsText(event.target.value)}
            placeholder={t('Separate tags with commas')}
          />
        </div>

        <div>
          <label className="field-label" htmlFor="edit-browser-notes">{t('Memo')}</label>
          <textarea
            id="edit-browser-notes"
            className="field min-h-[72px] resize-y"
            value={notes}
            onChange={event => setNotes(event.target.value)}
            placeholder={t('Account notes, purpose, or reminders…')}
          />
        </div>

        <label className="flex cursor-pointer items-center gap-2.5 rounded-xl border border-line bg-sidebar/70 px-3.5 py-2.5 text-[12.5px] text-ink">
          <input
            type="checkbox"
            checked={ipDriftGuard}
            onChange={event => setIpDriftGuard(event.target.checked)}
            className="accent-signal"
          />
          <span>{t('Exit IP drift guard (block launch if exit IP or country changes)')}</span>
        </label>

        {/* Fixed Hardware Identity Summary */}
        <div className="rounded-xl border border-line bg-sidebar/70 p-3.5">
          <div className="flex items-center justify-between gap-2">
            <div className="flex items-center gap-2">
              <ShieldCheck size={14} className="text-ok" aria-hidden="true" />
              <span className="text-[12px] font-semibold text-ink">{t('Pinned isolation identity')}</span>
              {fpHash && <span className="rounded-full bg-surface px-2 py-0.5 font-mono text-[10.5px] font-medium text-ink ring-1 ring-line">{fpHash}</span>}
            </div>
            <button
              type="button"
              className="btn btn-default h-[26px] px-2.5 text-[11.5px]"
              disabled={reoptimizing}
              onClick={handleReoptimize}
            >
              {reoptimizing ? <LoaderCircle size={12} className="animate-spin" aria-hidden="true" /> : <Sparkles size={12} className="text-signal" aria-hidden="true" />}
              {t('Re-optimize policy')}
            </button>
          </div>
          <dl className="mt-2.5 grid grid-cols-2 gap-x-4 gap-y-1.5 text-[11.5px]">
            <div><dt className="text-ink-faint">{t('Device')}</dt><dd className="font-mono text-ink">{OS_LABELS[osName] ?? osName} · {screen}</dd></div>
            <div><dt className="text-ink-faint">{t('Policy')}</dt><dd className="font-mono text-ink">{policyVer}</dd></div>
            <div className="col-span-2"><dt className="text-ink-faint">{t('Graphics')}</dt><dd className="truncate font-mono text-ink" title={gpu}>{gpu}</dd></div>
            <div><dt className="text-ink-faint">{t('Timezone')}</dt><dd className="font-mono text-ink">{tzMode === 'fixed' ? tzValue : t('Follow exit IP')}</dd></div>
            <div><dt className="text-ink-faint">{t('Language')}</dt><dd className="font-mono text-ink">{localeMode === 'fixed' ? langs : t('Follow exit IP')}</dd></div>
            <div className="col-span-2">
              <dt className="text-ink-faint">{t('Protection')}</dt>
              <dd className="mt-0.5 flex flex-wrap gap-1.5">
                {guardOn && <span className="rounded-full bg-ok-shade px-2 py-0.5 text-[10.5px] font-medium text-ok">{t('WebRTC/DNS guard active')}</span>}
                <span className="rounded-full bg-signal-shade px-2 py-0.5 text-[10.5px] font-medium text-signal">{t('Full-traffic fail-closed tunnel')}</span>
              </dd>
            </div>
          </dl>
        </div>

        {error && <p className="text-[12px] text-danger" role="alert">{error}</p>}
      </form>
    </Modal>
  )
}
