<script setup lang="ts">
import { ref, onMounted } from 'vue'
import request from '@/api/request'

const loading = ref(false)
const tableData = ref<Array<Record<string, any>>>([])

const pagination = ref({
  current: 1,
  pageSize: 15,
  total: 0
})

const searchForm = ref({
  search: '',
  user: '',
  module: ''
})

const modules = ['系统管理', '物料管理', '供应商管理', '客户管理', 'BOM管理', '库存管理', '销售订单', '采购订单', '产能管理', '数据导入', '报表中心']

const loadData = async () => {
  loading.value = true
  try {
    const params: Record<string, unknown> = {
      page: pagination.value.current,
      page_size: pagination.value.pageSize
    }
    if (searchForm.value.search) params.search = searchForm.value.search
    if (searchForm.value.user) params.user = searchForm.value.user
    if (searchForm.value.module) params.module = searchForm.value.module

    const res = await request.get('/audit/', { params })
    tableData.value = res?.results || []
    pagination.value.total = res?.count || 0
  } catch (e) {
    console.error('加载审计日志失败:', e)
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
  searchForm.value = { search: '', user: '', module: '' }
  handleSearch()
}


const handleCurrentChange = (val: number) => {
  pagination.value.current = val
  loadData()
}

const getStatusType = (status: string) => {
  return status === 'success' ? 'success' : 'danger'
}

const getStatusLabel = (status: string) => {
  return status === 'success' ? '成功' : '失败'
}

onMounted(() => {
  loadData()
})
</script>

<template>
  <div class="audit-page">
    <div class="page-header">
      <div>
        <h1 class="page-title">审计日志</h1>
        <p class="page-desc">记录系统操作日志和用户行为</p>
      </div>
    </div>

    <el-card class="search-card">
      <el-form :model="searchForm" inline>
        <el-form-item label="关键词">
          <el-input
            v-model="searchForm.search"
            placeholder="操作/模块"
            clearable
            @keyup.enter="handleSearch"
          />
        </el-form-item>

        <el-form-item label="操作用户">
          <el-input
            v-model="searchForm.user"
            placeholder="用户名"
            clearable
          />
        </el-form-item>

        <el-form-item label="模块">
          <el-select
            v-model="searchForm.module"
            placeholder="请选择"
            clearable
          >
            <el-option v-for="mod in modules" :key="mod" :label="mod" :value="mod" />
          </el-select>
        </el-form-item>

        <el-form-item>
          <el-button type="primary" @click="handleSearch">查询</el-button>
          <el-button @click="handleReset">重置</el-button>
        </el-form-item>
      </el-form>
    </el-card>

    <el-card class="table-card">
      <el-table border
        :data="tableData"
        :loading="loading"
        class="audit-table"
      >
        <template #empty>
          <div style="padding: 40px 0; color: #909399; text-align: center;">
            暂无审计日志记录
          </div>
        </template>
        <el-table-column prop="id" label="ID" width="60" show-overflow-tooltip />
        <el-table-column prop="user" label="操作用户" width="90" show-overflow-tooltip />
        <el-table-column prop="action_display" label="操作内容" width="130" show-overflow-tooltip />
        <el-table-column prop="module" label="所属模块" width="90" show-overflow-tooltip />
        <el-table-column prop="time" label="操作时间" width="155" show-overflow-tooltip />
        <el-table-column prop="ip" label="IP地址" width="125" show-overflow-tooltip />
        <el-table-column label="状态" width="65">
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
  </div>
</template>

<style scoped lang="scss">
.audit-page {
  max-width: 1500px;
  margin: 0 auto;
}

.page-header {
  margin-bottom: 24px;

  .page-title {
    font-size: 28px;
    font-weight: 700;
    color: #E8EAED;
    margin: 0 0 8px 0;
  }

  .page-desc {
    font-size: 14px;
    color: #B0B8C4;
    margin: 0;
  }
}

.search-card {
  margin-bottom: 20px;
  background: rgba(255, 255, 255, 0.03);
  border: 1px solid rgba(255, 255, 255, 0.08);

  :deep(.el-card__body) {
    padding: 20px;
  }
}

.table-card {
  background: rgba(255, 255, 255, 0.03);
  border: 1px solid rgba(255, 255, 255, 0.08);

  :deep(.el-card__body) {
    padding: 0;
  }
}

.audit-table {
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
  .page-title {
    font-size: 24px;
  }
}
</style>
