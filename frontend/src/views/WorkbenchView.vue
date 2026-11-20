<template>
  <section class="page" :data-module="meta.key">
    <header class="page-head">
      <div>
        <h2>{{ meta.name }}</h2>
        <p class="page-desc">{{ meta.desc }}</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">
          登记{{ meta.entity }}
        </button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">
          导出{{ meta.name }}清单
        </button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <p class="status-legend">
      <span v-for="item in statusSummary" :key="item.status" class="legend-item">
        {{ item.status }}：{{ item.count }}
      </span>
    </p>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in meta.fields" :key="column">{{ column }}</th>
          <th>当前状态</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in meta.fields" :key="column">{{ row[column] ?? '—' }}</td>
          <td>{{ row.status }}</td>
          <td class="row-actions">
            <button
              v-for="action in meta.actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="meta.fields.length + 2" class="empty-state">
            暂无{{ meta.name }}数据，可先登记{{ meta.entity }}
          </td>
        </tr>
      </tbody>
    </table>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记{{ meta.entity }}</h3>
        <label v-for="field in meta.fields" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <div v-if="meta.handoff" class="handoff-panel" data-testid="handoff-panel">
      <header class="handoff-head">
        <h3>{{ meta.handoff === 'studio' ? '审核发布交接' : '采集交付' }}</h3>
        <p class="page-desc">
          {{ meta.handoff === 'studio'
            ? '在任务行上选择「提交审核」「发布规则」，这里展示审核版本、冲突页面和发布候选。'
            : '已发布模板可生成采集输入并创建采集批次；批次只使用确认过的完整字段配对。' }}
        </p>
      </header>

      <div v-if="meta.handoff === 'studio'" class="handoff-body">
        <label class="filter-item">
          <span>任务</span>
          <select v-model.number="selectedTaskId" data-testid="handoff-task-select" @change="loadStudioHandoff">
            <option v-for="row in rows" :key="String(row.id)" :value="Number(row.id)">
              #{{ row.id }} {{ row['任务名称'] }}（{{ row.status }}）
            </option>
          </select>
        </label>
        <div class="page-actions">
          <button class="btn" type="button" data-testid="handoff-submit-review" @click="submitReview">提交审核</button>
          <button class="btn primary" type="button" data-testid="handoff-publish" @click="publishSelected">发布规则</button>
          <button class="btn ghost" type="button" data-testid="handoff-refresh" @click="loadStudioHandoff">刷新状态</button>
        </div>

        <div v-if="collectionVersion" class="handoff-grid">
          <article class="handoff-card">
            <span class="stat-label">审核版本</span>
            <strong>{{ collectionVersion.review_version ?? '—' }}</strong>
            <span class="handoff-note">任务状态：{{ collectionVersion.task_status }}</span>
          </article>
          <article class="handoff-card">
            <span class="stat-label">发布状态</span>
            <strong :class="{ 'error-text': collectionVersion.publish_status === '暂挂' }">
              {{ collectionVersion.publish_status }}
            </strong>
            <span class="handoff-note">数据集：{{ collectionVersion.dataset_status ?? '—' }}</span>
          </article>
          <article class="handoff-card">
            <span class="stat-label">交付模板</span>
            <strong>{{ collectionVersion.template_ids.length }}</strong>
            <span class="handoff-note">{{ collectionVersion.effective ? '可生成采集输入' : '未生效' }}</span>
          </article>
        </div>

        <p v-if="collectionVersion" class="handoff-note">
          冲突字段：{{ collectionVersion.impacted_fields.join('、') || '无' }}；
          冲突页面：{{ collectionVersion.impacted_pages.join('、') || '无' }}
        </p>

        <table v-if="conflicts.length" class="data-table" data-testid="conflict-table">
          <thead>
            <tr><th>字段</th><th>严重度</th><th>选择器</th><th>涉及布局</th><th>涉及页面</th><th>说明</th></tr>
          </thead>
          <tbody>
            <tr v-for="conflict in conflicts" :key="conflict.field" :data-severity="conflict.severity">
              <td>{{ conflict.field }}</td>
              <td>{{ conflict.severity }}</td>
              <td>{{ conflict.selectors.join(' / ') }}</td>
              <td>{{ conflict.layouts.join('、') }}</td>
              <td>{{ conflict.pages.join('、') }}</td>
              <td>{{ conflict.explanation }}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <div v-else class="handoff-body">
        <label class="filter-item">
          <span>已发布模板</span>
          <select v-model.number="selectedTemplateId" data-testid="handoff-template-select" @change="loadTemplateHandoff">
            <option v-for="row in collectableTemplates" :key="String(row.id)" :value="Number(row.id)">
              #{{ row.id }} {{ row['模板名称'] }}
            </option>
          </select>
        </label>

        <div v-if="collectionInput" class="handoff-grid">
          <article class="handoff-card">
            <span class="stat-label">审核版本</span>
            <strong>{{ collectionInput.review_version }}</strong>
            <span class="handoff-note">{{ collectionInput.layout }} / {{ collectionInput.site }}</span>
          </article>
          <article class="handoff-card">
            <span class="stat-label">采集字段</span>
            <strong>{{ collectionInput.field_count }}</strong>
            <span class="handoff-note">{{ collectionInput.pairs.map((pair) => pair.field).join('、') }}</span>
          </article>
          <article class="handoff-card">
            <span class="stat-label">采集页面</span>
            <strong>{{ collectionInput.pages.length }}</strong>
            <span class="handoff-note">{{ collectionInput.pages.join('、') }}</span>
          </article>
        </div>

        <div class="page-actions">
          <button class="btn primary" type="button" data-testid="handoff-create-batch" @click="createBatch">确认字段并建批次</button>
        </div>

        <table v-if="batches.length" class="data-table" data-testid="batch-table">
          <thead>
            <tr><th>批次</th><th>状态</th><th>审核版本</th><th>确认字段</th><th>选择器快照</th><th>页面</th><th>采集结果</th><th>操作</th></tr>
          </thead>
          <tbody>
            <tr v-for="batch in batches" :key="batch.id" :data-testid="`batch-${batch.id}`">
              <td>#{{ batch.id }}</td>
              <td>{{ batch.status }}</td>
              <td>{{ batch.review_version }}</td>
              <td>{{ batch.confirmed_fields.join('、') }}</td>
              <td>{{ batch.selector_snapshot.map((item) => `${item.field}=${item.selector}`).join(', ') }}</td>
              <td>{{ batch.pages.join('、') }}</td>
              <td>
                <span v-if="batch.match_ratio !== null">命中率 {{ batch.match_ratio }}</span>
                <span v-else>未运行</span>
              </td>
              <td>
                <button
                  class="link"
                  type="button"
                  :data-testid="`run-batch-${batch.id}`"
                  :disabled="batch.status === '已完成'"
                  @click="runBatch(batch.id)"
                >
                  运行采集
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条{{ meta.name }}记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  createEntry,
  downloadEntries,
  handoffApi,
  listEntries,
  moduleMeta,
  runAction as applyAction,
} from '@/api/backend-service'
import type { CollectionBatch, CollectionInput, CollectionVersion, Conflict } from '@/api/backend-service'
import type { EntryRow } from '@/data/types'

const props = defineProps<{ moduleKey: string }>()
const meta = moduleMeta(props.moduleKey)
const rows = ref<EntryRow[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = meta.fields.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})

// ---------------------------------------------------------------- 发布交接
const selectedTaskId = ref(0)
const selectedTemplateId = ref(0)
const conflicts = ref<Conflict[]>([])
const collectionVersion = ref<CollectionVersion | null>(null)
const collectionInput = ref<CollectionInput | null>(null)
const batches = ref<CollectionBatch[]>([])
// 只有由审核数据集生成、且仍在发布状态的模板才能交接采集。
const collectableTemplates = computed(() =>
  rows.value.filter((row) => row.status === '已发布' && row['来源数据集'] != null),
)

const stats = computed(() => [
  { label: meta.metrics[0], value: total.value },
  { label: meta.metrics[1], value: rows.value.filter((row) => row.status === meta.statuses[1]).length },
  {
    label: meta.metrics[2],
    value: rows.value.filter((row) => row.status === meta.statuses[meta.statuses.length - 1]).length,
  },
])
const statusSummary = computed(() =>
  meta.statuses.map((status) => ({
    status,
    count: rows.value.filter((row) => String(row.status) === status).length,
  })),
)

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  try {
    await downloadEntries(meta.key, filters.value)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(meta.fields.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const result = await createEntry(meta.key, createValues.value)
    if (!result.ok) throw new Error(result.message)
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: EntryRow) {
  errorMessage.value = ''
  try {
    const result = await applyAction(meta.key, Number(row.id), action)
    if (!result.ok) throw new Error(result.message)
    await reload()
    if (meta.handoff === 'studio') {
      selectedTaskId.value = Number(row.id)
      await loadStudioHandoff()
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '操作失败'
  }
}

// ---------------------------------------------------------------- 发布交接
async function loadStudioHandoff() {
  if (!selectedTaskId.value) return
  errorMessage.value = ''
  try {
    const [version, report] = await Promise.all([
      handoffApi.collectionVersion(selectedTaskId.value),
      handoffApi.conflicts(selectedTaskId.value),
    ])
    collectionVersion.value = version
    conflicts.value = report.conflicts
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '交接状态读取失败'
  }
}

async function submitReview() {
  if (!selectedTaskId.value) return
  errorMessage.value = ''
  try {
    const result = await handoffApi.submitReview(selectedTaskId.value)
    await reload()
    await loadStudioHandoff()
    if (result.duplicate) errorMessage.value = result.message
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '提交审核失败'
  }
}

async function publishSelected() {
  if (!selectedTaskId.value) return
  errorMessage.value = ''
  try {
    const result = await handoffApi.publish(selectedTaskId.value)
    await reload()
    await loadStudioHandoff()
    if (result.publish_status === '暂挂') errorMessage.value = result.message
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '发布失败'
  }
}

async function loadTemplateHandoff() {
  if (!selectedTemplateId.value) {
    collectionInput.value = null
    batches.value = []
    return
  }
  errorMessage.value = ''
  try {
    const [input, batchList] = await Promise.all([
      handoffApi.collectionInput(selectedTemplateId.value),
      handoffApi.batches(selectedTemplateId.value),
    ])
    collectionInput.value = input
    batches.value = batchList.items
  } catch (error) {
    collectionInput.value = null
    batches.value = []
    errorMessage.value = error instanceof Error ? error.message : '采集输入读取失败'
  }
}

async function createBatch() {
  if (!collectionInput.value) return
  errorMessage.value = ''
  try {
    await handoffApi.createBatch(
      selectedTemplateId.value,
      collectionInput.value.pages,
      collectionInput.value.pairs.map((pair) => pair.field),
    )
    await loadTemplateHandoff()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集批次创建失败'
  }
}

async function runBatch(batchId: number) {
  errorMessage.value = ''
  try {
    await handoffApi.runBatch(batchId)
    await reload()
    await loadTemplateHandoff()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集批次运行失败'
  }
}

async function reload() {
  errorMessage.value = ''
  try {
    const payload = await listEntries(meta.key, filters.value)
    rows.value = payload.items
    total.value = payload.total
    if (meta.handoff === 'studio') {
      const exists = payload.items.some((row) => Number(row.id) === selectedTaskId.value)
      if (!exists && payload.items.length) selectedTaskId.value = Number(payload.items[0].id)
      await loadStudioHandoff()
    }
    if (meta.handoff === 'template') {
      const exists = collectableTemplates.value.some(
        (row) => Number(row.id) === selectedTemplateId.value,
      )
      if (!exists) {
        selectedTemplateId.value = collectableTemplates.value.length
          ? Number(collectableTemplates.value[0].id)
          : 0
      }
      await loadTemplateHandoff()
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '业务列表读取失败'
  }
}

onMounted(reload)
</script>
