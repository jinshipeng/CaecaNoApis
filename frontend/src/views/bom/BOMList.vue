<script setup lang="ts">
import { ref, onMounted, onActivated } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus } from '@element-plus/icons-vue'
import { getBOMList, createBOM, updateBOM, deleteBOM } from '@/api'
import { getErrorMessage } from '@/api/request'
import type { BOM } from '@/types/api'

const loading = ref(false)
const dialogVisible = ref(false)
const formData = ref<Partial<BOM>>({})
const isEdit = ref(false)
const currentEditId = ref<number | null>(null)

const tableData = ref<BOM[]>([])
const pagination = ref({
  current: 1,
  pageSize: 15,
  total: 0
})

const searchForm = ref({
  search: ''
})

const loadData = async () => {
  try {
    loading.value = true
    const res = await getBOMList({
      page: pagination.value.current,
      page_size: pagination.value.pageSize,
      search: searchForm.value.search,
      ordering: 'parent_material__material_code'
    })

    tableData.value = res?.results || []
    pagination.value.total = res?.count || 0
  } catch (error: any) {
    ElMessage.error('加载数据失败')
    console.error('加载BOM数据失败:', error)
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
    search: ''
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
    parent_code: '',
    parent_name: '',
    child_code: '',
    child_name: '',
    quantity: 1,
    unit: '件',
    bom_level: 1,
    usage_ratio: 0,
    scrap_rate: 0,
    alternative_group: '',
    alternative_priority: 1,
    alternative_ratio: 1.0,
    is_active: true,
    version: 1,
    is_configurable: false,
    config_group: ''
  }
  dialogVisible.value = true
}

const handleEdit = (row: BOM) => {
  isEdit.value = true
  currentEditId.value = row.id
  formData.value = { ...row }
  dialogVisible.value = true
}

// @ts-expect-error - handler reserved for future template binding
const _handleDelete = async (row: BOM) => {
  try {
    await ElMessageBox.confirm(
      `确定要删除BOM ${row.parent_code} → ${row.child_code} 吗？`,
      '删除确认',
      {
        confirmButtonText: '确定',
        cancelButtonText: '取消',
        type: 'warning'
      }
    )

    await deleteBOM(row.id)
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
    if (isEdit.value && currentEditId.value) {
      await updateBOM(currentEditId.value, formData.value, { skipErrorHandler: true })
      ElMessage.success('更新成功')
    } else {
      await createBOM(formData.value, { skipErrorHandler: true })
      ElMessage.success('创建成功')
    }
    dialogVisible.value = false
    loadData()
  } catch (error: any) {
    ElMessage.error(getErrorMessage(error, '保存失败'))
  }
}

const selectedRows = ref<any[]>([])
const handleSelectionChange = (rows: any[]) => { selectedRows.value = rows }

const handleBatchDelete = async () => {
  if (selectedRows.value.length === 0) return
  try {
    await ElMessageBox.confirm(`确定要删除选中的 ${selectedRows.value.length} 条数据吗？`, '批量删除确认', { confirmButtonText:'确定', cancelButtonText:'取消', type:'warning' })
    for (const row of selectedRows.value) {
      await deleteBOM(row.id)
    }
    ElMessage.success('删除成功')
    loadData()
  } catch (e) { if (e !== 'cancel') ElMessage.error('操作失败') }
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
  <div class="bom-page">
    <div class="page-header">
      <div>
        <h1 class="page-title">BOM管理</h1>
        <p class="page-desc">管理产品物料清单</p>
      </div>
      <el-button type="primary" @click="handleAdd">
        <el-icon><Plus /></el-icon>
        新增BOM
      </el-button>
    </div>

    <el-card class="search-card">
      <el-form :model="searchForm" inline>
        <el-form-item label="关键词">
          <el-input
            v-model="searchForm.search"
            placeholder="父物料/子物料代码或名称"
            clearable
            @keyup.enter="handleSearch"
          />
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
          class="bom-table"
          @selection-change="handleSelectionChange"
        >
        <el-table-column type="selection" width="36" />
        <el-table-column prop="parent_code" label="父物料代码" width="82" />
        <el-table-column prop="parent_name" label="父物料名称" width="100" show-overflow-tooltip />
        <el-table-column prop="child_code" label="子物料代码" width="82" />
        <el-table-column prop="child_name" label="子物料名称" width="88" show-overflow-tooltip />
        <el-table-column prop="quantity" label="用量" width="52">
          <template #default="{ row }">{{ Number(row.quantity || 0) }}</template>
        </el-table-column>
        <el-table-column prop="unit" label="单位" width="48" />
        <el-table-column prop="bom_level" label="层级" width="48" show-overflow-tooltip />
        <el-table-column prop="alternative_priority" label="替代优先级" width="68" align="center">
          <template #default="{ row }">
            <el-tag :type="row.alternative_priority === 1 ? 'danger' : row.alternative_priority === 2 ? 'warning' : 'info'" size="small">
              P{{ row.alternative_priority || 1 }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="alternative_group" label="替代料组" width="68" show-overflow-tooltip />
        <el-table-column prop="alternative_ratio" label="替代比例" width="64" align="center">
          <template #default="{ row }">
            {{ Number(row.alternative_ratio || 0).toFixed(2) }}
          </template>
        </el-table-column>
        <el-table-column prop="usage_ratio" label="用量占比(%)" width="72">
          <template #default="{ row }">
            {{ Number(row.usage_ratio || 0).toFixed(1) }}
          </template>
        </el-table-column>
        <el-table-column prop="scrap_rate" label="报废率(%)" width="64" align="center">
          <template #default="{ row }">
            {{ Number(row.scrap_rate || 0).toFixed(3) }}
          </template>
        </el-table-column>
        <el-table-column label="状态" width="52">
          <template #default="{ row }">
            <el-tag :type="row.is_active ? 'success' : 'info'" size="small">
              {{ row.is_active ? '启用' : '禁用' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="version" label="BOM版本" width="72" />
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
      :title="isEdit ? '编辑BOM' : '新增BOM'"
      width="650px"
      :close-on-click-modal="false"
      append-to-body
    >
      <el-form :model="formData" label-width="120px">
        <el-form-item label="父物料代码" required>
          <el-input v-model="formData.parent_code" placeholder="输入父物料代码" />
        </el-form-item>

        <el-form-item label="父物料名称">
          <el-input v-model="formData.parent_name" placeholder="自动关联或手动输入" />
        </el-form-item>

        <el-form-item label="子物料代码" required>
          <el-input v-model="formData.child_code" placeholder="输入子物料代码" />
        </el-form-item>

        <el-form-item label="子物料名称">
          <el-input v-model="formData.child_name" placeholder="自动关联或手动输入" />
        </el-form-item>

        <el-form-item label="用量" required>
          <el-input-number v-model="formData.quantity" :precision="4" :min="0.0001" :step="1" />
        </el-form-item>

        <el-form-item label="单位">
          <el-input v-model="formData.unit" style="width: 200px" />
        </el-form-item>

        <el-form-item label="BOM层级">
          <el-input-number v-model="formData.bom_level" :min="1" :max="10" />
        </el-form-item>

        <el-form-item label="用量占比(%)">
          <el-input-number v-model="formData.usage_ratio" :precision="1" :min="0" :max="100" :step="1" />
        </el-form-item>

        <el-form-item label="报废率(%)">
          <el-input-number v-model="formData.scrap_rate" :precision="2" :min="0" :max="100" :step="0.1" />
        </el-form-item>

        <el-form-item label="替代料组">
          <el-input v-model="formData.alternative_group" placeholder="可选，用于标识替代料关系" />
        </el-form-item>

        <el-form-item label="替代优先级">
          <el-input-number v-model="formData.alternative_priority" :min="1" :max="10" />
        </el-form-item>

        <el-form-item label="替代比例">
          <el-input-number v-model="formData.alternative_ratio" :precision="2" :min="0" :max="1" :step="0.01" />
        </el-form-item>

        <el-form-item label="启用状态">
          <el-switch v-model="formData.is_active" />
        </el-form-item>

        <el-form-item label="BOM版本">
          <el-input-number v-model="formData.version" :min="1" />
        </el-form-item>

        <el-form-item label="是否可配置CTO">
          <el-switch v-model="formData.is_configurable" />
        </el-form-item>

        <el-form-item label="配置组" v-if="formData.is_configurable">
          <el-input v-model="formData.config_group" placeholder="输入配置组" />
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
.bom-page {
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

.bom-table {
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
  .bom-page { max-width: 100%; }
  .search-card :deep(.el-form-item) { margin-right: 0; width: 100%; }
  .search-card :deep(.el-input) { width: 100%; }
  .page-header {
    flex-direction: column;
    gap: 16px;

    .page-title {
      font-size: 24px;
    }
  }
}
</style>
