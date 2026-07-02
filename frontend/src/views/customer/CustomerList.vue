<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus } from '@element-plus/icons-vue'
import {
  getCustomerList,
  createCustomer,
  updateCustomer,
  deleteCustomer
} from '@/api'
import type { Customer } from '@/types/api'

const loading = ref(false)
const dialogVisible = ref(false)
const formData = ref<Partial<Customer>>({})
const isEdit = ref(false)
const currentEditId = ref<number | null>(null)

const tableData = ref<Customer[]>([])
const selectedRows = ref<any[]>([])
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
    const res = await getCustomerList(params)

    tableData.value = res?.results || []
    pagination.value.total = res?.count || 0
  } catch (error: any) {
    ElMessage.error('加载数据失败')
    console.error('加载客户数据失败:', error)
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


const handleCurrentChange = (val: number) => {
  pagination.value.current = val
  loadData()
}

const handleAdd = () => {
  isEdit.value = false
  formData.value = {
    customer_code: '',
    customer_name: '',
    contact_person: '',
    phone: '',
    email: '',
    address: '',
    is_active: true,
    credit_limit: 0,
    customer_type: '其他',
    payment_terms: '月结30天',
    customer_level: 'normal',
    delivery_priority: 5
  }
  dialogVisible.value = true
}

const handleEdit = (row?: Customer) => {
  const editRow = row || selectedRows.value[0]
  if (!editRow) return
  isEdit.value = true
  currentEditId.value = editRow.id
  formData.value = { ...editRow }
  dialogVisible.value = true
}

// @ts-expect-error - handler reserved for future template binding
const _handleDelete = async (row: Customer) => {
  try {
    await ElMessageBox.confirm(
      `确定要删除客户 ${row.customer_name} 吗？`,
      '删除确认',
      {
        confirmButtonText: '确定',
        cancelButtonText: '取消',
        type: 'warning'
      }
    )

    await deleteCustomer(row.id)
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
  if (selectedRows.value.length === 0) return
  try {
    await ElMessageBox.confirm(
      `确定要删除选中的 ${selectedRows.value.length} 个客户吗？`,
      '批量删除确认',
      {
        confirmButtonText: '确定',
        cancelButtonText: '取消',
        type: 'warning'
      }
    )
    for (const row of selectedRows.value) {
      await deleteCustomer(row.id)
    }
    ElMessage.success('批量删除成功')
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
      await updateCustomer(currentEditId.value, formData.value)
      ElMessage.success('更新成功')
    } else {
      await createCustomer(formData.value)
      ElMessage.success('创建成功')
    }
    dialogVisible.value = false
    loadData()
  } catch (error) {
    ElMessage.error('操作失败')
  }
}

onMounted(() => {
  loadData()
})
</script>

<template>
  <div class="customer-page">
    <div class="page-header">
      <div>
        <h1 class="page-title">客户管理</h1>
        <p class="page-desc">管理所有客户信息</p>
      </div>
      <el-button type="primary" @click="handleAdd">
        <el-icon><Plus /></el-icon>
        新增客户
      </el-button>
    </div>

    <el-card class="search-card">
      <el-form :model="searchForm" inline>
        <el-form-item label="关键词">
          <el-input
            v-model="searchForm.search"
            placeholder="客户代码/名称"
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
        <div class="toolbar-left">
          <span class="selection-info">已选 {{ selectedRows.length }} 项</span>
        </div>
        <div class="toolbar-right">
          <el-button type="primary" size="small" :disabled="selectedRows.length !== 1" @click="handleEdit(selectedRows[0])">
            编辑选中
          </el-button>
          <el-button type="danger" size="small" :disabled="selectedRows.length === 0" @click="handleBatchDelete">
            删除选中
          </el-button>
        </div>
      </div>

      <el-table border
          :data="tableData"
          :loading="loading"
          class="customer-table"
          @selection-change="handleSelectionChange"
        >
        <el-table-column type="selection" width="36" />
        <el-table-column prop="customer_code" label="客户代码" width="92" show-overflow-tooltip />
        <el-table-column prop="customer_name" label="客户名称" width="92" show-overflow-tooltip />
        <el-table-column prop="contact_person" label="联系人" width="68" show-overflow-tooltip />
        <el-table-column prop="phone" label="联系电话" width="98" show-overflow-tooltip />
        <el-table-column prop="email" label="邮箱" width="128" show-overflow-tooltip />
        <el-table-column prop="address" label="地址" width="110" show-overflow-tooltip>
          <template #default="{ row }">{{ row.address || '-' }}</template>
        </el-table-column>
        <el-table-column prop="credit_limit" label="信用额度" width="80">
          <template #default="{ row }">
            ¥{{ Number(row.credit_limit || 0).toFixed(2) }}
          </template>
        </el-table-column>
        <el-table-column prop="customer_type" label="客户类型" width="76" show-overflow-tooltip />
        <el-table-column prop="payment_terms" label="付款条件" width="76" show-overflow-tooltip />
        <el-table-column label="客户等级" width="68">
          <template #default="{ row }">
            <el-tag :type="row.customer_level === 'vip' ? 'danger' : row.customer_level === 'important' ? 'warning' : 'info'" size="small">
              {{ row.customer_level === 'vip' ? 'S级' : row.customer_level === 'important' ? 'A级' : 'B级' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="delivery_priority" label="交付优先级" width="72" show-overflow-tooltip />
        <el-table-column label="状态" width="58">
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
      :title="isEdit ? '编辑客户' : '新增客户'"
      width="600px"
      :close-on-click-modal="false"
      append-to-body
    >
      <el-form :model="formData" label-width="120px">
        <el-form-item label="客户代码" required>
          <el-input v-model="formData.customer_code" :disabled="isEdit" />
        </el-form-item>

        <el-form-item label="客户名称" required>
          <el-input v-model="formData.customer_name" />
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

        <el-form-item label="信用额度">
          <el-input-number v-model="formData.credit_limit" :min="0" />
        </el-form-item>

        <el-form-item label="客户类型">
          <el-select v-model="formData.customer_type" placeholder="选择类型">
            <el-option label="海外渠道" value="海外渠道" />
            <el-option label="电商平台" value="电商平台" />
            <el-option label="工程渠道" value="工程渠道" />
            <el-option label="企业集采" value="企业集采" />
            <el-option label="线下零售" value="线下零售" />
            <el-option label="运营商" value="运营商" />
            <el-option label="其他" value="其他" />
          </el-select>
        </el-form-item>

        <el-form-item label="付款条件">
          <el-select v-model="formData.payment_terms" placeholder="选择条件">
            <el-option label="现款现货" value="现款现货" />
            <el-option label="月结7天" value="月结7天" />
            <el-option label="月结15天" value="月结15天" />
            <el-option label="月结30天" value="月结30天" />
            <el-option label="月结45天" value="月结45天" />
            <el-option label="月结60天" value="月结60天" />
            <el-option label="月结90天" value="月结90天" />
          </el-select>
        </el-form-item>

        <el-form-item label="客户等级">
          <el-select v-model="formData.customer_level">
            <el-option label="S级(VIP)" value="vip" />
            <el-option label="A级(重要)" value="important" />
            <el-option label="B级(普通)" value="normal" />
          </el-select>
        </el-form-item>

        <el-form-item label="交付优先级">
          <el-input-number v-model="formData.delivery_priority" :min="1" :max="9" />
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
.customer-page {
  max-width: 1500px;
  margin: 0 auto;
}

.table-toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 12px 16px;
  background: rgba(64, 158, 255, 0.06);
  border-bottom: 1px solid rgba(255, 255, 255, 0.06);

  .selection-info {
    font-size: 13px;
    color: #8b92a5;
  }

  .toolbar-right {
    display: flex;
    gap: 8px;
  }
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

.customer-table {
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
  .customer-page { max-width: 100%; }

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
