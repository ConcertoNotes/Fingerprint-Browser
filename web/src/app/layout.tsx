import type { Metadata } from 'next'

import { AppShell } from '@/components/app-shell'
import { I18nProvider } from '@/components/i18n-provider'
import { ThemeProvider } from '@/components/theme-provider'
import { ToastProvider } from '@/components/toast'
import { WorkspaceProvider } from '@/components/workspace-provider'
import './fonts.css'
import './globals.css'

export const metadata: Metadata = {
  title: '棱镜浏览器 · Prism',
  description: '棱镜浏览器 (Prism Browser) — 极简高隔离桌面指纹浏览器与全球节点工作台',
}

const THEME_INIT_SCRIPT = `(function(){try{var t=localStorage.getItem('prism.theme');var m=(t==='light'||t==='dark')?t:'dark';document.documentElement.dataset.theme=m;document.documentElement.style.colorScheme=m;}catch(e){document.documentElement.dataset.theme='dark';document.documentElement.style.colorScheme='dark';}})();`

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN" data-theme="dark" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_INIT_SCRIPT }} />
      </head>
      <body>
        <ThemeProvider>
          <I18nProvider>
            <ToastProvider>
              <WorkspaceProvider>
                <AppShell>{children}</AppShell>
              </WorkspaceProvider>
            </ToastProvider>
          </I18nProvider>
        </ThemeProvider>
      </body>
    </html>
  )
}
