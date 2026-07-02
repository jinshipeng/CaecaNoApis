<script setup lang="ts">
import { ref, onMounted, onActivated, computed } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  WarnTriangleFilled,
  Check,
  Warning,
  Filter,
  Refresh
} from '@element-plus/icons-vue'
import { getInventoryList, getInventoryStats, createInventory, updateInventory, deleteInventory } from '@/api'
import { clearApiCache } from '@/utils/apiCache'
import { clearAllCaches } from '@/api/request'
import type { Inventory, Material } from '@/types/api'

const loading = ref(false)
const refreshing = ref(false)
const tableData = ref<Inventory[]>([])
const selectedRows = ref<any[]>([])
const dialogVisible = ref(false)
const formData = ref<Partial<Inventory>>({})
const editMaterial = computed<Material | null>(() => {
  const m = formData.value.material
  return typeof m === 'object' && m !== null ? m as Material : null
})
const isEdit = ref(false)
const currentEditId = ref<number | null>(null)
const pagination = ref({
  current: 1,
  pageSize: 15,
  total: 0
})

const searchForm = ref({
  search: '',
  hold_status: ''
})

const holdStatusOptions = [
  { label: '全部', value: '' },
  { label: '有Hold', value: 'hold' },
  { label: '无Hold', value: 'no_hold' }
]

const loadData = async () => {
  try {
    loading.value = true
    const params: any = {
      page: pagination.value.current,
      page_size: pagination.value.pageSize,
      search: searchForm.value.search,
      ordering: '-id'
    }

    
    if (searchForm.value.hold_status) {
      params.is_hold = searchForm.value.hold_status === 'hold'
    }

    const res = await getInventoryList(params)
    tableData.value = res?.results || []
    pagination.value.total = res?.count || 0
    await loadStats()
  } catch (error: any) {
    const status = error?.response?.status
    if (status === 401) {
      ElMessage.error('登录已过期，请重新登录')
    } else {
      ElMessage.error(`加载数据失败${status ? `(${status})` : ''}，请检查网络连接`)
    }
    console.error('加载库存数据失败:', error)
    tableData.value = []
    pagination.value.total = 0
  } finally {
    loading.value = false
  }
}

const handleSearch = () => {
  pagination.value.current = 1
  loadData()
}

const handleReset = () => {
  searchForm.value = {
    search: '',
    inventory_type: '',
    hold_status: ''
  }
  paadData()
}


const handleCurrentChange = (val: number) => {
  pagination.value.current = val
  loadData()
}

type TagType = 'primary' | 'success' | 'warning' | 'info' | 'danger'

const getDynamicSafetyStock = (row: Inventory): number => {
  const material = row.material
  if (!material || typeof material !== 'object') return 100

  const dbSafetyStock = Math.round(Number(material.safety_stock) || 0)
  const leadTime = Number(material.lead_time) || 7
  const currentQty = Number(row.quantity) || 0
  const standardCost = Number(material.standard_cost) || 0

  if (dbSafetyStock > 0 && dbSafetyStock !== 200) {
    return dbSafetyStock
  }

  const dailyUsageEstimate = Math.max(currentQty / 30, 10)

  let riskFactor = 1.2
  if (standardCost > 500) riskFactor = 1.5
  else if (standardCost > 100) riskFactor = 1.3

  const dynamicStock = Math.ceil(dailyUsageEstimate * leadTime * riskFactor)

  return Math.max(Math.min(dynamicStock, Math.ceil(currentQty * 0.3)), 20)
}

const getStockStatus = (row: Inventory): { type: TagType; text: string } => {
  const safetyStock = getDynamicSafetyStock(row)
  if (Number(row.quantity) < safetyStock * 0.5) return { type: 'danger', text: '库存不足' }
  if (Number(row.quantity) < safetyStock) return { type: 'warning', text: '接近安全库存' }
  return { type: 'success', text: '正常' }
}

const getExpiryStatus = (row: Inventory): { type: TagType; text: string } | null => {
  if (!row.expiry_date) return null
  const expiry = new Date(row.expiry_date)
  const now = new Date()
  const daysLeft = Math.ceil((expiry.getTime() - now.getTime()) / (1000 * 60 * 60 * 24))

  if (daysLeft < 7) return { type: 'danger', text: `即将到期 (${daysLeft}天)` }
  if (daysLeft < 30) return { type: 'warning', text: `(${daysLeft}天)` }
  return null
}

const stockStats = ref({
  low: 0,
  warning: 0,
  normal: 0,
  withHold: 0
})

const loadStats = async () => {
  try {
    // 加时间戳参数绕过所有层级的缓存（浏览器/代理/axios）
    const res = await getInventoryStats()
    console.log('[库存统计] 最新数据:', res)
    stockStats.value = {
      low: res.low_count || 0,
      warning: res.warning_count || 0,
      normal: res.normal_count || 0,
      withHold: res.with_hold || 0
    }
  } catch (e) {
    console.error('[库存统计] 获取失败:', e)
  }
}

const handleRefresh = async () => {
  refreshing.value = true
  clearApiCache()
  clearAllCaches() // 清除 axios 请求级缓存（60秒），确保获取最新数据
  try {
    await loadData()
    ElMessage.success('数据刷新成功')
  } catch (e) {
    // loadData 内部已处理错误提示
  } finally {
    refreshing.value = false
  }
}

const handleSelectionChange = (rows: any[]) => {
  selectedRows.value = rows
}

const handleEdit = (row: Inventory) => {
  isEdit.value = true
  currentEditId.value = row.id
  formData.value = { ...row }
  dialogVisible.value = true
}

const handleSubmit = async () => {
  try {
    if (isEdit.value && currentEditId.value) {
      await updateInventory(currentEditId.value, formData.value)
      ElMessage.success('更新成功')
    } else {
      await createInventory(formData.value)
      ElMessage.success('创建成功')
    }
    dialogVisible.value = false
    clearApiCache()
    clearAllCaches()
    loadData()
  } catch (error) {
    ElMessage.error('操作失败')
  }
}

const handleDeleteSelected = async () => {
  if (selectedRows.value.length === 0) return
  try {
    await ElMessageBox.confirm(
      `确定要删除选中的 ${selectedRows.value.length} 条记录吗？`,
      '确认删除',
      { confirmButtonText: '确定', cancelButtonText: '取消', type: 'warning' }
    )
    for (const row of selectedRows.value) {
      await deleteInventory(row.id)
    }
    ElMessage.success(`成功删除 ${selectedRows.value.length} 条记录`)
    clearApiCache()
    clearAllCaches()
    loadData()
  } catch (error: any) {
    if (error !== 'cancel') {
      ElMessage.error('删除失败')
    }
  }
}

const shouldRefresh = ref(true)

const setRefreshFlag = (flag: boolean) => {
  shouldRefresh.value = flag
}

defineExpose({ setRefreshFlag })

onMounted(() => {
  loadData()
})

onActivated(() => {
  if (shouldRefresh.value) {
    loadData()
  }
  shouldRefresh.value = true
})
</script>

<template>
  <div class="inventory-page">
    <div class="page-header">
      <div>
        <h1 class="page-title">库存管理</h1>
        <p class="page-desc">实时监控库存状态和物料库存</p>
      </div>
      <el-button type="primary" @click="handleRefresh" :loading="refreshing">
        <el-icon><Refresh /></el-icon>
        刷新数据
      </el-button>
    </div>

    <el-row :gutter="20" class="status-row">
      <el-col :xs="24" :sm="8" :lg="6" class="status-col">
        <div class="status-card danger">
          <div class="status-icon">
            <el-icon><Warning /></el-icon>
          </div>
          <div class="status-content">
            <div class="status-value">{{ stockStats.low }}</div>
            <div class="status-label">库存不足</div>
          </div>
        </div>
      </el-col>
      <el-col :xs="24" :sm="8" :lg="6" class="status-col">
        <div class="status-card warning">
          <div class="status-icon">
            <el-icon><WarnTriangleFilled /></el-icon>
          </div>
          <div class="status-content">
            <div class="status-value">{{ stockStats.warning }}</div>
            <div class="status-label">接近安全库存</div>
          </div>
        </div>
      </el-col>
      <el-col :xs="24" :sm="8" :lg="6" class="status-col">
        <div class="status-card success">
          <div class="status-icon">
            <el-icon><Check /></el-icon>
          </div>
          <div class="status-content">
            <div class="status-value">{{ stockStats.normal }}</div>
            <div class="status-label">库存正常</div>
          </div>
        </div>
      </el-col>
      <el-col :xs="24" :sm="8" :lg="6" class="status-col">
        <div class="status-card hold">
          <div class="status-icon">
            <el-icon><Filter /></el-icon>
          </div>
          <div class="status-content">
            <div class="status-value">{{ stockStats.withHold }}</div>
            <div class="status-label">库存Hold</div>
          </div>
        </div>
      </el-col>
    </el-row>

    <el-card class="search-card">
      <el-form :model="searchForm" inline>
        <el-form-item label="关键词">
          <el-input
            v-model="searchForm.search"
            placeholder="输入物料代码或名称搜索"
            clearable
            style="width: 300px"
            @keyup.enter="handleSearch"
          />
        </el-form-item>

        <el-form-item label="Hold状态">
          <el-select
            v-model="searchForm.hold_status"
            placeholder="选择状态"
            clearable
          >
            <el-option
              v-for="item in holdStatusOptions"
              :key="item.value"
              :label="item.label"
              :value="item.value"
            />
          </el-select>
        </el-form-item>
        <el-form-item>
          <el-button type="primary" @click="handleSearch">查询</el-button>
          <el-button @click="handleReset">重置</el-button>
        </el-form-item>
      </el-form>
    </el-card>

    <el-card class="table-card">
      <div class="table-toolbar">
        <span class="selection-info">已选 {{ selectedRows.length }} 项</span>
        <div style="display:flex;gap:8px">
          <el-button type="primary" size="small" :disabled="selectedRows.length !== 1" @click="handleEdit(selectedRows[0])">编辑选中</el-button>
          <el-button type="danger" size="small" :disabled="selectedRows.length === 0" @click="handleDeleteSelected">删除选中</el-button>
        </div>
      </div>

      <el-table border
        :data="tableData"
        :loading="loading"
        class="inventory-table"
        @selection-change="handleSelectionChange"
        style="width: 100%"
      >
        <el-table-column type="selection" width="38" />
        <el-table-column prop="material.material_code" label="物料代码" width="90" show-overflow-tooltip />
        <el-table-column prop="quantity" label="库存数量" width="65">
          <template #default="{ row }">
            <div class="qty-display">
              <span>{{ Math.round(row.quantity || 0) }}</span>
              <span v-if="Number(row.hold_quantity) > 0" class="hold-qty">(Hold: {{ Math.round(row.hold_quantity) }})</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column prop="available_quantity" label="可用数量" width="65">
          <template #default="{ row }">
            {{ Math.round(row.available_quantity || 0) }}
          </template>
        </el-table-column>
        <el-table-column label="安全库存" width="62">
          <template #default="{ row }">
            {{ getDynamicSafetyStock(row) }}
          </template>
        </el-table-column>
        <el-table-column label="库存状态" width="60">
          <template #default="{ row }">
            <el-tag :type="getStockStatus(row).type" size="small">
              {{ getStockStatus(row).text }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="库存价值" width="72">
          <template #default="{ row }">
            ¥{{ Number((row.material?.standard_cost || 0) * (row.quantity || 0)).toFixed(2) }}
          </template>
        </el-table-column>
        <el-table-column prop="expiry_date" label="有效期" width="78">
          <template #default="{ row }">
            <div class="expiry-cell">
              <span>{{ row.expiry_date || '-' }}</span>
              <el-tag
                v-if="getExpiryStatus(row)"
                :type="getExpiryStatus(row)!.type"
                size="small"
              >
                {{ getExpiryStatus(row)!.text }}
              </el-tag>
            </div>
          </template>
        </el-table-column>
        <el-table-column prop="hold_until" label="解Hold日期" width="78" show-overflow-tooltip />
        <el-table-column prop="data_date" label="数据日期" width="76" show-overflow-tooltip />
        <el-table-column label="Hold状态" width="68" show-overflow-tooltip>
          <template #default="{ row }">
            <span v-if="row.is_hold" class="hold-reason">{{ row.hold_reason }}</span>
            <span v-else class="no-hold">-</span>
          </template>
        </el-table-column>
      </el-table>

      <div class="pagination-container">
        <el-pagination
          v-model:current-page="pagination.current"
          :page-size="pagination.pageSize"
          :total="pagination.total"
          layout="total, prev, pager, next, jumper"
          @current-change="handleCurrentChange"
        />
      </div>
    </el-card>

    <el-dialog
      v-model="dialogVisible"
      title="编辑库存"
      width="650px"
      :close-on-click-modal="false"
      class="inventory-edit-dialog"
      destroy-on-close
      append-to-body
    >
      <el-form :model="formData" label-width="100px" class="edit-form">
        <el-form-item label="物料代码">
          <el-input :model-value="editMaterial?.material_code" disabled />
        </el-form-item>

        <el-form-item label="物料名称">
          <el-input :model-value="editMaterial?.material_name" disabled />
        </el-form-item>

        <el-form-item label="库存数量" required>
          <el-input-number v-model="formData.quantity" :min="0" style="width: 100%;" />
        </el-form-item>

        <el-form-item label="Hold数量">
          <el-input-number v-model="formData.hold_quantity" :min="0" style="width: 100%;" />
        </el-form-item>

        <el-form-item label="Hold状态">
          <el-switch v-model="formData.is_hold" />
        </el-form-item>

        <el-form-item label="Hold原因" v-if="formData.is_hold">
          <el-input v-model="formData.hold_reason" type="textarea" />
        </el-form-item>

        <el-form-item label="有效期">
          <el-date-picker v-model="formData.expiry_date" type="date" style="width: 100%;" />
        </el-form-item>
      </el-form>

      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" @click="handleSubmit">确定</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped lang="scss">
.inventory-page {
  max-width: 1500px;
  margin: 0 auto;
}

.page-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 24px;

  .page-title {
    font-size: 28px;
    font-weight: 700;
    color: #e2e8f0;
    margin: 0 0 8px 0;
  }

  .page-desc {
    font-size: 14px;
    color: #909399;
    margin: 0;
  }
}

.status-row {
  margin-bottom: 24px;
}

.status-col {
  margin-bottom: 20px;
}

.status-card {
  background: rgba(255, 255, 255, 0.05);
  border: 1px solid rgba(255, 255, 255, 0.1);
  border-radius: 12px;
  padding: 20px;
  display: flex;
  align-items: center;
  gap: 16px;
  transition: all 0.3s ease;

  &.danger {
    border-color: rgba(245, 108, 108, 0.3);
    .status-icon { background: rgba(245, 108, 108, 0.2); color: #f56c6c; }
    .status-value { color: #f56c6c; }
  }

  &.warning {
    border-color: rgba(230, 162, 60, 0.3);
    .status-icon { background: rgba(230, 162, 60, 0.2); color: #e6a23c; }
    .status-value { color: #e6a23c; }
  }

  &.success {
    border-color: rgba(103, 194, 58, 0.3);
    .status-icon { background: rgba(103, 194, 58, 0.2); color: #67c23a; }
    .status-value { color: #67c23a; }
  }

  &.hold {
    border-color: rgba(156, 136, 255, 0.3);
    .status-icon { background: rgba(156, 136, 255, 0.2); color: #9c88ff; }
    .status-value { color: #9c88ff; }
  }
}

.status-icon {
  width: 48px;
  height: 48px;
  border-radius: 12px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 24px;
}

.status-content {
  flex: 1;

  .status-value {
    font-size: 28px;
    font-weight: 700;
    line-height: 1.2;
  }

  .status-label {
    font-size: 14px;
    color: #909399;
    margin-top: 4px;
  }
}

.search-card {
  margin-bottom: 20px;
  background: rgba(255, 255, 255, 0.05);
  border: 1px solid rgba(255, 255, 255, 0.1);

  :deep(.el-card__body) {
    padding: 20px;
  }

  :deep(.el-form--inline .el-form-item) {
    margin-bottom: 0;
    vertical-align: middle;
  }
}

.table-card {
  background: rgba(255, 255, 255, 0.05);
  border: 1px solid rgba(255, 255, 255, 0.1);

  :deep(.el-card__body) {
    padding: 0;
  }
}

.table-toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 12px 16px;
  background: rgba(64, 158, 255, 0.06);
  border-bottom: 1px solid rgba(255, 255, 255, 0.06);
}

.selection-info {
  font-size: 13px;
  color: #8b92a5;
}

.inventory-table {
  :deep(.el-table) {
    background: rgba(255, 255, 255, 0.02) !important;
    --el-table-row-height: 36px;
    font-size: 13px;
  }

  :deep(.el-table__header-wrapper) {
    th {
      background: rgba(110, 158, 247, 0.08) !important;
      color: #B0B8C4;
      font-weight: 600;
      border-bottom: 1px solid rgba(255, 255, 255, 0.05);
      padding: 6px 0 !important;
      height: 38px !important;
      box-sizing: border-box;
    }
  }

  :deep(.el-table__body-wrapper) {
    background: rgba(255, 255, 255, 0.02);

    td {
      color: #E8EAED;
      border-bottom: 1px solid rgba(255, 255, 255, 0.03);
      padding: 4px 0 !important;
      height: 36px !important;
      box-sizing: border-box;
    }

    tr {
      background: rgba(255, 255, 255, 0.02) !important;

      &:hover > td {
        background: rgba(110, 158, 247, 0.06) !important;
      }
    }
  }

  :deep(.el-table__cell) {
    padding: 4px 12px !important;
  }
}

.qty-display {
  display: flex;
  align-items: center;
  gap: 8px;

  .hold-qty {
    font-size: 12px;
    color: #f56c6c;
  }
}

.expiry-cell {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.hold-info {
  display: flex;
  flex-direction: column;
  gap: 4px;

  .hold-reason {
    font-size: 12px;
    color: #f56c6c;
    word-break: break-all;
  }
}

.no-hold {
  color: #606266;
}

.pagination-container {
  display: flex;
  justify-content: center;
  padding: 16px 20px;
  border-top: 1px solid rgba(255, 255, 255, 0.06);
  background: rgba(255, 255, 255, 0.02);

  :deep(.el-pagination) {
    --el-pagination-button-bg-color: transparent;
    --el-pagination-hover-color: #409eff;

    .el-pager li {
      background: rgba(255, 255, 255, 0.05);
      color: #909399;
      border: 1px solid rgba(255, 255, 255, 0.1);
      border-radius: 4px;
      min-width: 32px;
      height: 32px;
      line-height: 30px;

      &.is-active {
        background: #409eff;
        color: #fff;
        border-color: #409eff;
      }

      &:hover:not(.is-active) {
        color: #409eff;
        border-color: #409eff;
      }
    }

    .btn-prev, .btn-next {
      background: rgba(255, 255, 255, 0.05);
      color: #909399;
      border: 1px solid rgba(255, 255, 255, 0.1);
      border-radius: 4px;
      min-width: 32px;
      height: 32px;

      &:hover {
        color: #409eff;
      }
    }

    .el-select .el-input__wrapper {
      background: rgba(255, 255, 255, 0.05);
    }

    .el-pagination__total,
    .el-pagination__jump {
      color: #909399;
    }
  }
}

@media (max-width: 767px) {
  .inventory-page { max-width: 100%; }
  .page-header {
    flex-direction: column;
    gap: 16px;

    .page-title {
      font-size: 24px;
    }
  }

  .status-card {
    padding: 16px;
  }

  .status-icon {
    width: 40px;
    height: 40px;
    font-size: 20px;
  }

  .status-value {
    font-size: 24px;
  }

  .search-card :deep(.el-form-item) { margin-right: 0; width: 100%; }
  .search-card :deep(.el-select), .search-card :deep(.el-input) { width: 100% !important; }
}
</style>
