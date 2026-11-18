<template>
  <section class="page" data-module="auth">
    <header class="page-head">
      <div>
        <h2>凭证管理管理</h2>
        <p class="page-desc">维护认证凭证，围绕凭证编号、凭证类型、关联数据源、凭证内容做登记、筛选与状态流转。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">登记认证凭证</button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">导出凭证管理清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <div class="panel" data-testid="rotation-panel">
      <div class="panel-head">
        <h3>多源凭证批量轮换</h3>
        <p class="panel-desc">
          勾选下方凭证的关联源端，按策略生成凭证轮换批次及来源引用快照；批次完成后每个源端只保留一份可用凭据。
        </p>
      </div>
      <div class="rotation-bar">
        <label class="rotation-field">
          <span>轮换策略</span>
          <select v-model="strategy" data-testid="rotation-strategy">
            <option value="先吊销后激活">先吊销后激活（安全边界前置，轮换期间暂停引用）</option>
            <option value="先激活后吊销">先激活后吊销（可用性前置，轮换期间记录凭据重叠）</option>
          </select>
        </label>
        <label class="rotation-field">
          <span>统一新有效期至（可选）</span>
          <input v-model="newExpiry" data-testid="rotation-expiry" type="date" />
        </label>
        <button
          class="btn primary"
          data-testid="submit-rotation"
          type="button"
          :disabled="!selectedSources.length || submitting"
          @click="submitRotation"
        >
          {{ submitting ? '提交中…' : `提交轮换批次（已选 ${selectedSources.length} 个源端）` }}
        </button>
      </div>
      <p v-if="rotationMessage" class="rotation-message" data-testid="rotation-message">{{ rotationMessage }}</p>
      <ul v-if="rotationResults.length" class="rotation-results">
        <li v-for="(line, index) in rotationResults" :key="index">{{ line }}</li>
      </ul>
    </div>

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
          <th class="check-col">轮换</th>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td class="check-col">
            <input
              v-model="selectedSources"
              type="checkbox"
              :value="String(row['关联数据源'] ?? '')"
              :disabled="!rotatable(row)"
              :data-testid="`select-${row.id}`"
            />
          </td>
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
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
          <td :colspan="columns.length + 2" class="empty-state">暂无凭证管理数据，可先登记认证凭证</td>
        </tr>
      </tbody>
    </table>

    <div class="panel" data-testid="batch-panel">
      <div class="panel-head">
        <h3>凭证轮换批次</h3>
        <p class="panel-desc">一次提交多点记录进入批量处理，逐条返回状态；点「查看」看轮换进度与旧新凭据引用。</p>
        <button class="btn ghost" type="button" @click="loadBatches">刷新批次</button>
      </div>
      <table class="data-table">
        <thead>
          <tr>
            <th>批次编号</th>
            <th>轮换策略</th>
            <th>批次状态</th>
            <th>轮换进度</th>
            <th>已拒绝</th>
            <th>提交时间</th>
            <th>完成时间</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="batch in batches" :key="String(batch.id)" :data-testid="`batch-${batch.id}`">
            <td>{{ batch['批次编号'] }}</td>
            <td>{{ batch['策略'] }}</td>
            <td><span class="tag" :data-state="String(batch.status)">{{ batch.status }}</span></td>
            <td>{{ batch['已切换数'] }}/{{ Number(batch['记录总数']) - Number(batch['已拒绝数']) }}</td>
            <td>{{ batch['已拒绝数'] }}</td>
            <td>{{ batch['提交时间'] }}</td>
            <td>{{ batch['完成时间'] || '—' }}</td>
            <td class="row-actions">
              <button class="link" type="button" :data-testid="`view-batch-${batch.id}`" @click="loadBatchDetail(Number(batch.id))">查看</button>
            </td>
          </tr>
          <tr v-if="!batches.length">
            <td colspan="8" class="empty-state">暂无轮换批次，勾选凭证后提交轮换批次</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div v-if="detail" class="panel" data-testid="batch-detail">
      <div class="panel-head">
        <h3>批次 {{ detail.batch['批次编号'] }} 明细 · {{ detail.batch['策略'] }}</h3>
        <p class="panel-desc">旧凭据吊销回执与新凭据生效回执同时到达同一源端时，按策略定序落库，不看到达先后。</p>
        <button
          v-if="detail.batch.status !== '已完成'"
          class="btn"
          data-testid="receipt-all"
          type="button"
          @click="receiptAll"
        >
          登记全部剩余回执
        </button>
      </div>
      <table class="data-table">
        <thead>
          <tr>
            <th>源端编号</th>
            <th>旧凭据引用</th>
            <th>新凭据引用</th>
            <th>有效期解析</th>
            <th>最后使用</th>
            <th>凭据引用状态</th>
            <th>吊销回执</th>
            <th>生效回执</th>
            <th>说明</th>
            <th>回执操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in detail.items" :key="String(item.id)" :data-testid="`item-${item.id}`">
            <td>{{ item['源端编号'] }}</td>
            <td>{{ item['旧凭据引用'] || '—' }}</td>
            <td>{{ item['新凭据引用'] || '—' }}</td>
            <td>{{ item['有效期解析'] || '—' }}</td>
            <td>{{ item['最后使用'] || '—' }}</td>
            <td><span class="tag" :data-state="String(item['状态'])">{{ item['状态'] }}</span></td>
            <td>{{ item['旧凭据吊销回执'] }}</td>
            <td>{{ item['新凭据生效回执'] }}</td>
            <td>{{ item['说明'] || '—' }}</td>
            <td class="row-actions">
              <template v-if="receiptable(item)">
                <button class="link" type="button" :data-testid="`receipt-revoke-${item.id}`" @click="sendReceipt(item, '吊销回执')">吊销回执</button>
                <button class="link" type="button" :data-testid="`receipt-activate-${item.id}`" @click="sendReceipt(item, '生效回执')">生效回执</button>
                <button class="link" type="button" :data-testid="`receipt-both-${item.id}`" @click="sendBothReceipts(item)">双回执同时到达</button>
              </template>
              <span v-else>—</span>
            </td>
          </tr>
        </tbody>
      </table>

      <h4 class="snapshot-title">来源引用快照（每个源端只有一份可用凭据）</h4>
      <table class="data-table">
        <thead>
          <tr>
            <th>源端编号</th>
            <th>凭据引用状态</th>
            <th>可用凭据</th>
            <th>可用凭据数</th>
            <th>可用状态</th>
            <th>更新时间</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="snap in detail['来源引用快照']" :key="String(snap.id)" :data-testid="`snapshot-${snap['源端编号']}`">
            <td>{{ snap['源端编号'] }}</td>
            <td><span class="tag" :data-state="String(snap['凭据引用状态'])">{{ snap['凭据引用状态'] }}</span></td>
            <td>{{ snap['可用凭据'] }}</td>
            <td>{{ snap['可用凭据数'] }}</td>
            <td>{{ snap['可用状态'] }}</td>
            <td>{{ snap['更新时间'] }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记认证凭证</h3>
        <label v-for="field in columns" :key="field" class="create-field">
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

    <footer class="page-foot">
      <span>共 {{ total }} 条凭证管理记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>
type BatchDetail = { batch: Row; items: Row[]; 来源引用快照: Row[] }

const ENDPOINT = '/api/auth'
const ROTATION_ENDPOINT = '/api/rotation'
const columns = ["凭证编号", "凭证类型", "关联数据源", "凭证内容", "有效期至", "创建时间", "最后使用", "凭证状态"]
const actions = ["续期凭证", "吊销凭证"]
const statuses = ["有效", "即将过期", "已过期", "已吊销"]
const metricNames = ["有效凭证", "即将过期凭证", "已过期凭证"]
const rotatableStatuses = ["有效", "即将过期", "已过期"]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const stats = computed(() => [
  { label: metricNames[0], value: total.value },
  { label: metricNames[1], value: rows.value.filter((row) => row.status === statuses[1]).length },
  { label: metricNames[2], value: rows.value.filter((row) => row.status === statuses[statuses.length - 1]).length },
])

// ---------- 多源凭证批量轮换 ----------
const selectedSources = ref<string[]>([])
const strategy = ref('先吊销后激活')
const newExpiry = ref('')
const submitting = ref(false)
const rotationMessage = ref('')
const rotationResults = ref<string[]>([])
const batches = ref<Row[]>([])
const detail = ref<BatchDetail | null>(null)

function rotatable(row: Row) {
  return rotatableStatuses.includes(String(row.status)) && String(row['关联数据源'] ?? '').trim() !== ''
}

function receiptable(item: Row) {
  return !['已切换', '已拒绝'].includes(String(item['状态']))
}

async function submitRotation() {
  submitting.value = true
  rotationMessage.value = ''
  rotationResults.value = []
  try {
    const records = selectedSources.value.map((source) => ({
      源端编号: source,
      ...(newExpiry.value ? { 新有效期至: newExpiry.value } : {}),
    }))
    const response = await request(`${ROTATION_ENDPOINT}/batches`, {
      method: 'POST',
      body: JSON.stringify({ 策略: strategy.value, records }),
    })
    const payload = await response.json()
    rotationMessage.value = payload.message || '批次提交完成'
    rotationResults.value = (payload.results ?? []).map(
      (item: Row) => `${item['源端编号'] || '（未填源端）'}：${item['状态']} — ${item['说明']}`,
    )
    if (payload.ok) {
      selectedSources.value = []
      if (payload.batch?.batch?.id) {
        await loadBatchDetail(Number(payload.batch.batch.id))
      }
    }
    await Promise.all([reload(), loadBatches()])
  } catch (error) {
    rotationMessage.value = error instanceof Error ? error.message : '轮换批次提交失败'
  } finally {
    submitting.value = false
  }
}

async function loadBatches() {
  try {
    const response = await request(`${ROTATION_ENDPOINT}/batches?size=100`)
    if (!response.ok) throw new Error('轮换批次列表读取失败')
    const payload = await response.json()
    batches.value = payload.items ?? []
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '轮换批次列表读取失败'
  }
}

async function loadBatchDetail(batchId: number) {
  try {
    const response = await request(`${ROTATION_ENDPOINT}/batches/${batchId}`)
    if (!response.ok) throw new Error('轮换批次详情读取失败')
    detail.value = (await response.json()) as BatchDetail
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '轮换批次详情读取失败'
  }
}

async function sendReceipts(receipts: { 源端编号: string; 回执类型: string }[]) {
  if (!detail.value) return
  rotationMessage.value = ''
  rotationResults.value = []
  try {
    const response = await request(`${ROTATION_ENDPOINT}/batches/${detail.value.batch.id}/receipts`, {
      method: 'POST',
      body: JSON.stringify({ receipts }),
    })
    const payload = await response.json()
    rotationMessage.value = payload.message || '回执处理完成'
    rotationResults.value = (payload.results ?? []).map(
      (item: Row) => `${item['源端编号']} ${item['回执类型']}：${item['状态']} — ${item['说明']}`,
    )
    if (payload.batch) {
      detail.value = payload.batch as BatchDetail
    }
    await Promise.all([reload(), loadBatches()])
  } catch (error) {
    rotationMessage.value = error instanceof Error ? error.message : '回执登记失败'
  }
}

async function sendReceipt(item: Row, receiptType: string) {
  await sendReceipts([{ 源端编号: String(item['源端编号']), 回执类型: receiptType }])
}

async function sendBothReceipts(item: Row) {
  // 双回执同时到达同一源端：一次提交两条，由服务端按策略定序
  const receipts = []
  if (item['旧凭据吊销回执'] === '未受理') receipts.push({ 源端编号: String(item['源端编号']), 回执类型: '吊销回执' })
  if (item['新凭据生效回执'] === '未受理') receipts.push({ 源端编号: String(item['源端编号']), 回执类型: '生效回执' })
  await sendReceipts(receipts)
}

async function receiptAll() {
  if (!detail.value) return
  const receipts: { 源端编号: string; 回执类型: string }[] = []
  for (const item of detail.value.items) {
    if (!receiptable(item)) continue
    if (item['旧凭据吊销回执'] === '未受理') receipts.push({ 源端编号: String(item['源端编号']), 回执类型: '吊销回执' })
    if (item['新凭据生效回执'] === '未受理') receipts.push({ 源端编号: String(item['源端编号']), 回执类型: '生效回执' })
  }
  await sendReceipts(receipts)
}

// ---------- 凭证台账 ----------

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value).toString()
  try {
    const response = await request(`${ENDPOINT}/export?${query}`)
    if (!response.ok) throw new Error('导出失败，请稍后重试')
    const url = URL.createObjectURL(await response.blob())
    const link = document.createElement('a')
    link.href = url
    link.download = 'auth.csv'
    link.click()
    URL.revokeObjectURL(url)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(columns.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const response = await request(ENDPOINT, {
      method: 'POST', body: JSON.stringify(createValues.value),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '登记失败')
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('凭证管理动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '凭证管理操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('认证凭证列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '凭证管理列表读取失败'
  }
}

onMounted(() => {
  void reload()
  void loadBatches()
})
</script>
