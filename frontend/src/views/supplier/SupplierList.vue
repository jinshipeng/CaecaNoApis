<script setup lang="ts">
import { ref, onMounted, onActivated } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus } from '@element-plus/icons-vue'
import {
  getSupplierList,
  createSupplier,
  updateSupplier,
  deleteSupplier
} from '@/api'
import { clearApiCache } from '@/utils/apiCache'
import type { Supplier } from '@/types/api'

const loading = ref(false)
const dialogVisible = ref(false)
const formData = ref<Partial<Supplier>>({})
const isEdit = ref(false)
const currentEditId = ref<number | null>(null)
const selectedRows = ref<Supplier[]>([])

const tableData = ref<Supplier[]>([])
const pagination = ref({
  current: 1,
  pageSize: 15,
  total: 0
})

const searchForm = ref({
  search: '',
  is_active: ''
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
    if (searchForm.value.is_active !== '') {
      params.is_active = searchForm.value.is_active === 'true'
    }
    const res = await getSupplierList(params)

    tableData.value = res?.results || []
    pagination.value.total = res?.count || 0
  } catch (error: any) {
    ElMessage.error('加载数据失败')
    console.error('加载供应商数据失败:', error)
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
    is_active: ''
  }
  pagination.value.current = 1
  loadData()
}

const handleSelectionChange = (rows: Supplier[]) => {
  selectedRows.value = rows
}

const handleBatchDelete = async () => {
  if (selectedRows.value.length === 0) return
  try {
    await ElMessageBox.confirm(
      `确定要删除选中的 ${selectedRows.value.length} 个供应商吗？`,
      '批量删除确认',
      { confirmButtonText: '确定', cancelButtonText: '取消', type: 'warning' }
    )
    for (const row of selectedRows.value) {
      await deleteSupplier(row.id)
    }
    ElMessage.success('批量删除成功')
    loadData()
  } catch (error: any) {
    if (error !== 'cancel') {
      ElMessage.error('删除失败')
    }
  }
}


const handleCurrentChange = (val: number) => {
  pagination.value.current = val
  loadData()
}

const handleAdd = () => {
  isEdit.value = false
  formData.value = {
    supplier_code: '',
    supplier_name: '',
    contact_person: '',
    phone: '',
    email: '',
    address: '',
    is_active: true,
    rating: 'B',
    delivery_reliability: 0.9,
    normal_lead_time: 7
  }
  dialogVisible.value = true
}

const handleEdit = (row: Supplier) => {
  isEdit.value = true
  currentEditId.value = row.id
  formData.value = { ...row }
  dialogVisible.value = true
}

const handleSubmit = async () => {
  try {
    if (isEdit.value && currentEditId.value) {
      await updateSupplier(currentEditId.value, formData.value)
      ElMessage.success('更新成功')
    } else {
      await createSupplier(formData.value)
      ElMessage.success('创建成功')
    }
    dialogVisible.value = false
    clearApiCache()
    loadData()
  } catch (error) {
    ElMessage.error('操作失败')
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
  <div class="supplier-page">
    <div class="page-header">
      <div>
        <h1 class="page-title">供应商管理</h1>
        <p class="page-desc">管理所有供应商信息</p>
      </div>
      <el-button type="primary" @click="handleAdd">
        <el-icon><Plus /></el-icon>
        新增供应商
      </el-button>
    </div>

    <el-card class="search-card">
      <el-form :model="searchForm" inline>
        <el-form-item label="关键词">
          <el-input
            v-model="searchForm.search"
            placeholder="供应商代码/名称"
            clearable
            @keyup.enter="handleSearch"
          />
        </el-form-item>

        <el-form-item label="状态">
          <el-select
            v-model="searchForm.is_active"
            placeholder="请选择"
            clearable
          >
            <el-option label="启用" value="true" />
            <el-option label="禁用" value="false" />
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
        class="supplier-table"
        @selection-change="handleSelectionChange"
      >
        <el-table-column type="selection" width="36" />
        <el-table-column prop="supplier_code" label="供应商代码" width="82" />
        <el-table-column prop="supplier_name" label="供应商名称" width="95" show-overflow-tooltip />
        <el-table-column prop="contact_person" label="联系人" width="60" />
        <el-table-column prop="phone" label="联系电话" width="90" />
        <el-table-column prop="email" label="邮箱" width="120" show-overflow-tooltip />
        <el-table-column prop="address" label="地址" width="103" show-overflow-tooltip />
        <el-table-column prop="rating" label="信用等级" width="56">
          <template #default="{ row }">
            <el-tag :type="row.rating === 'A' ? 'success' : row.rating === 'B' ? 'warning' : 'info'" size="small">
              {{ row.rating }}级
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="delivery_reliability" label="交付可靠率" width="68">
          <template #default="{ row }">
            {{ (Number(row.delivery_reliability || 0) * 100).toFixed(0) }}%
          </template>
        </el-table-column>
        <el-table-column prop="normal_lead_time" label="交期(天)" width="60" show-overflow-tooltip />
        <el-table-column prop="payment_terms" label="结算方式" width="70" show-overflow-tooltip />
        <el-table-column prop="min_order_qty" label="起订量" width="55" show-overflow-tooltip />
        <el-table-column prop="capacity_level" label="产能等级" width="58">
          <template #default="{ row }">
            <el-tag :type="row.capacity_level === 'A' ? 'success' : row.capacity_level === 'B' ? 'warning' : 'info'" size="small">
              {{ row.capacity_level || 'B' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="cooperation_years" label="合作年限" width="60" show-overflow-tooltip />
        <el-table-column prop="on_time_delivery_rate" label="准时交付率" width="72">
          <template #default="{ row }">
            {{ (Number(row.on_time_delivery_rate || 0) * 100).toFixed(0) }}%
          </template>
        </el-table-column>
        <el-table-column label="状态" width="52">
          <template #default="{ row }">
            <el-tag :type="row.is_active ? 'success' : 'info'" size="small">
              {{ row.is_active ? '启用' : '禁用' }}
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
      :title="isEdit ? '编辑供应商' : '新增供应商'"
      width="680px"
      :close-on-click-modal="false"
      append-to-body
    >
      <el-form :model="formData" label-width="120px">
        <el-form-item label="供应商代码" required>
          <el-input v-model="formData.supplier_code" :disabled="isEdit" />
        </el-form-item>

        <el-form-item label="供应商名称" required>
          <el-input v-model="formData.supplier_name" />
        </el-form-item>

        <el-form-item label="联系人">
          <el-input v-model="formData.contact_person" />
        </el-form-item>

        <el-form-item label="联系电话">
          <el-input v-model="formData.phone" />
        </el-form-item>

        <el-form-item label="邮箱">
          <el-input v-model="formData.email" />
        </el-form-item>

        <el-form-item label="地址">
          <el-input v-model="formData.address" />
        </el-form-item>

        <el-form-item label="信用等级">
          <el-select v-model="formData.rating" style="width: 100%">
            <el-option label="A" value="A" />
            <el-option label="B" value="B" />
            <el-option label="C" value="C" />
            <el-option label="D" value="D" />
          </el-select>
        </el-form-item>

        <el-form-item label="交付可靠率">
          <el-input-number v-model="formData.delivery_reliability" :precision="2" :min="0" :max="1" :step="0.01" />
        </el-form-item>

        <el-form-item label="正常交期(天)">
          <el-input-number v-model="formData.normal_lead_time" :min="1" />
        </el-form-item>

        <el-form-item label="结算方式">
          <el-input v-model="formData.payment_terms" placeholder="月结30天" />
        </el-form-item>

        <el-form-item label="最小起订量(件)">
          <el-input-number v-model="formData.min_order_qty" :min="0" />
        </el-form-item>

        <el-form-item label="产能等级">
          <el-select v-model="formData.capacity_level" style="width: 100%">
            <el-option label="A" value="A" />
            <el-option label="B" value="B" />
            <el-option label="C" value="C" />
          </el-select>
        </el-form-item>

        <el-form-item label="合作年限(年)">
          <el-input-number v-model="formData.cooperation_years" :min="0" :max="50" />
        </el-form-item>

        <el-form-item label="质保期(月)">
          <el-input-number v-model="formData.warranty_months" :min="0" :max="60" />
        </el-form-item>

        <el-form-item label="准时交付率">
          <el-input-number v-model="formData.on_time_delivery_rate" :precision="2" :min="0" :max="1" :step="0.01" />
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
.supplier-page {
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

.supplier-table {
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
      cursor: pointer;
    }

    tr {
      background: rgba(255, 255, 255, 0.02) !important;

      &:hover {
        background: rgba(110, 158, 247, 0.06) !important;
      }

      &.current-row > td.el-table__cell {
        background: rgba(110, 158, 247, 0.12) !important;
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
  .supplier-page { max-width: 100%; }

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
