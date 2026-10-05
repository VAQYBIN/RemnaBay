import { $api } from '@/api/client'
import { Module, PageTitle } from '@/components/frame/Module'
import { RemnaBayMark } from '@/components/frame/RemnaBayMark'

// Текст лицензии лежит в репозитории (решение 0039)
const FONT_LICENSE_PATH = '/blob/main/third-party-licenses/golos-text-OFL.txt'

/** А12. О программе (1.26) — единственное место, где упоминается RemnaBay. */
export function AboutPage() {
  const { data } = $api.useQuery('get', '/api/admin/about')
  return (
    <>
      <PageTitle>О программе</PageTitle>
      <Module title="RemnaBay" className="max-w-2xl">
        <div className="flex flex-col gap-5">
          <div className="flex items-center gap-4">
            <RemnaBayMark className="size-14" />
            <div>
              <p className="text-xl font-semibold">RemnaBay</p>
              <p className="text-sm text-muted-foreground">Магазин VPN для Remnawave</p>
            </div>
          </div>
          {data && (
            <dl className="grid gap-3 text-sm sm:grid-cols-[auto_1fr] sm:gap-x-6">
              <dt className="text-muted-foreground">Версия</dt>
              <dd className="tabular">{data.version}</dd>
              <dt className="text-muted-foreground">Лицензия</dt>
              <dd>{data.license}</dd>
              <dt className="text-muted-foreground">Исходный код</dt>
              <dd className="min-w-0 break-all">
                <a href={data.repository_url} target="_blank" rel="noreferrer" className="text-brand-text underline-offset-4 hover:underline">
                  {data.repository_url}
                </a>
              </dd>
              <dt className="text-muted-foreground">Шрифт</dt>
              <dd>
                {data.font},{' '}
                <a href={data.repository_url + FONT_LICENSE_PATH} target="_blank" rel="noreferrer" className="text-brand-text underline-offset-4 hover:underline">
                  {data.font_license}
                </a>
              </dd>
            </dl>
          )}
          <p className="rounded-xl bg-muted p-4 text-sm text-muted-foreground">
            Неофициальный проект сообщества. Не связан с командой Remnawave.
          </p>
        </div>
      </Module>
    </>
  )
}
