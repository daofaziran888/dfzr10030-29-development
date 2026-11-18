<template>
  <section class="page" data-module="datasource">
    <header class="page-head">
      <div>
        <h2>数据源配置管理</h2>
        <p class="page-desc">维护数据源，围绕源端编号、连接类型、接入地址、认证方式做登记、筛选与状态流转。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">登记数据源</button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">导出数据源配置清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
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
          <th v-for="column in displayColumns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in displayColumns" :key="column">
            <span v-if="column === '可用状态'" class="tag" :data-state="String(row['可用状态'] ?? '')">
              {{ row['可用状态'] ?? '—' }}
            </span>
            <template v-else>{{ row[column] ?? '—' }}</template>
          </td>
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
            <button class="link" type="button" :data-testid="`rotations-${row.id}`" @click="openRotations(row)">
              轮换记录
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="displayColumns.length + 1" class="empty-state">暂无数据源配置数据，可先登记数据源</td>
        </tr>
      </tbody>
    </table>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记数据源</h3>
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

    <div v-if="showRotations" class="modal-backdrop" data-testid="rotations-modal" @click.self="showRotations = false">
      <div class="modal">
        <h3>轮换记录 · {{ rotationSource }}</h3>
        <p class="panel-desc">围绕凭证轮换批次及来源引用快照留档的可回读记录。</p>
        <table class="data-table">
          <thead>
            <tr>
              <th>批次编号</th>
              <th>策略</th>
              <th>旧凭据引用</th>
              <th>新凭据引用</th>
              <th>凭据引用状态</th>
              <th>可用状态</th>
              <th>登记时间</th>
              <th>完成时间</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="record in rotationRecords" :key="String(record.id)">
              <td>{{ record['批次编号'] }}</td>
              <td>{{ record['策略'] }}</td>
              <td>{{ record['旧凭据引用'] || '—' }}</td>
              <td>{{ record['新凭据引用'] || '—' }}</td>
              <td><span class="tag" :data-state="String(record['凭据引用状态'])">{{ record['凭据引用状态'] }}</span></td>
              <td>{{ record['可用状态'] }}</td>
              <td>{{ record['登记时间'] }}</td>
              <td>{{ record['完成时间'] || '—' }}</td>
            </tr>
            <tr v-if="!rotationRecords.length">
              <td colspan="8" class="empty-state">该源端暂无轮换记录</td>
            </tr>
          </tbody>
        </table>
        <div class="page-actions">
          <button class="btn" type="button" @click="showRotations = false">关闭</button>
        </div>
      </div>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条数据源配置记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>

const ENDPOINT = '/api/datasource'
const columns = ["源端编号", "连接类型", "接入地址", "认证方式", "采样周期", "超时秒数", "重试上限", "负责人"]
// 列表额外展示凭证轮换同步过来的可用状态；登记表单仍只用基础字段
const displayColumns = [...columns, "可用状态", "启用决策", "当前凭据引用", "轮换批次"]
const actions = ["提交校验", "启用配置", "停用配置"]
const statuses = ["草稿", "待校验", "已启用", "已停用"]
const metricNames = ["数据源总数", "已启用数据源", "待校验数据源"]

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

// ---------- 轮换记录回读 ----------
const showRotations = ref(false)
const rotationRecords = ref<Row[]>([])
const rotationSource = ref('')

async function openRotations(row: Row) {
  rotationSource.value = String(row['源端编号'] ?? '')
  rotationRecords.value = []
  showRotations.value = true
  try {
    const response = await request(`${ENDPOINT}/${row.id}/rotations`)
    if (!response.ok) throw new Error('轮换记录读取失败')
    const payload = await response.json()
    rotationRecords.value = payload.items ?? []
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '轮换记录读取失败'
  }
}

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
    link.download = 'datasource.csv'
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
      throw new Error('数据源配置动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '数据源配置操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('数据源列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '数据源配置列表读取失败'
  }
}

onMounted(reload)
</script>
