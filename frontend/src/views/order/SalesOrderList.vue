<script setup lang="ts">
import { ref, reactive, onMounted, onActivated, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus } from '@element-plus/icons-vue'
import { getOrderList, createOrder, updateOrder, deleteOrder, getMaterialList } from '@/api'
import { cancelOrderWithRelease } from '@/api/scheduling'
import type { SalesOrder, CustomerRef, Material } from '@/types/api'

const loading = ref(false)
const dialogVisible = ref(false)
const formData = reactive({
  order_no: '',
  customer_id: null as number | null,
  customer_name: '',
  customer_code: '',
  material_id: null as number | null,
  material_code: '',
  material_name: '',
  material_unit: '',
  quantity: 0,
  unit_price: 0,
  total_amount: 0,
  order_date: '',
  demand_date: '',
  status: 'pending',
  priority: 1,
  shipping_method: 'sea',
  shipping_days: 45
})
const isEdit = ref(false)
const currentEditId = ref<number | null>(null)

const tableData = ref<SalesOrder[]>([])
const selectedRows = ref<any[]>([])
const pagination = ref({
  current: 1,
  pageSize: 15,
  total: 0
})

const searchForm = ref({
  search: '',
  status: ''
})

const orderStatuses = [
  { label: '全部', value: '' },
  { label: '待处理', value: 'pending' },
  { label: '已确认', value: 'confirmed' },
  { label: '生产中', value: 'in_production' },
  { label: '已占料', value: 'allocated' },
  { label: '部分齐套', value: 'partial' },
  { label: '完全齐套', value: 'complete' },
  { label: '已发货', value: 'shipped' },
  { label: '已交付', value: 'delivered' },
  { label: '进行中', value: 'processing' },
  { label: '已取消', value: 'cancelled' }
]

const priorityOptions = [
  { label: '紧急', value: 1, type: 'danger' },
  { label: '高', value: 2, type: 'warning' },
  { label: '中', value: 3, type: 'info' },
  { label: '低', value: 4, type: 'success' }
]

const materialList = ref<Material[]>([])
const loadMaterialList = async () => {
  try {
    const res = await getMaterialList({ page_size: 1000, is_active: true })
    materialList.value = res?.results || []
  } catch (error) {
    console.error('加载物料列表失败:', error)
  }
}

const handleMaterialChange = (materialId: number) => {
  const material = materialList.value.find(m => m.id === materialId)
  if (material) {
    formData.material_id = material.id
    formData.material_code = material.material_code
    formData.material_name = material.material_name
    formData.material_unit = material.unit
  }
}

// 自动计算总金额
watch([() => formData.quantity, () => formData.unit_price], ([qty, price]) => {
  formData.total_amount = Math.round(Number(qty || 0) * Number(price || 0) * 100) / 100
})

const loadData = async () => {
  try {
    loading.value = true
    const params: Record<string, unknown> = {
      page: pagination.value.current,
      page_size: pagination.value.pageSize,
      search: searchForm.value.search,
      ordering: '-id'
    }
    if (searchForm.value.status) {
      params.status = searchForm.value.status
    }
    const res = await getOrderList(params)

    tableData.value = res?.results || []
    pagination.value.total = res?.count || 0
  } catch (error: any) {
    ElMessage.error('加载数据失败')
    console.error('加载销售订单数据失败:', error)
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
    status: ''
  }
  pagination.value.current = 1
  loadData()
}


const handleCurrentChange = (val: number) => {
  pagination.value.current = val
  loadData()
}

const handleAdd = () => {
  isEdit.value = false
  Object.assign(formData, {
    order_no: '',
    customer_id: null,
    customer_name: '',
    customer_code: '',
    material_id: null,
    material_code: '',
    material_name: '',
    material_unit: '',
    quantity: 0,
    unit_price: 0,
    total_amount: 0,
    order_date: '',
    demand_date: '',
    status: 'pending',
    priority: 1,
    shipping_method: 'sea',
    shipping_days: 45
  })
  dialogVisible.value = true
}

const handleEdit = (row: SalesOrder) => {
  isEdit.value = true
  currentEditId.value = row.id
  const customer = typeof row.customer === 'object' ? row.customer as CustomerRef : null
  const material = typeof row.material === 'object' ? row.material as Material : null
  Object.assign(formData, {
    order_no: row.order_no || '',
    customer_id: customer?.id || null,
    customer_name: customer?.customer_name || '',
    customer_code: customer?.customer_code || '',
    material_id: material?.id || null,
    material_code: material?.material_code || '',
    material_name: material?.material_name || '',
    material_unit: material?.unit || '',
    quantity: row.quantity || 0,
    unit_price: row.unit_price || 0,
    total_amount: row.total_amount || 0,
    order_date: row.order_date || '',
    demand_date: row.demand_date || '',
    status: row.status || 'pending',
    priority: row.priority || 1,
    shipping_method: row.shipping_method || 'sea',
    shipping_days: row.shipping_days || 45
  })
  dialogVisible.value = true
}

const handleSelectionChange = (rows: any[]) => {
  selectedRows.value = rows
}

const handleBatchDelete = async () => {
  try {
    await ElMessageBox.confirm(
      `确定要删除选中的 ${selectedRows.value.length} 条订单吗？`,
      '批量删除确认',
      {
        confirmButtonText: '确定',
        cancelButtonText: '取消',
        type: 'warning'
      }
    )
    for (const row of selectedRows.value) {
      await deleteOrder(row.id)
    }
    ElMessage.success('批量删除成功')
    loadData()
  } catch (error: any) {
    if (error !== 'cancel') {
      ElMessage.error('删除失败')
    }
  }
}

// @ts-expect-error - handler reserved for future template binding
const _handleDelete = async (row: SalesOrder) => {
  try {
    await ElMessageBox.confirm(
      `确定要删除订单 ${row.order_no} 吗？`,
      '删除确认',
      {
        confirmButtonText: '确定',
        cancelButtonText: '取消',
        type: 'warning'
      }
    )

    await deleteOrder(row.id)
    ElMessage.success('删除成功')
    loadData()
  } catch (error: any) {
    if (error !== 'cancel') {
      ElMessage.error('删除失败')
    }
  }
}

const handleSubmit = async () => {
  try {
    const submitData: Partial<SalesOrder> = {
      order_no: formData.order_no,
      customer_name: formData.customer_name,
      material_id: formData.material_id,
      material_code: formData.material_code,
      material_name: formData.material_name,
      quantity: formData.quantity,
      unit_price: formData.unit_price,
      total_amount: formData.total_amount,
      order_date: formData.order_date,
      demand_date: formData.demand_date,
      status: formData.status as any,
      priority: formData.priority,
      shipping_method: formData.shipping_method as any,
      shipping_days: formData.shipping_days
    }
    
    if (isEdit.value && currentEditId.value) {
      await updateOrder(currentEditId.value, submitData)
      ElMessage.success('更新成功')
    } else {
      await createOrder(submitData)
      ElMessage.success('创建成功')
    }
    dialogVisible.value = false
    loadData()
  } catch (error) {
    ElMessage.error('操作失败')
  }
}

const handleCancelOrder = async (row: SalesOrder) => {
  try {
    await ElMessageBox.confirm(
      `确认取消订单 ${row.order_no} 并释放已占物料吗？此操作不可恢复。`,
      '取消订单确认',
      {
        confirmButtonText: '确认取消',
        cancelButtonText: '返回',
        type: 'warning'
      }
    )
    await cancelOrderWithRelease(row.id)
    ElMessage.success('订单已取消，物料已释放')
    loadData()
  } catch (error: any) {
    if (error !== 'cancel') {
      ElMessage.error('取消订单失败')
    }
  }
}

const getStatusType = (status: string) => {
  const typeMap: Record<string, any> = {
    pending: 'warning', confirmed: 'success', in_production: 'primary',
    allocated: 'primary', partial: 'info', complete: 'success',
    completed: 'success', shipped: 'primary', delivered: 'success',
    processing: 'primary', cancelled: 'danger'
  }
  return typeMap[status] || 'info'
}

const getStatusLabel = (status: string) => {
  if (!status) return '待处理'
  // 兼容后端原始值('pending'等)和显示标签('待处理'等)
  const labelMap: Record<string, string> = {
    pending: '待处理', confirmed: '已确认', in_production: '生产中',
    allocated: '已占料', partial: '部分齐套', complete: '完全齐套',
    completed: '完全齐套', shipped: '已发货', delivered: '已交付',
    processing: '进行中', cancelled: '已取消',
    // 兼容中文标签直接传入的情况
    '待处理': '待处理', '已确认': '已确认', '生产中': '生产中',
    '已占料': '已占料', '部分齐套': '部分齐套', '完全齐套': '完全齐套',
    '已发货': '已发货', '已交付': '已交付', '进行中': '进行中', '已取消': '已取消'
  }
  return labelMap[status] || status || '待处理'
}

const getPriorityType = (priority: number) => {
  const typeMap: Record<number, any> = {
    1: 'danger',
    2: 'warning',
    3: 'info',
    4: 'success'
  }
  return typeMap[priority] || 'info'
}

const getPriorityLabel = (priority: number) => {
  const labelMap: Record<number, string> = {
    1: '紧急',
    2: '高',
    3: '中',
    4: '低'
  }
  return labelMap[priority] || '普通'
}

const shouldRefresh = ref(true)

const setRefreshFlag = (flag: boolean) => {
  shouldRefresh.value = flag
}

defineExpose({ setRefreshFlag })

onMounted(() => {
  loadData()
  loadMaterialList()
})

onActivated(() => {
  if (shouldRefresh.value) {
    loadData()
  }
  shouldRefresh.value = true
})
</script>

<template>
  <div class="order-page">
    <div class="page-header">
      <div>
        <h1 class="page-title">销售订单管理</h1>
        <p class="page-desc">管理和跟踪所有销售订单</p>
      </div>
      <el-button type="primary" @click="handleAdd">
        <el-icon><Plus /></el-icon>
        新增订单
      </el-button>
    </div>

    <el-card class="search-card">
      <el-form :model="searchForm" inline>
        <el-form-item label="关键词">
          <el-input
            v-model="searchForm.search"
            placeholder="订单号/客户名"
            clearable
            @keyup.enter="handleSearch"
          />
        </el-form-item>

        <el-form-item label="订单状态">
          <el-select
            v-model="searchForm.status"
            placeholder="请选择"
            clearable
          >
            <el-option
              v-for="item in orderStatuses"
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
          <el-button type="danger" size="small" :disabled="selectedRows.length === 0" @click="handleBatchDelete">删除选中</el-button>
        </div>
      </div>
      <el-table border
          :data="tableData"
          :loading="loading"
          class="order-table"
          @selection-change="handleSelectionChange"
        >
        <el-table-column type="selection" width="36" />
        <el-table-column prop="order_no" label="订单号" width="120" show-overflow-tooltip />
        <el-table-column label="客户名称" width="90" show-overflow-tooltip>
          <template #default="{ row }">
            {{ row.customer_name || '-' }}
          </template>
        </el-table-column>
        <el-table-column label="物料" width="78" show-overflow-tooltip>
          <template #default="{ row }">
            {{ row.material_code || '-' }}
          </template>
        </el-table-column>
        <el-table-column prop="quantity" label="数量" width="56">
          <template #default="{ row }">
            {{ Math.round(Number(row.quantity || 0)).toLocaleString() }}
          </template>
        </el-table-column>
        <el-table-column label="单价" width="65">
          <template #default="{ row }">
            ¥{{ Number(row.unit_price || 0).toFixed(2) }}
          </template>
        </el-table-column>
        <el-table-column label="总金额" width="78">
          <template #default="{ row }">
            ¥{{ Number(row.total_amount || 0).toFixed(2) }}
          </template>
        </el-table-column>
        <el-table-column prop="order_date" label="订单日期" width="82" show-overflow-tooltip />
        <el-table-column prop="demand_date" label="需求日期" width="82" />
        <el-table-column label="优先级" width="60">
          <template #default="{ row }">
            <el-tag :type="getPriorityType(row.priority)" size="small">
              {{ getPriorityLabel(row.priority) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="65">
          <template #default="{ row }">
            <el-tag :type="getStatusType(row.status)" size="small">
              {{ getStatusLabel(row.status) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="86" fixed="right">
          <template #default="{ row }">
            <el-button
              v-if="row.status !== 'cancelled'"
              type="danger"
              size="small"
              link
              @click="handleCancelOrder(row)"
            >
              取消订单
            </el-button>
            <span v-else class="text-muted">-</span>
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
      :title="isEdit ? '编辑订单' : '新增订单'"
      width="600px"
      :close-on-click-modal="false"
      append-to-body
    >
      <el-form :model="formData" label-width="120px">
        <el-form-item label="订单号" required>
          <el-input v-model="formData.order_no" :disabled="isEdit" />
        </el-form-item>

        <el-form-item label="客户名称" required>
          <el-input v-model="formData.customer_name" />
        </el-form-item>

        <el-form-item label="物料" required>
          <el-select
            v-model="formData.material_id"
            filterable
            placeholder="选择物料"
            style="width: 100%;"
            @change="handleMaterialChange"
          >
            <el-option
              v-for="item in materialList"
              :key="item.id"
              :label="`${item.material_code} - ${item.material_name}`"
              :value="item.id"
            />
          </el-select>
        </el-form-item>

        <el-form-item label="数量" required>
          <el-input-number v-model="formData.quantity" :min="1" style="width: 100%;" />
        </el-form-item>

        <el-form-item label="单价" required>
          <el-input-number v-model="formData.unit_price" :min="0" :precision="2" style="width: 100%;" />
        </el-form-item>

        <el-form-item label="订单日期" required>
          <el-date-picker
            v-model="formData.order_date"
            type="date"
            placeholder="选择日期"
            style="width: 100%;"
          />
        </el-form-item>

        <el-form-item label="需求日期" required>
          <el-date-picker
            v-model="formData.demand_date"
            type="date"
            placeholder="选择日期"
            style="width: 100%;"
          />
        </el-form-item>

        <el-form-item label="优先级">
          <el-select v-model="formData.priority" style="width: 100%;">
            <el-option
              v-for="item in priorityOptions"
              :key="item.value"
              :label="item.label"
              :value="item.value"
            />
          </el-select>
        </el-form-item>

        <el-form-item label="状态">
          <el-select v-model="formData.status" style="width: 100%;">
            <el-option
              v-for="item in orderStatuses.filter(s => s.value)"
              :key="item.value"
              :label="item.label"
              :value="item.value"
            />
          </el-select>
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
.order-page {
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

.table-toolbar { display:flex; justify-content:space-between; align-items:center; padding:12px 16px; background:rgba(64,158,255,0.06); border-bottom:1px solid rgba(255,255,255,0.06); }
.selection-info { font-size:13px; color:#8b92a5; }
.text-muted { color: #606266; font-size: 13px; }

.order-table {
  :deep(.el-table) {
    background: rgba(255, 255, 255, 0.02) !important;
  }

  :deep(.el-table__header-wrapper) {
    th {
      background: rgba(110, 158, 247, 0.08) !important;
      color: #B0B8C4;
      font-weight: 600;
      border-bottom: 1px solid rgba(255, 255, 255, 0.05);
    }
  }

  :deep(.el-table__body-wrapper) {
    background: rgba(255, 255, 255, 0.02);

    td {
      color: #E8EAED;
      border-bottom: 1px solid rgba(255, 255, 255, 0.03);
    }

    tr {
      background: rgba(255, 255, 255, 0.02) !important;

      &:hover {
        background: rgba(110, 158, 247, 0.06) !important;
      }
    }
  }
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
  .order-page { max-width: 100%; }
  .search-card :deep(.el-form-item) { margin-right: 0; width: 100%; }
  .page-header {
    flex-direction: column;
    gap: 16px;

    .page-title {
      font-size: 24px;
    }
  }
}
</style>
