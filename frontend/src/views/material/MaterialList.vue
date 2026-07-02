<script setup lang="ts">
import { ref, onMounted, onActivated } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus } from '@element-plus/icons-vue'
import { getMaterialList, createMaterial, updateMaterial, deleteMaterial } from '@/api'
import { clearApiCache } from '@/utils/apiCache'
import { getErrorMessage } from '@/api/request'
import type { Material } from '@/types/api'

const loading = ref(false)
const dialogVisible = ref(false)
const formData = ref<Partial<Material>>({})
const isEdit = ref(false)
const currentEditId = ref<number | null>(null)

const tableData = ref<Material[]>([])
const selectedRows = ref<any[]>([])
const pagination = ref({
  current: 1,
  pageSize: 15,
  total: 0
})

const searchForm = ref({
  search: '',
  material_type: ''
})

const materialTypes = [
  { label: '原材料', value: 'raw' },
  { label: '半成品', value: 'semi' },
  { label: '成品', value: 'finished' }
]

const loadData = async () => {
  try {
    loading.value = true
    const res = await getMaterialList({
      page: pagination.value.current,
      page_size: pagination.value.pageSize,
      search: searchForm.value.search,
      material_type: searchForm.value.material_type,
      ordering: '-id'
    })

    tableData.value = res?.results || []
    pagination.value.total = res?.count || 0
  } catch (error: any) {
    console.error('加载物料数据失败:', error)
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
    material_type: ''
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
  formData.value = {
    material_code: '',
    material_name: '',
    material_type: 'raw',
    unit: '件',
    standard_cost: 0,
    sales_price: 0,
    safety_stock: 0,
    is_active: true,
    shelf_life: 0,
    lead_time: 7,
    min_order_qty: 1,
    min_production_qty: 1
  }
  dialogVisible.value = true
}

const handleEdit = (row: Material) => {
  isEdit.value = true
  currentEditId.value = row.id
  formData.value = { ...row }
  dialogVisible.value = true
}

// @ts-expect-error - handler reserved for future template binding
const _handleDelete = async (row: Material) => {
  try {
    await ElMessageBox.confirm(
      `确定要删除物料 ${row.material_code} 吗？`,
      '删除确认',
      {
        confirmButtonText: '确定',
        cancelButtonText: '取消',
        type: 'warning'
      }
    )

    await deleteMaterial(row.id)
    ElMessage.success('删除成功')
    loadData()
  } catch (error: any) {
    if (error !== 'cancel') {
      ElMessage.error('删除失败')
    }
  }
}

const handleSelectionChange = (rows: any[]) => {
  selectedRows.value = rows
}

const handleBatchDelete = async () => {
  try {
    const names = selectedRows.value.map((r: any) => r.material_code).join('、')
    await ElMessageBox.confirm(
      `确定要删除选中的 ${selectedRows.value.length} 项物料（${names}）吗？`,
      '批量删除确认',
      {
        confirmButtonText: '确定',
        cancelButtonText: '取消',
        type: 'warning'
      }
    )
    for (const row of selectedRows.value) {
      await deleteMaterial(row.id)
    }
    ElMessage.success(`成功删除 ${selectedRows.value.length} 项`)
    selectedRows.value = []
    loadData()
  } catch (error: any) {
    if (error !== 'cancel') {
      ElMessage.error('批量删除失败')
    }
  }
}

const handleSubmit = async () => {
  try {
    if (isEdit.value && currentEditId.value) {
      await updateMaterial(currentEditId.value, formData.value, { skipErrorHandler: true })
      ElMessage.success('更新成功')
    } else {
      await createMaterial(formData.value, { skipErrorHandler: true })
      ElMessage.success('创建成功')
    }
    dialogVisible.value = false
    clearApiCache()
    loadData()
  } catch (error: any) {
    ElMessage.error(getErrorMessage(error, '保存失败'))
  }
}

const getTypeLabel = (type: string) => {
  if (!type) return '其他'
  // 兼容后端原始值('raw'/'semi'/'finished')和显示标签('原材料'/'半成品'/'成品')
  const item = materialTypes.find(t =>
    t.value === type || t.label === type
  )
  return item?.label || '其他'
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
  <div class="material-page">
    <!-- 页面标题 -->
    <div class="page-header">
      <div>
        <h1 class="page-title">物料管理</h1>
        <p class="page-desc">管理所有物料主数据信息</p>
      </div>
      <el-button type="primary" @click="handleAdd">
        <el-icon><Plus /></el-icon>
        新增物料
      </el-button>
    </div>

    <!-- 搜索表单 -->
    <el-card class="search-card">
      <el-form :model="searchForm" inline>
        <el-form-item label="关键词">
          <el-input
            v-model="searchForm.search"
            placeholder="物料代码/名称"
            clearable
            @keyup.enter="handleSearch"
          />
        </el-form-item>

        <el-form-item label="物料类型">
          <el-select
            v-model="searchForm.material_type"
            placeholder="请选择"
            clearable
          >
            <el-option
              v-for="item in materialTypes"
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

    <!-- 数据表格 -->
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
          class="material-table"
          @selection-change="handleSelectionChange"
        >
        <el-table-column type="selection" width="38" />
        <el-table-column prop="material_code" label="物料代码" width="95" />
        <el-table-column prop="material_name" label="物料名称" width="115" show-overflow-tooltip />
        <el-table-column label="类型" width="60">
          <template #default="{ row }">
            <el-tag :type="row.material_type === 'finished' ? 'success' : 'primary'" size="small">
              {{ getTypeLabel(row.material_type) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="unit" label="单位" width="48" align="center" />
        <el-table-column prop="lead_time" label="提前期(天)" width="70" align="center" />
        <el-table-column prop="shelf_life" label="保质期(天)" width="75" align="center" show-overflow-tooltip />
        <el-table-column prop="standard_cost" label="标准成本" width="75">
          <template #default="{ row }">
            {{ row.standard_cost ? '¥' + Number(row.standard_cost).toFixed(2) : '-' }}
          </template>
        </el-table-column>
        <el-table-column prop="sales_price" label="销售价格" width="75">
          <template #default="{ row }">
            {{ row.sales_price ? '¥' + Number(row.sales_price).toFixed(2) : '-' }}
          </template>
        </el-table-column>
        <el-table-column label="当前库存" width="72">
          <template #default="{ row }">{{ Math.round(Number(row.actual_stock || 0)).toLocaleString() }}</template>
        </el-table-column>
        <el-table-column label="状态" width="55">
          <template #default="{ row }">
            <el-tag :type="row.is_active ? 'success' : 'info'" size="small">
              {{ row.is_active ? '启用' : '禁用' }}
            </el-tag>
          </template>
        </el-table-column>
      </el-table>

      <!-- 分页 -->
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

    <!-- 编辑对话框 -->
    <el-dialog
      v-model="dialogVisible"
      :title="isEdit ? '编辑物料' : '新增物料'"
      width="600px"
      :close-on-click-modal="false"
      append-to-body
    >
      <el-form :model="formData" label-width="120px">
        <el-form-item label="物料代码" required>
          <el-input v-model="formData.material_code" :disabled="isEdit" />
        </el-form-item>

        <el-form-item label="物料名称" required>
          <el-input v-model="formData.material_name" />
        </el-form-item>

        <el-form-item label="物料类型" required>
          <el-select v-model="formData.material_type" style="width: 100%">
            <el-option
              v-for="item in materialTypes"
              :key="item.value"
              :label="item.label"
              :value="item.value"
            />
          </el-select>
        </el-form-item>

        <el-form-item label="单位">
          <el-input v-model="formData.unit" />
        </el-form-item>

        <el-form-item label="标准成本">
          <el-input-number v-model="formData.standard_cost" :precision="2" :min="0" />
        </el-form-item>

        <el-form-item label="销售价格">
          <el-input-number v-model="formData.sales_price" :precision="2" :min="0" />
        </el-form-item>

        <el-form-item label="安全库存">
          <el-input-number v-model="formData.safety_stock" :min="0" />
        </el-form-item>

        <el-form-item label="最小起订量">
          <el-input-number v-model="formData.min_order_qty" :min="1" />
        </el-form-item>

        <el-form-item label="最小生产批量">
          <el-input-number v-model="formData.min_production_qty" :min="1" />
        </el-form-item>

        <el-form-item label="采购提前期">
          <el-input-number v-model="formData.lead_time" :min="1" />
        </el-form-item>

        <el-form-item label="保质期(天)">
          <el-input-number v-model="formData.shelf_life" :min="0" />
        </el-form-item>

        <el-form-item label="启用状态">
          <el-switch v-model="formData.is_active" />
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
.material-page {
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

.material-table {
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

// 响应式
@media (max-width: 767px) {
  .material-page { max-width: 100%; }

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
