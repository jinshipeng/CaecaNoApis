<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus } from '@element-plus/icons-vue'
import { getPurchaseOrderList, createPurchaseOrder, updatePurchaseOrder, deletePurchaseOrder, getSupplierList, getMaterialList } from '@/api'
import type { PurchaseOrder, Supplier, Material } from '@/types/api'

const loading = ref(false)
const dialogVisible = ref(false)
const formData = reactive({
  po_no: '',
  supplier_id: null as number | null,
  supplier_name: '',
  supplier_code: '',
  material_id: null as number | null,
  material_code: '',
  material_name: '',
  quantity: 0,
  unit_price: 0,
  total_amount: 0,
  order_date: '',
  delivery_date: '',
  actual_delivery_date: '',
  status: 'draft' as any,
  remarks: ''
})
const isEdit = ref(false)
const currentEditId = ref<number | null>(null)

const tableData = ref<PurchaseOrder[]>([])
const pagination = ref({
  current: 1,
  pageSize: 15,
  total: 0
})

const searchForm = ref({
  search: '',
  status: ''
})

const supplierList = ref<Supplier[]>([])
const materialList = ref<Material[]>([])

const loadSupplierList = async () => {
  try {
    const res = await getSupplierList({ page_size: 1000, is_active: true })
    supplierList.value = res?.results || []
  } catch (error) {
    console.error('加载供应商列表失败:', error)
  }
}

const loadMaterialList = async () => {
  try {
    const res = await getMaterialList({ page_size: 1000, is_active: true })
    materialList.value = res?.results || []
  } catch (error) {
    console.error('加载物料列表失败:', error)
  }
}

const handleSupplierChange = (supplierId: number) => {
  const supplier = supplierList.value.find(s => s.id === supplierId)
  if (supplier) {
    formData.supplier_id = supplier.id
    formData.supplier_code = supplier.supplier_code
    formData.supplier_name = supplier.supplier_name
  }
}

const handleMaterialChange = (materialId: number) => {
  const material = materialList.value.find(m => m.id === materialId)
  if (material) {
    formData.material_id = material.id
    formData.material_code = material.material_code
    formData.material_name = material.material_name
  }
}

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
    const res = await getPurchaseOrderList(params)

    tableData.value = res?.results || []
    pagination.value.total = res?.count || 0
  } catch (error: any) {
    ElMessage.error('加载数据失败')
    console.error('加载采购订单数据失败:', error)
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
    po_no: '',
    supplier_id: null,
    supplier_name: '',
    supplier_code: '',
    material_id: null,
    material_code: '',
    material_name: '',
    quantity: 0,
    unit_price: 0,
    total_amount: 0,
    order_date: '',
    delivery_date: '',
    status: 'draft',
    remarks: ''
  })
  dialogVisible.value = true
}

const handleEdit = (row: PurchaseOrder) => {
  isEdit.value = true
  currentEditId.value = row.id
  const supplierId = typeof row.supplier === 'object' ? (row.supplier as Supplier)?.id : row.supplier
  const materialId = typeof row.material === 'object' ? (row.material as Material)?.id : row.material
  Object.assign(formData, {
    po_no: row.po_no || '',
    supplier_id: supplierId || null,
    supplier_name: (typeof row.supplier === 'object' ? (row.supplier as Supplier)?.supplier_name : row.supplier_name) || '',
    supplier_code: (typeof row.supplier === 'object' ? (row.supplier as Supplier)?.supplier_code : row.supplier_code) || '',
    material_id: materialId || null,
    material_code: (typeof row.material === 'object' ? (row.material as Material)?.material_code : row.material_code) || '',
    material_name: (typeof row.material === 'object' ? (row.material as Material)?.material_name : row.material_name) || '',
    quantity: row.quantity || 0,
    unit_price: row.unit_price || 0,
    total_amount: row.total_amount || 0,
    order_date: row.order_date || '',
    delivery_date: row.delivery_date || '',
    actual_delivery_date: row.actual_delivery_date || '',
    status: row.status || 'draft',
    remarks: row.remarks || ''
  })
  dialogVisible.value = true
}

// @ts-expect-error - handler reserved for future template binding
const _handleDelete = async (row: PurchaseOrder) => {
  try {
    await ElMessageBox.confirm(
      `确定要删除采购订单 ${row.po_no} 吗？`,
      '删除确认',
      {
        confirmButtonText: '确定',
        cancelButtonText: '取消',
        type: 'warning'
      }
    )

    await deletePurchaseOrder(row.id)
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
    const submitData: Record<string, unknown> = {
      po_no: formData.po_no,
      supplier: formData.supplier_id,
      material: formData.material_id,
      quantity: formData.quantity,
      unit_price: formData.unit_price,
      total_amount: Number(formData.quantity || 0) * Number(formData.unit_price || 0),
      order_date: formData.order_date,
      delivery_date: formData.delivery_date,
      actual_delivery_date: formData.actual_delivery_date,
      status: formData.status,
      remarks: formData.remarks
    }
    if (isEdit.value && currentEditId.value) {
      await updatePurchaseOrder(currentEditId.value, submitData)
      ElMessage.success('更新成功')
    } else {
      await createPurchaseOrder(submitData)
      ElMessage.success('创建成功')
    }
    dialogVisible.value = false
    loadData()
  } catch (error) {
    ElMessage.error('操作失败')
  }
}

const getStatusLabel = (status: string) => {
  if (!status) return '草稿'
  // 兼容后端原始值('draft'等)和显示标签('草稿'等)
  const map: Record<string, string> = {
    draft: '草稿', issued: '已下达', confirmed: '已确认',
    pending: '待处理', in_production: '生产中', shipped: '已发货',
    partial_shipped: '部分发货', partial: '部分到货', processing: '进行中',
    completed: '已完成', cancelled: '已取消',
    // 兼容中文标签直接传入的情况
    '草稿': '草稿', '已下达': '已下达', '已确认': '已确认',
    '待处理': '待处理', '生产中': '生产中', '已发货': '已发货',
    '部分发货': '部分发货', '部分到货': '部分到货', '进行中': '进行中',
    '已完成': '已完成', '已取消': '已取消',
    // 兼容可能出现的异常值
    delivered: '已到货', received: '已收货', arrived: '已到达',
    approved: '已审批', rejected: '已拒绝', closed: '已关闭'
  }
  return map[status] || status || '草稿'
}

const getStatusType = (status: string): 'primary' | 'success' | 'warning' | 'info' | 'danger' => {
  const map: Record<string, 'primary' | 'success' | 'warning' | 'info' | 'danger'> = {
    draft: 'info', issued: 'primary', confirmed: 'success',
    pending: 'warning', in_production: 'primary', shipped: 'success',
    partial_shipped: 'warning', partial: 'warning', processing: 'primary',
    completed: 'success', cancelled: 'danger'
  }
  return map[status] || 'info'
}

const selectedRows = ref<any[]>([])
const handleSelectionChange = (rows: any[]) => { selectedRows.value = rows }

const handleBatchDelete = async () => {
  if (selectedRows.value.length === 0) return
  try {
    await ElMessageBox.confirm(`确定要删除选中的 ${selectedRows.value.length} 条数据吗？`, '批量删除确认', { confirmButtonText:'确定', cancelButtonText:'取消', type:'warning' })
    for (const row of selectedRows.value) {
      await deletePurchaseOrder(row.id)
    }
    ElMessage.success('删除成功')
    loadData()
  } catch (e) { if (e !== 'cancel') ElMessage.error('操作失败') }
}

onMounted(() => {
  loadData()
  loadSupplierList()
  loadMaterialList()
})
</script>

<template>
  <div class="purchase-page">
    <div class="page-header">
      <div>
        <h1 class="page-title">采购订单</h1>
        <p class="page-desc">管理采购订单信息</p>
      </div>
      <el-button type="primary" @click="handleAdd">
        <el-icon><Plus /></el-icon>
        新增采购订单
      </el-button>
    </div>

    <el-card class="search-card">
      <el-form :model="searchForm" inline>
        <el-form-item label="关键词">
          <el-input
            v-model="searchForm.search"
            placeholder="订单号/供应商"
            clearable
            @keyup.enter="handleSearch"
          />
        </el-form-item>

        <el-form-item label="状态">
          <el-select
            v-model="searchForm.status"
            placeholder="请选择"
            clearable
          >
            <el-option label="草稿" value="draft" />
            <el-option label="待处理" value="pending" />
            <el-option label="已下达" value="issued" />
            <el-option label="已确认" value="confirmed" />
            <el-option label="生产中" value="in_production" />
            <el-option label="部分到货" value="partial" />
            <el-option label="部分发货" value="partial_shipped" />
            <el-option label="已发货" value="shipped" />
            <el-option label="进行中" value="processing" />
            <el-option label="已完成" value="completed" />
            <el-option label="已取消" value="cancelled" />
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
          class="purchase-table"
          @selection-change="handleSelectionChange"
        >
        <el-table-column type="selection" width="36" />
        <el-table-column prop="po_no" label="采购订单号" width="125" />
        <el-table-column prop="supplier_name" label="供应商" width="100" show-overflow-tooltip />
        <el-table-column prop="material_code" label="物料代码" width="70" />
        <el-table-column prop="material_name" label="物料名称" width="135" show-overflow-tooltip />
        <el-table-column prop="quantity" label="采购数量" width="68">
          <template #default="{ row }">{{ Math.round(Number(row.quantity || 0)) }}</template>
        </el-table-column>
        <el-table-column prop="unit_price" label="单价" width="78">
          <template #default="{ row }">
            ¥{{ Number(row.unit_price || 0).toFixed(2) }}
          </template>
        </el-table-column>
        <el-table-column label="总金额" width="90">
          <template #default="{ row }">
            ¥{{ (Number(row.quantity || 0) * Number(row.unit_price || 0)).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) }}
          </template>
        </el-table-column>
        <el-table-column prop="delivery_date" label="预计到货" width="98" />
        <el-table-column prop="actual_delivery_date" label="实际交付" width="98" show-overflow-tooltip />
        <el-table-column prop="order_date" label="下单日期" width="98" />
        <el-table-column label="状态" width="68">
          <template #default="{ row }">
            <el-tag :type="getStatusType(row.status)" size="small">
              {{ getStatusLabel(row.status) }}
            </el-tag>
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
      :title="isEdit ? '编辑采购订单' : '新增采购订单'"
      width="600px"
      :close-on-click-modal="false"
      append-to-body
    >
      <el-form :model="formData" label-width="120px">
        <el-form-item label="采购订单号" required>
          <el-input v-model="formData.po_no" :disabled="isEdit" />
        </el-form-item>

        <el-form-item label="供应商" required>
          <el-select
            v-model="formData.supplier_id"
            filterable
            placeholder="选择供应商"
            style="width: 100%;"
            @change="handleSupplierChange"
          >
            <el-option
              v-for="item in supplierList"
              :key="item.id"
              :label="`${item.supplier_code} - ${item.supplier_name}`"
              :value="item.id"
            />
          </el-select>
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

        <el-form-item label="采购数量" required>
          <el-input-number v-model="formData.quantity" :min="1" />
        </el-form-item>

        <el-form-item label="单价">
          <el-input-number v-model="formData.unit_price" :precision="2" :min="0" />
        </el-form-item>

        <el-form-item label="总金额">
          <el-input v-model="formData.total_amount" disabled style="width: 100%;" />
        </el-form-item>

        <el-form-item label="下单日期">
          <el-date-picker v-model="formData.order_date" type="date" style="width: 100%;" />
        </el-form-item>

        <el-form-item label="预计到货日期">
          <el-date-picker v-model="formData.delivery_date" type="date" />
        </el-form-item>

        <el-form-item label="实际交付日期">
          <el-date-picker v-model="formData.actual_delivery_date" type="date" placeholder="到货后填写" />
        </el-form-item>

        <el-form-item label="状态">
          <el-select v-model="formData.status" style="width: 100%">
            <el-option label="草稿" value="draft" />
            <el-option label="待处理" value="pending" />
            <el-option label="已下达" value="issued" />
            <el-option label="已确认" value="confirmed" />
            <el-option label="生产中" value="in_production" />
            <el-option label="部分到货" value="partial" />
            <el-option label="部分发货" value="partial_shipped" />
            <el-option label="已发货" value="shipped" />
            <el-option label="进行中" value="processing" />
            <el-option label="已完成" value="completed" />
            <el-option label="已取消" value="cancelled" />
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
.purchase-page {
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

.purchase-table {
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
  .purchase-page { max-width: 100%; }

  .page-header {
    flex-direction: column;
    gap: 16px;

    .page-title {
      font-size: 24px;
    }
  }

  .search-card :deep(.el-form--inline) {
    flex-wrap: wrap;
  }

  .table-toolbar {
    flex-direction: column;
    gap: 8px;
    align-items: flex-start;
  }
}
</style>
