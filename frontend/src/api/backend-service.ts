import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type { ActionResult, EntryRow, OverviewResult, PageResult } from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

// ---------------------------------------------------------------- 发布交接

async function postJson<T>(path: string, body: unknown): Promise<T> {
  const response = await request(path, { method: 'POST', body: JSON.stringify(body) })
  const payload = await response.json()
  if (!response.ok) {
    const detail =
      typeof payload?.detail === 'string' ? payload.detail : '接口未返回可读原因'
    throw new Error(detail)
  }
  return payload as T
}

async function getJson<T>(path: string): Promise<T> {
  const response = await request(path)
  const payload = await response.json()
  if (!response.ok) {
    const detail =
      typeof payload?.detail === 'string' ? payload.detail : '接口未返回可读原因'
    throw new Error(detail)
  }
  return payload as T
}

export type ConflictVariant = {
  selector: string
  layouts: string[]
  pages: string[]
  tasks: { id: number; name: string; review_version: string; receipt_id: number | null }[]
}

export type Conflict = {
  field: string
  site: string
  severity: string
  selectors: string[]
  variants: ConflictVariant[]
  pages: string[]
  layouts: string[]
  explanation: string
}

export type CollectionVersion = {
  ok: boolean
  task_id: number
  task_status: string
  publish_status: string
  review_version: string | null
  review_receipt_id: number | null
  dataset_id: number | null
  dataset_status: string | null
  template_ids: number[]
  effective: boolean
  conflict_count: number
  impacted_fields: string[]
  impacted_pages: string[]
}

export type StudioPublishResult = {
  ok: boolean
  duplicate: boolean
  message: string
  publish_status: string
  held: boolean
  receipt: { id: number; review_version: string } | null
  candidate: { id: number; review_version: string; status: string } | null
  dataset: { id: number; status: string; review_version: string; fields: { field: string; selector: string }[] }
  template_ids: number[]
  conflicts: Conflict[]
  impacted_fields: string[]
  impacted_pages: string[]
  review_sync: CollectionVersion
}

export type CollectionInput = {
  ok: boolean
  template_id: number
  template_name: string
  dataset_id: number
  review_version: string
  task_id: number
  site: string
  layout: string
  pages: string[]
  pairs: { field: string; selector: string }[]
  field_count: number
  conflicts: Conflict[]
}

export type CollectionBatch = {
  id: number
  template_id: number
  dataset_id: number
  task_id: number
  site: string
  layout: string
  status: string
  review_version: string
  review_receipt_id: number
  pages: string[]
  confirmed_fields: string[]
  selector_snapshot: { field: string; selector: string; layout: string; pages: string[] }[]
  selector_snapshot_hash: string
  field_data: Record<string, { page: string; value: string | null; matched: boolean; selector: string }[]>
  match_ratio: number | null
  review_state_at_confirm: Record<string, unknown> | null
  review_state_at_complete: Record<string, unknown> | null
  current_review_state: Record<string, unknown> | null
  failure_reason: string | null
}

export type PublishTrace = {
  ok: boolean
  task: EntryRow
  receipts: Record<string, unknown>[]
  candidates: Record<string, unknown>[]
  datasets: Record<string, unknown>[]
  batches: Record<string, unknown>[]
  current_receipt: Record<string, unknown> | null
  collection_version: CollectionVersion
}

export const handoffApi = {
  submitReview: (taskId: number) =>
    postJson<StudioPublishResult & { message: string }>(`/api/extract_studio/${taskId}/review-submit`, {}),
  publish: (taskId: number) =>
    postJson<StudioPublishResult>(`/api/extract_studio/${taskId}/publish`, {}),
  conflicts: (taskId: number) =>
    getJson<{ ok: boolean; site: string; blocking: boolean; conflicts: Conflict[]; impacted_fields: string[]; impacted_pages: string[] }>(
      `/api/extract_studio/${taskId}/conflicts`,
    ),
  collectionVersion: (taskId: number) =>
    getJson<CollectionVersion>(`/api/extract_studio/${taskId}/collection-version`),
  trace: (taskId: number) => getJson<PublishTrace>(`/api/extract_studio/${taskId}/publish-trace`),
  collectionInput: (templateId: number) =>
    getJson<CollectionInput>(`/api/template_rule/${templateId}/collection-input`),
  createBatch: (templateId: number, pages: string[], confirmedFields: string[]) =>
    postJson<{ ok: boolean; batch: CollectionBatch }>(
      `/api/template_rule/${templateId}/collection-batches`,
      { pages, confirmed_fields: confirmedFields },
    ),
  runBatch: (batchId: number) =>
    postJson<{ ok: boolean; message: string; batch: CollectionBatch }>(
      `/api/collection_batches/${batchId}/run`,
      {},
    ),
  batches: (templateId: number) =>
    getJson<{ ok: boolean; items: CollectionBatch[] }>(
      `/api/template_rule/${templateId}/collection-batches`,
    ),
}
